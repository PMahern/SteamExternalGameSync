import type {} from 'react';
import appIcon from '../../icon.png';
import { useCallback, useEffect, useRef, useState } from 'react';
import { callable, definePlugin, routerHook } from '@decky/api';
import {
  afterPatch,
  appDetailsClasses,
  findInReactTree,
  playSectionClasses,
  ButtonItem,
  DialogBody,
  DialogButton,
  DialogFooter,
  DialogHeader,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  Router,
  showModal,
  ToggleField,
} from '@decky/ui';

// ── Backend callables ──────────────────────────────────────────────────────────

interface StatusResult { status: string; name?: string; game_id?: string; last_known_status?: string | null; }
interface SyncResult   { status: string; msg: string; }
interface ResolveResult { ok: boolean; msg: string; }
interface DeckySettings { polling_enabled: boolean; poll_auto_pull: boolean; }

const getSyncStatusForAppId  = callable<[string], StatusResult>('get_sync_status_for_appid');
const getSyncStatuses        = callable<[], Record<string, string>>('get_sync_statuses');
const syncGame               = callable<[string], SyncResult>('sync_game');
const resolveConflict        = callable<[string, string], ResolveResult>('resolve_conflict');
const getDeckySettings       = callable<[], DeckySettings>('get_decky_settings');
const setDeckySettings       = callable<[boolean, boolean], { ok: boolean }>('set_decky_settings');
const autoPullIfNoConflict   = callable<[], Record<string, unknown>>('auto_pull_if_no_conflict');

// ── Display map ────────────────────────────────────────────────────────────────

const OVERLAY_STATUSES: Record<string, { label: string; color: string }> = {
  in_sync:       { label: 'Saves in sync',                                                                    color: '#4caf76' },
  syncing:       { label: 'Syncing saves…',                                                                   color: '#67c1f5' },
  cloud_ahead:   { label: 'Cloud has newer saves — tap to sync now',                                          color: '#67c1f5' },
  local_ahead:   { label: 'Local saves not pushed — tap to sync now',                                         color: '#f39c12' },
  conflict:      { label: 'Save conflict — tap to resolve',                                                   color: '#e74c3c' },
  no_connection: { label: 'Unable to reach cloud provider',                                                   color: '#8899a6' },
  error:         { label: 'Save sync error',                                                                  color: '#e74c3c' },
};

// ── Conflict modal ─────────────────────────────────────────────────────────────

function ConflictModal({
  closeModal,
  onKeep,
  onDismiss,
}: {
  closeModal?: () => void;
  onKeep: (keep: 'local' | 'remote') => void;
  onDismiss: () => void;
}) {
  const choose = useCallback((keep: 'local' | 'remote') => {
    onDismiss();
    closeModal?.();
    onKeep(keep);
  }, [closeModal, onKeep, onDismiss]);

  return (
    <ModalRoot closeModal={closeModal}>
      <DialogHeader>Save Conflict</DialogHeader>
      <DialogBody>
        <p style={{ marginBottom: '12px' }}>
          Both local and cloud saves have changed. Which version should be kept?
        </p>
      </DialogBody>
      <DialogFooter>
        <Focusable style={{ display: 'flex', gap: '8px' }} {...{ 'flow-children': 'row' }}>
          <DialogButton onClick={() => choose('remote')}>Keep Cloud</DialogButton>
          <DialogButton onClick={() => choose('local')}>Keep Local</DialogButton>
          <DialogButton onClick={() => { onDismiss(); closeModal?.(); }}>Cancel</DialogButton>
        </Focusable>
      </DialogFooter>
    </ModalRoot>
  );
}

// ── Status bar ─────────────────────────────────────────────────────────────────

const CLICKABLE = new Set(['cloud_ahead', 'local_ahead', 'conflict']);
const TRANSIENT_STATUSES = new Set(['no_connection', 'error', 'syncing']);
const SHORT_LABELS: Record<string, string> = {
  in_sync:     'was in sync',
  cloud_ahead: 'cloud was ahead',
  local_ahead: 'local was ahead',
  conflict:    'conflict pending',
  unknown:     'never synced',
};

function SyncStatusBar({ appid }: { appid: string }) {
  const [status, setStatus] = useState<string | null>(null);
  const [gameId, setGameId] = useState<string | null>(null);
  const [lastKnownStatus, setLastKnownStatus] = useState<string | null>(null);
  const conflictModalOpen = useRef(false);

  const fetchStatus = useCallback(() => {
    getSyncStatusForAppId(appid)
      .then(res => {
        setStatus(res.status);
        setGameId(res.game_id ?? null);
        if (res.last_known_status) {
          setLastKnownStatus(res.last_known_status);
        } else if (!TRANSIENT_STATUSES.has(res.status)) {
          setLastKnownStatus(res.status);
        }
      })
      .catch(() => {});
  }, [appid]);

  useEffect(() => {
    setStatus(null);
    setGameId(null);
    fetchStatus();
  }, [appid]);

  const entry = status ? (OVERLAY_STATUSES[status] ?? null) : null;
  const clickable = status !== null && CLICKABLE.has(status);
  const lastKnownSuffix =
    entry && TRANSIENT_STATUSES.has(status!) && lastKnownStatus && SHORT_LABELS[lastKnownStatus]
      ? ` — ${SHORT_LABELS[lastKnownStatus]}`
      : '';

  const handleActivate = useCallback(() => {
    if (!gameId || status === 'syncing') return;

    if (status === 'conflict') {
      if (conflictModalOpen.current) return;
      conflictModalOpen.current = true;
      showModal(
        <ConflictModal
          onKeep={(keep) => {
            setStatus('syncing');
            resolveConflict(gameId, keep)
              .then(() => fetchStatus())
              .catch(() => fetchStatus());
          }}
          onDismiss={() => { conflictModalOpen.current = false; }}
        />,
      );
      return;
    }

    // cloud_ahead or local_ahead — sync immediately
    setStatus('syncing');
    syncGame(gameId)
      .then(() => fetchStatus())
      .catch(() => fetchStatus());
  }, [status, gameId, fetchStatus]);

  if (!entry) return null;

  const inner = (
    <>
      <div
        className={(playSectionClasses as any).CloudStatusIcon ?? ''}
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'inherit', marginRight: '5px' }}
      >
        <svg
          viewBox="0 0 24 24"
          width="18"
          height="18"
          fill="currentColor"
          style={{ display: 'block' }}
          aria-hidden="true"
        >
          <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4c-2.89 0-5.4 1.64-6.65 4.04C2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z"/>
        </svg>
      </div>
      <div
        className={(playSectionClasses as any).CloudStatusLabel ?? ''}
        style={{ cursor: 'inherit' }}
      >
        {entry.label}{lastKnownSuffix}
      </div>
    </>
  );

  if (clickable) {
    return (
      <Focusable
        className={(playSectionClasses as any).CloudStatusRow ?? ''}
        style={{ color: entry.color, position: 'relative' }}
        onActivate={handleActivate}
      >
        {inner}
        <div
          onClick={handleActivate}
          style={{ position: 'absolute', inset: 0, cursor: 'pointer' }}
        />
      </Focusable>
    );
  }

  return (
    <div
      className={(playSectionClasses as any).CloudStatusRow ?? ''}
      style={{ color: entry.color }}
    >
      {inner}
    </div>
  );
}

// ── QAM panel ──────────────────────────────────────────────────────────────────

import SyncPage from './SyncPage';

const MANAGE_ROUTE = '/externalgamesync-manage';

interface SettingsRef {
  polling_enabled: boolean;
  poll_auto_pull:  boolean;
}

function Content({ settingsRef }: { settingsRef: SettingsRef }) {
  const [pollingEnabled, setPollingEnabled] = useState(settingsRef.polling_enabled);
  const [pollAutoPull,   setPollAutoPull]   = useState(settingsRef.poll_auto_pull);
  const [loaded, setLoaded]                 = useState(false);

  useEffect(() => {
    getDeckySettings()
      .then(s => {
        setPollingEnabled(s.polling_enabled);
        setPollAutoPull(s.poll_auto_pull);
        settingsRef.polling_enabled = s.polling_enabled;
        settingsRef.poll_auto_pull  = s.poll_auto_pull;
      })
      .catch(() => {})
      .finally(() => setLoaded(true));
  }, []);

  const handlePollingChange = useCallback((val: boolean) => {
    const newAutoPull = val ? pollAutoPull : false;
    setPollingEnabled(val);
    setPollAutoPull(newAutoPull);
    settingsRef.polling_enabled = val;
    settingsRef.poll_auto_pull  = newAutoPull;
    setDeckySettings(val, newAutoPull).catch(() => {});
  }, [pollAutoPull, settingsRef]);

  const handleAutoPullChange = useCallback((val: boolean) => {
    setPollAutoPull(val);
    settingsRef.poll_auto_pull = val;
    setDeckySettings(pollingEnabled, val).catch(() => {});
  }, [pollingEnabled, settingsRef]);

  return (
    <>
      <PanelSection title="Save Sync">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => { Router.CloseSideMenus(); Router.Navigate(MANAGE_ROUTE); }}>
            Manage Saves
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Settings">
        <PanelSectionRow>
          <ToggleField
            label="Background Polling"
            description="Check for save changes every 5 min"
            checked={pollingEnabled}
            onChange={handlePollingChange}
            disabled={!loaded}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Auto-pull on Poll"
            description="Pull cloud saves when no conflict detected"
            checked={pollAutoPull}
            onChange={handleAutoPullChange}
            disabled={!loaded || !pollingEnabled}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

// ── Plugin entry point ─────────────────────────────────────────────────────────

const GAME_ROUTE = '/library/app/:appid';
const STATUS_KEY = 'ges-sync-status';

const POLL_INTERVAL_MS = 5 * 60 * 1000;

export default definePlugin(() => {
  const patchedNodes = new WeakSet<object>();
  const patchedMemos = new WeakSet<object>();
  let patches: Array<{ unpatch: () => void }> = [];

  const settingsRef: SettingsRef = { polling_enabled: true, poll_auto_pull: false };
  getDeckySettings()
    .then(s => {
      settingsRef.polling_enabled = s.polling_enabled;
      settingsRef.poll_auto_pull  = s.poll_auto_pull;
    })
    .catch(() => {});

  const pollInterval = setInterval(async () => {
    if (!settingsRef.polling_enabled) return;
    if ((Router as any).MainRunningApp) return;
    try {
      if (settingsRef.poll_auto_pull) {
        await autoPullIfNoConflict();
      } else {
        await getSyncStatuses();
      }
    } catch { /* background poll — ignore errors */ }
  }, POLL_INTERVAL_MS);

  const patchFn = (props: any): any => {
    try {
      const renderNode = findInReactTree(props, (n: any) => typeof n?.renderFunc === 'function');
      if (!renderNode || patchedNodes.has(renderNode)) return props;
      patchedNodes.add(renderNode);

      const outerPatch = afterPatch(renderNode, 'renderFunc', (_args: any[], ret: any) => {
        const gameNode = findInReactTree(
          ret,
          (x: any) => x?.props?.overview !== undefined && x?.props?.LaunchingDetails !== undefined,
        );
        if (!gameNode) return ret;

        const rawType = gameNode.type;
        if (typeof rawType !== 'object' || typeof rawType?.type !== 'function') return ret;
        if (patchedMemos.has(rawType)) return ret;

        patchedMemos.add(rawType);
        const innerPatch = afterPatch(rawType, 'type', (_fnArgs: any[], innerRet: any) => {
          const appid = window.location.pathname.match(/\/app\/(\d+)/)?.[1];
          if (!appid) return innerRet;

          const ps = playSectionClasses as any;
          const ad = appDetailsClasses  as any;

          // ── Injection attempt 1: PlayBarCloudStatusContainer ─────────────
          const cloudClass = ps?.PlayBarCloudStatusContainer;
          if (cloudClass) {
            const container = findInReactTree(innerRet, (x: any) =>
              typeof x?.props?.className === 'string' &&
              x.props.className.includes(cloudClass),
            );
            if (container) {
              const kids = container.props.children;
              const already = Array.isArray(kids)
                ? kids.some((c: any) => c?.key === STATUS_KEY)
                : kids?.key === STATUS_KEY;
              if (!already) {
                container.props.children = [
                  <SyncStatusBar key={STATUS_KEY} appid={appid} />,
                  ...(Array.isArray(kids) ? kids : (kids ? [kids] : [])),
                ];
              }
              return innerRet;
            }
          }

          // ── Injection attempt 2: AppDetails InnerContainer at index 1 ────
          const innerClass = ad?.InnerContainer;
          if (innerClass) {
            const container = findInReactTree(innerRet, (x: any) =>
              Array.isArray(x?.props?.children) &&
              typeof x?.props?.className === 'string' &&
              x.props.className.includes(innerClass),
            );
            if (container) {
              const kids: any[] = container.props.children;
              if (!kids.some((c: any) => c?.key === STATUS_KEY)) {
                kids.splice(1, 0, <SyncStatusBar key={STATUS_KEY} appid={appid} />);
              }
            }
          }

          return innerRet;
        });

        patches.push(innerPatch);
        return ret;
      });

      patches.push(outerPatch);
    } catch (e) {
      console.error('[ExternalGameSync] patchFn error:', e);
    }
    return props;
  };

  routerHook.addPatch(GAME_ROUTE, patchFn);
  routerHook.addRoute(MANAGE_ROUTE, () => <SyncPage />, { exact: true });

  return {
    name:      'ExternalGameSync',
    titleView: <div>ExternalGameSync</div>,
    content:   <Content settingsRef={settingsRef} />,
    icon:      <img src={appIcon} style={{ width: '20px', height: '20px' }} />,
    onDismount() {
      clearInterval(pollInterval);
      routerHook.removePatch(GAME_ROUTE, patchFn);
      routerHook.removeRoute(MANAGE_ROUTE);
      patches.forEach(p => p.unpatch());
      patches = [];
    },
  };
});
