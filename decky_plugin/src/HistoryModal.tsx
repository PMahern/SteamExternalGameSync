import { useCallback, useEffect, useState } from 'react';
import {
  DialogBody,
  DialogButton,
  DialogFooter,
  DialogHeader,
  Focusable,
  ModalRoot,
} from '@decky/ui';
import { callable } from '@decky/api';

// ── Types ──────────────────────────────────────────────────────────────────────

interface BackupEntry {
  source: 'cloud' | 'local';
  name: string;
  when: string | null;
  deleted: boolean;
  files: number;
  size: number;
  newest_file: string | null;
}

interface HistoryResult {
  entries: BackupEntry[];
  cloud_ok: boolean;
  keep: number;
  error: string | null;
}

interface RestoreResult {
  ok: boolean;
  lines: { msg: string; ok: boolean }[];
}

// ── Backend callables ─────────────────────────────────────────────────────────

const getSaveHistory = callable<[string], HistoryResult>('get_save_history');
const restoreBackup  = callable<[string, string, string], RestoreResult>('restore_backup');

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function whereLabel(e: BackupEntry): string {
  return (e.source === 'cloud' ? 'Cloud' : 'This device') + (e.deleted ? ' (deleted config)' : '');
}

const GREY = '#8899a6';
const RED  = '#e74c3c';

type Phase =
  | { kind: 'loading' }
  | { kind: 'list'; data: HistoryResult }
  | { kind: 'confirm'; data: HistoryResult; entry: BackupEntry }
  | { kind: 'restoring'; entry: BackupEntry }
  | { kind: 'done'; result: RestoreResult }
  | { kind: 'error'; msg: string };

// ── Modal ─────────────────────────────────────────────────────────────────────

export default function HistoryModal({
  closeModal,
  gameId,
  gameName,
  onRestored,
}: {
  closeModal?: () => void;
  gameId: string;
  gameName: string;
  onRestored?: () => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });

  const load = useCallback(() => {
    setPhase({ kind: 'loading' });
    getSaveHistory(gameId)
      .then(data => setPhase(data.error ? { kind: 'error', msg: data.error } : { kind: 'list', data }))
      .catch(() => setPhase({ kind: 'error', msg: 'Request failed' }));
  }, [gameId]);

  useEffect(load, [load]);

  const restore = useCallback((entry: BackupEntry) => {
    setPhase({ kind: 'restoring', entry });
    restoreBackup(gameId, entry.source, entry.name)
      .then(result => {
        setPhase({ kind: 'done', result });
        if (result.ok) onRestored?.();
      })
      .catch(() => setPhase({ kind: 'error', msg: 'Restore request failed' }));
  }, [gameId, onRestored]);

  // Can't close mid-restore: the backend keeps going and the user would lose the result
  const close = phase.kind === 'restoring' ? undefined : closeModal;

  let body: React.ReactNode;
  let footer: React.ReactNode;

  switch (phase.kind) {
    case 'loading':
      body = <div style={{ color: GREY }}>Loading backups...</div>;
      footer = <DialogButton onClick={closeModal}>Close</DialogButton>;
      break;

    case 'error':
      body = <div style={{ color: RED }}>{phase.msg}</div>;
      footer = (
        <>
          <DialogButton onClick={load}>Retry</DialogButton>
          <DialogButton onClick={closeModal}>Close</DialogButton>
        </>
      );
      break;

    case 'list': {
      const { entries, cloud_ok, keep } = phase.data;
      body = (
        <>
          <div style={{ color: GREY, fontSize: '13px', marginBottom: '8px' }}>
            Saves are backed up whenever they're about to be replaced. The {keep} newest
            are kept on each side. Restoring puts a backup on this device and the cloud;
            other devices get it on their next launch.
          </div>
          {!cloud_ok && (
            <div style={{ color: RED, fontSize: '13px', marginBottom: '8px' }}>
              Couldn't reach cloud storage, so only backups on this device are shown.
            </div>
          )}
          {entries.length === 0 && <div style={{ color: GREY }}>No backups yet.</div>}
          <Focusable style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {entries.map(e => (
              <Focusable
                key={`${e.source}/${e.name}`}
                style={{
                  display: 'flex', alignItems: 'center', gap: '12px',
                  padding: '6px 10px', background: 'rgba(255,255,255,0.04)', borderRadius: '4px',
                }}
                {...{ 'flow-children': 'row' }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: '14px', fontWeight: 500 }}>
                    {e.when ?? e.name}
                    <span style={{ color: GREY, fontWeight: 400, marginLeft: '8px' }}>{whereLabel(e)}</span>
                  </div>
                  <div style={{ fontSize: '12px', color: GREY }}>
                    {e.files} file{e.files !== 1 ? 's' : ''} · {fmtSize(e.size)}
                    {e.newest_file && ` · newest save ${e.newest_file}`}
                  </div>
                </div>
                <DialogButton
                  style={{ width: '90px', minWidth: 'unset', fontSize: '12px', padding: '4px 0' }}
                  onClick={() => setPhase({ kind: 'confirm', data: phase.data, entry: e })}
                >
                  Restore
                </DialogButton>
              </Focusable>
            ))}
          </Focusable>
        </>
      );
      footer = <DialogButton onClick={closeModal}>Close</DialogButton>;
      break;
    }

    case 'confirm': {
      const e = phase.entry;
      body = (
        <>
          <p style={{ marginTop: 0 }}>
            Restore the backup from <b>{e.when ?? e.name}</b> ({whereLabel(e).toLowerCase()},{' '}
            {e.files} files, {fmtSize(e.size)})?
          </p>
          <p style={{ color: GREY, fontSize: '13px' }}>
            This replaces the saves on this device and in the cloud. The restored files get the
            current time as their timestamp, so every other device treats them as newest and
            downloads them on its next launch. Your current saves are backed up first, so you
            can undo this from here.
          </p>
          <p style={{ color: RED, fontSize: '13px' }}>Close the game on every device before restoring.</p>
        </>
      );
      footer = (
        <>
          <DialogButton onClick={() => restore(e)}>Restore</DialogButton>
          <DialogButton onClick={() => setPhase({ kind: 'list', data: phase.data })}>Back</DialogButton>
        </>
      );
      break;
    }

    case 'restoring':
      body = <div style={{ color: GREY }}>Restoring backup from {phase.entry.when ?? phase.entry.name}...</div>;
      footer = <DialogButton disabled>Restoring...</DialogButton>;
      break;

    case 'done':
      body = (
        <>
          <div style={{ color: phase.result.ok ? '#4caf76' : RED, fontWeight: 600, marginBottom: '8px' }}>
            {phase.result.ok ? 'Saves restored' : 'Restore failed'}
          </div>
          {phase.result.lines.map((l, i) => (
            <div key={i} style={{ fontSize: '13px', color: l.ok ? undefined : RED }}>
              {l.ok ? '✓' : '✗'} {l.msg}
            </div>
          ))}
        </>
      );
      footer = (
        <>
          <DialogButton onClick={closeModal}>Done</DialogButton>
          <DialogButton onClick={load}>Back to history</DialogButton>
        </>
      );
      break;
  }

  return (
    <ModalRoot closeModal={close}>
      <DialogHeader>Save History — {gameName}</DialogHeader>
      <DialogBody>{body}</DialogBody>
      <DialogFooter>
        <Focusable style={{ display: 'flex', gap: '8px' }} {...{ 'flow-children': 'row' }}>
          {footer}
        </Focusable>
      </DialogFooter>
    </ModalRoot>
  );
}
