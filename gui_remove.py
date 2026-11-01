"""
ExternalGameSync GUI -- unassign a game config from this machine, or delete it everywhere.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from games import load_games
from machine_config import get_local_config
from remove import unassign_game, delete_game_everywhere, orphaned_local_games
from gui_common import (
    set_nav_active, clear_content, add_header, add_action_bar, add_table,
    run_async, show_progress, stop_progress, show_done, shutdown_steam_sync,
    evict_cached_status,
)
from gui_home import require_configured


def flow_remove():
    if not require_configured():
        return
    set_nav_active("remove")
    _remove_s1_pick()


def _assigned_label(game_id: str) -> str:
    mc = get_local_config(game_id)
    if not mc:
        return "Not assigned"
    kind = "native Steam" if mc.get("native_steam") else "shortcut"
    return f"Assigned ({mc.get('platform', '?')}, {kind}, app {mc.get('app_id', '?')})"


# ── Step 1: pick a game ───────────────────────────────────────────────────────

def _remove_s1_pick():
    clear_content()
    add_header("Remove / Unassign", "Pick a game config")

    games   = load_games()
    orphans = orphaned_local_games()
    if not games and not orphans:
        dpg.add_text("No game configs found.", parent="content_group", color=(207, 34, 46))
        return

    rows = [[g["id"], g["name"], _assigned_label(g["id"])] for g in games]
    rows += [[g["id"], g["name"], "Assigned, but config was deleted"] for g in orphans]
    id_map = {g["id"]: (g, False) for g in games}
    id_map.update({g["id"]: (g, True) for g in orphans})
    state = add_table(["ID", "Name", "This machine"], rows,
                      col_weights=[2, 3, 4], filterable=True)

    def _next():
        sel = state["selected"]
        if sel and sel[0] in id_map:
            _remove_s2_options(*id_map[sel[0]])

    add_action_bar("Next -->", _next)


# ── Step 2: choose unassign vs delete ─────────────────────────────────────────

def _remove_s2_options(game_cfg: dict, orphaned: bool):
    clear_content()
    add_header("Remove / Unassign", f"{game_cfg['name']}  ({game_cfg['id']})")
    assigned = get_local_config(game_cfg["id"]) is not None
    grey = (130, 130, 155)

    dpg.add_text(f"This machine: {_assigned_label(game_cfg['id'])}", parent="content_group")
    dpg.add_spacer(height=10, parent="content_group")

    dpg.add_text("Unassign from this machine", parent="content_group")
    dpg.add_text("Restores the Steam shortcut / launch options (only if they still point at "
                 "this config), removes the launch wrapper and save symlink, and forgets "
                 "this machine's settings. Save files and the cloud config are kept.",
                 parent="content_group", color=grey, wrap=800)
    dpg.add_button(label="Unassign", width=140, height=32, enabled=assigned,
                   callback=lambda: _run(game_cfg, delete=False), parent="content_group")
    if not assigned:
        dpg.add_text("Not assigned on this machine.", parent="content_group", color=grey)

    if not orphaned:
        dpg.add_spacer(height=14, parent="content_group")
        dpg.add_separator(parent="content_group")
        dpg.add_spacer(height=6, parent="content_group")
        dpg.add_text("Delete config everywhere", parent="content_group")
        dpg.add_text("Unassigns it here, and removes the config from the cloud so every "
                     "machine drops it. Other machines keep launching the game, just without "
                     "syncing, until it's unassigned there too.",
                     parent="content_group", color=grey, wrap=800)
        dpg.add_checkbox(label="Also remove cloud saves and artwork "
                               "(saves are moved to saves_backup, not erased)",
                         tag="_remove_cloud_data", parent="content_group")
        dpg.add_button(label="Delete...", width=140, height=32,
                       callback=lambda: _remove_s3_confirm(
                           game_cfg, dpg.get_value("_remove_cloud_data")),
                       parent="content_group")

    dpg.add_spacer(height=10, parent="content_group")
    add_action_bar("<-- Back", _remove_s1_pick)


# ── Step 3: confirm delete ────────────────────────────────────────────────────

def _remove_s3_confirm(game_cfg: dict, cloud_data: bool):
    clear_content()
    add_header("Delete Game Config", "This cannot be undone from the app")
    dpg.add_text(f"Delete '{game_cfg['name']}' ({game_cfg['id']}) from every machine?",
                 parent="content_group", color=(207, 34, 46))
    if cloud_data:
        dpg.add_text("Cloud saves will be moved to saves_backup and artwork removed.",
                     parent="content_group", color=(207, 34, 46))
    add_action_bar("Delete", lambda: _run(game_cfg, delete=True, cloud_data=cloud_data),
                   back_cb=lambda: _remove_s2_options(game_cfg, False))


# ── Work ──────────────────────────────────────────────────────────────────────

def _run(game_cfg: dict, delete: bool, cloud_data: bool = False):
    name = game_cfg["name"]
    mc   = get_local_config(game_cfg["id"])
    show_progress(f"{'Deleting' if delete else 'Unassigning'} '{name}'...")

    def _work():
        # Steam rewrites localconfig.vdf on exit, so it must be closed first
        if mc and mc.get("native_steam"):
            shutdown_steam_sync()
        if delete:
            return delete_game_everywhere(game_cfg, remove_cloud_data=cloud_data)
        return unassign_game(game_cfg)

    def _done(log):
        evict_cached_status(game_cfg["id"])
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        verb   = "deleted" if delete else "unassigned"
        show_done(f"'{name}' {verb}" if all_ok else f"{verb.capitalize()} with warnings",
                  lines, success=all_ok, show_restart_steam=mc is not None)

    run_async(_work, (), _done)
