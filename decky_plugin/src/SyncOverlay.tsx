import { useEffect, useState } from 'react';
import { callable } from '@decky/api';

// ── Types ──────────────────────────────────────────────────────────────────────

interface StatusResult {
  status: string;
  name?: string;
}

// ── Backend callable ───────────────────────────────────────────────────────────

const getSyncStatusForAppId = callable<[string], StatusResult>('get_sync_status_for_appid');

// ── Display map ────────────────────────────────────────────────────────────────
// Only statuses worth showing on the launch screen are included.
// in_sync / unknown / none → no overlay.

const OVERLAY_STATUSES: Record<string, { label: string; color: string }> = {
  cloud_ahead:   { label: 'Cloud has newer saves — sync before playing', color: '#67c1f5' },
  local_ahead:   { label: 'Local saves not pushed to cloud',             color: '#f39c12' },
  conflict:      { label: 'Save conflict — open plugin to resolve',      color: '#e74c3c' },
  no_connection: { label: 'Save sync offline',                           color: '#8899a6' },
  error:         { label: 'Save sync error',                             color: '#e74c3c' },
};

// ── Component ──────────────────────────────────────────────────────────────────

export default function SyncOverlay({ appid }: { appid: string }) {
  const [entry, setEntry] = useState<{ label: string; color: string } | null>(null);

  useEffect(() => {
    setEntry(null);
    console.log('[ExternalGameSync] fetching status for appid:', appid);
    getSyncStatusForAppId(appid)
      .then(res => {
        setEntry(OVERLAY_STATUSES[res.status] ?? { label: `status: ${res.status}`, color: '#aaaaaa' });
      })
      .catch(err => {
        setEntry({ label: `error: ${err}`, color: '#e74c3c' });
      });
  }, [appid]);

  // DEBUG: always render to verify the overlay pipeline.
  // Shows appid + raw status while loading/unknown so we can confirm rendering
  // and URL matching work. Remove this block and restore `if (!entry) return null`
  // once confirmed working.
  const debugEntry = entry ?? { label: `appid ${appid} — checking...`, color: '#aaaaaa' };

  return (
    <div style={{
      position:       'fixed',
      top:            '24px',
      left:           '24px',
      zIndex:         9999,
      background:     'rgba(14, 15, 22, 0.92)',
      border:         `1px solid ${debugEntry.color}`,
      borderRadius:   '4px',
      padding:        '6px 14px',
      color:          debugEntry.color,
      fontSize:       '13px',
      fontWeight:     500,
      pointerEvents:  'none',
      backdropFilter: 'blur(4px)',
      userSelect:     'none',
    }}>
      ☁ {debugEntry.label}
    </div>
  );
}
