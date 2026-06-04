"""
ExternalGameSync GUI — home dashboard and navigation guard.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from config import is_configured
from games import load_games
from machine_config import get_local_config
import rclone as rclone_mod
from gui_common import (
    set_nav_active, clear_content, add_header, add_table,
    run_async, show_progress, stop_progress,
    get_cached_status, set_cached_status,
)

# ── Sync status display ───────────────────────────────────────────────────────

_STATUS_LABEL = {
    "in_sync":       "In sync",
    "cloud_ahead":   "Cloud ahead",
    "local_ahead":   "Local ahead",
    "conflict":      "Conflict!",
    "no_connection": "Offline",
    "unknown":       "Sync to check",
    "error":         "Error",
}
_STATUS_COLOR = {
    "in_sync":       (80,  200, 120),
    "cloud_ahead":   (100, 180, 255),
    "local_ahead":   (240, 180,  50),
    "conflict":      (220,  80,  60),
    "no_connection": (130, 130, 155),
    "unknown":       (130, 130, 155),
    "error":         (220,  80,  60),
}


def require_configured() -> bool:
    """Guard: show setup prompt if not yet configured. Returns True if configured."""
    if is_configured():
        return True
    clear_content()
    dpg.add_spacer(height=60, parent="content_group")
    dpg.add_text("Not configured yet", parent="content_group")
    dpg.add_text("Complete the cloud storage setup first.", parent="content_group",
                 color=(130, 130, 155))
    dpg.add_spacer(height=16, parent="content_group")
    from gui_setup import flow_setup
    dpg.add_button(label="Open Setup", width=140, height=32,
                   callback=flow_setup, parent="content_group")
    return False


def pull_then_home():
    show_progress("Syncing game list from cloud storage...")

    def _done(result):
        stop_progress()
        warning = info = None
        if isinstance(result, tuple):
            ok, msg = result
            if not ok:
                warning = f"Could not sync game list: {msg}"
            elif msg:
                info = msg
        else:
            warning = f"Unexpected error syncing game list: {result!r}"
        refresh_and_home(warning=warning, info=info)

    run_async(rclone_mod.rclone_sync_games_json, (), _done)


def refresh_and_home(warning: str | None = None, info: str | None = None):
    set_nav_active("home")
    clear_content()

    if not is_configured():
        dpg.add_spacer(height=60, parent="content_group")
        dpg.add_text("Welcome to ExternalGameSync", parent="content_group")
        dpg.add_spacer(height=8, parent="content_group")
        dpg.add_text("Sync non-Steam game saves across machines via cloud storage.",
                     parent="content_group", color=(130, 130, 155))
        dpg.add_text("Start by connecting to your cloud storage server.",
                     parent="content_group", color=(130, 130, 155))
        dpg.add_spacer(height=20, parent="content_group")
        from gui_setup import flow_setup
        dpg.add_button(label="Begin Setup", width=140, height=32,
                       callback=flow_setup, parent="content_group")
        return

    games   = load_games()
    n       = len(games)
    on_mach = sum(1 for g in games if get_local_config(g["id"]))

    dpg.add_text(f"{n} game{'s' if n != 1 else ''} synced  |  "
                 f"{on_mach} configured on this machine",
                 parent="content_group", color=(130, 130, 155))

    if warning:
        dpg.add_text(f"Warning: {warning}", parent="content_group",
                     color=(200, 150, 50), wrap=800)
    if info:
        dpg.add_text(info, parent="content_group", color=(45, 164, 78), wrap=800)

    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")

    if games:
        with dpg.table(
            parent="content_group",
            header_row=True, row_background=True, scrollY=True,
            borders_outerH=True, borders_innerV=True, height=-80,
            policy=dpg.mvTable_SizingStretchProp,
        ):
            dpg.add_table_column(label="Game",     width_stretch=True, init_width_or_weight=5.0)
            dpg.add_table_column(label="Platform", width_stretch=True, init_width_or_weight=2.0)
            dpg.add_table_column(label="Status",   width_stretch=True, init_width_or_weight=2.0)

            for g in games:
                mc   = get_local_config(g["id"])
                plat = mc.get("platform", "").capitalize() if mc else "Not assigned"
                with dpg.table_row():
                    dpg.add_text(g["name"])
                    dpg.add_text(plat)
                    if mc:
                        dpg.add_text("...", tag=f"_hstatus_{g['id']}",
                                     color=(130, 130, 155))
                    else:
                        dpg.add_text("--", color=(130, 130, 155))

        # Check sync status sequentially — dpg.set_frame_callback only holds one
        # callback per frame slot, so concurrent run_async calls overwrite each other.
        from sync import get_sync_status
        from config import log_err

        assigned = [g for g in games if get_local_config(g["id"])]

        def _run_chain(queue: list):
            if not queue:
                return
            g, rest = queue[0], queue[1:]
            gid = g["id"]
            tag = f"_hstatus_{gid}"

            cached = get_cached_status(gid)
            if cached is not None:
                if dpg.does_item_exist(tag):
                    dpg.set_value(tag, _STATUS_LABEL.get(cached, cached))
                    dpg.configure_item(tag, color=_STATUS_COLOR.get(cached, (130, 130, 155)))
                _run_chain(rest)
                return

            def _done(status):
                if dpg.does_item_exist(tag):
                    if not isinstance(status, str):
                        msg = status[0][0] if (isinstance(status, list) and status) else str(status)
                        dpg.set_value(tag, "Error")
                        dpg.configure_item(tag, color=_STATUS_COLOR["error"])
                        log_err(f"get_sync_status({gid}): {msg}")
                    else:
                        set_cached_status(gid, status)
                        dpg.set_value(tag, _STATUS_LABEL.get(status, status))
                        dpg.configure_item(tag, color=_STATUS_COLOR.get(status, (130, 130, 155)))
                _run_chain(rest)

            run_async(get_sync_status, (gid, g), _done)

        _run_chain(assigned)
    else:
        dpg.add_spacer(height=20, parent="content_group")
        dpg.add_text("No games configured yet.", parent="content_group")
        dpg.add_text("Use 'Add New Game Config' to create a config for a game.",
                     parent="content_group", color=(130, 130, 155))

    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=6, parent="content_group")
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label="Refresh Game List", width=150, height=32,
                       callback=pull_then_home)
        dpg.add_button(label="Sync Artwork", width=130, height=32,
                       callback=_flow_sync_art_deferred)


def _flow_sync_art_deferred():
    from gui_sync import flow_sync_art
    flow_sync_art()
