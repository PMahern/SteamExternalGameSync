"""
ExternalGameSync GUI -- edit an existing game config (exe/save paths, options).
"""

from __future__ import annotations

import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

from machine_config import get_local_config
from games import load_games, save_games
import rclone as rclone_mod
from steam import (
    proton_drive_c, resolve_exe_path, resolve_save_path,
    make_save_symlink, make_save_symlink_native,
)
from gui_common import (
    set_nav_active, clear_content, add_header, add_action_bar,
    add_table, add_path_row, run_async, show_progress, stop_progress,
    show_done, refresh_prefix_tree,
)
from gui_home import require_configured
import ludusavi


def flow_edit():
    if not require_configured():
        return
    set_nav_active("edit")
    _edit_s1_pick()


# ── Step 1: pick a game ───────────────────────────────────────────────────────

def _edit_s1_pick():
    clear_content()
    add_header("Edit Game Config", "Pick a game to edit")

    games = load_games()
    if not games:
        dpg.add_text("No game configs found.", parent="content_group", color=(207, 34, 46))
        return

    rows    = [[g["id"], g["name"], g.get("exe_path", ""), g.get("save_path", "")]
               for g in games]
    id_map  = {g["id"]: g for g in games}
    state   = add_table(["ID", "Name", "Exe Path", "Save Path"],
                        rows, col_weights=[2, 2, 3, 3], filterable=True)

    def _next():
        sel = state["selected"]
        if not sel:
            return
        game_cfg = id_map.get(sel[0])
        if game_cfg:
            _edit_s2_form(game_cfg)

    add_action_bar("Edit -->", _next)


# ── Step 2: edit form ─────────────────────────────────────────────────────────

def _edit_s2_form(game_cfg: dict):
    import os
    mc           = get_local_config(game_cfg["id"])
    platform     = mc.get("platform") if mc else None
    app_id       = mc.get("app_id") if mc else None
    native_steam = bool(mc.get("native_steam")) if mc else False
    linux_native = platform == "linux_native"

    clear_content()
    add_header("Edit Game Config", f"Editing: {game_cfg['name']}")

    exe_tag    = "_edit_exe"
    save_tag   = "_edit_save"
    env_tag    = "_edit_env"
    filter_tag = "_edit_filter"

    # Resolve stored paths to absolute for pre-filling the pickers
    exe_prefill = save_prefill = ""
    if platform == "linux" and app_id:
        try:
            ep = resolve_exe_path(app_id, game_cfg.get("exe_path", ""))
            exe_prefill = str(ep.resolve()) if ep.exists() else game_cfg.get("exe_path", "")
        except Exception:
            exe_prefill = game_cfg.get("exe_path", "")
        try:
            sp = resolve_save_path(app_id, game_cfg.get("save_path", ""))
            save_prefill = str(sp.resolve()) if sp.exists() else game_cfg.get("save_path", "")
        except Exception:
            save_prefill = game_cfg.get("save_path", "")
    elif platform == "linux_native":
        exe_prefill  = ""
        save_prefill = ((mc.get("save_path", "") if mc else "")
                        or ludusavi.expand_linux_home_path(game_cfg.get("save_path", "")))
    elif platform == "windows":
        exe_prefill  = (mc.get("exe_path", "") if mc else "") or game_cfg.get("exe_path", "")
        save_prefill = (mc.get("save_path", "") if mc else "") or game_cfg.get("save_path", "")
    else:
        # No local machine config -- show raw stored values
        exe_prefill  = game_cfg.get("exe_path", "")
        save_prefill = game_cfg.get("save_path", "")

    # ---- browser start dirs ----

    def _exe_start():
        v = dpg.get_value(exe_tag).strip() if dpg.does_item_exist(exe_tag) else ""
        if v and Path(v).parent.exists():
            return str(Path(v).parent)
        if sys.platform == "win32":
            return os.environ.get("PROGRAMFILES", r"C:\Program Files")
        if app_id and platform == "linux":
            pf = proton_drive_c(app_id) / "Program Files"
            return str(pf) if pf.exists() else str(proton_drive_c(app_id))
        return str(Path.home())

    def _save_start():
        v = dpg.get_value(save_tag).strip() if dpg.does_item_exist(save_tag) else ""
        if v and Path(v).parent.exists():
            return str(Path(v).parent)
        if sys.platform == "win32":
            return os.environ.get("APPDATA", str(Path.home()))
        if linux_native:
            return str(Path.home())
        if app_id and platform == "linux":
            r = proton_drive_c(app_id) / "users" / "steamuser" / "AppData" / "Roaming"
            return str(r) if r.exists() else str(proton_drive_c(app_id))
        return str(Path.home())

    # ---- stored values (read-only info) ----

    dpg.add_text("Currently stored in cloud config:", parent="content_group",
                 color=(130, 130, 155))
    dpg.add_text(f"  exe:  {game_cfg.get('exe_path', '(none)')}", parent="content_group",
                 color=(110, 110, 135))
    dpg.add_text(f"  save: {game_cfg.get('save_path', '(none)')}", parent="content_group",
                 color=(110, 110, 135))
    dpg.add_spacer(height=8, parent="content_group")

    # ---- path pickers ----

    dpg.add_text("Paths", parent="content_group")
    if native_steam or linux_native:
        dpg.add_text("Executable: provided by Steam at launch -- no path needed.",
                     parent="content_group", color=(130, 130, 155))
        dpg.add_input_text(tag=exe_tag, default_value="", width=1, show=False,
                           parent="content_group")
    else:
        add_path_row("Executable", exe_tag, False, _exe_start)
        if exe_prefill:
            dpg.set_value(exe_tag, exe_prefill)

    add_path_row("Save folder", save_tag, True, _save_start)
    if save_prefill:
        dpg.set_value(save_tag, save_prefill)

    # Proton prefix folder tree for reference when on Linux with a known prefix
    if sys.platform != "win32" and app_id and platform == "linux":
        dpg.add_spacer(height=4, parent="content_group")
        dpg.add_text("Proton prefix folders:", parent="content_group",
                     color=(130, 130, 155))
        dpg.add_group(tag="_edit_pfx_tree", parent="content_group")
        refresh_prefix_tree(app_id, "_edit_pfx_tree", height=100)

    # ---- options ----

    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text("Options", parent="content_group")

    if sys.platform != "win32":
        with dpg.group(horizontal=True, parent="content_group"):
            dpg.add_text("Env vars")
            dpg.add_input_text(tag=env_tag, default_value=game_cfg.get("env_vars", ""),
                               hint="DXVK_ASYNC=1 ...", width=-1)
    else:
        dpg.add_input_text(tag=env_tag, default_value="", width=1, show=False,
                           parent="content_group")

    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_text("Save filter")
        dpg.add_input_text(tag=filter_tag, default_value=game_cfg.get("save_filter", ""),
                           hint="e.g. save_*/**  (blank = sync whole folder)", width=-1)

    def _run():
        exe_p  = dpg.get_value(exe_tag).strip() if dpg.does_item_exist(exe_tag) else ""
        save_p = dpg.get_value(save_tag).strip()
        if (not exe_p and not native_steam and not linux_native) or not save_p:
            return
        _edit_run(
            game_cfg,
            exe_real=Path(exe_p) if exe_p else None,
            save_real=Path(save_p),
            env_vars=dpg.get_value(env_tag).strip() if dpg.does_item_exist(env_tag) else "",
            save_filter=dpg.get_value(filter_tag).strip(),
            platform=platform,
            app_id=app_id,
            native_steam=native_steam,
            linux_native=linux_native,
        )

    add_action_bar("Save Config -->", _run, back_cb=_edit_s1_pick)


# ── Save logic ────────────────────────────────────────────────────────────────

def _edit_run(game_cfg: dict, exe_real: Path | None, save_real: Path,
              env_vars: str, save_filter: str,
              platform: str | None, app_id: str | None,
              native_steam: bool = False, linux_native: bool = False):
    game_id   = game_cfg["id"]
    game_name = game_cfg["name"]
    show_progress(f"Saving config for '{game_name}'...")

    def _work():
        log      = []
        exe_rel  = ""
        save_rel = ""

        if sys.platform == "win32":
            exe_rel  = ludusavi.normalize_path_for_storage(exe_real) if exe_real else ""
            save_rel = ludusavi.normalize_path_for_storage(save_real)
        elif linux_native:
            save_rel = ludusavi.normalize_path_for_storage(save_real.resolve())
        elif app_id:
            drive_c = proton_drive_c(app_id)
            if native_steam:
                exe_rel = ""
                try:
                    save_rel = str(save_real.resolve().relative_to(drive_c.resolve()))
                except ValueError:
                    return [(f"Save path is outside Proton prefix: {drive_c.resolve()}", False)]
            else:
                try:
                    if exe_real:
                        exe_rel = str(exe_real.resolve().relative_to(drive_c.resolve()))
                    save_rel = str(save_real.resolve().relative_to(drive_c.resolve()))
                except ValueError:
                    return [(f"Path is outside Proton prefix: {drive_c.resolve()}", False)]
            save_rel = ludusavi.normalize_path_for_storage(save_rel)
        else:
            # No local machine config: normalize as-is (best effort)
            exe_rel  = ludusavi.normalize_path_for_storage(exe_real) if exe_real else ""
            save_rel = ludusavi.normalize_path_for_storage(save_real)

        rclone_mod.rclone_pull_games_json()
        all_g   = load_games()
        updated = False
        for g in all_g:
            if g["id"] == game_id:
                if exe_rel:
                    g["exe_path"] = exe_rel
                g["save_path"]   = save_rel
                g["save_filter"] = save_filter
                if sys.platform != "win32":
                    g["env_vars"] = env_vars
                updated = True
                break

        if not updated:
            return [("Game not found in local config -- pull may be needed.", False)]

        save_games(all_g)
        log.append(("Config updated", True))

        ok_push, msg_push = rclone_mod.rclone_push_games_json()
        log.append(("Config pushed to cloud" if ok_push
                    else f"Cloud push failed: {msg_push}", ok_push))

        # Recreate symlink on Linux when we have an app_id
        if sys.platform != "win32" and app_id:
            if linux_native:
                ok_lnk, msg_lnk = make_save_symlink_native(game_id, save_real.resolve())
            else:
                ok_lnk, msg_lnk = make_save_symlink(game_id, app_id, save_rel)
            log.append((f"Save symlink {'updated' if ok_lnk else 'failed: ' + msg_lnk}",
                        ok_lnk))

        return log

    def _done(log):
        stop_progress()
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        show_done(f"'{game_name}' updated!" if all_ok else "Updated with warnings",
                  lines, success=all_ok)

    run_async(_work, (), _done)
