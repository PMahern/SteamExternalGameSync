"""
ExternalGameSync GUI -- sync saves, sync artwork, and relink symlinks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

from games import load_games
from machine_config import get_local_config
from sync import (
    rclone_sync_pull, rclone_sync_push, rclone_sync_pull_force,
    PULL_CONFLICT,
)
from steam import make_save_symlink, make_save_symlink_native
from artwork import push_art_to_nextcloud, pull_art_from_nextcloud
from gui_common import (
    set_nav_active, set_nav_enabled, clear_content, add_header, add_action_bar,
    add_table, run_async, show_progress, stop_progress, show_done, show_error,
)
from gui_home import require_configured, _STATUS_LABEL, _STATUS_COLOR
from gui_common import get_cached_status, set_cached_status, evict_cached_status


# ── Sync saves ────────────────────────────────────────────────────────────────

def flow_sync_all():
    if not require_configured():
        return
    set_nav_active("sync_all")

    all_games = load_games()
    games = [g for g in all_games if get_local_config(g["id"])]

    clear_content()
    add_header("Sync", "Sync individual games or all at once")

    if not games:
        dpg.add_spacer(height=20, parent="content_group")
        dpg.add_text("No games configured on this machine.", parent="content_group")
        dpg.add_text("Use 'Assign Config' to assign a game to this machine.",
                     parent="content_group", color=(130, 130, 155))
        return

    active_count = [0]  # in-progress individual syncs

    # ── UI state helpers ───────────────────────────────────────────────────────

    def _toggle_conflict_ui(gid: str, *, show: bool):
        if dpg.does_item_exist(f"_ss_conflict_{gid}"):
            dpg.configure_item(f"_ss_conflict_{gid}", show=show)
        if dpg.does_item_exist(f"_ss_btn_{gid}"):
            dpg.configure_item(f"_ss_btn_{gid}", show=not show)

    def _apply_result(gid: str, status_key: str, msg: str):
        if status_key == "conflict":
            set_cached_status(gid, "conflict")
            if dpg.does_item_exist(f"_ss_status_{gid}"):
                dpg.set_value(f"_ss_status_{gid}", "Conflict!")
                dpg.configure_item(f"_ss_status_{gid}", color=(220, 80, 60))
            for btn in (f"_ss_local_{gid}", f"_ss_cloud_{gid}"):
                if dpg.does_item_exist(btn):
                    dpg.configure_item(btn, enabled=True)
            _toggle_conflict_ui(gid, show=True)
        elif status_key == "ok":
            set_cached_status(gid, "in_sync")
            if dpg.does_item_exist(f"_ss_status_{gid}"):
                dpg.set_value(f"_ss_status_{gid}", msg)
                dpg.configure_item(f"_ss_status_{gid}", color=(45, 164, 78))
            _toggle_conflict_ui(gid, show=False)
        else:
            evict_cached_status(gid)
            if dpg.does_item_exist(f"_ss_status_{gid}"):
                dpg.set_value(f"_ss_status_{gid}", msg)
                dpg.configure_item(f"_ss_status_{gid}", color=(207, 34, 46))
            _toggle_conflict_ui(gid, show=False)

    # ── Individual sync ────────────────────────────────────────────────────────

    def _do_sync_one(game: dict):
        gid = game["id"]
        active_count[0] += 1
        if dpg.does_item_exist(f"_ss_btn_{gid}"):
            dpg.configure_item(f"_ss_btn_{gid}", enabled=False, label="...")
        if dpg.does_item_exist("_ss_all_btn"):
            dpg.configure_item("_ss_all_btn", enabled=False)
        if dpg.does_item_exist(f"_ss_status_{gid}"):
            dpg.set_value(f"_ss_status_{gid}", "Syncing...")
            dpg.configure_item(f"_ss_status_{gid}", color=(100, 180, 255))

        def _work():
            try:
                ok1, msg1 = rclone_sync_pull(gid, game)
                if msg1 == PULL_CONFLICT:
                    return gid, "conflict", "Conflict!"
                if not ok1:
                    return gid, "error", "Pull failed"
                ok2, _ = rclone_sync_push(gid, game)
                return gid, "ok" if ok2 else "error", "Synced" if ok2 else "Push failed"
            except Exception as exc:
                return gid, "error", f"Error: {exc}"

        def _done(res):
            active_count[0] = max(0, active_count[0] - 1)
            if isinstance(res, list):
                msg = res[0][0] if res else "Error"
                _apply_result(gid, "error", f"Error: {msg}")
                if dpg.does_item_exist(f"_ss_btn_{gid}"):
                    dpg.configure_item(f"_ss_btn_{gid}", enabled=True, label="Sync")
            else:
                g_id, status_key, msg = res
                _apply_result(g_id, status_key, msg)
                if status_key != "conflict" and dpg.does_item_exist(f"_ss_btn_{g_id}"):
                    dpg.configure_item(f"_ss_btn_{g_id}", enabled=True, label="Sync")
            if active_count[0] == 0 and dpg.does_item_exist("_ss_all_btn"):
                dpg.configure_item("_ss_all_btn", enabled=True)

        run_async(_work, (), _done)

    # ── Sync all ───────────────────────────────────────────────────────────────

    def _do_sync_all():
        for g in games:
            gid = g["id"]
            if dpg.does_item_exist(f"_ss_btn_{gid}"):
                dpg.configure_item(f"_ss_btn_{gid}", enabled=False, label="...", show=True)
            if dpg.does_item_exist(f"_ss_conflict_{gid}"):
                dpg.configure_item(f"_ss_conflict_{gid}", show=False)
            if dpg.does_item_exist(f"_ss_status_{gid}"):
                dpg.set_value(f"_ss_status_{gid}", "Waiting...")
                dpg.configure_item(f"_ss_status_{gid}", color=(130, 130, 155))
        if dpg.does_item_exist("_ss_all_btn"):
            dpg.configure_item("_ss_all_btn", enabled=False, label="Syncing...")
        set_nav_enabled(False)

        def _work():
            results: dict[str, tuple[str, str]] = {}
            for g in games:
                gid = g["id"]
                try:
                    ok1, msg1 = rclone_sync_pull(gid, g)
                    if msg1 == PULL_CONFLICT:
                        results[gid] = ("conflict", "Conflict!")
                        continue
                    if not ok1:
                        results[gid] = ("error", "Pull failed")
                        continue
                    ok2, _ = rclone_sync_push(gid, g)
                    results[gid] = ("ok" if ok2 else "error",
                                    "Synced" if ok2 else "Push failed")
                except Exception as exc:
                    results[gid] = ("error", f"Error: {exc}")
            return results

        def _done(results):
            set_nav_enabled(True)
            if isinstance(results, list):
                for g in games:
                    gid = g["id"]
                    _apply_result(gid, "error", "Error")
                    if dpg.does_item_exist(f"_ss_btn_{gid}"):
                        dpg.configure_item(f"_ss_btn_{gid}", enabled=True, label="Sync")
            else:
                for g in games:
                    gid = g["id"]
                    status_key, msg = results.get(gid, ("error", "No result"))
                    _apply_result(gid, status_key, msg)
                    if status_key != "conflict" and dpg.does_item_exist(f"_ss_btn_{gid}"):
                        dpg.configure_item(f"_ss_btn_{gid}", enabled=True, label="Sync")
            if dpg.does_item_exist("_ss_all_btn"):
                dpg.configure_item("_ss_all_btn", enabled=True, label="Sync All")

        run_async(_work, (), _done)

    # ── Conflict resolution ────────────────────────────────────────────────────

    def _do_resolve(game: dict, keep: str):
        gid = game["id"]
        for btn in (f"_ss_local_{gid}", f"_ss_cloud_{gid}"):
            if dpg.does_item_exist(btn):
                dpg.configure_item(btn, enabled=False)
        if dpg.does_item_exist(f"_ss_status_{gid}"):
            dpg.set_value(f"_ss_status_{gid}", "Resolving...")
            dpg.configure_item(f"_ss_status_{gid}", color=(100, 180, 255))

        def _work():
            try:
                ok, _ = rclone_sync_pull_force(gid, keep, game)
                if not ok:
                    return "error", "Resolve failed"
                if keep == "local":
                    ok2, _ = rclone_sync_push(gid, game)
                    if not ok2:
                        return "error", "Resolve: push failed"
                return "ok", "Resolved"
            except Exception as exc:
                return "error", f"Error: {exc}"

        def _done(res):
            if isinstance(res, list):
                status_key, msg = "error", (res[0][0] if res else "Error")
            else:
                status_key, msg = res
            _apply_result(gid, status_key, msg)
            if status_key != "conflict" and dpg.does_item_exist(f"_ss_btn_{gid}"):
                dpg.configure_item(f"_ss_btn_{gid}", enabled=True, label="Sync")

        run_async(_work, (), _done)

    # ── Build game list ────────────────────────────────────────────────────────

    with dpg.child_window(parent="content_group", height=-50, width=-1):
        for game in games:
            gid  = game["id"]
            mc   = get_local_config(gid)
            plat = mc.get("platform", "").capitalize() if mc else ""

            with dpg.group(horizontal=False):
                with dpg.group(horizontal=True):
                    dpg.add_text(game["name"])
                    dpg.add_text("...", tag=f"_ss_status_{gid}",
                                 color=(130, 130, 155))
                    if plat:
                        dpg.add_text(f"({plat})", color=(90, 90, 110))
                    dpg.add_button(
                        label="Sync", tag=f"_ss_btn_{gid}",
                        width=72, height=22,
                        callback=lambda s, a, u: _do_sync_one(u),
                        user_data=game,
                    )
                with dpg.group(horizontal=True, tag=f"_ss_conflict_{gid}", show=False):
                    dpg.add_text("Conflict -- keep:", indent=16)
                    dpg.add_button(
                        label="Local", tag=f"_ss_local_{gid}",
                        width=80, height=22,
                        callback=lambda s, a, u: _do_resolve(u, "local"),
                        user_data=game,
                    )
                    dpg.add_button(
                        label="Cloud", tag=f"_ss_cloud_{gid}",
                        width=80, height=22,
                        callback=lambda s, a, u: _do_resolve(u, "remote"),
                        user_data=game,
                    )
            dpg.add_separator()

    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")
    dpg.add_button(label="Sync All", tag="_ss_all_btn", width=130, height=32,
                   callback=_do_sync_all, parent="content_group")

    # ── Pre-check sync status (sequential to avoid frame-callback collision) ──

    from sync import get_sync_status
    from config import log_err

    def _run_precheck(queue: list):
        if not queue:
            return
        g, rest = queue[0], queue[1:]
        gid = g["id"]
        tag = f"_ss_status_{gid}"

        cached = get_cached_status(gid)
        if cached is not None:
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, _STATUS_LABEL.get(cached, cached))
                dpg.configure_item(tag, color=_STATUS_COLOR.get(cached, (130, 130, 155)))
            _run_precheck(rest)
            return

        def _precheck_done(status):
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
            _run_precheck(rest)

        run_async(get_sync_status, (gid, g), _precheck_done)

    _run_precheck(list(games))


# ── Sync artwork ──────────────────────────────────────────────────────────────

def flow_sync_art():
    if not require_configured():
        return
    set_nav_active("art")
    _art_s1_pick_game()


def _art_s1_pick_game():
    all_games = load_games()
    games = [g for g in all_games if get_local_config(g["id"])]
    if not games:
        show_error("No Games", "No game configs found for this machine.")
        return
    clear_content()
    add_header("Sync Artwork", "Step 1 -- Pick a game")

    rows   = [[g["id"], g["name"]] for g in games]
    id_map = {g["id"]: g for g in games}
    state  = add_table(["ID", "Name"], rows, col_weights=[2, 4])

    def _next():
        sel = state["selected"]
        if not sel:
            return
        game = id_map.get(sel[0].strip())
        if not game:
            return
        mc    = get_local_config(game["id"])
        appid = mc.get("app_id") if mc else None
        if not appid:
            show_error("Not Assigned",
                       f"'{game['name']}' is not assigned on this machine. "
                       "Run Assign Config first.")
            return
        _art_s2_direction(game, appid)

    add_action_bar("Next -->", _next)


def _art_s2_direction(game: dict, appid: str):
    clear_content()
    add_header("Sync Artwork", f"Step 2 -- What to do with '{game['name']}' artwork?")

    rows  = [["push", "Upload this machine's artwork to cloud storage"],
             ["pull", "Download artwork from cloud storage to this machine"]]
    state = add_table(["Action", "Description"], rows, col_weights=[1, 4])

    def _next():
        sel = state["selected"]
        if not sel:
            return
        _art_run(game, appid, sel[0])

    add_action_bar("Go -->", _next, back_cb=_art_s1_pick_game)


def _art_run(game: dict, appid: str, direction: str):
    verb = "Uploading" if direction == "push" else "Downloading"
    show_progress(f"{verb} artwork for '{game['name']}'...")

    def _work():
        if direction == "push":
            results = push_art_to_nextcloud(game["id"], appid)
        else:
            results = pull_art_from_nextcloud(game["id"], appid)
        done = [t for t, ok in results.items() if ok]
        fail = [t for t, ok in results.items() if not ok]
        return done, fail, direction

    def _done(res):
        stop_progress()
        done, fail, direction = res
        past  = "Uploaded" if direction == "push" else "Downloaded"
        lines = []
        if done:
            lines.append(f"OK  {past}: {', '.join(done)}")
        if fail:
            lines.append(f"X  Failed: {', '.join(fail)}")
        if not done and not fail:
            lines = ["No artwork found."]
        if done and direction == "pull":
            lines += ["", "Restart Steam for artwork changes to take effect."]
        show_done("Artwork sync complete", lines, success=not fail)

    run_async(_work, (), _done)


# ── Relink ────────────────────────────────────────────────────────────────────

def flow_relink():
    if not require_configured():
        return
    set_nav_active("relink")
    clear_content()
    add_header("Relink Saves", "Recreate save symlinks after a reinstall")

    dpg.add_text(
        "On Linux, each game's save folder is a symlink inside the Proton prefix\n"
        "pointing to the shared sync directory. If you reinstall a game or Steam,\n"
        "those symlinks may be missing or broken.",
        parent="content_group", wrap=800,
    )
    dpg.add_spacer(height=10, parent="content_group")
    dpg.add_text(
        "This operation recreates the symlinks for every game assigned to this\n"
        "machine. On Windows it verifies that the configured save paths exist\n"
        "instead (no symlinks are used there).",
        parent="content_group", wrap=800, color=(130, 130, 155),
    )
    dpg.add_spacer(height=20, parent="content_group")
    dpg.add_button(label="Relink Saves", width=140, height=32,
                   callback=_run_relink, parent="content_group")


def _run_relink():
    all_games = load_games()
    games = [g for g in all_games if get_local_config(g["id"])]
    if not games:
        show_error("No Games", "No games configured on this machine.")
        return
    show_progress("Relinking save symlinks...")

    def _work():
        results = []
        for g in games:
            mc = get_local_config(g["id"])
            if mc.get("platform") == "windows":
                sp  = mc.get("save_path", "")
                ok  = bool(sp and Path(sp).exists())
                msg = sp if ok else f"Not found: {sp}"
                results.append((g["name"], ok, msg))
            elif mc.get("platform") == "linux_native":
                sp = mc.get("save_path", "")
                if sp:
                    ok, msg = make_save_symlink_native(g["id"], Path(sp))
                else:
                    ok, msg = False, "No save path in machine config"
                results.append((g["name"], ok, msg))
            else:
                ok, msg = make_save_symlink(g["id"], mc["app_id"], g["save_path"])
                results.append((g["name"], ok, msg))
        return results

    def _done(results):
        stop_progress()
        lines  = [("OK  " if ok else "X  ") + f"{n}: {m}" for n, ok, m in results]
        all_ok = all(ok for _, ok, _ in results)
        show_done("Relink complete", lines, success=all_ok)

    run_async(_work, (), _done)
