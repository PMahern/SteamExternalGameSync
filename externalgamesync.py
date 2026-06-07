#!/usr/bin/env python3
"""
ExternalGameSync - CLI entry point
Handles game launch wrapping and manual sync commands.
"""
from __future__ import annotations

import glob
import os
import select
import struct
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import log, log_err, log_ok, LOG_FILE, SYNC_ROOT, hostname
from games import find_game, load_games, save_games, get_local_save_path, hash_file, add_game_hash
from machine_config import get_local_config, migrate_from_games_json
from rclone import rclone_push_games_json
from sync import (
    rclone_sync_pull, rclone_sync_pull_force, rclone_sync_push, rclone_bisync,
    PULL_OK, PULL_NO_REMOTE, PULL_CONFLICT, PULL_FAILED, PULL_NO_CONNECTION,
    GameLock, load_sync_state,
)
from steam import resolve_exe_path


def _pygame_dialog(title: str, body: str, label_a: str, label_b: str, label_c: str | None = None, app_id: str | None = None) -> int | None:
    """
    Show a gamepad-friendly dialog in-process (not a subprocess).
    Running in the same process as the Steam-launched wrapper means gamescope
    treats the window as the game window and surfaces it correctly.
    Returns 0 (A/Enter), 2 (B/Escape), 3 (window closed), or None on error.
    """
    log(f"_pygame_dialog: start  DISPLAY={os.environ.get('DISPLAY')}  WAYLAND={os.environ.get('WAYLAND_DISPLAY')}  GAMESCOPE_WL={os.environ.get('GAMESCOPE_WAYLAND_DISPLAY')}  SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER')}")
    try:
        import pygame
    except ImportError:
        log("_pygame_dialog: pygame not importable")
        return None

    # In gaming mode, force SDL onto gamescope's XWayland display (X11 driver)
    # rather than the Wayland socket — more reliably surfaces the window.
    # Save and restore env so the game process launched afterwards is unaffected.
    _saved_env: dict[str, str | None] = {}
    if os.environ.get("GAMESCOPE_WAYLAND_DISPLAY"):
        for k in ("WAYLAND_DISPLAY", "SDL_VIDEODRIVER", "SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR"):
            _saved_env[k] = os.environ.get(k)
        os.environ["SDL_VIDEODRIVER"] = "x11"
        os.environ["SDL_VIDEO_X11_NET_WM_BYPASS_COMPOSITOR"] = "0"
        os.environ.pop("WAYLAND_DISPLAY", None)

    def _restore_env():
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    SDL_WINDOW_ALWAYS_ON_TOP = 0x00008000
    try:
        pygame.init()
        log("_pygame_dialog: pygame.init OK")
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN | SDL_WINDOW_ALWAYS_ON_TOP)
        W, H = screen.get_size()
        if W <= 0 or H <= 0:
            W, H = 1280, 800
        log(f"_pygame_dialog: display mode set OK  driver={pygame.display.get_driver()}  size={W}x{H}")
        pygame.display.set_caption("ExternalGameSync")
        # Tell gamescope to surface this window: mark it as the game window and as
        # an external overlay so it appears even before Proton starts.
        if sys.platform != "win32":
            wm_info = pygame.display.get_wm_info()
            win_id  = wm_info.get("window", 0)
            if win_id:
                _atoms = [
                    ["xprop", "-id", str(win_id), "-format", "GAMESCOPE_EXTERNAL_OVERLAY", "32c",
                     "-set", "GAMESCOPE_EXTERNAL_OVERLAY", "1"],
                    ["xprop", "-id", str(win_id), "-format", "STEAM_GAME", "32c",
                     "-set", "STEAM_GAME", str(app_id) if app_id else "769"],
                ]
                for _cmd in _atoms:
                    try:
                        subprocess.run(_cmd, capture_output=True, timeout=2)
                    except Exception:
                        pass
                log(f"_pygame_dialog: set gamescope atoms on window {win_id}")
    except Exception as e:
        log(f"_pygame_dialog: display init failed: {e}")
        _restore_env()
        try:
            pygame.quit()
        except Exception:
            pass
        return None

    BG   = (26,  27,  38)
    BLUE = (31, 111, 235)
    DARK = (48,  54,  61)
    RED  = (160,  40,  40)
    WHT  = (255, 255, 255)
    GRAY = (160, 160, 176)

    try:
        ft  = pygame.font.SysFont("Sans", 18, bold=True)
        fb  = pygame.font.SysFont("Sans", 12)
        fbt = pygame.font.SysFont("Sans", 13, bold=True)
    except Exception:
        ft  = pygame.font.Font(None, 28)
        fb  = pygame.font.Font(None, 22)
        fbt = pygame.font.Font(None, 24)

    result = [0]
    done   = threading.Event()
    CX, CY = W // 2, H // 2
    if label_c is not None:
        # Three buttons of width 172 with 16px gaps, centred on CX
        BW = 172
        RA = pygame.Rect(CX - 274, CY + 50, BW, 52)
        RB = pygame.Rect(CX -  86, CY + 50, BW, 52)
        RC = pygame.Rect(CX + 102, CY + 50, BW, 52)
    else:
        RA = pygame.Rect(CX - 280, CY + 50, 264, 52)
        RB = pygame.Rect(CX +  16, CY + 50, 264, 52)
        RC = None

    # evdev: direct controller input, works regardless of Steam Input mapping
    BTN_SOUTH, BTN_EAST, BTN_NORTH = 304, 305, 308
    _lsz   = 8 if sys.maxsize > 2**32 else 4
    EV_FMT = ("q" if _lsz == 8 else "l") * 2 + "HHi"
    EV_SZ  = struct.calcsize(EV_FMT)
    evdev_ready = threading.Event()

    def _evdev(path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            try:
                evdev_ready.wait()
                while select.select([fd], [], [], 0)[0]:
                    os.read(fd, EV_SZ)
                while not done.is_set():
                    if select.select([fd], [], [], 0.1)[0]:
                        d = os.read(fd, EV_SZ)
                        if len(d) == EV_SZ:
                            *_, t, code, val = struct.unpack(EV_FMT, d)
                            if t == 1 and val == 1 and not done.is_set():
                                if   code == BTN_SOUTH: result[0] = 0; done.set()
                                elif code == BTN_EAST:  result[0] = 2; done.set()
                                elif code == BTN_NORTH and label_c is not None: result[0] = 1; done.set()
            finally:
                os.close(fd)
        except Exception:
            pass

    if sys.platform != "win32":
        for _p in glob.glob("/dev/input/event*"):
            threading.Thread(target=_evdev, args=(_p,), daemon=True).start()

    pygame.joystick.init()
    for _i in range(pygame.joystick.get_count()):
        pygame.joystick.Joystick(_i).init()

    def _wrap(text, font, max_w):
        lines, cur = [], ""
        for word in text.split():
            test = (cur + " " + word).strip()
            if font.size(test)[0] > max_w:
                if cur: lines.append(cur)
                cur = word
            else:
                cur = test
        if cur: lines.append(cur)
        return lines

    clock       = pygame.time.Clock()
    first_frame = True
    try:
        while not done.is_set():
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    result[0] = 3; done.set()
                elif ev.type == pygame.KEYDOWN:
                    if ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        result[0] = 0; done.set()
                    elif ev.key == pygame.K_ESCAPE:
                        result[0] = 1 if label_c is not None else 2; done.set()
                elif ev.type == pygame.JOYBUTTONDOWN:
                    if ev.button == 0: result[0] = 0; done.set()
                    if ev.button == 1: result[0] = 2; done.set()
                    if ev.button == 3 and label_c is not None: result[0] = 1; done.set()
                elif ev.type == pygame.JOYDEVICEADDED:
                    pygame.joystick.Joystick(ev.device_index).init()
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if RA.collidepoint(ev.pos): result[0] = 0; done.set()
                    if RB.collidepoint(ev.pos): result[0] = 2; done.set()
                    if RC and RC.collidepoint(ev.pos): result[0] = 1; done.set()

            screen.fill(BG)
            s = ft.render(title, True, WHT)
            screen.blit(s, (CX - s.get_width() // 2, CY - 80))
            y = CY - 44
            for ln in _wrap(body, fb, min(W - 80, 580)):
                s = fb.render(ln, True, GRAY)
                screen.blit(s, (CX - s.get_width() // 2, y))
                y += 22
            pygame.draw.rect(screen, BLUE, RA, border_radius=6)
            pygame.draw.rect(screen, DARK, RB, border_radius=6)
            buttons = [(RA, label_a), (RB, label_b)]
            if RC is not None:
                pygame.draw.rect(screen, RED, RC, border_radius=6)
                buttons.append((RC, label_c))
            for rect, lbl in buttons:
                s = fbt.render(lbl, True, WHT)
                screen.blit(s, (rect.centerx - s.get_width() // 2,
                                rect.centery - s.get_height() // 2))
            pygame.display.flip()
            if first_frame:
                first_frame = False
                evdev_ready.set()
            clock.tick(30)
    finally:
        done.set()
        pygame.quit()
        _restore_env()

    log(f"_pygame_dialog: result={result[0]}")
    return result[0]

COMMANDS = {
    "gui":                "Open the management GUI",
    "launch":             "Sync + launch a game + sync on exit  (used in Steam Launch Options)",
    "pull":               "Pull saves from cloud storage before launch  (use in Steam Launch Options)",
    "push":               "Push saves to cloud storage after exit       (use in Steam Launch Options)",
    "pre-launch":         "Silent pre-launch check; writes IPC status file for pre-launcher.exe",
    "pull-force":         "Force-resolve a conflict pull  --keep=remote|local",
    "sync":               "Manually sync one game or all games",
    "update-shortcuts":   "Regenerate Steam shortcuts + copy wrapper/pre-launcher for all configured games",
    "list":               "List tracked games and status",
    "log":                "Show recent log  [N lines, default 50]",
    "install-decky":      "Install the Decky Loader plugin to ~/homebrew/plugins/",
    "update":             "Update ExternalGameSync to the latest version from GitHub",
}


def _prompt_conflict(game_name: str, app_id: str | None = None) -> str | None:
    """
    Show a gamepad-friendly dialog asking the user to resolve a save conflict.
    Returns "remote", "local", or None (closed without choosing = cancel launch).
    Both sides are backed up regardless of choice.
    """
    body = (game_name + " saves have changed on cloud storage since your last sync. "
            "Both sides will be backed up before any changes are made.")

    rc = _pygame_dialog("Save Conflict", body, "(A)  Keep Cloud", "(B)  Keep Local", "(Y)  Cancel Launch", app_id=app_id)
    log(f"_prompt_conflict: pygame rc={rc}")
    if rc == 0: return "remote"
    if rc == 2: return "local"
    if rc in (1, 3): return None  # cancel / closed = abort launch

    # Fallback: zenity (pygame not installed or display init failed)
    import shutil as _sh
    if sys.platform != "win32" and _sh.which("zenity"):
        r = subprocess.run([
            "zenity", "--question", "--title=Save Conflict",
            "--text=" + body,
            "--ok-label=Keep Cloud", "--cancel-label=Keep Local", "--width=420",
        ], capture_output=True)
        if r.returncode == 0: return "remote"
        if r.returncode == 1: return "local"

    if sys.platform == "win32":
        import ctypes
        flags  = 0x00000003 | 0x00000030 | 0x00040000  # MB_YESNOCANCEL|ICONWARNING|TOPMOST
        result = ctypes.windll.user32.MessageBoxW(
            0,
            game_name + " saves have changed on cloud storage since your last sync.\n"
            "Both sides will be backed up.\n\n"
            "Yes = Keep Cloud    No = Keep Local    Cancel = Abort launch",
            "ExternalGameSync — Save Conflict", flags,
        )
        if result == 6:  return "remote"   # IDYES
        if result == 7:  return "local"    # IDNO
        return None                         # IDCANCEL or closed

    log("Conflict prompt unavailable — cancelling launch")
    return None


def _prompt_no_connection(game_name: str, server_ahead: bool = False) -> bool:
    """
    Prompt the user when cloud storage is unreachable before launching.
    Returns True = continue with local saves, False = cancel launch.
    If server_ahead is True, show a stronger warning that the server was last
    known to have unsynced changes.
    """
    if server_ahead:
        body = ("Could not reach cloud storage before launching " + game_name + ". "
                "The server was last known to have unsynced changes — "
                "you may be playing with outdated saves. "
                "Continue launching the game anyway?")
        win_body = ("Could not reach cloud storage before launching " + game_name + ".\n"
                    "WARNING: The server was last known to have unsynced changes.\n"
                    "You may be playing with outdated saves.\n\n"
                    "Continue launching the game anyway?")
    else:
        body = ("Could not reach cloud storage before launching " + game_name + ". "
                "Your local saves are safe, but they will not be updated from the server. "
                "Continue launching the game anyway?")
        win_body = ("Could not reach cloud storage before launching " + game_name + ".\n"
                    "Your local saves are safe.\n\nContinue launching the game anyway?")

    rc = _pygame_dialog("Cannot Connect", body, "(A)  Continue Anyway", "(B)  Cancel")
    if rc == 0:          return True
    if rc in (2, 3):     return False

    import shutil as _sh
    if sys.platform != "win32" and _sh.which("zenity"):
        r = subprocess.run([
            "zenity", "--question", "--title=Cannot Connect",
            "--text=" + body,
            "--ok-label=Continue Anyway", "--cancel-label=Cancel", "--width=420",
        ], capture_output=True)
        return r.returncode == 0

    if sys.platform == "win32":
        import ctypes
        flags  = 0x00000004 | 0x00000030 | 0x00040000
        result = ctypes.windll.user32.MessageBoxW(
            0, win_body, "ExternalGameSync — Cannot Connect", flags,
        )
        return result == 6

    log("No dialog available for connection failure — defaulting to continue")
    return True


def cmd_launch(args):
    """
    Pull saves -> launch game -> push saves.
    Used as Steam Launch Option:
      externalgamesync launch "Game Name" %command%

    Steam expands %command% into the full Proton-wrapped launch command,
    which we receive as extra args and pass straight through unchanged.
    This means Steam still handles all the Proton/Wine setup — we just
    bookend it with save syncing.

    If called without %command% (manual/testing), falls back to the
    exe_path stored in the game config (runs without Proton — testing only).
    """
    if not args:
        print("Usage: externalgamesync launch <game-name> %command%")
        sys.exit(1)

    game_name = args[0]
    # Everything after the game name is %command% expanded by Steam.
    # Steam passes each token as a separate argv element so we pass them
    # through as-is — no splitting or joining needed.
    extra = args[1:] if len(args) > 1 else []
    if extra and extra[0] == "--":
        extra = extra[1:]
    log(f"cmd_launch: game={repr(game_name)} extra_count={len(extra)}")
    if extra:
        log(f"cmd_launch: exe_parts[0]={repr(extra[0])}")

    game = find_game(game_name)
    if not game:
        log_err(f"Game not found: {game_name}")
        print(f"Known games: {', '.join(g['name'] for g in load_games())}")
        sys.exit(1)

    game_id = game["id"]

    # %command% from Steam is the full Proton-wrapped command — pass through as-is
    if extra:
        exe_parts = extra
    else:
        # Fallback for manual testing only (no Proton/Steam wrapping)
        mc = get_local_config(game["id"])
        if not mc:
            log_err(f"No config for this machine ({hostname()}). Run the GUI to assign.")
            sys.exit(1)
        log("[warn] No %command% — running exe directly without Steam (testing only)")
        if mc.get("platform") == "windows":
            exe_parts = [mc["exe_path"]]
        else:
            exe_parts = [str(resolve_exe_path(mc["app_id"], game["exe_path"]))]

    lock = GameLock(game_id)
    if not lock.acquire():
        log_err(f"Could not acquire lock for {game_id}")
        sys.exit(1)

    try:
        log(f"=== Launching '{game['name']}' ===")

        # Pull latest saves from cloud storage before launching
        log("Pre-launch pull...")
        pull_ok, pull_msg = rclone_sync_pull(game_id, game)

        if pull_msg == PULL_CONFLICT:
            # Remote changed since last sync — ask user what to keep
            log(f"Conflict detected for '{game['name']}' — prompting user")
            keep = _prompt_conflict(game["name"])
            log(f"User chose: keep {keep}")
            if keep is None:
                log("User cancelled at conflict dialog — aborting launch")
                sys.exit(1)
            pull_ok, pull_msg = rclone_sync_pull_force(game_id, keep=keep)
            if not pull_ok:
                print(f"[warn] Conflict resolution failed: {pull_msg}")

        elif pull_msg == PULL_NO_CONNECTION:
            log(f"Cannot reach cloud storage for '{game['name']}' — prompting user")
            last_status = load_sync_state(game_id)
            server_ahead = last_status in ("cloud_ahead", "conflict")
            if not _prompt_no_connection(game["name"], server_ahead=server_ahead):
                log("User cancelled launch due to connection failure")
                sys.exit(1)
            log("User chose to continue with local saves")

        elif not pull_ok:
            print(f"[warn] Pre-launch pull had issues: {pull_msg}")
            print(f"  Check: externalgamesync log")

        # Launch — Steam already tokenizes %command% correctly into argv elements.
        # Pass the list directly and inherit the full environment.
        log(f"Launching: {' '.join(exe_parts)}")
        proc = subprocess.run(exe_parts, env=os.environ.copy())
        log(f"Game exited (rc={proc.returncode})")

        # Push saves to cloud storage after exit
        log(f"[push] cmd_launch post-game push triggered (game rc={proc.returncode})")
        push_ok, push_msg = rclone_sync_push(game_id, game)
        if push_ok:
            log_ok(f"Saves pushed for '{game['name']}'")
        else:
            log_err(f"Post-exit push failed: {push_msg}")

    finally:
        lock.release()

    sys.exit(proc.returncode)


def cmd_pre_launch(args):
    """
    Silent pre-launch check used by the wrapper.sh + pre-launcher.exe flow.

    Usage: externalgamesync pre-launch <game_id> <status_file> <game_exe_win>

    Runs the pull/conflict check with no dialog.  Writes the IPC status file
    for pre-launcher.exe (which runs inside Proton and shows the actual dialog).

    Exit codes:
      0  clean pull completed (or first-run, no remote yet)
      1  conflict detected   — pre-launcher.exe must handle dialog + rclone
      2  no connection       — pre-launcher.exe shows warning, game still launches
      3  other error         — treated as ok by wrapper, game still launches
    """
    if len(args) < 3:
        print("Usage: externalgamesync pre-launch <game_id> <status_file> <game_exe_win>")
        sys.exit(3)

    game_id, status_file, game_exe_win = args[0], args[1], args[2]

    game = find_game(game_id)
    if not game:
        log_err(f"pre-launch: game not found: {game_id}")
        sys.exit(3)

    def _write(status: str):
        try:
            preserved = {}
            try:
                with open(status_file) as _f:
                    for _line in _f:
                        if '=' in _line:
                            _k, _v = _line.strip().split('=', 1)
                            if _k in ('FULLSCREEN',):
                                preserved[_k] = _v
            except Exception:
                pass
            with open(status_file, "w") as f:
                f.write(f"STATUS={status}\n")
                f.write(f"GAME={game['name']}\n")
                f.write(f"GAME_EXE={game_exe_win}\n")
                if status == "no_connection":
                    last = load_sync_state(game_id)
                    if last:
                        f.write(f"LAST_STATUS={last}\n")
                for _k, _v in preserved.items():
                    f.write(f"{_k}={_v}\n")
        except Exception as e:
            log_err(f"pre-launch: could not write status file: {e}")

    ok, msg = rclone_sync_pull(game_id, game)

    if game_exe_win:
        try:
            if sys.platform == "win32":
                exe_path = Path(game_exe_win)
            elif game_exe_win.upper().startswith("Z:"):
                exe_path = Path(game_exe_win[2:].replace("\\", "/"))
            else:
                exe_path = None
            if exe_path and exe_path.is_file():
                h = hash_file(exe_path)
                if add_game_hash(game_id, h, "exe_hashes"):
                    rclone_push_games_json()
        except Exception as e:
            log_err(f"pre-launch: exe hash update failed: {e}")

    if msg in (PULL_OK, PULL_NO_REMOTE):
        _write("ok")
        sys.exit(0)
    elif msg == PULL_CONFLICT:
        _write("conflict")
        sys.exit(1)
    elif msg == PULL_NO_CONNECTION:
        _write("no_connection")
        sys.exit(2)
    else:
        _write("ok")
        sys.exit(3)


def cmd_pull_force(args):
    """
    Force-resolve a conflict pull.  Called by wrapper.sh's background handler
    after pre-launcher.exe writes the user's choice to egs_choice.txt.

    Usage: externalgamesync pull-force <game_id> --keep=remote|local
    """
    if not args:
        print("Usage: externalgamesync pull-force <game_id> --keep=remote|local")
        sys.exit(1)

    game_id = args[0]
    keep = None
    for a in args[1:]:
        if a.startswith("--keep="):
            keep = a.split("=", 1)[1]

    if keep not in ("remote", "local"):
        print("Error: --keep must be 'remote' or 'local'")
        sys.exit(1)

    game = find_game(game_id)
    if not game:
        log_err(f"pull-force: game not found: {game_id}")
        sys.exit(1)

    ok, msg = rclone_sync_pull_force(game_id, keep=keep, game=game)
    if not ok:
        log_err(f"pull-force failed: {msg}")
        sys.exit(1)
    sys.exit(0)


def cmd_pull(args):
    """Pull saves from cloud storage. Used in Steam Launch Options before %command%."""
    if not args:
        print("Usage: externalgamesync pull <game>")
        sys.exit(1)
    game = find_game(args[0])
    if not game:
        print(f"Game not found: {args[0]}")
        sys.exit(1)
    ok, msg = rclone_sync_pull(game["id"], game)
    if msg == PULL_CONFLICT:
        log(f"Conflict detected for '{game['name']}' — prompting user")
        keep = _prompt_conflict(game["name"])
        log(f"User chose: keep {keep}")
        if keep is None:
            log("User cancelled at conflict dialog — aborting")
            sys.exit(1)
        ok, msg = rclone_sync_pull_force(game["id"], keep=keep, game=game)
        if not ok:
            log_err(f"Conflict resolution failed: {msg}")
    elif msg == PULL_NO_CONNECTION:
        log(f"Cannot reach cloud storage for '{game['name']}' — prompting user")
        last_status = load_sync_state(game["id"])
        server_ahead = last_status in ("cloud_ahead", "conflict")
        if not _prompt_no_connection(game["name"], server_ahead=server_ahead):
            log("User cancelled launch due to connection failure")
            sys.exit(2)  # non-zero so the wrapper script knows to abort
        log("User chose to continue with local saves")
    elif not ok:
        log_err(f"Pull failed: {msg}")
        # Don't exit non-zero — we still want the game to launch even if pull fails
    sys.exit(0)


def cmd_push(args):
    """Push saves to cloud storage. Used in Steam Launch Options after %command%."""
    if not args:
        print("Usage: externalgamesync push <game>")
        sys.exit(1)
    game = find_game(args[0])
    if not game:
        print(f"Game not found: {args[0]}")
        sys.exit(1)
    log(f"[push] cmd_push triggered for '{args[0]}' — post-game push via wrapper")
    ok, msg = rclone_sync_push(game["id"])
    if not ok:
        log_err(f"Push failed: {msg}")
    sys.exit(0)


def cmd_sync(args):
    games = load_games()
    if not games:
        print("No games configured.")
        return
    targets = [find_game(args[0])] if args else games
    targets = [g for g in targets if g]
    for g in targets:
        print(f"Syncing '{g['name']}'...")
        ok, msg = rclone_bisync(g["id"])
        print(f"  {'✓' if ok else '✗'} {msg}")
    rclone_push_games_json()


def cmd_resync(args):
    if not args:
        print("Usage: externalgamesync resync <game>")
        sys.exit(1)
    game = find_game(args[0])
    if not game:
        print(f"Game not found: {args[0]}")
        sys.exit(1)
    print(f"Force resyncing '{game['name']}'...")
    ok, msg = rclone_bisync(game["id"], resync=True)
    if ok:
        games = load_games()
        for g in games:
            if g["id"] == game["id"]:
                g["bisync_initialized"] = True
        save_games(games)
    print(f"{'✓' if ok else '✗'} {msg}")


def cmd_update_shortcuts(args):
    """Regenerate Steam shortcuts and copy wrapper/pre-launcher for all locally configured games."""
    from steam import (
        read_shortcuts, get_non_steam_games, update_shortcut_launch,
        update_native_game_launch,
    )
    from gui_common import SAVESYNC_BIN

    shortcuts_data, vdf_path = read_shortcuts()

    def _appid_key(raw):
        try:
            return str(int(raw) & 0xFFFFFFFF)
        except (ValueError, TypeError):
            return str(raw) if raw else None

    ns_games    = get_non_steam_games(shortcuts_data) if shortcuts_data else []
    ns_by_name  = {g["name"]: g for g in ns_games}
    ns_by_appid = {k: g for g in ns_games if (k := _appid_key(g["appid"]))}

    # Shut Steam down before touching localconfig.vdf — Steam overwrites it on exit
    all_configs = [(game_cfg, get_local_config(game_cfg["id"])) for game_cfg in load_games()]
    has_native  = any(mc and mc.get("native_steam") for _, mc in all_configs)
    if has_native:
        from gui_common import shutdown_steam_sync
        print("Native Steam games detected — closing Steam before updating launch options...")
        was_running = shutdown_steam_sync()
        if was_running:
            print("Steam closed.")

    results = []
    for game_cfg, mc in all_configs:
        if not mc:
            continue

        if mc.get("native_steam"):
            app_id = mc.get("app_id", "")
            if not app_id:
                results.append((game_cfg["name"], False, "no app_id in machine config"))
                continue
            ok, msg = update_native_game_launch(
                app_id=app_id,
                game_name=game_cfg["name"],
                savesync_bin=SAVESYNC_BIN,
                game_cfg=game_cfg,
            )
            results.append((game_cfg["name"], ok, "updated" if ok else msg))
        else:
            if not shortcuts_data:
                results.append((game_cfg["name"], False, "could not read shortcuts.vdf"))
                continue
            app_id   = mc.get("app_id", "")
            ns_entry = (ns_by_appid.get(_appid_key(app_id)) if app_id else None) or ns_by_name.get(game_cfg["name"])
            if not ns_entry:
                results.append((game_cfg["name"], False, "shortcut not found in Steam"))
                continue
            _linux_native = mc.get("platform") == "linux_native"
            if mc.get("platform") == "windows":
                exe_real = Path(mc["exe_path"])
            elif _linux_native:
                _saved_exe = mc.get("shortcut_exe", "")
                exe_real = Path(_saved_exe) if _saved_exe else Path(ns_entry.get("exe", "").strip().strip('"'))
            else:
                exe_real = resolve_exe_path(mc["app_id"], game_cfg["exe_path"])
            ok, msg = update_shortcut_launch(
                shortcuts_data, vdf_path,
                shortcut_index=ns_entry["index"],
                game_name=game_cfg["name"],
                savesync_bin=SAVESYNC_BIN,
                real_exe=str(exe_real),
                start_dir=ns_entry["start_dir"],
                game_cfg=game_cfg,
                linux_native=_linux_native,
            )
            results.append((game_cfg["name"], ok, "updated" if ok else msg))

    if not results:
        print("No games are configured on this machine.")
        return

    any_fail = False
    for name, ok, msg in results:
        print(f"  {'OK' if ok else ' X'}  {name}: {msg}")
        if not ok:
            any_fail = True

    if any_fail:
        print("\nSome shortcuts could not be updated (see above).")
        sys.exit(1)
    else:
        print("\nAll shortcuts updated. Restart Steam to apply changes.")


def cmd_list(args):
    games = load_games()
    if not games:
        print("No games configured. Open the GUI to add games.")
        return
    print(f"\n{'NAME':<28} {'ID':<25} {'THIS MACHINE':<30} {'SYNCED'}")
    print("─" * 90)
    for g in games:
        mc = get_local_config(g["id"])
        if mc and mc.get("platform") == "windows":
            machine_info = mc.get("exe_path", "(windows)")[:30]
        elif mc and mc.get("app_id"):
            machine_info = f"app_id={mc['app_id']}"
        else:
            machine_info = "(not assigned)"
        synced  = "✓" if g.get("bisync_initialized") else "○"
        local   = get_local_save_path(g["id"], g)
        save_ok = " [ok]" if local.exists() else " [missing]"
        print(f"{g['name']:<28} {g['id']:<25} {machine_info:<30} {synced}{save_ok}")
        print(f"  exe: {g.get('exe_path','?')}  |  save: {g.get('save_path','?')}")
    print()


def cmd_log(args):
    # Write mode: externalgamesync log "some message"  (first arg isn't a number)
    if args and not args[0].isdigit():
        log(" ".join(args))
        return
    n = int(args[0]) if args else 50
    if not LOG_FILE.exists():
        print("No log yet.")
        return
    lines = LOG_FILE.read_text().splitlines()
    for line in lines[-n:]:
        print(line)


def cmd_gui(args):
    """Launch the management GUI."""
    gui_script = Path(__file__).parent / "gui.py"
    subprocess.run([sys.executable, str(gui_script)] + args)


def cmd_install_decky(args):
    """Install the Decky Loader plugin to ~/homebrew/plugins/ExternalGameSync/."""
    import shutil

    decky_dir = Path.home() / "homebrew" / "plugins"
    if not decky_dir.exists():
        print("Decky Loader not found.")
        print("  Expected: ~/homebrew/plugins/")
        print("  Install Decky first: https://decky.xyz/")
        sys.exit(1)

    plugin_src = Path(__file__).parent / "decky_plugin"
    if not plugin_src.exists():
        print(f"Plugin source not found: {plugin_src}")
        print("Re-run install.sh to restore it.")
        sys.exit(1)

    dist_js = plugin_src / "dist" / "index.js"
    if not dist_js.exists():
        print("Frontend bundle not found: decky_plugin/dist/index.js")
        print("Build it once on a desktop/laptop with Node.js, then re-run install-decky:")
        print(f"  cd {plugin_src} && npm install && npm run build")
        sys.exit(1)

    dest = decky_dir / "ExternalGameSync"

    def _install():
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir()
        shutil.copy2(plugin_src / "main.py",      dest / "main.py")
        shutil.copy2(plugin_src / "plugin.json",  dest / "plugin.json")
        shutil.copy2(plugin_src / "package.json", dest / "package.json")
        (dest / "dist").mkdir()
        shutil.copy2(dist_js, dest / "dist" / "index.js")
        src_map = dist_js.with_suffix(".js.map")
        if src_map.exists():
            shutil.copy2(src_map, dest / "dist" / "index.js.map")

    try:
        _install()
    except PermissionError:
        # Decky's plugin service runs as root, so a previously installed copy
        # may be owned by root. Re-run the copy operations under sudo.
        print("Permission denied — retrying with sudo...")
        cmds = [
            ["sudo", "rm", "-rf", str(dest)],
            ["sudo", "mkdir", str(dest)],
            ["sudo", "cp", str(plugin_src / "main.py"),      str(dest / "main.py")],
            ["sudo", "cp", str(plugin_src / "plugin.json"),  str(dest / "plugin.json")],
            ["sudo", "cp", str(plugin_src / "package.json"), str(dest / "package.json")],
            ["sudo", "mkdir", str(dest / "dist")],
            ["sudo", "cp", str(dist_js), str(dest / "dist" / "index.js")],
            ["sudo", "chown", "-R", f"{os.getenv('USER', 'deck')}:", str(dest)],
        ]
        for cmd in cmds:
            r = subprocess.run(cmd)
            if r.returncode != 0:
                print(f"Failed: {' '.join(cmd)}")
                sys.exit(1)

    print(f"Installed: {dest}")

    # Restart the Decky plugin-loader service so the new version is active immediately
    r = subprocess.run(["systemctl", "restart", "plugin_loader"], capture_output=True)
    if r.returncode == 0:
        print("plugin_loader service restarted.")
    else:
        # May need sudo on some setups
        r2 = subprocess.run(["sudo", "systemctl", "restart", "plugin_loader"], capture_output=True)
        if r2.returncode == 0:
            print("plugin_loader service restarted (via sudo).")
        else:
            print("Could not restart plugin_loader automatically.")
            print("  Restart manually: Quick Access Menu > Settings > Decky > Restart plugin service")


def cmd_update(args):
    """Update ExternalGameSync to the latest version from GitHub."""
    import shutil
    import tarfile
    import tempfile
    import urllib.request

    TARBALL_URL = "https://github.com/pmahern/steamexternalgamesync/archive/refs/heads/master.tar.gz"

    print("Downloading latest version from GitHub...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tarball = os.path.join(tmp, "master.tar.gz")
            urllib.request.urlretrieve(TARBALL_URL, tarball)

            print("Extracting...")
            with tarfile.open(tarball, "r:gz") as tf:
                tf.extractall(tmp)

            # The tarball extracts to externalgamesync-master/
            extracted = os.path.join(tmp, "externalgamesync-master")
            if not os.path.isdir(extracted):
                # Fallback: find any extracted dir
                subdirs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)) and d != "__MACOSX"]
                if not subdirs:
                    print("Could not find extracted directory.")
                    sys.exit(1)
                extracted = os.path.join(tmp, subdirs[0])

            if sys.platform == "win32":
                installer = os.path.join(extracted, "install.ps1")
                if not os.path.exists(installer):
                    print(f"Installer not found: {installer}")
                    sys.exit(1)
                print("Running installer...")
                subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", installer], check=True)
            else:
                installer = os.path.join(extracted, "install.sh")
                if not os.path.exists(installer):
                    print(f"Installer not found: {installer}")
                    sys.exit(1)
                os.chmod(installer, 0o755)
                print("Running installer...")
                subprocess.run(["bash", installer], check=True)

    except Exception as e:
        print(f"Update failed: {e}")
        sys.exit(1)

    print()

    # Prompt to update shortcuts
    try:
        answer = input("Update Steam shortcuts for all configured games? [Y/n] ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("", "y", "yes"):
        print()
        cmd_update_shortcuts([])

    # Prompt to update Decky plugin (only if already installed)
    decky_dest = Path.home() / "homebrew" / "plugins" / "ExternalGameSync"
    if decky_dest.exists():
        print()
        try:
            answer = input("Update the Decky plugin? [Y/n] ").strip().lower()
        except EOFError:
            answer = ""
        if answer in ("", "y", "yes"):
            print()
            cmd_install_decky([])


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("ExternalGameSync\n")
        print("Usage: externalgamesync <command> [args]\n")
        for cmd, desc in COMMANDS.items():
            print(f"  {cmd:<10} {desc}")
        print("\nFirst time? Run:  externalgamesync gui")
        sys.exit(0)

    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    migrate_from_games_json()
    dispatch = {
        "gui":           cmd_gui,
        "launch":        cmd_launch,
        "pull":          cmd_pull,
        "pre-launch":    cmd_pre_launch,
        "pull-force":    cmd_pull_force,
        "push":          cmd_push,
        "sync":               cmd_sync,
        "resync":             cmd_resync,
        "update-shortcuts":   cmd_update_shortcuts,
        "list":               cmd_list,
        "log":           cmd_log,
        "install-decky": cmd_install_decky,
        "update":        cmd_update,
    }
    dispatch[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
