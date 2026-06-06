#!/usr/bin/env python3
"""ExternalGameSync — dearpygui management GUI entry point."""

from __future__ import annotations

import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

sys.path.insert(0, str(Path(__file__).parent))

from config import is_configured
from machine_config import migrate_from_games_json
from gui_common import (
    TITLE, WIN_W, WIN_H, MIN_W, MIN_H, SIDEBAR_W,
    _theme_nav_normal, _nav_btn_ids,
    setup_theme,
)
from gui_home import refresh_and_home, pull_then_home
from gui_setup import flow_setup
from gui_assign import flow_assign
from gui_add import flow_add
from gui_sync import flow_sync_art, flow_sync_all, flow_relink
from gui_install import flow_install
from gui_decky import flow_decky
from gui_community import flow_community


def _build_sidebar():
    dpg.add_text("External\nGameSync", parent="sidebar")
    dpg.add_separator(parent="sidebar")
    dpg.add_spacer(height=4, parent="sidebar")

    nav_items = [
        ("Home",           "home",     refresh_and_home),
        ("Assign Config",  "assign",   flow_assign),
        ("Add New Game Config",       "add",      flow_add),
        ("Sync Artwork",   "art",      flow_sync_art),
        ("Sync",           "sync_all", flow_sync_all),
        ("Relink Saves",   "relink",   flow_relink),
        ("Community",      "community", flow_community),
    ]
    if sys.platform != "win32":
        nav_items.insert(1, ("Install Game", "install", flow_install))
        nav_items.append(("Decky Plugin",   "decky",   flow_decky))
    for label, key, cmd in nav_items:
        bid = dpg.add_button(label=label, callback=cmd,
                             width=-1, height=30, parent="sidebar")
        dpg.bind_item_theme(bid, _theme_nav_normal)
        _nav_btn_ids[key] = bid

    dpg.add_separator(parent="sidebar")
    dpg.add_spacer(height=4, parent="sidebar")
    dpg.add_button(label="Setup / Reconfigure", callback=flow_setup,
                   width=-1, height=30, parent="sidebar")


def main():
    migrate_from_games_json()
    dpg.create_context()
    setup_theme()

    with dpg.window(tag="root_window", no_title_bar=True, no_move=True,
                    no_resize=True, no_scrollbar=True):
        with dpg.group(horizontal=True):
            with dpg.child_window(tag="sidebar", width=SIDEBAR_W, height=-1,
                                  no_scrollbar=False, border=False):
                pass
            with dpg.child_window(tag="content_pane", width=-1, height=-1, border=False):
                with dpg.group(tag="content_group"):
                    pass

    dpg.create_viewport(
        title=TITLE,
        width=WIN_W, height=WIN_H,
        min_width=MIN_W, min_height=MIN_H,
    )
    _base = Path(__file__).parent
    _icon = _base / ("icon.ico" if sys.platform == "win32" else "icon.png")
    if _icon.exists():
        try:
            dpg.set_viewport_small_icon(str(_icon))
            dpg.set_viewport_large_icon(str(_icon))
        except Exception:
            pass
    dpg.setup_dearpygui()
    dpg.set_primary_window("root_window", True)
    dpg.show_viewport()

    _build_sidebar()

    if not is_configured():
        flow_setup()
    else:
        pull_then_home()

    dpg.start_dearpygui()
    dpg.destroy_context()


if __name__ == "__main__":
    main()
