# ExternalGameSync — Decky Plugin Notes

## What this plugin does
A [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for the Steam Deck that surfaces save sync status and controls in the Steam UI. It does not contain sync logic itself — it calls into the installed Python app at `~/.local/share/externalgamesync/`.

## File map
| File | Role |
|------|------|
| `main.py` | Python backend — bridges Decky's RPC to the app's sync/game modules |
| `src/index.tsx` | Plugin entry point — QAM panel, game page status bar, conflict modal, router patch |
| `src/SyncModal.tsx` | QAM modal — game list with per-game sync buttons and conflict resolution |
| `src/SyncOverlay.tsx` | (unused/legacy) |
| `dist/index.js` | Compiled frontend — what Decky actually loads |
| `build.sh` / `build.bat` | Build scripts that compile TSX and copy output |

## Backend (main.py)
Python class `Plugin` with async methods callable from the frontend via `callable<>()`:

| Method | Description |
|--------|-------------|
| `get_games()` | All games + whether assigned to this machine |
| `get_sync_statuses()` | `{game_id: status}` for all assigned games (metadata-only, no transfer) |
| `get_sync_status_for_appid(app_id)` | Status for a single game by Steam app ID — used by the game page overlay |
| `sync_game(game_id)` | Pull then push a single game |
| `sync_all()` | Pull then push all assigned games (emits `sync_progress` events) |
| `resolve_conflict(game_id, keep)` | Resolve conflict: `keep='local'` or `keep='remote'` |

Backend imports from the installed app (`~/.local/share/externalgamesync/`) at call time via `_ensure_path()` — not at module load — so the plugin tolerates the app not being installed.

## Frontend (index.tsx)
- **QAM panel** (`Content`): single "Manage Saves" button that opens `SyncModal`
- **`SyncStatusBar`**: injected into the game detail page via `routerHook` + `afterPatch`. Shows sync status colored label (in sync / cloud ahead / local ahead / conflict / offline). Clickable for actionable states — triggers immediate sync or opens conflict modal
- **Injection strategy**: two-attempt approach patching `PlayBarCloudStatusContainer` first, falling back to `AppDetails InnerContainer` index 1. Uses `WeakSet` guards to avoid double-patching memos/nodes
- **`ConflictModal`**: shown on conflict tap — "Keep Cloud" / "Keep Local" / Cancel

## Frontend (SyncModal.tsx)
- Loads game list immediately, then fetches sync statuses in background (two-phase load so the list appears fast)
- Sync All iterates games sequentially from the frontend (not a single backend call) so per-game status updates live in React state
- Per-game sync button + conflict resolution (Local / Cloud buttons) inline in each row
- Left border color indicates sync status at a glance

## Status values (shared between backend and frontend)
| Value | Meaning |
|-------|---------|
| `in_sync` | Local matches cloud |
| `cloud_ahead` | Cloud changed since last sync |
| `local_ahead` | Local changed since last sync |
| `conflict` | Both sides changed since last sync |
| `unknown` | No snapshot yet (never synced on this machine) |
| `no_connection` | Cannot reach cloud provider |
| `error` | Something went wrong |

## Build
```sh
cd decky_plugin
./build.sh        # Linux
build.bat         # Windows
```
Compiles TSX with pnpm/rollup and writes `dist/index.js`. The compiled file is what Decky loads — always rebuild after frontend changes.

## Key constraints
- DPG (used in the main GUI) is not involved here — this is pure Decky/React
- No state is stored in the plugin — all data comes from the app's files on disk
- The `app_id` used by the game page overlay is the Steam compatdata ID (same as stored in `machine_configs.json`)
