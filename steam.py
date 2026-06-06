"""
ExternalGameSync — Proton path resolution, symlinks, Steam shortcuts, and launch wrappers.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from config import (
    _run, log, log_err, log_ok,
    BACKUP_ROOT, SYNC_ROOT,
    find_steam_root_linux, find_steam_root_windows,
)
from games import game_id_from_name
from machine_config import get_local_config


# ── Proton / symlink helpers ──────────────────────────────────────────────────

def saves_link_path(game_id: str) -> Path:
    return SYNC_ROOT / "saves" / game_id


def get_proton_base() -> Path | None:
    """Return the compatdata directory for the active Steam install."""
    if sys.platform == "win32":
        return None
    root = find_steam_root_linux()
    return root / "steamapps/compatdata" if root else None


def find_compatdata(app_id: str) -> Path | None:
    """Find the compatdata directory for app_id across all Steam library folders."""
    for steamapps in _find_all_steamapps_dirs():
        candidate = steamapps / "compatdata" / str(app_id)
        if candidate.exists():
            return candidate
    return None


def proton_drive_c(steam_app_id: str) -> Path:
    compatdata = find_compatdata(str(steam_app_id))
    if compatdata:
        return compatdata / "pfx" / "drive_c"
    # Fall back to default location (prefix may not exist yet)
    base = get_proton_base()
    if not base:
        raise RuntimeError("Could not locate Steam compatdata directory")
    return base / str(steam_app_id) / "pfx" / "drive_c"


def _to_drive_c_rel(path_str: str) -> str:
    """Strip Windows drive letter and convert backslashes to forward slashes."""
    m = re.match(r'^[A-Za-z]:[/\\](.*)', path_str)
    rel = m.group(1) if m else path_str
    return rel.replace('\\', '/')


_VAR_TO_PROTON = {
    "<winAppData>":      "users/steamuser/AppData/Roaming",
    "<winLocalAppData>": "users/steamuser/AppData/Local",
    "<winDocuments>":    "users/steamuser/Documents",
    "<winDesktop>":      "users/steamuser/Desktop",
    "<winProgramData>":  "ProgramData",
}


def _normalize_proton_save_path(path_str: str) -> str:
    """Normalize a save path for use inside a Proton prefix.
    Handles variable form (<winDocuments>/...) and Windows absolute paths."""
    if "<" in path_str:
        for var, rel in _VAR_TO_PROTON.items():
            if path_str.startswith(var + "/"):
                return rel + "/" + path_str[len(var) + 1:]
            if path_str == var:
                return rel
    rel = _to_drive_c_rel(path_str)
    return re.sub(r'^[Uu]sers/[^/]+/', 'users/steamuser/', rel)


def resolve_save_path(steam_app_id: str, relative_save_path: str) -> Path:
    """Resolve a save path inside a Proton drive_c, handling Windows absolute paths."""
    return proton_drive_c(steam_app_id) / _normalize_proton_save_path(relative_save_path)


def resolve_exe_path(steam_app_id: str, relative_exe_path: str) -> Path:
    """Resolve an exe path inside a Proton drive_c, handling Windows absolute paths."""
    return proton_drive_c(steam_app_id) / _to_drive_c_rel(relative_exe_path)


def make_save_symlink(game_id: str, steam_app_id: str,
                      relative_save_path: str) -> tuple[bool, str]:
    """Create symlink: SYNC_ROOT/saves/<game_id>/ -> real save dir in Proton prefix."""
    real_path = resolve_save_path(steam_app_id, relative_save_path)
    link      = saves_link_path(game_id)

    real_path.mkdir(parents=True, exist_ok=True)

    link.parent.mkdir(parents=True, exist_ok=True)

    if link.exists() and not link.is_symlink():
        backup = BACKUP_ROOT / game_id / datetime.now().strftime("%Y%m%d_%H%M%S")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(link), str(backup))
        shutil.rmtree(str(link))
        log(f"Backed up existing data at {link} to {backup}")

    if link.is_symlink():
        if link.resolve() == real_path.resolve():
            return True, "symlink already correct"
        link.unlink()

    link.symlink_to(real_path)
    log(f"Symlink: {link} -> {real_path}")
    return True, f"linked to {real_path}"


# ── Steam shortcuts.vdf ───────────────────────────────────────────────────────

def find_shortcuts_vdf() -> Path | None:
    """Find Steam's shortcuts.vdf, returning the largest one if multiple user dirs exist."""
    steam_root = find_steam_root_windows() if sys.platform == "win32" else find_steam_root_linux()
    if not steam_root:
        return None
    userdata = steam_root / "userdata"
    if not userdata.exists():
        return None
    candidates = [
        user_dir / "config" / "shortcuts.vdf"
        for user_dir in userdata.iterdir()
        if (user_dir / "config" / "shortcuts.vdf").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def read_shortcuts() -> tuple[dict, Path | None]:
    """Parse shortcuts.vdf. Returns ({}, None) if not found or parse fails."""
    try:
        import vdf as vdflib
    except ImportError:
        log_err("Python 'vdf' package not installed")
        return {}, None

    vdf_path = find_shortcuts_vdf()
    if not vdf_path:
        log_err("shortcuts.vdf not found")
        return {}, None

    try:
        with open(vdf_path, "rb") as f:
            data = vdflib.binary_load(f)
        return data, vdf_path
    except Exception as e:
        log_err(f"Failed to parse shortcuts.vdf: {e}")
        return {}, None


def write_shortcuts(data: dict, vdf_path: Path) -> tuple[bool, str]:
    """Write shortcuts dict back to shortcuts.vdf (binary format), with backup."""
    try:
        import vdf as vdflib
    except ImportError:
        return False, "Python 'vdf' package not installed"

    backup = vdf_path.with_suffix(f".vdf.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(vdf_path, backup)

    try:
        with open(vdf_path, "wb") as f:
            vdflib.binary_dump(data, f)
        log(f"Wrote shortcuts.vdf (backup: {backup})")
        return True, str(backup)
    except Exception as e:
        shutil.copy2(backup, vdf_path)
        return False, f"Write failed: {e}"


def get_non_steam_games(shortcuts_data: dict) -> list[dict]:
    """Extract non-Steam games from parsed shortcuts.vdf data."""
    games = []
    try:
        shortcuts = shortcuts_data.get("shortcuts", shortcuts_data.get("Shortcuts", {}))
        for idx, entry in shortcuts.items():
            def get(key):
                for k in [key, key.lower(), key.capitalize(), key.upper()]:
                    if k in entry:
                        return entry[k]
                return ""

            games.append({
                "index":          str(idx),
                "appid":          get("appid") or get("AppId") or "",
                "name":           get("AppName") or get("appname") or f"Game {idx}",
                "exe":            get("Exe") or get("exe") or "",
                "start_dir":      get("StartDir") or get("startdir") or "",
                "launch_options": get("LaunchOptions") or get("launchoptions") or "",
                "icon":           get("icon") or "",
            })
    except Exception as e:
        log_err(f"Error parsing shortcuts: {e}")
    return games


# ── Launch wrappers ───────────────────────────────────────────────────────────

_WIN_WRAPPER_PRELAUNCHER_TEMPLATE = '''\
import subprocess, sys, os, threading, time

SYNC_SCRIPT  = {sync_script!r}
GAME_ID      = {game_id!r}
GAME_NAME    = {game_name!r}
PRELAUNCHER  = {prelauncher_exe!r}
DISC_IMAGE   = {disc_image!r}
CNW          = 0x08000000

_tmp = os.environ.get('TEMP') or os.environ.get('TMP') or os.path.join(
    os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'Temp')
STATUS_FILE     = os.path.join(_tmp, 'egs_status.txt')
CHOICE_FILE     = os.path.join(_tmp, 'egs_choice.txt')
READY_FILE      = os.path.join(_tmp, 'egs_ready.txt')
CANCEL_FILE     = os.path.join(_tmp, 'egs_cancelled.txt')
PUSH_START_FILE = os.path.join(_tmp, 'egs_push_start.txt')
PUSH_DONE_FILE  = os.path.join(_tmp, 'egs_push_done.txt')

def _rm(p):
    try: os.remove(p)
    except FileNotFoundError: pass

def _touch(p):
    with open(p, 'w') as f: f.write('ok\\n')

for _f in [STATUS_FILE, CHOICE_FILE, READY_FILE, CANCEL_FILE, PUSH_START_FILE, PUSH_DONE_FILE]:
    _rm(_f)

if DISC_IMAGE:
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Mount-DiskImage -ImagePath "' + DISC_IMAGE + '" -ErrorAction SilentlyContinue'],
        creationflags=CNW, check=False)

_game_exe = sys.argv[1] if len(sys.argv) > 1 else ''

def _is_bigpicture():
    if os.environ.get('STEAM_GAMEPADUI','0') == '1': return True
    if os.environ.get('SteamGamepadUI','0') == '1': return True
    try:
        import ctypes, ctypes.wintypes as _wt
        _u = ctypes.windll.user32
        _found = [False]
        @ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)
        def _cb(hwnd, _):
            if not _u.IsWindowVisible(hwnd): return True
            _cls = ctypes.create_string_buffer(64)
            _u.GetClassNameA(hwnd, _cls, 64)
            if _cls.value not in (b'SDL_app', b'CUIEngineWin32'): return True
            _wr = _wt.RECT(); _u.GetWindowRect(hwnd, ctypes.byref(_wr))
            _hm = _u.MonitorFromWindow(hwnd, 2)
            class _MI(ctypes.Structure):
                _fields_ = [('cb',_wt.DWORD),('m',_wt.RECT),('w',_wt.RECT),('f',_wt.DWORD)]
            _mi = _MI(); _mi.cb = ctypes.sizeof(_MI)
            if _u.GetMonitorInfoA(_hm, ctypes.byref(_mi)):
                _m = _mi.m
                if _wr.left<=_m.left and _wr.top<=_m.top and _wr.right>=_m.right and _wr.bottom>=_m.bottom:
                    _found[0] = True; return False
            return True
        _u.EnumWindows(_cb, 0)
        return _found[0]
    except Exception:
        return False

with open(STATUS_FILE, 'w') as _sf:
    _sf.write('STATUS=syncing\\nGAME=' + GAME_NAME + '\\nGAME_EXE=' + _game_exe
              + '\\nFULLSCREEN=' + ('1' if _is_bigpicture() else '0') + '\\n')

def _sync_handler():
    rc = subprocess.run(
        [sys.executable, SYNC_SCRIPT, 'pre-launch', GAME_ID, STATUS_FILE, _game_exe],
        creationflags=CNW).returncode
    if rc == 1:
        _touch(READY_FILE)
        for _ in range(600):
            if os.path.exists(CHOICE_FILE) or os.path.exists(CANCEL_FILE):
                break
            time.sleep(0.5)
        if os.path.exists(CANCEL_FILE):
            return
        if os.path.exists(CHOICE_FILE):
            try:
                with open(CHOICE_FILE) as _cf: _choice = _cf.read().strip()
                _rm(CHOICE_FILE)
            except OSError:
                _choice = ''
            if _choice in ('remote', 'local'):
                subprocess.run(
                    [sys.executable, SYNC_SCRIPT, 'pull-force', GAME_ID, '--keep=' + _choice],
                    creationflags=CNW)
    _touch(READY_FILE)

def _push_handler():
    for _ in range(172800):
        if os.path.exists(PUSH_START_FILE) or os.path.exists(CANCEL_FILE):
            break
        time.sleep(0.5)
    if os.path.exists(CANCEL_FILE):
        return
    if not os.path.exists(PUSH_START_FILE):
        return
    _rm(PUSH_START_FILE)
    subprocess.run([sys.executable, SYNC_SCRIPT, 'push', GAME_ID], creationflags=CNW)
    _touch(PUSH_DONE_FILE)

_t_sync = threading.Thread(target=_sync_handler, daemon=True)
_t_push = threading.Thread(target=_push_handler, daemon=True)
_t_sync.start()
_t_push.start()

_proc = subprocess.Popen([PRELAUNCHER])
_exit_code = _proc.wait()

if not os.path.exists(CANCEL_FILE):
    try: _touch(PUSH_START_FILE)
    except OSError: pass

_t_sync.join(timeout=30)
_t_push.join(timeout=300)

if DISC_IMAGE:
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         'Dismount-DiskImage -ImagePath "' + DISC_IMAGE + '" -ErrorAction SilentlyContinue'],
        creationflags=CNW, check=False)

sys.exit(_exit_code)
'''


_WIN_WRAPPER_TEMPLATE = '''\
import subprocess, sys, time, ctypes, ctypes.wintypes as wt, threading

SYNC_SCRIPT = {sync_script!r}
GAME_NAME   = {game_name!r}
DISC_IMAGE  = {disc_image!r}
game_exe    = sys.argv[1] if len(sys.argv) > 1 else ''
CNW         = 0x08000000  # CREATE_NO_WINDOW

def run_sync(action):
    import tkinter as tk
    done = threading.Event()
    result = [0]
    def worker():
        r = subprocess.run([sys.executable, SYNC_SCRIPT, action, GAME_NAME], creationflags=CNW)
        result[0] = r.returncode
        done.set()
    threading.Thread(target=worker, daemon=True).start()
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', 0.92)
    root.configure(bg='#23272e')
    word = 'Pulling' if action == 'pull' else 'Pushing'
    tk.Label(root,
             text='  ExternalGameSync\\n  ' + word + ' saves for ' + GAME_NAME + '...',
             fg='#e0e0e0', bg='#23272e', font=('Segoe UI', 9),
             justify='left', padx=14, pady=10).pack()
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    w  = root.winfo_reqwidth()
    h  = root.winfo_reqheight()
    root.geometry('+' + str(sw - w - 20) + '+' + str(sh - h - 60))
    def check():
        if done.is_set():
            root.destroy()
        else:
            root.after(100, check)
    root.after(100, check)
    root.mainloop()
    return result[0]

if DISC_IMAGE:
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f'Mount-DiskImage -ImagePath "{{DISC_IMAGE}}" -ErrorAction SilentlyContinue'],
        creationflags=CNW, check=False,
    )

if run_sync('pull') != 0:
    if DISC_IMAGE:
        subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             f'Dismount-DiskImage -ImagePath "{{DISC_IMAGE}}" -ErrorAction SilentlyContinue'],
            creationflags=CNW, check=False,
        )
    sys.exit(1)

exit_code = 0
if game_exe:
    k32  = ctypes.windll.kernel32
    iocp = k32.CreateIoCompletionPort(wt.HANDLE(-1), None, 0, 1)
    job  = k32.CreateJobObjectW(None, None)
    class _P(ctypes.Structure):
        _fields_ = [('Key', ctypes.c_size_t), ('Port', wt.HANDLE)]
    k32.SetInformationJobObject(job, 7, ctypes.byref(_P(1, iocp)), ctypes.sizeof(_P))
    proc = subprocess.Popen([game_exe])
    if k32.AssignProcessToJobObject(job, int(proc._handle)):
        code, key, ov = wt.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        while True:
            if k32.GetQueuedCompletionStatus(iocp, ctypes.byref(code), ctypes.byref(key), ctypes.byref(ov), 0xFFFFFFFF):
                if code.value == 4:  # JOB_OBJECT_MSG_ACTIVE_PROCESS_ZERO
                    break
    else:
        try:
            import psutil
            seen = set()
            try:
                ps = psutil.Process(proc.pid)
                while ps.is_running() and ps.status() != psutil.STATUS_ZOMBIE:
                    for c in ps.children(recursive=True):
                        seen.add(c.pid)
                    time.sleep(0.1)
            except psutil.NoSuchProcess:
                pass
            time.sleep(2)
            for pid in seen:
                try:
                    psutil.Process(pid).wait(timeout=7200)
                except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                    pass
        except ImportError:
            proc.wait()
    k32.CloseHandle(iocp)
    k32.CloseHandle(job)
    exit_code = proc.poll() or 0

run_sync('push')
if DISC_IMAGE:
    subprocess.run(
        ['powershell', '-NoProfile', '-Command',
         f'Dismount-DiskImage -ImagePath "{{DISC_IMAGE}}" -ErrorAction SilentlyContinue'],
        creationflags=CNW, check=False,
    )
sys.exit(exit_code)
'''


def _write_launch_wrapper_win(game_name: str, savesync_bat: str,
                              disc_image: str = "",
                              game_id: str = "",
                              prelauncher_exe: str = "") -> str:
    wrapper_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExternalGameSync" / "wrappers"
    wrapper_dir.mkdir(parents=True, exist_ok=True)

    safe_name    = game_id_from_name(game_name)
    wrapper_path = wrapper_dir / f"{safe_name}.py"
    sync_script  = str(Path(savesync_bat).parent / "externalgamesync.py")
    if game_id and prelauncher_exe:
        script = _WIN_WRAPPER_PRELAUNCHER_TEMPLATE.format(
            sync_script=sync_script, game_id=game_id, game_name=game_name,
            prelauncher_exe=prelauncher_exe, disc_image=disc_image)
    else:
        script = _WIN_WRAPPER_TEMPLATE.format(
            sync_script=sync_script, game_name=game_name, disc_image=disc_image)
    wrapper_path.write_text(script)
    log(f"Wrote launch wrapper: {wrapper_path}")
    return str(wrapper_path)


def _write_launch_wrapper(game_name: str, savesync_bin: str,
                          game_id: str = "", game_exe_win: str = "",
                          disc_image: str = "",
                          prelauncher_linux: str = "") -> str:
    """Write a per-game shell wrapper for the pre-launcher.exe flow."""
    wrapper_dir = Path.home() / ".local" / "share" / "externalgamesync" / "wrappers"
    wrapper_dir.mkdir(parents=True, exist_ok=True)

    safe_name    = game_id_from_name(game_name)
    wrapper_path = wrapper_dir / f"{safe_name}.sh"

    script = f"""#!/bin/bash
# ExternalGameSync wrapper for: {game_name}
# Auto-generated — do not edit manually

SAVESYNC="{savesync_bin}"
GAME_ID="{game_id}"
GAME_EXE_WIN="{game_exe_win}"
DISC_IMAGE="{disc_image}"
PRELAUNCHER="{prelauncher_linux}"

# IPC directory inside the Proton prefix (accessible from both Linux and Wine)
_ipc_dir() {{
    if [ -n "$STEAM_COMPAT_DATA_PATH" ]; then
        printf '%s/pfx/drive_c/users/steamuser/AppData/Local/Temp' "$STEAM_COMPAT_DATA_PATH"
    else
        printf '/tmp/externalgamesync'
    fi
}}

IPC_DIR=$(_ipc_dir)
mkdir -p "$IPC_DIR"

STATUS_FILE="$IPC_DIR/egs_status.txt"
CHOICE_FILE="$IPC_DIR/egs_choice.txt"
READY_FILE="$IPC_DIR/egs_ready.txt"
CANCEL_FILE="$IPC_DIR/egs_cancelled.txt"
PUSH_START_FILE="$IPC_DIR/egs_push_start.txt"
PUSH_DONE_FILE="$IPC_DIR/egs_push_done.txt"

# Clean up stale IPC files from a previous run
rm -f "$STATUS_FILE" "$CHOICE_FILE" "$READY_FILE" "$CANCEL_FILE" "$PUSH_START_FILE" "$PUSH_DONE_FILE"

# Background sync handler: runs pre-launch, signals pre-launcher.exe via READY_FILE.
# On conflict: waits for the user's choice from pre-launcher.exe, runs resolution,
# then signals again so pre-launcher.exe knows it can proceed to launch the game.
_sync_handler() {{
    "$SAVESYNC" pre-launch "$GAME_ID" "$STATUS_FILE" "${{_RUNTIME_GAME_EXE:-$GAME_EXE_WIN}}"
    PRE_RC=$?

    if [ $PRE_RC -eq 1 ]; then
        # Conflict: signal pre-launcher.exe to show the conflict dialog
        echo ok > "$READY_FILE"
        # Wait for user's choice (written by pre-launcher.exe) or cancellation
        local i=0
        while [ $i -lt 600 ] && [ ! -f "$CHOICE_FILE" ] && [ ! -f "$CANCEL_FILE" ]; do
            sleep 0.5
            i=$((i+1))
        done
        [ -f "$CANCEL_FILE" ] && return
        if [ -f "$CHOICE_FILE" ]; then
            choice=$(tr -d '\\r\\n ' < "$CHOICE_FILE")
            rm -f "$CHOICE_FILE"
            case "$choice" in
                remote) "$SAVESYNC" pull-force "$GAME_ID" --keep=remote ;;
                local)  "$SAVESYNC" pull-force "$GAME_ID" --keep=local  ;;
            esac
        fi
        # Signal conflict resolution complete so pre-launcher.exe can launch the game
        echo ok > "$READY_FILE"
    else
        # Clean pull, no connection, or error: signal pre-launcher.exe to proceed
        echo ok > "$READY_FILE"
    fi
}}

# Background push handler: waits for pre-launcher.exe to signal that the game has
# exited, then pushes saves and signals completion so the push screen can close.
_push_handler() {{
    local i=0
    while [ $i -lt 172800 ] && [ ! -f "$PUSH_START_FILE" ] && [ ! -f "$CANCEL_FILE" ]; do
        sleep 0.5
        i=$((i+1))
    done
    [ -f "$CANCEL_FILE" ] && return
    [ ! -f "$PUSH_START_FILE" ] && return
    rm -f "$PUSH_START_FILE"
    "$SAVESYNC" log "[wrapper] push STARTING"
    "$SAVESYNC" push "$GAME_ID"
    echo ok > "$PUSH_DONE_FILE"
}}

# For native Steam (PRELAUNCHER set): find the Proton verb in %command%, capture
# the game exe (converting to a Wine Z: path so pre-launcher can launch it), and
# splice in pre-launcher.exe.  Pre-launcher handles the full flow: sync dialogs,
# game launch via run_wait, push signalling — identical to non-Steam shortcuts.
_ORIG_CMD=("$@")
_LAUNCH_CMD=("$@")
_NATIVE_PRELAUNCHER=0
_RUNTIME_GAME_EXE=""
if [ -n "$PRELAUNCHER" ]; then
    _new_cmd=()
    _found_verb=0
    for _arg in "$@"; do
        case "$_found_verb" in
            0)
                _new_cmd+=("$_arg")
                case "$_arg" in
                    waitforexitandrun|run|runinprefix) _found_verb=1 ;;
                esac
                ;;
            1)
                # Convert the Linux game exe to a Wine Z: path and capture it,
                # then inject pre-launcher in its place.
                _RUNTIME_GAME_EXE="Z:$(printf '%s' "$_arg" | sed 's|/|\\\\|g')"
                _new_cmd+=("$PRELAUNCHER")
                _found_verb=2
                ;;
        esac
    done
    if [ "$_found_verb" -ge 2 ]; then
        _LAUNCH_CMD=("${{_new_cmd[@]}}")
        _NATIVE_PRELAUNCHER=1
        "$SAVESYNC" log "[wrapper] native Steam: injecting pre-launcher, game exe: $_RUNTIME_GAME_EXE"
        "$SAVESYNC" log "[wrapper] launch cmd: ${{_LAUNCH_CMD[*]}}"
    else
        "$SAVESYNC" log "[wrapper] native Steam: Proton verb not found, running without pre-launcher"
        "$SAVESYNC" log "[wrapper] raw args: $*"
    fi
fi

# Detect gaming/Big Picture mode on the Linux side where env vars are reliable.
# GAMESCOPE_WAYLAND_DISPLAY is set by gamescope (SteamOS/Bazzite gaming mode).
# STEAM_GAMEPADUI is set by Steam when it propagates the flag to child processes.
# On desktop Linux (e.g. Mint), Steam may not propagate these vars, so fall back
# to reading Steam's own process environment and cmdline via /proc.
_FULLSCREEN=0
[ -n "$GAMESCOPE_WAYLAND_DISPLAY" ] && _FULLSCREEN=1
[ "${{STEAM_GAMEPADUI:-0}}" = "1" ] && _FULLSCREEN=1
[ "${{SteamGamepadUI:-0}}" = "1" ] && _FULLSCREEN=1
if [ "$_FULLSCREEN" = "0" ]; then
    for _spid in $(pgrep -x steam 2>/dev/null); do
        if tr '\\0' '\\n' < "/proc/$_spid/environ" 2>/dev/null \
               | grep -qxE 'STEAM_GAMEPADUI=1|SteamGamepadUI=1'; then
            _FULLSCREEN=1; break
        fi
        if tr '\\0' '\\n' < "/proc/$_spid/cmdline" 2>/dev/null \
               | grep -qxE -- '-gamepadui|-bigpicture'; then
            _FULLSCREEN=1; break
        fi
    done
fi

# Status file: for native Steam, pass the runtime-extracted game exe so
# pre-launcher can launch it directly (same as non-Steam shortcut flow).
# For non-Steam shortcuts GAME_EXE_WIN is set at generation time; _RUNTIME_GAME_EXE is empty.
printf 'STATUS=syncing\\nGAME={game_name}\\nGAME_EXE=%s\\nFULLSCREEN=%s\\n' "${{_RUNTIME_GAME_EXE:-$GAME_EXE_WIN}}" "$_FULLSCREEN" > "$STATUS_FILE"

# Start sync handler in background before anything else
_sync_handler &

# Mount disc image if configured (uses udisksctl, no root required)
_disc_mount=""
_disc_mountpoint=""
if [ -n "$DISC_IMAGE" ] && [ -f "$DISC_IMAGE" ]; then
    "$SAVESYNC" log "[disc] ISO set: $DISC_IMAGE"
    if command -v udisksctl >/dev/null 2>&1; then
        _loop_out=$(udisksctl loop-setup --file "$DISC_IMAGE" --no-user-interaction 2>&1)
        "$SAVESYNC" log "[disc] loop-setup: $_loop_out"
        _disc_dev=$(printf '%s' "$_loop_out" | grep -o '/dev/loop[0-9]*')
        if [ -n "$_disc_dev" ]; then
            _mount_out=$(udisksctl mount --block-device "$_disc_dev" --no-user-interaction 2>&1)
            "$SAVESYNC" log "[disc] mount: $_mount_out"
            _disc_mountpoint=$(printf '%s' "$_mount_out" | sed -n 's/.*at \\([^ .]*\\).*/\\1/p')
            _disc_mount="$_disc_dev"
            if [ -n "$_disc_mountpoint" ] && [ -n "$STEAM_COMPAT_DATA_PATH" ]; then
                ln -sfn "$_disc_mountpoint" "$STEAM_COMPAT_DATA_PATH/pfx/dosdevices/d:"
                "$SAVESYNC" log "[disc] mapped d: -> $_disc_mountpoint"
            fi
        else
            "$SAVESYNC" log "[disc] loop-setup failed or returned no device"
        fi
    else
        "$SAVESYNC" log "[disc] udisksctl not found in PATH=$PATH"
    fi
fi

# Start push handler in background — waits for PUSH_START_FILE.
_push_handler &

# Run the launch command.  For both non-Steam shortcuts and native Steam games,
# this is pre-launcher.exe via Proton.  Pre-launcher handles sync UI, launches
# the game via run_wait, and signals push when done — the wrapper just waits.
"${{_LAUNCH_CMD[@]}}"
LAUNCH_RC=$?
"$SAVESYNC" log "[wrapper] launch exited rc=$LAUNCH_RC native_prelauncher=$_NATIVE_PRELAUNCHER"

# Log pre-launcher diagnostics if available
_diag_file="$IPC_DIR/../ExternalGameSync/egs_diag.txt"
if [ -f "$_diag_file" ]; then
    "$SAVESYNC" log "[wrapper] egs_diag.txt found:"
    while IFS= read -r _line; do
        "$SAVESYNC" log "[diag] $_line"
    done < "$_diag_file"
else
    "$SAVESYNC" log "[wrapper] egs_diag.txt NOT found (pre-launcher may not have run)"
fi

# Fallback: if pre-launcher.exe exited without writing the push signal (e.g. crash),
# ensure _push_handler still runs the push.
[ ! -f "$CANCEL_FILE" ] && touch "$PUSH_START_FILE"

# Wait for both background handlers to finish
wait 2>/dev/null || true

# Unmount disc image
if [ -n "$_disc_mount" ]; then
    udisksctl unmount --block-device "$_disc_mount" --no-user-interaction 2>/dev/null || true
    udisksctl loop-delete --block-device "$_disc_mount" --no-user-interaction 2>/dev/null || true
    [ -n "$STEAM_COMPAT_DATA_PATH" ] && rm -f "$STEAM_COMPAT_DATA_PATH/pfx/dosdevices/d:"
fi

exit $LAUNCH_RC
"""

    wrapper_path.write_text(script)
    wrapper_path.chmod(0o755)
    log(f"Wrote launch wrapper: {wrapper_path}")
    return str(wrapper_path)


def _install_prelauncher(app_id: str) -> str | None:
    """Copy pre-launcher.exe from the app dir into the game's Proton prefix."""
    src = Path.home() / ".local" / "share" / "externalgamesync" / "pre-launcher.exe"
    if not src.exists():
        log_err(f"pre-launcher.exe not found at {src} — run pre-launcher/build.sh first")
        return None

    compatdata = find_compatdata(str(app_id))
    if compatdata is None:
        log_err(f"Proton prefix for app_id={app_id} not found — launch the game once first")
        return None
    prefix = compatdata / "pfx"

    dst_dir = prefix / "drive_c" / "users" / "steamuser" / "AppData" / "Local" / "ExternalGameSync"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "pre-launcher.exe")
    log(f"Installed pre-launcher.exe → {dst_dir / 'pre-launcher.exe'}")
    return r"C:\users\steamuser\AppData\Local\ExternalGameSync\pre-launcher.exe"


# ── Proton tool enumeration ───────────────────────────────────────────────────

def _proton_dir_to_internal(dir_name: str) -> str | None:
    """Map a Proton steamapps/common directory name to its CompatToolMapping internal name."""
    if dir_name in ("Proton - Experimental", "Proton Experimental"):
        return "proton_experimental"
    if dir_name in ("Proton - Hotfix", "Proton Hotfix"):
        return "proton_hotfixes"
    m = re.match(r'^Proton\s+(\d+)', dir_name)
    if m:
        return f"proton_{m.group(1)}"
    return None


def list_proton_tools() -> list[tuple[str, str]]:
    """Return (display_name, internal_name) pairs for all installed Proton versions.
    Official Proton comes first, then custom tools sorted by name."""
    from config import find_steam_root_windows
    steam_root = find_steam_root_windows() if sys.platform == "win32" else find_steam_root_linux()

    tools: list[tuple[str, str]] = []

    # Official Proton in steamapps/common/Proton*/
    if steam_root:
        common = steam_root / "steamapps" / "common"
        if common.exists():
            for d in sorted(common.iterdir()):
                if not (d.is_dir() and d.name.startswith("Proton")):
                    continue
                if not (d / "toolmanifest.vdf").exists():
                    continue
                internal = _proton_dir_to_internal(d.name)
                if internal:
                    tools.append((d.name, internal))

    # Custom tools from compatibilitytools.d (steam root + user home)
    compat_dirs: list[Path] = []
    if steam_root:
        compat_dirs.append(steam_root / "compatibilitytools.d")
    compat_dirs.append(Path.home() / ".local" / "share" / "Steam" / "compatibilitytools.d")

    seen: set[str] = set()
    for compat_dir in compat_dirs:
        if not compat_dir.exists():
            continue
        for d in sorted(compat_dir.iterdir()):
            if not d.is_dir():
                continue
            vdf_file = d / "compatibilitytool.vdf"
            if not vdf_file.exists():
                continue
            try:
                import vdf as vdflib
                with open(vdf_file, "r", encoding="utf-8", errors="replace") as f:
                    data = vdflib.load(f)
                ct = data.get("compatibilitytools", {}).get("compat_tools", {})
                for internal_name, tool_data in ct.items():
                    if internal_name in seen:
                        continue
                    seen.add(internal_name)
                    display = tool_data.get("display_name", internal_name)
                    tools.append((display, internal_name))
            except Exception:
                if d.name not in seen:
                    seen.add(d.name)
                    tools.append((d.name, d.name))

    return tools


# ── Non-Steam shortcut creation ───────────────────────────────────────────────

def add_non_steam_shortcut(exe_path: str, app_name: str) -> tuple[int | None, str | None, str]:
    """Add a new non-Steam shortcut to shortcuts.vdf.
    Returns (app_id_unsigned, shortcut_index, error_msg); error_msg is empty on success."""
    import binascii
    import struct

    crc             = binascii.crc32((exe_path + app_name).encode("utf-8")) | 0x80000000
    app_id_unsigned = crc & 0xFFFFFFFF
    app_id_signed   = struct.unpack("i", struct.pack("I", app_id_unsigned))[0]

    data, vdf_path = read_shortcuts()
    if not vdf_path:
        return None, None, "Could not find shortcuts.vdf"

    shortcuts = data.get("shortcuts", data.get("Shortcuts"))
    if shortcuts is None:
        shortcuts = {}
        data["shortcuts"] = shortcuts

    try:
        next_idx = str(max(int(k) for k in shortcuts.keys()) + 1) if shortcuts else "0"
    except (ValueError, TypeError):
        next_idx = str(len(shortcuts))

    shortcuts[next_idx] = {
        "AppName":             app_name,
        "Exe":                 f'"{exe_path}"',
        "StartDir":            f'"{str(Path(exe_path).parent)}"',
        "icon":                "",
        "ShortcutPath":        "",
        "LaunchOptions":       "",
        "IsHidden":            0,
        "AllowDesktopConfig":  1,
        "AllowOverlay":        1,
        "OpenVR":              0,
        "Devkit":              0,
        "DevkitGameID":        "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime":        0,
        "FlatpakAppID":        "",
        "appid":               app_id_signed,
        "tags":                {},
    }

    ok, msg = write_shortcuts(data, vdf_path)
    if not ok:
        return None, None, msg
    return app_id_unsigned, next_idx, ""


def set_compat_tool(app_id_unsigned: int, internal_name: str) -> tuple[bool, str]:
    """Write a CompatToolMapping entry to Steam's config.vdf for a non-Steam game."""
    from config import find_steam_root_windows
    steam_root = find_steam_root_windows() if sys.platform == "win32" else find_steam_root_linux()
    if not steam_root:
        return False, "Could not locate Steam root"

    config_path = steam_root / "config" / "config.vdf"
    if not config_path.exists():
        return False, f"config.vdf not found at {config_path}"

    try:
        import vdf as vdflib
    except ImportError:
        return False, "Python 'vdf' package not installed"

    backup = config_path.with_suffix(f".vdf.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(config_path, backup)

    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as f:
            data = vdflib.load(f)

        compat_map = (
            data
            .setdefault("InstallConfigStore", {})
            .setdefault("Software", {})
            .setdefault("Valve", {})
            .setdefault("Steam", {})
            .setdefault("CompatToolMapping", {})
        )
        compat_map[str(app_id_unsigned)] = {
            "name":     internal_name,
            "config":   "",
            "priority": "250",
        }

        with open(config_path, "w", encoding="utf-8") as f:
            vdflib.dump(data, f, pretty=True)

        log(f"Set CompatToolMapping {app_id_unsigned} → {internal_name} (backup: {backup})")
        return True, str(backup)
    except Exception as e:
        shutil.copy2(backup, config_path)
        return False, f"Write failed: {e}"


def update_shortcut_launch(
    shortcuts_data: dict,
    vdf_path: Path,
    shortcut_index: str,
    game_name: str,
    savesync_bin: str,
    real_exe: str,
    start_dir: str,
    game_cfg: dict = None,
    disc_image_override: str | None = None,
) -> tuple[bool, str]:
    """Rewrite a non-Steam shortcut to wrap launch with save syncing."""
    try:
        shortcuts = shortcuts_data.get("shortcuts", shortcuts_data.get("Shortcuts", {}))
        entry     = shortcuts[shortcut_index]

        def set_key(preferred, value):
            for k in [preferred, preferred.lower(), preferred.capitalize()]:
                if k in entry:
                    entry[k] = value
                    return
            entry[preferred] = value

        mc = get_local_config(game_cfg["id"]) if game_cfg else None
        disc_image = disc_image_override if disc_image_override is not None else (mc or {}).get("disc_image", "")

        if sys.platform == "win32":
            _gid        = (game_cfg or {}).get("id", game_id_from_name(game_name))
            _pl_path    = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExternalGameSync" / "pre-launcher.exe"
            _pl_exe     = str(_pl_path) if _pl_path.exists() else ""
            wrapper_path = _write_launch_wrapper_win(game_name, savesync_bin,
                                                     disc_image=disc_image,
                                                     game_id=_gid,
                                                     prelauncher_exe=_pl_exe)
            pythonw = str(Path(sys.executable).parent / "pythonw.exe")
            set_key("Exe", f'"{pythonw}"')
            if real_exe:
                set_key("StartDir", f'"{str(Path(real_exe).parent)}"')
            set_key("LaunchOptions", f'"{wrapper_path}" "{real_exe}"')
        else:
            game_id      = (game_cfg or {}).get("id", game_id_from_name(game_name))
            exe_path     = (game_cfg or {}).get("exe_path", "")
            game_exe_win = "C:\\" + _to_drive_c_rel(exe_path).replace("/", "\\") if exe_path else ""

            app_id        = mc.get("app_id") if mc else None
            prelauncher_win = None
            if app_id:
                prelauncher_win = _install_prelauncher(str(app_id))

            wrapper_path = _write_launch_wrapper(
                game_name, savesync_bin,
                game_id=game_id,
                game_exe_win=game_exe_win,
                disc_image=disc_image,
            )

            env_vars    = (game_cfg or {}).get("env_vars", "").strip()
            launch_opts = f'"{wrapper_path}" %command%'
            if env_vars:
                launch_opts = f'{env_vars} {launch_opts}'

            if prelauncher_win:
                set_key("Exe", f'"{prelauncher_win}"')
                set_key("StartDir", f'"{str(Path(prelauncher_win).parent)}"')
            elif real_exe:
                log("pre-launcher.exe not installed — shortcut Exe left as real game exe")
                set_key("Exe", f'"{real_exe}"')
                set_key("StartDir", f'"{str(Path(real_exe).parent)}"')
            set_key("LaunchOptions", launch_opts)

        return write_shortcuts(shortcuts_data, vdf_path)
    except Exception as e:
        return False, f"Failed to update shortcut: {e}"


# ── Native Steam game support ─────────────────────────────────────────────────

def _find_all_steamapps_dirs() -> list[Path]:
    """Return all steamapps directories across all Steam library folders."""
    steam_root = find_steam_root_windows() if sys.platform == "win32" else find_steam_root_linux()
    if not steam_root:
        return []
    main = steam_root / "steamapps"
    dirs = [main] if main.exists() else []
    lf_path = main / "libraryfolders.vdf"
    if lf_path.exists():
        try:
            import vdf as vdflib
            with open(lf_path, encoding="utf-8", errors="replace") as f:
                data = vdflib.load(f)
            lf = data.get("libraryfolders", data.get("LibraryFolders", {}))
            for key, val in lf.items():
                if not key.isdigit():
                    continue
                path_str = val.get("path") if isinstance(val, dict) else val
                if path_str:
                    extra = Path(path_str) / "steamapps"
                    if extra.exists() and extra not in dirs:
                        dirs.append(extra)
        except Exception:
            pass
    return dirs


def find_steam_game_dir(app_id: str) -> Path | None:
    """Return the steamapps/common/<installdir> path for the given app_id, or None."""
    for steamapps in _find_all_steamapps_dirs():
        acf = steamapps / f"appmanifest_{app_id}.acf"
        if not acf.exists():
            continue
        try:
            import vdf as vdflib
            with open(acf, encoding="utf-8", errors="replace") as f:
                data = vdflib.load(f)
            state    = data.get("AppState", data.get("appstate", {}))
            inst_dir = state.get("installdir", state.get("InstallDir", ""))
            if inst_dir:
                p = steamapps / "common" / inst_dir
                return p if p.exists() else None
        except Exception:
            continue
    return None


def list_steam_games() -> list[dict]:
    """Scan appmanifest_*.acf files and return installed Steam games.
    Each entry: {app_id, name, install_dir}."""
    try:
        import vdf as vdflib
    except ImportError:
        return []
    games = []
    for steamapps in _find_all_steamapps_dirs():
        for acf in steamapps.glob("appmanifest_*.acf"):
            try:
                with open(acf, encoding="utf-8", errors="replace") as f:
                    data = vdflib.load(f)
                state    = data.get("AppState", data.get("appstate", {}))
                app_id   = str(state.get("appid",      state.get("AppID",      "")))
                name     = state.get("name",        state.get("Name",        ""))
                inst_dir = state.get("installdir",  state.get("InstallDir",  ""))
                if app_id and name:
                    games.append({"app_id": app_id, "name": name, "install_dir": inst_dir})
            except Exception:
                continue
    games.sort(key=lambda g: g["name"].lower())
    return games


def find_localconfig_vdf() -> Path | None:
    """Find localconfig.vdf for the primary Steam user (largest file wins)."""
    steam_root = find_steam_root_windows() if sys.platform == "win32" else find_steam_root_linux()
    if not steam_root:
        return None
    userdata = steam_root / "userdata"
    if not userdata.exists():
        return None
    candidates = [
        user_dir / "config" / "localconfig.vdf"
        for user_dir in userdata.iterdir()
        if (user_dir / "config" / "localconfig.vdf").exists()
    ]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def _vdf_ci(d: dict, key: str):
    """Case-insensitive dict lookup for VDF data."""
    if key in d:
        return d[key]
    key_l = key.lower()
    for k, v in d.items():
        if k.lower() == key_l:
            return v
    return None


def _vdf_ci_setdefault(d: dict, key: str) -> dict:
    """Case-insensitive setdefault: return existing value or create {} under key."""
    existing = _vdf_ci(d, key)
    if existing is None:
        d[key] = {}
        return d[key]
    return existing


def get_native_launch_options(app_id: str) -> str:
    """Return current LaunchOptions for a native Steam app from localconfig.vdf."""
    lc = find_localconfig_vdf()
    if not lc:
        return ""
    try:
        import vdf as vdflib
        with open(lc, encoding="latin-1") as f:
            data = vdflib.load(f)
        root  = _vdf_ci(data,  "UserLocalConfigStore") or {}
        sw    = _vdf_ci(root,  "Software") or {}
        valve = _vdf_ci(sw,    "Valve") or {}
        steam = _vdf_ci(valve, "Steam") or {}
        apps  = _vdf_ci(steam, "Apps") or {}
        app   = _vdf_ci(apps,  app_id) or {}
        return _vdf_ci(app, "LaunchOptions") or ""
    except Exception:
        return ""


def set_native_launch_options(app_id: str, launch_opts: str) -> tuple[bool, str]:
    """Write LaunchOptions for a native Steam app to localconfig.vdf."""
    lc = find_localconfig_vdf()
    if not lc:
        return False, "localconfig.vdf not found — launch Steam once first"
    try:
        import vdf as vdflib
    except ImportError:
        return False, "Python 'vdf' package not installed"

    backup = lc.with_suffix(f".vdf.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(lc, backup)
    try:
        with open(lc, encoding="latin-1") as f:
            data = vdflib.load(f)

        root  = _vdf_ci_setdefault(data,  "UserLocalConfigStore")
        sw    = _vdf_ci_setdefault(root,  "Software")
        valve = _vdf_ci_setdefault(sw,    "Valve")
        steam = _vdf_ci_setdefault(valve, "Steam")
        apps  = _vdf_ci_setdefault(steam, "Apps")

        app_entry = _vdf_ci(apps, app_id)
        if app_entry is None:
            apps[app_id] = {}
            app_entry = apps[app_id]

        lo_key = next((k for k in app_entry if k.lower() == "launchoptions"), "LaunchOptions")
        app_entry[lo_key] = launch_opts

        with open(lc, "w", encoding="latin-1") as f:
            vdflib.dump(data, f, pretty=True)

        log(f"Set native launch options app_id={app_id}: {launch_opts}")
        return True, str(backup)
    except Exception as e:
        shutil.copy2(backup, lc)
        return False, f"Write failed: {e}"


def update_native_game_launch(
    app_id: str,
    game_name: str,
    savesync_bin: str,
    game_cfg: dict = None,
    disc_image_override: str | None = None,
) -> tuple[bool, str]:
    """Write launch wrapper and set localconfig.vdf LaunchOptions for a native Steam game.
    Unlike update_shortcut_launch, this never touches shortcuts.vdf and skips pre-launcher."""
    mc         = get_local_config(game_cfg["id"]) if game_cfg else None
    disc_image = disc_image_override if disc_image_override is not None else (mc or {}).get("disc_image", "")

    if sys.platform == "win32":
        _gid     = (game_cfg or {}).get("id", game_id_from_name(game_name))
        _pl_path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExternalGameSync" / "pre-launcher.exe"
        _pl_exe  = str(_pl_path) if _pl_path.exists() else ""
        wrapper_path = _write_launch_wrapper_win(game_name, savesync_bin,
                                                  disc_image=disc_image,
                                                  game_id=_gid,
                                                  prelauncher_exe=_pl_exe)
        pythonw     = str(Path(sys.executable).parent / "pythonw.exe")
        launch_opts = f'"{pythonw}" "{wrapper_path}" %command%'
    else:
        game_id      = (game_cfg or {}).get("id", game_id_from_name(game_name))
        exe_path     = (game_cfg or {}).get("exe_path", "")
        game_exe_win = "C:\\" + _to_drive_c_rel(exe_path).replace("/", "\\") if exe_path else ""
        _install_prelauncher(str(app_id))  # copies binary into prefix
        compatdata = find_compatdata(str(app_id))
        prelauncher_linux = str(
            compatdata / "pfx" / "drive_c" / "users" / "steamuser"
            / "AppData" / "Local" / "ExternalGameSync" / "pre-launcher.exe"
        ) if compatdata else ""
        wrapper_path = _write_launch_wrapper(
            game_name, savesync_bin,
            game_id=game_id, game_exe_win=game_exe_win, disc_image=disc_image,
            prelauncher_linux=prelauncher_linux,
        )
        env_vars    = (game_cfg or {}).get("env_vars", "").strip()
        launch_opts = f'"{wrapper_path}" %command%'
        if env_vars:
            launch_opts = f'{env_vars} {launch_opts}'

    return set_native_launch_options(app_id, launch_opts)
