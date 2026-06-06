"""
ExternalGameSync GUI — create a new game config from a Steam shortcut or native Steam game.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

from machine_config import get_local_config, set_local_config
from games import load_games, save_games, game_id_from_name, hash_file
import rclone as rclone_mod
from sync import rclone_sync_pull, rclone_sync_push
from steam import (
    read_shortcuts, get_non_steam_games, update_shortcut_launch,
    proton_drive_c, make_save_symlink,
    list_steam_games, update_native_game_launch, find_steam_game_dir,
)
from gui_common import (
    SAVESYNC_BIN, set_nav_active, clear_content, add_header, add_action_bar,
    add_table, add_path_row, run_async, show_progress, stop_progress, show_done, show_error,
    find_proton_prefix_for_ns_exe, list_proton_prefixes, refresh_prefix_tree,
    shutdown_steam_sync,
)
from gui_home import require_configured
import ludusavi


def flow_add():
    if not require_configured():
        return
    set_nav_active("add")
    _add_s1_type()


# ── Step 1: game type picker ──────────────────────────────────────────────────

def _add_s1_type():
    clear_content()
    add_header("Add New Game Config", "Step 1 -- Choose how this game is installed")

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_text(
        "Non-Steam Shortcut",
        parent="content_group",
    )
    dpg.add_text(
        "A game added to Steam via Games > Add a Non-Steam Game.\n"
        "Gets its own Proton prefix separate from any Steam purchase.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )
    dpg.add_button(
        label="Non-Steam Shortcut  -->", width=240, height=32,
        callback=_add_s1_shortcut,
        parent="content_group",
    )

    dpg.add_spacer(height=16, parent="content_group")
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=16, parent="content_group")

    dpg.add_text(
        "Native Steam Game",
        parent="content_group",
    )
    dpg.add_text(
        "A game you bought on Steam that lacks working cloud saves (e.g. Dead Space 2).\n"
        "Uses the game's own Proton prefix and sets its Steam launch options.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )
    dpg.add_button(
        label="Native Steam Game  -->", width=240, height=32,
        callback=_add_s1_native,
        parent="content_group",
    )


# ── Step 1a: pick non-Steam shortcut ─────────────────────────────────────────

def _add_s1_shortcut():
    shortcuts_data, vdf_path = read_shortcuts()
    if not shortcuts_data:
        show_error("Shortcuts Unavailable", "Could not read Steam shortcuts.vdf.")
        return
    ns_games = get_non_steam_games(shortcuts_data)
    if not ns_games:
        show_error("No Shortcuts", "No non-Steam games found. Add one in Steam first.")
        return

    clear_content()
    add_header("Add New Game Config", "Step 1 -- Pick the Steam shortcut for this game")

    rows    = [[g["name"], g["exe"]] for g in ns_games]
    idx_map = {g["name"]: g for g in ns_games}
    state   = add_table(["Shortcut Name", "Executable"], rows, col_weights=[2, 3],
                        filterable=True)

    def _next():
        sel = state["selected"]
        if not sel:
            return
        ns = idx_map.get(sel[0])
        if ns:
            _add_s2_details(ns, shortcuts_data, vdf_path, native_steam=False,
                            native_app_id=None)

    add_action_bar("Next -->", _next, back_cb=_add_s1_type)


# ── Step 1b: pick native Steam game ──────────────────────────────────────────

def _add_s1_native():
    clear_content()
    add_header("Add New Game Config", "Step 1 -- Pick the installed Steam game")

    dpg.add_text(
        "Loading installed games...", tag="_native_loading",
        parent="content_group", color=(130, 130, 155),
    )

    state = {"games": [], "table": None}

    def _load():
        return list_steam_games()

    def _done(games):
        if dpg.does_item_exist("_native_loading"):
            dpg.delete_item("_native_loading")
        if not games:
            dpg.add_text("No installed Steam games found.", parent="content_group",
                         color=(207, 34, 46))
            return
        state["games"] = games
        rows = [[g["app_id"], g["name"]] for g in games]
        tbl  = add_table(["App ID", "Game Name"], rows, col_weights=[1, 4],
                         filterable=True)
        state["table"] = tbl

    run_async(_load, (), _done)

    def _next():
        tbl = state["table"]
        if not tbl or not tbl["selected"]:
            return
        sel_row = tbl["selected"]
        match   = next((g for g in state["games"] if g["app_id"] == sel_row[0]), None)
        if match:
            _add_s2_details(ns_entry=None, shortcuts_data=None, vdf_path=None,
                            native_steam=True, native_app_id=match["app_id"],
                            prefill_name=match["name"])

    add_action_bar("Next -->", _next, back_cb=_add_s1_type)


# ── Step 2: name + prefix / Steam Cloud warning ───────────────────────────────

def _add_s2_details(ns_entry, shortcuts_data, vdf_path,
                    native_steam: bool = False,
                    native_app_id: str | None = None,
                    prefill_name: str = ""):
    clear_content()
    add_header("Add New Game Config",
               "Step 2 -- Game name" +
               (" and Proton prefix" if sys.platform != "win32" and not native_steam else ""))

    default_name = prefill_name or (ns_entry["name"] if ns_entry else "")
    dpg.add_text("Display name", parent="content_group")
    dpg.add_input_text(tag="_add_name", default_value=default_name,
                       width=400, parent="content_group")

    aid_tag = "_add_aid"

    if native_steam:
        # App ID is fixed — show it read-only and warn about Steam Cloud
        dpg.add_spacer(height=8, parent="content_group")
        dpg.add_text(f"Steam App ID:  {native_app_id}", parent="content_group",
                     color=(130, 130, 155))
        dpg.add_input_text(tag=aid_tag, default_value=native_app_id or "",
                           width=1, show=False, parent="content_group")

        dpg.add_spacer(height=12, parent="content_group")
        with dpg.group(parent="content_group"):
            dpg.add_text("Important: Steam Cloud conflict warning",
                         color=(240, 180, 50))
            dpg.add_text(
                "ExternalGameSync and Steam Cloud both managing the same saves will cause\n"
                "conflicts every launch. Before continuing, disable Steam Cloud for this game:\n"
                "  Right-click the game in Steam > Properties > General\n"
                "  Uncheck 'Keep games saves in the Steam Cloud'",
                color=(200, 150, 50), wrap=750,
            )

    elif sys.platform != "win32":
        detected = find_proton_prefix_for_ns_exe(ns_entry["exe"] if ns_entry else "")
        dpg.add_spacer(height=8, parent="content_group")
        dpg.add_text("Proton Prefix (App ID)", parent="content_group")
        dpg.add_input_text(tag=aid_tag, default_value=detected or "",
                           hint="App ID (e.g. 123456789)", width=300,
                           parent="content_group")
        prefix_rows = list_proton_prefixes()
        if prefix_rows:
            dpg.add_text("Select prefix:", parent="content_group", color=(130, 130, 155))
            pfx_state = add_table(["App ID", "Game folders"], prefix_rows,
                                  col_weights=[1, 2], height=150)
            dpg.add_group(tag="_add_pfx_tree", parent="content_group")
            if detected:
                refresh_prefix_tree(detected, "_add_pfx_tree")
            for t in pfx_state["sel_tags"]:
                dpg.configure_item(t, callback=lambda s, a, u: (
                    [dpg.set_value(x, False) for x in pfx_state["sel_tags"] if x != s],
                    dpg.set_value(aid_tag, u[0]) if u and dpg.does_item_exist(aid_tag) else None,
                    refresh_prefix_tree(u[0], "_add_pfx_tree") if u else None,
                ))
    else:
        dpg.add_input_text(tag=aid_tag, default_value="",
                           width=1, show=False, parent="content_group")

    def _next():
        try:
            name = dpg.get_value("_add_name").strip()
            if not name:
                return
            if native_steam:
                aid = native_app_id or ""
            else:
                aid = dpg.get_value(aid_tag).strip() if dpg.does_item_exist(aid_tag) else ""
            if sys.platform != "win32" and not aid:
                return
            existing = next(
                (g for g in load_games() if g["id"] == game_id_from_name(name)),
                None,
            )
            if existing:
                _add_s2c_confirm_overwrite(ns_entry, shortcuts_data, vdf_path, name, aid,
                                           existing, native_steam)
            else:
                _add_s2b_manifest(ns_entry, shortcuts_data, vdf_path, name, aid, native_steam)
        except Exception:
            import traceback
            show_error("Add Config Error", traceback.format_exc())

    def _back():
        if native_steam:
            _add_s1_native()
        else:
            _add_s1_shortcut()

    add_action_bar("Next -->", _next, back_cb=_back)


def _add_s2c_confirm_overwrite(ns_entry, shortcuts_data, vdf_path,
                               game_name: str, app_id: str, existing: dict,
                               native_steam: bool = False):
    clear_content()
    add_header("Add New Game Config", "Step 3 -- Game already exists")

    dpg.add_text(
        f"A game named \"{existing['name']}\" already exists in the cloud config.",
        parent="content_group",
    )
    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text(
        "Continuing will overwrite its shared cloud configuration (exe path, save path,\n"
        "and options). The local machine config and any existing save files are kept.",
        parent="content_group", color=(240, 180, 50), wrap=800,
    )
    dpg.add_spacer(height=4, parent="content_group")
    dpg.add_text(
        f"  Existing exe:   {existing.get('exe_path', '(none)')}",
        parent="content_group", color=(130, 130, 155),
    )
    dpg.add_text(
        f"  Existing saves: {existing.get('save_path', '(none)')}",
        parent="content_group", color=(130, 130, 155),
    )
    dpg.add_spacer(height=20, parent="content_group")

    def _continue():
        _add_s2b_manifest(ns_entry, shortcuts_data, vdf_path, game_name, app_id, native_steam)

    def _back():
        _add_s2_details(ns_entry, shortcuts_data, vdf_path,
                        native_steam=native_steam,
                        native_app_id=app_id if native_steam else None)

    from gui_home import refresh_and_home
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label="Overwrite Config", width=160, height=32, callback=_continue)
        dpg.add_button(label="<-- Back",         width=90,  height=32, callback=_back)
        dpg.add_button(label="Cancel",           width=80,  height=32, callback=refresh_and_home)


def _add_s2b_manifest(ns_entry, shortcuts_data, vdf_path,
                      game_name: str, app_id: str, native_steam: bool = False):
    clear_content()
    add_header("Add New Game Config", "Step 3 -- Find in game database (optional)")
    dpg.add_text(
        "Search the ludusavi community manifest to auto-detect save paths.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )
    dpg.add_spacer(height=6, parent="content_group")

    search_tag  = "_lud_search"
    results_tag = "_lud_results_group"
    state       = {"entry": None, "table": None}

    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_input_text(tag=search_tag, default_value=game_name, width=360,
                           hint="Game name...")
        dpg.add_button(label="Search", width=80, height=22,
                       callback=lambda: _lud_do_search(search_tag, results_tag, state))

    dpg.add_spacer(height=4, parent="content_group")
    dpg.add_group(tag=results_tag, parent="content_group")

    def _skip():
        _add_s3_paths(ns_entry, shortcuts_data, vdf_path, game_name, app_id,
                      manifest_entry=None, suggested_exe=None, suggested_saves=[],
                      native_steam=native_steam)

    def _next():
        if not state["entry"]:
            return
        entry_name, entry_data = state["entry"]
        _lud_resolve_and_proceed(
            ns_entry, shortcuts_data, vdf_path, game_name, app_id,
            entry_name, entry_data, native_steam,
        )

    def _back():
        _add_s2_details(ns_entry, shortcuts_data, vdf_path,
                        native_steam=native_steam,
                        native_app_id=app_id if native_steam else None)

    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label="Next -->", width=140, height=32, callback=_next)
        dpg.add_button(label="Skip", width=80, height=32, callback=_skip)
        dpg.add_button(label="<-- Back", width=90, height=32, callback=_back)
        from gui_home import refresh_and_home
        dpg.add_button(label="Cancel", width=80, height=32, callback=refresh_and_home)


def _lud_do_search(search_tag: str, results_tag: str, state: dict):
    query = dpg.get_value(search_tag).strip()
    if not query:
        return

    if dpg.does_item_exist(results_tag):
        dpg.delete_item(results_tag, children_only=True)

    import time as _time
    yaml_p = ludusavi._cache_path()
    proc_p = ludusavi._processed_path()
    yaml_fresh = yaml_p.exists() and (
        (_time.time() - yaml_p.stat().st_mtime) / 86400 < ludusavi._MANIFEST_MAX_AGE_DAYS
    )
    proc_ready = proc_p.exists() and yaml_p.exists() and (
        proc_p.stat().st_mtime >= yaml_p.stat().st_mtime
    )
    if ludusavi._manifest_cache is not None:
        status_msg = "Searching..."
    elif proc_ready:
        status_msg = "Loading game database..."
    elif yaml_fresh:
        status_msg = "Processing game database (first time only)..."
    else:
        status_msg = "Downloading game database (~17 MB)..."
    dpg.add_text(status_msg, parent=results_tag, tag="_lud_status")

    def _work():
        try:
            manifest = ludusavi.load_manifest()
            return ludusavi.search_games(manifest, query)
        except Exception as exc:
            return exc

    def _done(result):
        if dpg.does_item_exist("_lud_status"):
            dpg.delete_item("_lud_status")
        if isinstance(result, Exception):
            dpg.add_text(f"Error: {result}", parent=results_tag,
                         color=(207, 34, 46))
            return
        if not result:
            dpg.add_text("No matches found.", parent=results_tag,
                         color=(130, 130, 155))
            return

        rows = []
        for nm, entry in result:
            steam_id = str(entry.get("steam", {}).get("id", "")) if isinstance(entry, dict) else ""
            has_saves = (
                isinstance(entry, dict)
                and any(
                    ludusavi._is_save(fe)
                    for fe in entry.get("files", {}).values()
                    if isinstance(fe, dict)
                )
            )
            rows.append([nm, steam_id, "yes" if has_saves else ""])

        tbl_state = add_table(
            ["Game", "Steam ID", "Saves"],
            rows,
            col_weights=[4, 1, 1],
            height=180,
            parent_tag=results_tag,
        )
        state["table"] = tbl_state

        name_map = {nm: entry for nm, entry in result}

        def _on_sel(sender, app_data, user_data):
            if app_data and user_data:
                state["entry"] = (user_data[0], name_map.get(user_data[0]))
            for t in tbl_state["sel_tags"]:
                if t != sender and dpg.does_item_exist(t):
                    dpg.set_value(t, False)

        for t in tbl_state["sel_tags"]:
            dpg.configure_item(t, callback=_on_sel)

    run_async(_work, (), _done)


def _lud_resolve_and_proceed(ns_entry, shortcuts_data, vdf_path,
                             game_name, app_id, entry_name, entry_data,
                             native_steam: bool = False):
    show_progress("Looking up save paths...")

    def _work():
        suggested_exe   = None
        suggested_saves = []

        fixed = ludusavi.get_fixed_save_paths(
            entry_data, app_id if sys.platform != "win32" else None
        )
        suggested_saves.extend(fixed)

        install_dir = None
        if app_id:
            if sys.platform == "win32":
                from steam import find_steam_game_dir
                install_dir = find_steam_game_dir(app_id)
            else:
                hints       = list(entry_data.get("installDir", {}).keys())
                install_dir = ludusavi.find_install_dir(app_id, hints)
            if install_dir:
                suggested_saves.extend(
                    ludusavi.get_install_relative_save_paths(entry_data, install_dir)
                )
                exe = ludusavi.find_exe_in_dir(install_dir, game_name)
                if exe:
                    suggested_exe = str(exe)

        return suggested_exe, list(dict.fromkeys(suggested_saves))

    def _done(result):
        stop_progress()
        if isinstance(result, list) and result and isinstance(result[0], tuple):
            show_error("Manifest Lookup Error", result[0][0])
            return
        suggested_exe, suggested_saves = result
        _add_s3_paths(ns_entry, shortcuts_data, vdf_path, game_name, app_id,
                      manifest_entry=entry_data,
                      suggested_exe=suggested_exe,
                      suggested_saves=suggested_saves,
                      native_steam=native_steam)

    run_async(_work, (), _done)


def _add_s3_paths(ns_entry, shortcuts_data, vdf_path,
                  game_name: str, app_id: str,
                  manifest_entry=None, suggested_exe=None, suggested_saves=None,
                  native_steam: bool = False):
    import os
    clear_content()
    add_header("Add New Game Config", "Step 4 -- Select paths and options")

    exe_tag    = "_add_exe"
    save_tag   = "_add_save"
    env_tag    = "_add_env"
    filter_tag = "_add_filter"
    disc_tag   = "_add_disc"

    if suggested_saves is None:
        suggested_saves = []

    exe_default = suggested_exe or ""
    if not exe_default and sys.platform == "win32" and not native_steam:
        shortcut_exe = (ns_entry.get("exe", "") if ns_entry else "").strip().strip('"')
        if shortcut_exe and Path(shortcut_exe).exists() and shortcut_exe.lower().endswith(".exe"):
            exe_default = shortcut_exe

    def _exe_start():
        if sys.platform == "win32":
            v = dpg.get_value(exe_tag) if dpg.does_item_exist(exe_tag) else ""
            return str(Path(v).parent) if v else os.environ.get("PROGRAMFILES", r"C:\Program Files")
        if app_id:
            pf = proton_drive_c(app_id) / "Program Files"
            return str(pf) if pf.exists() else str(proton_drive_c(app_id))
        return str(Path.home())

    def _save_start():
        if sys.platform == "win32":
            return os.environ.get("APPDATA", str(Path.home()))
        if app_id:
            r = proton_drive_c(app_id) / "users" / "steamuser" / "AppData" / "Roaming"
            return str(r) if r.exists() else str(proton_drive_c(app_id))
        return str(Path.home())

    dpg.add_text("Paths", parent="content_group")
    if native_steam:
        dpg.add_text(
            "Executable: provided automatically by Steam at launch -- no path needed.",
            parent="content_group", color=(130, 130, 155),
        )
        dpg.add_input_text(tag=exe_tag, default_value="", width=1,
                           show=False, parent="content_group")
    else:
        add_path_row("Executable", exe_tag, False, _exe_start)
        if exe_default:
            dpg.set_value(exe_tag, exe_default)
    add_path_row("Save folder", save_tag, True, _save_start)

    if suggested_saves:
        dpg.add_spacer(height=4, parent="content_group")
        dpg.add_text("Save folder suggestions (click to use):",
                     parent="content_group", color=(130, 130, 155))
        for s in suggested_saves:
            dpg.add_button(
                label=s, width=-1, height=20,
                parent="content_group",
                user_data=s,
                callback=lambda sender, app_data, user_data: dpg.set_value(save_tag, user_data),
            )

    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text("Options", parent="content_group")

    if sys.platform != "win32":
        with dpg.group(horizontal=True, parent="content_group"):
            dpg.add_text("Env vars")
            dpg.add_input_text(tag=env_tag, hint="DXVK_ASYNC=1 ...", width=-1)
    else:
        dpg.add_input_text(tag=env_tag, default_value="",
                           width=1, show=False, parent="content_group")

    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_text("Save filter")
        dpg.add_input_text(tag=filter_tag,
                           hint="e.g. save_*/**  (blank = sync whole folder)", width=-1)

    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text("Disc Image (optional -- auto-mount ISO on launch)", parent="content_group")
    add_path_row("Disc image (.iso)", disc_tag, False, lambda: str(Path.home()))

    def _run():
        exe_p  = dpg.get_value(exe_tag).strip() if dpg.does_item_exist(exe_tag) else ""
        save_p = dpg.get_value(save_tag).strip()
        if (not exe_p and not native_steam) or not save_p:
            return
        _add_run(ns_entry, shortcuts_data, vdf_path, game_name, app_id,
                 Path(exe_p) if exe_p else None, Path(save_p),
                 dpg.get_value(env_tag).strip(),
                 dpg.get_value(filter_tag).strip(),
                 dpg.get_value(disc_tag).strip() if dpg.does_item_exist(disc_tag) else "",
                 native_steam=native_steam)

    add_action_bar("Configure -->", _run,
                   back_cb=lambda: _add_s2b_manifest(ns_entry, shortcuts_data, vdf_path,
                                                     game_name, app_id, native_steam))


def _add_run(ns_entry, shortcuts_data, vdf_path,
             game_name: str, app_id: str,
             exe_real: Path | None, save_real: Path,
             env_vars: str, save_filter: str, disc_image: str = "",
             native_steam: bool = False):
    game_id = game_id_from_name(game_name)
    show_progress(f"Configuring '{game_name}'...")

    def _work():
        log = []
        if native_steam:
            shutdown_steam_sync()
        if sys.platform == "win32":
            if native_steam:
                aid = app_id
            else:
                raw = ns_entry.get("appid") or 0
                aid = str(raw & 0xFFFFFFFF) if raw else None
            exe_rel  = str(exe_real) if exe_real else ""
            save_rel = str(save_real)
            mc = {"platform": "windows", "exe_path": exe_rel, "save_path": save_rel,
                  "app_id": aid}
            if native_steam:
                mc["native_steam"] = True
        else:
            aid     = app_id
            drive_c = proton_drive_c(aid)
            if native_steam:
                # Exe is provided by Steam via %command% at runtime — no path needed
                exe_rel = ""
                try:
                    save_rel = str(save_real.resolve().relative_to(drive_c.resolve()))
                except ValueError:
                    return [(f"Save path is outside Proton prefix: {drive_c.resolve()}", False)]
            else:
                try:
                    exe_rel  = str(exe_real.resolve().relative_to(drive_c.resolve()))
                    save_rel = str(save_real.resolve().relative_to(drive_c.resolve()))
                except ValueError:
                    return [(f"Path is outside Proton prefix: {drive_c.resolve()}", False)]
            mc = {"platform": "linux", "app_id": aid, "native_steam": True} if native_steam \
                else {"platform": "linux", "app_id": aid}
        if disc_image:
            mc["disc_image"] = disc_image

        # Normalize paths for games.json (shared/community).
        # On Windows: mc keeps absolute paths for local sync; new_cfg gets variable form.
        # On Linux: drive_c-relative steamuser paths are normalized to variable form too.
        if sys.platform == "win32":
            exe_rel  = ludusavi.normalize_path_for_storage(exe_real) if exe_real else ""
            save_rel = ludusavi.normalize_path_for_storage(save_real)
        else:
            save_rel = ludusavi.normalize_path_for_storage(save_rel)

        rclone_mod.rclone_pull_games_json()
        all_g = [g for g in load_games() if g["id"] != game_id]
        new_cfg = {
            "id":          game_id,
            "name":        game_name,
            "exe_path":    exe_rel,
            "save_path":   save_rel,
            "save_filter": save_filter,
            "env_vars":    env_vars,
            "added":       datetime.datetime.now().isoformat(),
        }
        if native_steam and aid:
            new_cfg["steam_app_id"] = aid
        if exe_real and exe_real.is_file():
            try:
                new_cfg["exe_hashes"] = [hash_file(exe_real)]
            except Exception:
                pass

        if sys.platform == "win32" and not native_steam and ns_entry:
            shortcut_exe_str = ns_entry.get("exe", "").strip().strip('"')
            if shortcut_exe_str:
                shortcut_exe = Path(shortcut_exe_str)
                if shortcut_exe.is_file() and shortcut_exe.resolve() != exe_real.resolve():
                    try:
                        new_cfg["installer_hashes"] = [hash_file(shortcut_exe)]
                    except Exception:
                        pass

        set_local_config(game_id, mc)
        all_g.append(new_cfg)
        save_games(all_g)
        log.append(("Config created", True))

        ok_push, msg_push = rclone_mod.rclone_push_games_json()
        log.append(("Config pushed to cloud storage" if ok_push
                    else f"Cloud push failed: {msg_push}", ok_push))

        if sys.platform != "win32":
            ok_lnk, msg_lnk = make_save_symlink(game_id, aid, save_rel)
            log.append((f"Save symlink {'created' if ok_lnk else 'failed: ' + msg_lnk}",
                        ok_lnk))

        ok1, _ = rclone_sync_pull(game_id, new_cfg)
        ok2, _ = rclone_sync_push(game_id, new_cfg)
        log.append(("Initial sync", ok1 and ok2))

        if native_steam:
            ok3, msg3 = update_native_game_launch(
                app_id=aid,
                game_name=game_name,
                savesync_bin=SAVESYNC_BIN,
                game_cfg=new_cfg,
            )
            log.append((f"Steam launch options {'updated' if ok3 else 'failed: ' + msg3}", ok3))
            if ok3:
                log.append(("Restart Steam for launch option changes to take effect.", True))
        else:
            ok3, msg3 = update_shortcut_launch(
                shortcuts_data, vdf_path,
                shortcut_index=ns_entry["index"],
                game_name=game_name,
                savesync_bin=SAVESYNC_BIN,
                real_exe=str(exe_real),
                start_dir=ns_entry["start_dir"],
                game_cfg=new_cfg,
            )
            log.append((f"Steam shortcut {'updated' if ok3 else 'failed: ' + msg3}", ok3))
            if ok3:
                log.append(("Restart Steam for shortcut changes to take effect.", True))
        return log

    def _done(log):
        stop_progress()
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        show_done(f"'{game_name}' added!" if all_ok else "Added with warnings",
                  lines, success=all_ok)

    run_async(_work, (), _done)
