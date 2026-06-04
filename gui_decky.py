"""
ExternalGameSync GUI -- Decky Loader plugin installation panel.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import dearpygui.dearpygui as dpg

from config import load_settings, save_settings
from gui_common import (
    set_nav_active, clear_content, add_header,
    run_async, show_progress, stop_progress, show_done, show_error,
)
from gui_home import refresh_and_home

_DECKY_PLUGINS_DIR = Path.home() / "homebrew" / "plugins"
_APP_DIR           = Path.home() / ".local" / "share" / "externalgamesync"
_PLUGIN_SRC        = _APP_DIR / "decky_plugin"
_DIST_JS           = _PLUGIN_SRC / "dist" / "index.js"
_DEST              = _DECKY_PLUGINS_DIR / "ExternalGameSync"

_COLOR_OK   = (45,  164,  78)
_COLOR_WARN = (207, 180,  34)
_COLOR_BAD  = (207,  34,  46)
_COLOR_DIM  = (130, 130, 155)
_COLOR_BODY = (180, 180, 200)


def _status() -> tuple[str, str, str]:
    """Return (label, detail, color_key) for the current install state."""
    if not _DECKY_PLUGINS_DIR.exists():
        return "Decky not found", "~/homebrew/plugins/ does not exist. Install Decky Loader first.", "bad"
    if not _DIST_JS.exists():
        return "Not built", "decky_plugin/dist/index.js not found. Run decky_plugin/build.sh on a desktop with Node.js.", "bad"
    if (_DEST / "main.py").exists():
        return "Installed", str(_DEST), "ok"
    return "Not installed", "Ready to install.", "warn"


# ── Settings helpers ──────────────────────────────────────────────────────────

def _save_decky_settings_from_ui():
    if not dpg.does_item_exist("_polling_enabled_cb"):
        return
    s = load_settings()
    s["polling_enabled"] = dpg.get_value("_polling_enabled_cb")
    s["poll_auto_pull"]  = dpg.get_value("_poll_auto_pull_cb")
    save_settings(s)


def _on_polling_changed(sender, app_data):
    enabled = app_data
    dpg.configure_item("_poll_auto_pull_cb", enabled=enabled)
    if not enabled:
        dpg.set_value("_poll_auto_pull_cb", False)
    _save_decky_settings_from_ui()


def _on_auto_pull_changed(sender, app_data):
    _save_decky_settings_from_ui()


# ── Main panel ────────────────────────────────────────────────────────────────

def flow_decky():
    set_nav_active("decky")
    clear_content()
    add_header("Decky Plugin", "Steam Deck Quick Access Menu integration")

    label, detail, color_key = _status()
    status_color = {"ok": _COLOR_OK, "warn": _COLOR_WARN, "bad": _COLOR_BAD}[color_key]

    decky_ok = _DECKY_PLUGINS_DIR.exists()
    dist_ok  = _DIST_JS.exists()

    # ── Description ───────────────────────────────────────────────────────────
    _FEATURES = [
        ("Manage Saves screen",
         "Launched via a button in the Quick Access Menu (... button). Shows all managed games with live sync statuses, "
         "and displays Local, Cloud, and Abort buttons for any conflicts so you can resolve them before launching."),
        ("Game detail sync badge",
         "Adds a sync status indicator to each managed game's detail screen; becomes interactive when the game is out of sync with the server."),
        ("Background auto-polling",
         "Periodically checks the server for save changes when a game is not running (every 5 min when enabled). Can optionally auto-pull saves when no conflict is detected. Can be set here or in the plugin in game mode."),
    ]

    dpg.add_text("FEATURES", parent="content_group")
    for title, body in _FEATURES:
        dpg.add_text(title, parent="content_group", color=(220, 220, 240))
        dpg.add_text(body, parent="content_group", color=_COLOR_BODY, wrap=700)
        dpg.add_spacer(height=6, parent="content_group")

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=8, parent="content_group")

    # ── Requirements ──────────────────────────────────────────────────────────
    dpg.add_text("Requirements", parent="content_group")
    dpg.add_spacer(height=6, parent="content_group")

    dpg.add_text(
        ("OK  " if decky_ok else "X   ") + "Decky Loader installed  (~/homebrew/plugins/)",
        parent="content_group",
        color=_COLOR_OK if decky_ok else _COLOR_BAD,
    )
    dpg.add_text(
        ("OK  " if dist_ok else "X   ") + "Plugin frontend built   (decky_plugin/dist/index.js)",
        parent="content_group",
        color=_COLOR_OK if dist_ok else _COLOR_BAD,
    )
    if not dist_ok:
        dpg.add_text(
            "     Build it on a desktop:  bash decky_plugin/build.sh",
            parent="content_group", color=_COLOR_DIM,
        )

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=8, parent="content_group")

    # ── Current status ────────────────────────────────────────────────────────
    dpg.add_text(f"Status:  {label}", parent="content_group", color=status_color)
    dpg.add_text(detail, parent="content_group", color=_COLOR_DIM, wrap=700)
    dpg.add_spacer(height=16, parent="content_group")

    # ── Buttons ───────────────────────────────────────────────────────────────
    btn_label = "Reinstall Plugin" if label == "Installed" else "Install Plugin"
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(
            tag="_decky_install_btn",
            label=btn_label,
            width=160, height=32,
            enabled=decky_ok and dist_ok,
            callback=_do_install,
        )
        dpg.add_button(
            label="Cancel", width=80, height=32,
            callback=refresh_and_home,
        )

    # ── Plugin settings ───────────────────────────────────────────────────────
    dpg.add_spacer(height=16, parent="content_group")
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text("Plugin Settings", parent="content_group")
    dpg.add_spacer(height=6, parent="content_group")

    s = load_settings()
    polling_enabled = s.get("polling_enabled", True)
    poll_auto_pull  = s.get("poll_auto_pull",  False)

    dpg.add_checkbox(
        tag="_polling_enabled_cb",
        label="Enable background polling (every 5 min)",
        default_value=polling_enabled,
        callback=_on_polling_changed,
        parent="content_group",
    )
    dpg.add_spacer(height=4, parent="content_group")
    dpg.add_checkbox(
        tag="_poll_auto_pull_cb",
        label="Auto-pull cloud saves when no conflict detected",
        default_value=poll_auto_pull,
        enabled=polling_enabled,
        callback=_on_auto_pull_changed,
        parent="content_group",
    )


# ── Install flow ──────────────────────────────────────────────────────────────

def _do_install():
    if dpg.does_item_exist("_decky_install_btn"):
        dpg.configure_item("_decky_install_btn", enabled=False, label="Installing...")

    def _work():
        try:
            _install_plain()
            return "ok", ""
        except PermissionError:
            return "need_sudo", ""
        except Exception as exc:
            return "error", str(exc)

    def _done(res):
        outcome, msg = res
        if outcome == "ok":
            _show_installed()
        elif outcome == "need_sudo":
            _show_sudo_prompt()
        else:
            show_error("Install failed", msg, on_done=flow_decky)

    run_async(_work, (), _done)


def _show_installed():
    show_done(
        "Plugin installed",
        [
            f"Installed to: {_DEST}",
            "",
            "Plugin service restarted. The plugin should be active now.",
        ],
        success=True,
    )


def _show_sudo_prompt(error_msg: str = ""):
    """Show a modal password dialog when the install needs elevated permissions."""
    if dpg.does_item_exist("_sudo_modal"):
        dpg.delete_item("_sudo_modal")

    height = 210 if error_msg else 185
    with dpg.window(
        label="Administrator Password Required",
        tag="_sudo_modal",
        modal=True, no_resize=True,
        width=430, height=height,
        pos=[265, 220],
    ):
        dpg.add_text(
            "The existing plugin folder is owned by root (installed by Decky).",
            wrap=410,
        )
        dpg.add_text(
            "Enter your sudo password to replace it.",
            wrap=410, color=_COLOR_DIM,
        )
        if error_msg:
            dpg.add_spacer(height=4)
            dpg.add_text(error_msg, color=_COLOR_BAD, wrap=410)
        dpg.add_spacer(height=10)
        dpg.add_input_text(
            tag="_sudo_pw",
            password=True,
            width=-1,
            hint="Password",
            on_enter=True,
            callback=lambda: _run_with_sudo(dpg.get_value("_sudo_pw")),
        )
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            dpg.add_button(
                label="Install",
                width=110, height=28,
                callback=lambda: _run_with_sudo(dpg.get_value("_sudo_pw")),
            )
            dpg.add_button(
                label="Cancel",
                width=80, height=28,
                callback=_cancel_sudo,
            )


def _cancel_sudo():
    if dpg.does_item_exist("_sudo_modal"):
        dpg.delete_item("_sudo_modal")
    flow_decky()


def _run_with_sudo(password: str):
    if dpg.does_item_exist("_sudo_modal"):
        dpg.delete_item("_sudo_modal")

    show_progress("Installing with elevated permissions...")

    def _work():
        try:
            _install_sudo(password)
            return "ok", ""
        except RuntimeError as exc:
            msg = str(exc)
            if "incorrect" in msg.lower() or "sorry" in msg.lower():
                return "bad_password", msg
            return "error", msg
        except Exception as exc:
            return "error", str(exc)

    def _done(res):
        stop_progress()
        outcome, msg = res
        if outcome == "ok":
            _show_installed()
        elif outcome == "bad_password":
            _show_sudo_prompt("Incorrect password -- please try again.")
        else:
            show_error("Install failed", msg, on_done=flow_decky)

    run_async(_work, (), _done)


# ── Low-level install helpers ─────────────────────────────────────────────────

def _install_plain():
    if _DEST.exists():
        shutil.rmtree(_DEST)
    _DEST.mkdir()
    shutil.copy2(_PLUGIN_SRC / "main.py",      _DEST / "main.py")
    shutil.copy2(_PLUGIN_SRC / "plugin.json",  _DEST / "plugin.json")
    shutil.copy2(_PLUGIN_SRC / "package.json", _DEST / "package.json")
    shutil.copytree(_PLUGIN_SRC / "dist",      _DEST / "dist")
    subprocess.run(["systemctl", "restart", "plugin_loader"], capture_output=True)


def _install_sudo(password: str):
    """Run all install operations in a single sudo -S call."""
    user = os.environ.get("USER", "deck")
    dest = shlex.quote(str(_DEST))
    script = " && ".join([
        f"rm -rf {dest}",
        f"mkdir {dest}",
        f"cp {shlex.quote(str(_PLUGIN_SRC / 'main.py'))} {dest}/main.py",
        f"cp {shlex.quote(str(_PLUGIN_SRC / 'plugin.json'))} {dest}/plugin.json",
        f"cp {shlex.quote(str(_PLUGIN_SRC / 'package.json'))} {dest}/package.json",
        f"cp -r {shlex.quote(str(_PLUGIN_SRC / 'dist'))} {dest}/dist",
        f"chown -R {shlex.quote(user)}: {dest}",
        "systemctl restart plugin_loader",
    ])
    r = subprocess.run(
        ["sudo", "-S", "bash", "-c", script],
        input=password + "\n",
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        stderr = r.stderr.lower()
        if "sorry" in stderr or "incorrect" in stderr or "failure" in stderr:
            raise RuntimeError("Incorrect password")
        raise RuntimeError(r.stderr.strip() or "Install command failed")
