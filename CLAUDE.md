# ExternalGameSync — Claude Code Notes

## What this project does
Syncs non-Steam game saves across machines (Windows + Linux/Proton) via rclone (WebDAV, Nextcloud, Owncloud, SFTP, and other rclone-compatible storage).
Each game has a config in `games.json` on the remote (shared, no machine-specific data). Each machine stores its own paths locally in `machine_configs.json` (see `machine_config.py`).

## File map
| File | Role |
|------|------|
| `config.py` | Paths, subprocess helper, Steam root discovery, logging, settings |
| `games.py` | games.json registry, machine config helpers |
| `rclone.py` | rclone availability/setup (WebDAV + SFTP), games.json push/pull/merge |
| `sync.py` | Save snapshots, conflict detection, backup, pull/push sync ops, GameLock |
| `steam.py` | Proton path resolution, symlinks, shortcuts.vdf read/write, launch wrappers |
| `artwork.py` | Steam grid artwork push/pull via cloud storage |
| `gui_common.py` | DPG theme, nav state, shared widgets, async helpers, Win/Proton path helpers |
| `gui_setup.py` | Cloud storage setup flow |
| `gui_home.py` | Home dashboard, navigation guard |
| `gui_assign.py` | Assign existing cloud config to a Steam shortcut |
| `gui_add.py` | Create new game config from a Steam shortcut |
| `gui_sync.py` | Sync all saves, sync artwork, relink symlinks |
| `gui.py` | Window layout, sidebar, entry point (`main()`) |
| `externalgamesync.py` | CLI entry point: `launch`, `pull`, `push`, `sync`, `list`, `log`, `gui` commands |

## Key data structures

### games.json (stored on cloud via SYNC_ROOT/games.json — shared, no machine data)
```json
{
  "games": [{
    "id": "game_id",
    "name": "Game Name",
    "exe_path": "...",         // relative to drive_c (Windows-style separators)
    "save_path": "...",        // relative to drive_c (Windows-style separators)
    "save_filter": "",         // optional rclone glob filter
    "env_vars": "",            // env vars for launch (Linux only)
    "added": "2026-01-01T00:00:00"
  }]
}
```

### machine_configs.json (local only — APP_CONFIG_DIR/machine_configs.json)
```json
{
  "game_id": {
    "platform": "linux",
    "app_id": "steam_compatdata_id"
  }
}
```
Linux entries store only `platform` and `app_id` — exe/save paths are resolved at runtime from the shared `exe_path`/`save_path` via `resolve_exe_path` / `resolve_save_path`.

Windows entries additionally store absolute paths since they vary by username/install:
```json
{
  "game_id": {
    "platform": "windows",
    "app_id": "123456",
    "exe_path": "C:\\...",
    "save_path": "C:\\..."
  }
}
```

## Path handling — critical detail

### Linux/Proton paths
- Proton prefix: `~/.local/share/Steam/steamapps/compatdata/<app_id>/pfx/drive_c/`
- Exe stored as: `Program Files\Game\game.exe` (relative to drive_c)
- Save stored as: `users\steamuser\AppData\Roaming\Game` (relative to drive_c, always `steamuser`)

### Windows paths
- Exe stored as: `C:\Program Files\Game\game.exe` (absolute)
- Save stored as: `C:\Users\<username>\AppData\Roaming\Game` (absolute)

### Cross-platform conversion (core.py)
- `_to_drive_c_rel(path)` — strips drive letter, converts `\` to `/`
- `_normalize_proton_save_path(path)` — additionally maps `Users/<any-name>/` → `users/steamuser/` so Windows-created configs work on Linux
- `resolve_exe_path(app_id, path)` — resolves exe inside Proton prefix
- `resolve_save_path(app_id, path)` — resolves save inside Proton prefix (uses normalize)

### Windows auto-detection (gui.py)
- `_win_exe_candidates(path)` — given any path format, returns Windows absolute Path candidates to check
- `_win_save_candidates(path)` — same for save paths; maps `users\steamuser\AppData\Roaming\X` → `%APPDATA%\X` etc.
- `_try_resolve_windows_paths(game_cfg)` — tries other Windows machine configs, then top-level paths; returns `(exe, save)` Paths or None

## GUI flows
- **flow_assign** — assigns an existing cloud storage config to a Steam shortcut on this machine. On Windows, tries to auto-detect paths before showing file pickers.
- **flow_add_config** — creates a new config from a Steam shortcut. On Windows, tries to use the shortcut's exe directly.
- **flow_sync_art** — push/pull Steam grid artwork via cloud storage
- **flow_sync_all** — pull + push all games
- **flow_relink** — recreate symlinks (Linux only, after reinstall)

## Save sync mechanism
- Linux: `SYNC_ROOT/saves/<game_id>/` is a **symlink** pointing to the Proton save directory
- Windows: rclone syncs directly to/from the absolute `save_path` stored in machine_configs
- rclone remote: `externalgamesync_nc:ExternalGameSync/`
- Conflict detection uses snapshot files in `APP_CONFIG_DIR/snapshots/`

## Steam launch integration
Steam shortcut Launch Options are rewritten to:
```
"<wrapper>.bat" %command%    # Windows
"<wrapper>.sh" %command%     # Linux
```
The wrapper script calls `externalgamesync pull`, then `%command%` (the real game via Proton), then `externalgamesync push`.

## Platform locations
| | Windows | Linux |
|---|---|---|
| App config | `%LOCALAPPDATA%\ExternalGameSync\` | `~/.config/externalgamesync/` |
| Sync root | `~/ExternalGameSync/` | `~/ExternalGameSync/` |
| games.json | `~/ExternalGameSync/games.json` | `~/ExternalGameSync/games.json` |
| Log | `APP_CONFIG_DIR/sync.log` | `APP_CONFIG_DIR/sync.log` |
| Wrappers | `%LOCALAPPDATA%\ExternalGameSync\wrappers\*.bat` | `~/.local/share/externalgamesync/wrappers/*.sh` |

## Pre-launcher (Windows only)

`pre-launcher/pre-launcher.c` — a native Win32 C app that runs **on the Windows side** during a Steam launch. It is invoked by the Windows `.bat` wrapper instead of showing nothing while syncing happens. It renders fullscreen styled dialogs with controller (XInput) and keyboard support, scaled to any resolution from 1080p to 4K.

### Dialog flow (phases)
1. **Syncing** (`setup_syncing`) — shown while the Linux-side pull runs. Polls `%TEMP%\egs_ready.txt`; auto-dismisses when the file appears.
2. **Conflict** (`setup_conflict`) — shown if pull detected a conflict. Three buttons: Keep Cloud (A), Keep Local (B), Cancel Launch (Y). Writes `egs_choice.txt` with `"remote"` or `"local"`, then shows another Syncing screen while Linux applies the choice.
3. **No Connection** (`setup_no_connection` / `setup_no_connection_server_ahead`) — shown if cloud was unreachable. Two buttons: Continue Anyway (A), Cancel Launch (B). The "server ahead" variant adds a stronger warning when the last known status was `cloud_ahead` or `conflict`.
4. **Saves synced!** (`setup_synced`) — brief 2-second auto-dismiss notification shown after a clean pull before launching.
5. **Game runs** — `run_wait()` launches the game exe and waits for all job-tracked processes to exit, plus a directory scan for launchers that break out of the job (e.g. RE2-style two-stage launchers).
6. **Pushing** (`setup_pushing`) — shown while Linux-side push runs. Polls `%TEMP%\egs_push_done.txt`.
7. **Saves uploaded!** (`setup_pushed`) — brief 3-second auto-dismiss notification after push completes.

### IPC files (all in `%TEMP%`)
| File | Written by | Meaning |
|------|-----------|---------|
| `egs_status.txt` | Linux wrapper | `STATUS=`, `GAME=`, `GAME_EXE=`, `LAST_STATUS=` read at startup |
| `egs_ready.txt` | Linux handler | Pull (or conflict resolution) complete — pre-launcher may proceed |
| `egs_choice.txt` | pre-launcher | Conflict resolution: `"remote"` or `"local"` |
| `egs_cancelled.txt` | pre-launcher | User cancelled launch |
| `egs_push_start.txt` | pre-launcher | Game has exited; Linux should start push |
| `egs_push_done.txt` | Linux handler | Push complete |

### Visual design
- Fullscreen `WS_POPUP` window (topmost), double-buffered via a compatible memory DC
- Dark theme: `C_BG` `#0e0e1a`, panel `#161626`, accent bar `#4040d0`
- Panel is 70% screen width × 60% screen height, centered
- Thin colored accent bar at top of panel (always `C_ACCENT`)
- Buttons are color-coded: Green=confirm/keep-cloud, Red=destructive/cancel, Gold=cancel-launch, Blue=continue-with-risk
- Selected button gets a bright `C_SEL_BDR` `#6060ff` border; unselected buttons have a 1px same-color border
- Font: Segoe UI throughout. Heading 26pt bold, body 18pt, labels 13pt, nav hint 14pt
- Navigation hint drawn below the panel: D-Pad/Arrows, Start/Enter, Esc

### Build
```sh
# Linux cross-compile (mingw-w64):
x86_64-w64-mingw32-gcc -O2 -mwindows -Wall -o pre-launcher.exe pre-launcher.c

# Windows MSVC (Developer PowerShell):
cl /O2 /W3 pre-launcher.c user32.lib gdi32.lib /Fe:pre-launcher.exe /link /subsystem:windows
```
Build scripts: `build.sh` (cross-compile), `build_windows.ps1` (MSVC).

## Dependencies
- `rclone` (external binary) — file sync
- `vdf` Python package — parse/write Steam's binary shortcuts.vdf
- `tkinter` — GUI dialogs on Windows
- `zenity` — GUI dialogs on Linux
