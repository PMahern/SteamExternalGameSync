import { useCallback, useEffect, useState } from 'react';
import { DialogButton, Focusable } from '@decky/ui';
import { callable } from '@decky/api';

// ── Types ──────────────────────────────────────────────────────────────────────

interface GameInfo {
  id: string;
  name: string;
  assigned: boolean;
}

type SyncStatus     = 'idle' | 'syncing' | 'ok' | 'error' | 'conflict' | 'resolved';
type SyncStatusType = 'in_sync' | 'cloud_ahead' | 'local_ahead' | 'conflict'
                    | 'unknown' | 'no_connection' | 'error';

interface GameSyncState {
  id: string;
  name: string;
  status: SyncStatus;
  syncStatus?: SyncStatusType;
  lastKnownSyncStatus?: SyncStatusType;
  msg?: string;
}

interface SyncStatusEntry {
  status: SyncStatusType;
  last_known_status?: SyncStatusType | null;
}

interface SyncGameResult {
  status: string;
  msg: string;
}

// ── Backend callables ─────────────────────────────────────────────────────────

const getGames        = callable<[], GameInfo[]>('get_games');
const syncGame        = callable<[string], SyncGameResult>('sync_game');
const resolveConflict = callable<[string, string], { ok: boolean; msg: string }>('resolve_conflict');
const getSyncStatuses = callable<[], Record<string, SyncStatusEntry>>('get_sync_statuses');

// ── Display helpers ───────────────────────────────────────────────────────────

const STATUS_COLORS: Record<SyncStatus, string> = {
  idle:     '#8899a6',
  syncing:  '#67c1f5',
  ok:       '#57cbde',
  error:    '#e74c3c',
  conflict: '#f39c12',
  resolved: '#57cbde',
};

const SYNC_STATUS: Record<SyncStatusType, { label: string; color: string }> = {
  in_sync:       { label: 'in sync',       color: '#4ab87a' },
  cloud_ahead:   { label: 'cloud ahead',   color: '#67c1f5' },
  local_ahead:   { label: 'local ahead',   color: '#f39c12' },
  conflict:      { label: 'conflict',      color: '#e74c3c' },
  unknown:       { label: 'sync to check', color: '#8899a6' },
  no_connection: { label: 'offline',       color: '#8899a6' },
  error:         { label: 'error',         color: '#e74c3c' },
};

function getStatusDisplay(g: GameSyncState, statusesLoaded: boolean): { label: string; color: string } {
  if (g.status !== 'idle') {
    const label =
      g.status === 'syncing'  ? 'syncing...' :
      g.status === 'ok'       ? 'done'       :
      g.status === 'resolved' ? 'resolved'   :
      g.status === 'conflict' ? 'conflict'   :
      g.msg ? `error: ${g.msg}` : 'error';
    return { label, color: STATUS_COLORS[g.status] };
  }
  if (!statusesLoaded) return { label: '...', color: '#8899a6' };
  if (!g.syncStatus)   return { label: '',    color: '#8899a6' };
  const ss = SYNC_STATUS[g.syncStatus];
  if (g.syncStatus === 'no_connection' || g.syncStatus === 'error') {
    const lkSs = g.lastKnownSyncStatus ? SYNC_STATUS[g.lastKnownSyncStatus] : null;
    return { label: lkSs ? `${ss.label} (${lkSs.label})` : ss.label, color: ss.color };
  }
  return ss;
}

function leftBorderColor(g: GameSyncState): string {
  if (g.status !== 'idle') return STATUS_COLORS[g.status];
  switch (g.syncStatus) {
    case 'cloud_ahead': return '#67c1f5';
    case 'local_ahead': return '#f39c12';
    case 'conflict':    return '#e74c3c';
    case 'in_sync':     return '#4a6e5a';
    default:            return STATUS_COLORS.idle;
  }
}

// ── Page component ────────────────────────────────────────────────────────────

export default function SyncPage() {
  const [gameStates, setGameStates]         = useState<GameSyncState[]>([]);
  const [isSyncingAll, setIsSyncingAll]     = useState(false);
  const [syncingGames, setSyncingGames]     = useState<Set<string>>(new Set());
  const [resolvingGames, setResolvingGames] = useState<Set<string>>(new Set());
  const [loaded, setLoaded]                 = useState(false);
  const [statusesLoaded, setStatusesLoaded] = useState(false);

  const updateGame = useCallback((id: string, patch: Partial<GameSyncState>) => {
    setGameStates(prev => prev.map(g => (g.id === id ? { ...g, ...patch } : g)));
  }, []);

  useEffect(() => {
    getGames()
      .then(games => {
        const assigned = games.filter(g => g.assigned);
        setGameStates(assigned.map(g => ({ id: g.id, name: g.name, status: 'idle' })));
        setLoaded(true);
        getSyncStatuses()
          .then(statuses => {
            setGameStates(prev => prev.map(g => {
              const entry = statuses[g.id];
              return {
                ...g,
                syncStatus:          (entry?.status            ?? 'unknown') as SyncStatusType,
                lastKnownSyncStatus: (entry?.last_known_status ?? undefined) as SyncStatusType | undefined,
              };
            }));
          })
          .catch(() => {})
          .finally(() => setStatusesLoaded(true));
      })
      .catch(() => setLoaded(true));
  }, []);

  const handleSyncAll = useCallback(async () => {
    if (isSyncingAll) return;
    setIsSyncingAll(true);
    let ids: string[] = [];
    setGameStates(prev => {
      ids = prev.map(g => g.id);
      return prev.map(g => ({ ...g, status: 'idle' as SyncStatus, msg: undefined }));
    });
    await new Promise(r => setTimeout(r, 50));
    for (const game_id of ids) {
      updateGame(game_id, { status: 'syncing', msg: undefined });
      try {
        const res = await syncGame(game_id);
        updateGame(game_id, { status: res.status as SyncStatus, msg: res.msg });
      } catch {
        updateGame(game_id, { status: 'error', msg: 'Request failed' });
      }
    }
    setIsSyncingAll(false);
  }, [isSyncingAll, updateGame]);

  const handleSyncOne = useCallback(async (game_id: string) => {
    if (isSyncingAll || syncingGames.has(game_id)) return;
    setSyncingGames(prev => new Set(prev).add(game_id));
    updateGame(game_id, { status: 'syncing', msg: undefined });
    try {
      const res = await syncGame(game_id);
      updateGame(game_id, { status: res.status as SyncStatus, msg: res.msg });
    } catch {
      updateGame(game_id, { status: 'error', msg: 'Request failed' });
    } finally {
      setSyncingGames(prev => { const s = new Set(prev); s.delete(game_id); return s; });
    }
  }, [isSyncingAll, syncingGames, updateGame]);

  const handleResolve = useCallback(async (game_id: string, keep: 'local' | 'remote') => {
    if (resolvingGames.has(game_id)) return;
    setResolvingGames(prev => new Set(prev).add(game_id));
    try {
      const res = await resolveConflict(game_id, keep);
      updateGame(game_id, {
        status: res.ok ? 'resolved' : 'error',
        msg:    res.ok ? undefined  : res.msg,
      });
    } catch {
      updateGame(game_id, { status: 'error', msg: 'Request failed' });
    } finally {
      setResolvingGames(prev => { const s = new Set(prev); s.delete(game_id); return s; });
    }
  }, [resolvingGames, updateGame]);

  const anySyncing    = isSyncingAll || syncingGames.size > 0;
  const conflictCount = gameStates.filter(g => g.status === 'conflict').length;

  return (
    <div style={{ overflowY: 'auto', height: '100%', padding: '48px 16px 16px' }}>

      {/* Title */}
      <div style={{ fontSize: '20px', fontWeight: 700, marginBottom: '12px' }}>
        Manage ExternalGameSync Saves
      </div>

      {/* Conflict notice */}
      {conflictCount > 0 && (
        <div style={{ color: '#e74c3c', fontSize: '13px', marginBottom: '8px' }}>
          {conflictCount} conflict{conflictCount !== 1 ? 's' : ''} — choose what to keep below
        </div>
      )}

      {/* Sync All */}
      <Focusable style={{ marginBottom: '16px' }}>
        <DialogButton
          disabled={anySyncing || !loaded}
          onClick={handleSyncAll}
          style={{ width: '100%' }}
        >
          {isSyncingAll ? 'Syncing...' : 'Sync All'}
        </DialogButton>
      </Focusable>

      {/* Game list */}
      <Focusable style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
        {loaded && gameStates.length === 0 && (
          <div style={{ color: '#8899a6', padding: '8px 0' }}>
            No games assigned to this machine.
          </div>
        )}

        {gameStates.map(g => {
          const isConflict    = g.status === 'conflict';
          const isThisSyncing = g.status === 'syncing' || syncingGames.has(g.id);
          const isResolving   = resolvingGames.has(g.id);
          const { label: statusLabel, color: statusColor } = getStatusDisplay(g, statusesLoaded);

          return (
            <div
              key={g.id}
              style={{
                display:     'flex',
                alignItems:  'center',
                padding:     '8px 12px',
                background:  'rgba(255, 255, 255, 0.04)',
                borderRadius: '4px',
                borderLeft:  `3px solid ${leftBorderColor(g)}`,
                gap:         '12px',
              }}
            >
              {/* Left: name + status */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flex: 1, minWidth: 0 }}>
                <span style={{
                  fontSize: '14px', fontWeight: 500,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {g.name}
                </span>
                {statusLabel !== '' && (
                  <span style={{ fontSize: '12px', color: statusColor, whiteSpace: 'nowrap', flexShrink: 0 }}>
                    {statusLabel}
                  </span>
                )}
              </div>

              {/* Right: action buttons */}
              <div style={{ display: 'flex', gap: '6px', flexShrink: 0 }}>
                {!isConflict && !isSyncingAll && (
                  <DialogButton
                    disabled={isThisSyncing || anySyncing}
                    onClick={() => handleSyncOne(g.id)}
                    style={{ width: '70px', minWidth: 'unset', fontSize: '12px', padding: '4px 0' }}
                  >
                    {isThisSyncing ? '...' : 'Sync'}
                  </DialogButton>
                )}
                {isConflict && (
                  <>
                    <DialogButton
                      disabled={isResolving}
                      onClick={() => handleResolve(g.id, 'local')}
                      style={{ width: '70px', minWidth: 'unset', fontSize: '12px', padding: '4px 0' }}
                    >
                      Local
                    </DialogButton>
                    <DialogButton
                      disabled={isResolving}
                      onClick={() => handleResolve(g.id, 'remote')}
                      style={{ width: '70px', minWidth: 'unset', fontSize: '12px', padding: '4px 0' }}
                    >
                      Cloud
                    </DialogButton>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </Focusable>
    </div>
  );
}
