"""
ExternalGameSync GUI -- add a new non-Steam game shortcut and assign a Proton version,
so the user can run a Windows installer (or game) inside Proton.

Flow:
  Step 1 -- pick the Windows executable / installer and enter a display name
  Step 2 -- pick the Proton version to use
  Result -- shuts Steam down, writes shortcuts.vdf + config.vdf, relaunches Steam
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import dearpygui.dearpygui as dpg

from steam import list_proton_tools, add_non_steam_shortcut, set_compat_tool
from gui_common import (
    set_nav_active, clear_content, add_header, add_action_bar,
    add_path_row, add_table, show_done, show_error,
    show_progress, stop_progress, run_async,
)
from gui_home import require_configured


# ── Steam process helpers ─────────────────────────────────────────────────────

def _steam_cmd() -> str | None:
    """Return the steam executable to use, or None if only available via flatpak."""
    if shutil.which("steam"):
        return "steam"
    return None


def _steam_is_running() -> bool:
    return subprocess.run(["pgrep", "-x", "steam"],
                          capture_output=True).returncode == 0


def _shutdown_steam() -> bool:
    """Send -shutdown and wait up to 30 s. Returns True if Steam exited."""
    cmd = _steam_cmd()
    if cmd:
        subprocess.run([cmd, "-shutdown"], capture_output=True)
    else:
        subprocess.run(
            ["flatpak", "run", "--command=steam", "com.valvesoftware.Steam", "-shutdown"],
            capture_output=True,
        )
    for _ in range(60):
        if not _steam_is_running():
            return True
        time.sleep(0.5)
    return False


def _launch_steam(app_id_unsigned: int | None = None):
    """Launch Steam, optionally queuing a non-Steam game to run after startup."""
    args: list[str] = []
    if app_id_unsigned is not None:
        # Steam's 64-bit game ID for non-Steam shortcuts
        rungameid = (app_id_unsigned << 32) | 0x02000000
        args = [f"steam://rungameid/{rungameid}"]

    cmd = _steam_cmd()
    if cmd:
        subprocess.Popen([cmd] + args)
    else:
        subprocess.Popen(["flatpak", "run", "com.valvesoftware.Steam"] + args)


# ── GUI flow ──────────────────────────────────────────────────────────────────

def flow_install():
    if not require_configured():
        return
    set_nav_active("install")
    _install_s1()


def _install_s1():
    clear_content()
    add_header("Install Game",
               "Step 1 -- Name the game and choose the executable or installer to run")

    dpg.add_text("Game name", parent="content_group")
    dpg.add_input_text(tag="_inst_name", hint="e.g. My Game", width=400,
                       parent="content_group")

    dpg.add_spacer(height=8, parent="content_group")
    add_path_row("Executable / Installer", "_inst_exe", False,
                 lambda: str(Path.home()))

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_text(
        "Steam will be shut down automatically before writing and restarted afterwards.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )

    def _next():
        exe  = dpg.get_value("_inst_exe").strip().strip('"')
        name = dpg.get_value("_inst_name").strip()
        if not name and exe:
            name = Path(exe).stem
        if not exe or not name:
            return
        _install_s2(exe, name)

    add_action_bar("Next -->", _next)


def _install_s2(exe_path: str, app_name: str):
    tools = list_proton_tools()
    if not tools:
        show_error(
            "No Proton Found",
            "No Proton versions found. Install at least one via "
            "Steam -> Library -> Tools.",
        )
        return

    clear_content()
    add_header("Install Game",
               f'Step 2 -- Choose Proton version for "{app_name}"')

    rows  = [[display, internal] for display, internal in tools]
    state = add_table(["Proton Version", "Internal Name"], rows,
                      col_weights=[3, 2], height=-80)

    def _run():
        sel = state["selected"]
        if not sel:
            return
        _install_run(exe_path, app_name, internal_name=sel[1])

    add_action_bar("Add to Steam -->", _run, back_cb=_install_s1)


def _set_progress_msg(msg: str):
    if dpg.does_item_exist("_prog_msg"):
        dpg.set_value("_prog_msg", msg)


def _install_run(exe_path: str, app_name: str, internal_name: str):
    show_progress(f'Adding "{app_name}" to Steam...')

    def _work():
        log_lines = []

        if _steam_is_running():
            _set_progress_msg("Shutting down Steam...")
            exited = _shutdown_steam()
            log_lines.append((
                "Steam shut down" if exited else "Steam did not exit cleanly (proceeding anyway)",
                exited,
            ))
        else:
            log_lines.append(("Steam was not running", True))

        _set_progress_msg(f'Writing shortcut for "{app_name}"...')
        app_id, _idx, err = add_non_steam_shortcut(exe_path, app_name)
        if err:
            log_lines.append((f"Failed to write shortcut: {err}", False))
            return log_lines
        log_lines.append(("Shortcut written to shortcuts.vdf", True))

        ok, msg = set_compat_tool(app_id, internal_name)
        log_lines.append((
            f"Proton set to {internal_name}" if ok else f"Failed to set Proton: {msg}",
            ok,
        ))

        _set_progress_msg("Relaunching Steam...")
        try:
            _launch_steam(app_id)
            log_lines.append(("Steam relaunched, game queued to launch", True))
        except Exception as e:
            log_lines.append((f"Could not relaunch Steam: {e}", False))

        return log_lines

    def _done(log_lines):
        stop_progress()
        all_ok = all(ok for _, ok in log_lines)
        lines  = [("OK  " if ok else "X   ") + msg for msg, ok in log_lines]
        lines += [
            "",
            "Steam is restarting and will launch the game automatically.",
            "If Steam asks you to pick a user, sign in and the game will start.",
            'Once done, return here and use "Add New Game Config" or "Assign Config" to set up',
            "save syncing.",
        ]
        show_done(
            f'"{app_name}" added to Steam' if all_ok else "Added with warnings",
            lines,
            success=all_ok,
        )

    run_async(_work, (), _done)
