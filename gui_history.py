"""
ExternalGameSync GUI -- browse a game's save backups and restore one everywhere.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from games import load_games
from machine_config import get_local_config
from sync import list_save_history, restore_save_backup, history_keep
from gui_common import (
    set_nav_active, clear_content, add_header, add_action_bar, add_table,
    run_async, show_progress, stop_progress, show_done, evict_cached_status,
)
from gui_home import require_configured

_GREY = (130, 130, 155)
_RED  = (207, 34, 46)


def flow_history():
    if not require_configured():
        return
    set_nav_active("history")
    _history_s1_pick()


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _fmt_when(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"


# ── Step 1: pick a game ───────────────────────────────────────────────────────

def _history_s1_pick():
    clear_content()
    add_header("Save History", "Pick a game to see its save backups")

    games = [g for g in load_games() if get_local_config(g["id"])]
    if not games:
        dpg.add_text("No games are assigned on this machine.", parent="content_group", color=_RED)
        return

    by_id = {g["id"]: g for g in games}
    state = add_table(["ID", "Name"], [[g["id"], g["name"]] for g in games],
                      col_weights=[2, 4], filterable=True)

    def _next():
        sel = state["selected"]
        if sel and sel[0] in by_id:
            _history_s2_load(by_id[sel[0]])

    add_action_bar("Next -->", _next)


# ── Step 2: list backups ──────────────────────────────────────────────────────

def _history_s2_load(game: dict):
    show_progress(f"Loading backups for '{game['name']}'...")
    run_async(list_save_history, (game["id"],), lambda res: _history_s2_list(game, res))


def _history_s2_list(game: dict, res):
    stop_progress()
    clear_content()
    add_header("Save History", f"{game['name']}  ({game['id']})")

    if isinstance(res, list):  # run_async error shape
        dpg.add_text(f"Could not list backups: {res[0][0] if res else '?'}",
                     parent="content_group", color=_RED)
        add_action_bar("<-- Back", _history_s1_pick)
        return
    entries, cloud_ok = res

    dpg.add_text("A backup is taken of whatever gets replaced: the cloud copy before each "
                 f"upload, and this machine's copy before a download overwrites unsynced "
                 f"changes. The {history_keep()} newest of each are kept. Pick one to restore "
                 "it on this machine and the cloud; other machines get it on their next launch.",
                 parent="content_group", color=_GREY, wrap=800)
    if not cloud_ok:
        dpg.add_text("Could not reach cloud storage -- only local backups are shown.",
                     parent="content_group", color=_RED)
    dpg.add_spacer(height=4, parent="content_group")

    if not entries:
        dpg.add_text("No backups yet.", parent="content_group")
        add_action_bar("<-- Back", _history_s1_pick)
        return

    rows = []
    for i, e in enumerate(entries, 1):
        where = "Cloud" if e["source"] == "cloud" else "This machine"
        if e["deleted"]:
            where += " (deleted config)"
        rows.append([str(i), _fmt_when(e["when"]), where, e["files"],
                     _fmt_size(e["size"]), _fmt_when(e["newest_file"])])
    state = add_table(["#", "Backed up", "Stored on", "Files", "Size", "Newest save file"],
                      rows, col_weights=[1, 3, 3, 1, 2, 3])

    def _next():
        sel = state["selected"]
        if sel:
            _history_s3_confirm(game, entries[int(sel[0]) - 1])

    add_action_bar("Restore...", _next, back_cb=_history_s1_pick)


# ── Step 3: confirm ───────────────────────────────────────────────────────────

def _history_s3_confirm(game: dict, entry: dict):
    clear_content()
    add_header("Restore Saves", f"{game['name']}  ({game['id']})")
    where = "cloud storage" if entry["source"] == "cloud" else "this machine"
    dpg.add_text(f"Restore the backup from {_fmt_when(entry['when'])} (stored on {where}, "
                 f"{entry['files']} files, {_fmt_size(entry['size'])})?",
                 parent="content_group")
    dpg.add_spacer(height=6, parent="content_group")
    dpg.add_text("This replaces the saves on this machine and in the cloud. The restored "
                 "files get the current time as their timestamp, so every other machine "
                 "treats them as the newest saves and downloads them on its next launch.",
                 parent="content_group", color=_GREY, wrap=800)
    dpg.add_text("Your current saves (here and in the cloud) are backed up first, so this "
                 "can be undone from this page.",
                 parent="content_group", color=_GREY, wrap=800)
    dpg.add_spacer(height=6, parent="content_group")
    dpg.add_text("Close the game on every machine before restoring.",
                 parent="content_group", color=_RED)
    add_action_bar("Restore", lambda: _run(game, entry),
                   back_cb=lambda: _history_s2_load(game))


# ── Work ──────────────────────────────────────────────────────────────────────

def _run(game: dict, entry: dict):
    show_progress(f"Restoring saves for '{game['name']}'...")

    def _done(log):
        evict_cached_status(game["id"])
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        show_done("Saves restored" if all_ok else "Restore failed", lines, success=all_ok)

    run_async(restore_save_backup, (game["id"], entry, game), _done)
