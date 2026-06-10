"""
ExternalGameSync GUI — assign an existing cloud config to a Steam shortcut or native Steam game.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

from games import load_games, save_games
from machine_config import get_local_config, set_local_config
import rclone as rclone_mod
from sync import rclone_sync_pull, rclone_sync_push
from steam import (
    read_shortcuts, get_non_steam_games, update_shortcut_launch,
    resolve_exe_path, resolve_save_path, proton_drive_c, get_proton_base,
    list_steam_games, update_native_game_launch, find_steam_game_dir,
    _find_all_steamapps_dirs,
)
from artwork import list_remote_art, pull_art_from_nextcloud
from gui_common import (
    SAVESYNC_BIN, set_nav_active, clear_content, add_header, add_action_bar,
    add_table, add_path_row, run_async, show_progress, stop_progress, show_done, show_error,
    win_exe_candidates, win_save_candidates, try_resolve_windows_paths,
    find_proton_prefix_for_shortcut, list_proton_prefixes, refresh_prefix_tree,
    shutdown_steam_sync,
)
from gui_home import require_configured, refresh_and_home


def flow_assign():
    if not require_configured():
        return
    set_nav_active("assign")
    _assign_s1_game()


def _assign_s1_game():
    games = load_games()
    if not games:
        show_error("No Configs",
                   "No game configs found on cloud storage. Use 'Add New Game Config' to create one first.")
        return
    clear_content()
    add_header("Assign Config", "Step 1 -- Pick a game config from cloud storage")

    configured_count = sum(1 for g in games if get_local_config(g["id"]))
    dpg.add_button(
        label=f"Update All Configured Games  ({configured_count} on this machine)",
        callback=_assign_update_all, width=-1, height=30,
        parent="content_group",
    )
    dpg.add_spacer(height=6, parent="content_group")

    rows  = [[g["id"], g["name"], g.get("exe_path", ""), g.get("save_path", "")]
             for g in games]
    state = add_table(["ID", "Name", "Exe", "Save"], rows, col_weights=[2, 2, 3, 3],
                      filterable=True)

    def _next():
        sel = state["selected"]
        if not sel:
            return
        gc = next((g for g in games if g["id"] == sel[0].strip()), None)
        if gc:
            _assign_s2_type(gc)

    add_action_bar("Next -->", _next)


def _assign_update_all():
    show_progress("Updating all configured games...")

    def _work():
        log = []
        shortcuts_data, vdf_path = read_shortcuts()

        def _appid_key(raw):
            try:
                return str(int(raw) & 0xFFFFFFFF)
            except (ValueError, TypeError):
                return str(raw) if raw else None

        ns_games    = get_non_steam_games(shortcuts_data) if shortcuts_data else []
        ns_by_name  = {g["name"]: g for g in ns_games}
        ns_by_appid = {k: g for g in ns_games if (k := _appid_key(g["appid"]))}

        for game_cfg in load_games():
            mc = get_local_config(game_cfg["id"])
            if not mc:
                continue

            if mc.get("native_steam"):
                app_id = mc.get("app_id", "")
                if not app_id:
                    log.append((f"{game_cfg['name']}: no app_id in machine config", False))
                    continue
                ok, msg = update_native_game_launch(
                    app_id=app_id,
                    game_name=game_cfg["name"],
                    savesync_bin=SAVESYNC_BIN,
                    game_cfg=game_cfg,
                )
                log.append((f"{game_cfg['name']}: {'updated' if ok else msg}", ok))
            else:
                if not shortcuts_data:
                    log.append((f"{game_cfg['name']}: could not read shortcuts.vdf", False))
                    continue
                app_id   = mc.get("app_id", "")
                ns_entry = (ns_by_appid.get(_appid_key(app_id)) if app_id else None) or ns_by_name.get(game_cfg["name"])
                if not ns_entry:
                    log.append((f"{game_cfg['name']}: shortcut not found in Steam", False))
                    continue
                if mc.get("platform") == "windows":
                    exe_real = Path(mc["exe_path"])
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
                    linux_native=mc.get("platform") == "linux_native",
                )
                log.append((f"{game_cfg['name']}: {'updated' if ok else msg}", ok))

        if not log:
            log = [("No games are configured on this machine", False)]
        return log

    def _done(log):
        stop_progress()
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        show_done("All games updated" if all_ok else "Update complete",
                  lines, success=all_ok, on_done=_assign_s1_game,
                  show_restart_steam=any(ok for _, ok in log))

    run_async(_work, (), _done)


# ── Step 2: choose native Steam vs non-Steam shortcut ─────────────────────────

def _assign_s2_type(game_cfg: dict):
    clear_content()
    add_header("Assign Config",
               f"Step 2 -- How is '{game_cfg['name']}' installed on this machine?")

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_text("Non-Steam Shortcut", parent="content_group")
    dpg.add_text(
        "Installed as a non-Steam shortcut with its own Proton prefix.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )
    dpg.add_button(
        label="Non-Steam Shortcut  -->", width=240, height=32,
        callback=lambda: _assign_s2_shortcut(game_cfg),
        parent="content_group",
    )

    dpg.add_spacer(height=16, parent="content_group")
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=16, parent="content_group")

    dpg.add_text("Native Steam Game", parent="content_group")
    dpg.add_text(
        "A Steam purchase running in its own Proton prefix.\n"
        "Paths are detected automatically from the Steam App ID.",
        parent="content_group", color=(130, 130, 155), wrap=700,
    )
    dpg.add_button(
        label="Native Steam Game  -->", width=240, height=32,
        callback=lambda: _assign_s2_native(game_cfg),
        parent="content_group",
    )

    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label="<-- Back", callback=_assign_s1_game, width=90, height=32)
        dpg.add_button(label="Cancel",   callback=refresh_and_home, width=80, height=32)


# ── Step 2a: non-Steam shortcut picker (unchanged logic, new back target) ─────

def _assign_s2_shortcut(game_cfg: dict):
    shortcuts_data, vdf_path = read_shortcuts()
    if not shortcuts_data:
        show_error("Shortcuts Unavailable",
                   "Could not read Steam shortcuts.vdf. "
                   "Make sure Steam is installed and has non-Steam games.")
        return
    ns_games = get_non_steam_games(shortcuts_data)
    if not ns_games:
        show_error("No Shortcuts",
                   "No non-Steam games found. Add a game via Games > Add a Non-Steam Game.")
        return

    clear_content()
    add_header("Assign Config", f"Step 3 -- Assign '{game_cfg['name']}' to a Steam shortcut")

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
            exe_str = ns.get("exe", "").strip().strip('"')
            _linux_native = sys.platform != "win32" and exe_str and not exe_str.lower().endswith(".exe")
            _assign_s3_paths(game_cfg, ns, shortcuts_data, vdf_path,
                             native_steam=False, native_app_id=None,
                             linux_native=_linux_native)

    add_action_bar("Next -->", _next, back_cb=lambda: _assign_s2_type(game_cfg))


# ── Step 2b: native Steam game — pick app_id and auto-detect paths ─────────────

def _assign_s2_native(game_cfg: dict):
    clear_content()
    add_header("Assign Config",
               f"Step 3 -- Confirm Steam App ID for '{game_cfg['name']}'")

    dpg.add_text(
        "Important: Steam Cloud conflict warning",
        parent="content_group", color=(240, 180, 50),
    )
    dpg.add_text(
        "ExternalGameSync and Steam Cloud both managing the same saves will cause\n"
        "conflicts every launch. Before continuing, disable Steam Cloud for this game:\n"
        "  Right-click the game in Steam > Properties > General\n"
        "  Uncheck 'Keep games saves in the Steam Cloud'",
        parent="content_group", color=(200, 150, 50), wrap=750,
    )
    dpg.add_spacer(height=10, parent="content_group")

    aid_tag = "_assign_native_aid"

    # Pre-fill from existing machine config if already assigned
    existing_mc  = get_local_config(game_cfg["id"]) or {}
    existing_aid = existing_mc.get("app_id", "")

    dpg.add_text("Steam App ID", parent="content_group")
    dpg.add_input_text(tag=aid_tag, hint="e.g. 17470", width=200,
                       default_value=existing_aid,
                       parent="content_group")

    dpg.add_spacer(height=6, parent="content_group")
    dpg.add_text("Or pick from installed games:", parent="content_group",
                 color=(130, 130, 155))

    native_state = {"games": [], "table": None}

    dpg.add_text("Loading...", tag="_assign_native_loading",
                 parent="content_group", color=(130, 130, 155))

    def _load():
        return list_steam_games()

    def _loaded(games):
        if dpg.does_item_exist("_assign_native_loading"):
            dpg.delete_item("_assign_native_loading")
        if not games:
            dpg.add_text("No installed Steam games found.", parent="content_group",
                         color=(207, 34, 46))
            return
        native_state["games"] = games
        rows = [[g["app_id"], g["name"]] for g in games]
        tbl  = add_table(["App ID", "Game Name"], rows, col_weights=[1, 4],
                         filterable=True, height=200)
        native_state["table"] = tbl
        for t in tbl["sel_tags"]:
            dpg.configure_item(t, callback=lambda s, a, u: (
                [dpg.set_value(x, False) for x in tbl["sel_tags"] if x != s],
                dpg.set_value(aid_tag, u[0]) if u and dpg.does_item_exist(aid_tag) else None,
            ))
        # Auto-select matching row by app_id from existing machine config
        if existing_aid:
            for i, row in enumerate(rows):
                if row[0] == existing_aid:
                    dpg.set_value(tbl["sel_tags"][i], True)
                    tbl["selected"] = row
                    break

    run_async(_load, (), _loaded)

    def _next():
        aid = dpg.get_value(aid_tag).strip() if dpg.does_item_exist(aid_tag) else ""
        if not aid:
            return
        _assign_s3_paths(game_cfg, ns_entry=None, shortcuts_data=None, vdf_path=None,
                         native_steam=True, native_app_id=aid)

    add_action_bar("Next -->", _next, back_cb=lambda: _assign_s2_type(game_cfg))


# ── Step 3: confirm / browse paths ────────────────────────────────────────────

def _find_prefix_for_game(game_cfg: dict) -> tuple[str, str, str]:
    """Check the 5 most recently modified Proton prefixes (across all Steam libraries)
    for the game's exe. Returns (app_id, exe_path, save_path); paths are '' if not found."""
    all_compatdata = []
    for steamapps in _find_all_steamapps_dirs():
        compatdata = steamapps / "compatdata"
        if compatdata.exists():
            all_compatdata.extend(
                d for d in compatdata.iterdir() if d.is_dir() and d.name.isdigit()
            )
    dirs = sorted(all_compatdata, key=lambda d: d.stat().st_mtime, reverse=True)[:5]
    for d in dirs:
        exe_p = resolve_exe_path(d.name, game_cfg.get("exe_path", ""))
        if exe_p.exists():
            save_p = resolve_save_path(d.name, game_cfg.get("save_path", ""))
            return d.name, str(exe_p.resolve()), str(save_p.resolve())
    return "", "", ""


def _assign_s3_paths(game_cfg: dict, ns_entry, shortcuts_data, vdf_path,
                     native_steam: bool = False, native_app_id: str | None = None,
                     linux_native: bool = False):
    import os
    clear_content()
    add_header("Assign Config", "Step 4 -- Confirm or browse paths")

    exe_tag  = "_assign_exe"
    save_tag = "_assign_save"
    aid_tag  = "_assign_aid"
    env_tag  = "_assign_env"
    disc_tag = "_assign_disc"

    auto_exe_val = auto_save_val = ""
    auto_aid_val = ""

    if sys.platform == "win32":
        auto_exe, auto_save = try_resolve_windows_paths(game_cfg)
        auto_exe_val  = str(auto_exe)  if auto_exe  else ""
        auto_save_val = str(auto_save) if auto_save else ""
        if not auto_save_val and native_steam:
            candidates = win_save_candidates(game_cfg.get("save_path", ""))
            if candidates:
                auto_save_val = str(candidates[0])
        dpg.add_input_text(tag=aid_tag, default_value=native_app_id or "",
                           width=1, show=False, parent="content_group")
    else:
        if linux_native:
            # Native Linux app — no Proton prefix. Store the shortcut's app ID so we
            # can find it in shortcuts.vdf later; it's not a compatdata ID.
            raw_appid = ns_entry.get("appid") or 0
            _ln_aid = str(int(raw_appid) & 0xFFFFFFFF) if raw_appid else ""
            auto_aid_val = ""  # don't trigger the "Game found in prefix" status msg
            dpg.add_text("Native Linux app detected -- no Proton prefix needed.",
                         parent="content_group", color=(45, 164, 78))
            dpg.add_input_text(tag=aid_tag, default_value=_ln_aid,
                               width=1, show=False, parent="content_group")
            # Pre-fill save path by expanding XDG vars from the shared games.json entry
            _sp = game_cfg.get("save_path", "")
            if _sp:
                _h = Path.home()
                for _var, _val in [
                    ("<xdgData>",   str(_h / ".local" / "share")),
                    ("<xdgConfig>", str(_h / ".config")),
                    ("<xdgCache>",  str(_h / ".cache")),
                    ("<home>",      str(_h)),
                ]:
                    _sp = _sp.replace(_var, _val)
                if "<" not in _sp:
                    auto_save_val = _sp
        elif native_steam and native_app_id:
            # For native Steam, exe is in steamapps/common — save is in drive_c
            auto_aid_val = native_app_id
            save_p = resolve_save_path(native_app_id, game_cfg.get("save_path", ""))
            auto_save_val = str(save_p.resolve())
            # Don't try to auto-fill the exe — it lives in steamapps/common,
            # not drive_c, so the user needs to browse to it there.
            dpg.add_input_text(tag=aid_tag, default_value=auto_aid_val,
                               width=1, show=False, parent="content_group")
        else:
            auto_aid_val, auto_exe_val, auto_save_val = _find_prefix_for_game(game_cfg)

            dpg.add_text("Proton Prefix (App ID)", parent="content_group")
            aid_readonly = native_steam and native_app_id
            dpg.add_input_text(tag=aid_tag,
                               default_value=auto_aid_val,
                               hint="App ID (e.g. 123456789)", width=300,
                               enabled=not aid_readonly,
                               parent="content_group")
            if aid_readonly:
                dpg.add_text("  (fixed — native Steam game uses its own prefix)",
                             parent="content_group", color=(130, 130, 155))

            prefix_rows = list_proton_prefixes()
            pfx_state = None
            if prefix_rows:
                dpg.add_text("Select prefix:", parent="content_group", color=(130, 130, 155))
                pfx_state = add_table(["App ID", "Game folders"], prefix_rows,
                                      col_weights=[1, 2], height=100)
                dpg.add_group(tag="_assign_pfx_tree", parent="content_group")
                for t in pfx_state["sel_tags"]:
                    dpg.configure_item(t, callback=lambda s, a, u: (
                        [dpg.set_value(x, False) for x in pfx_state["sel_tags"] if x != s],
                        dpg.set_value(aid_tag, u[0]) if u and dpg.does_item_exist(aid_tag) else None,
                        refresh_prefix_tree(u[0], "_assign_pfx_tree") if u else None,
                    ))
                if auto_aid_val:
                    for i, row in enumerate(prefix_rows):
                        if row[0] == auto_aid_val:
                            dpg.set_value(pfx_state["sel_tags"][i], True)
                            pfx_state["selected"] = row
                            refresh_prefix_tree(auto_aid_val, "_assign_pfx_tree")
                            break
        dpg.add_spacer(height=4, parent="content_group")

    def _autodetect():
        aid = dpg.get_value(aid_tag).strip() if dpg.does_item_exist(aid_tag) else ""
        if not aid:
            return
        dpg.set_value(exe_tag, "")
        dpg.set_value(save_tag, "")
        dpg.configure_item("_detect_ok",     show=False)
        dpg.configure_item("_detect_err",    show=False)
        dpg.configure_item("_detect_nosave", show=False)

        exe_p = resolve_exe_path(aid, game_cfg.get("exe_path", ""))
        if exe_p.exists():
            dpg.set_value(exe_tag, str(exe_p.resolve()))
            save_p = resolve_save_path(aid, game_cfg.get("save_path", ""))
            dpg.set_value(save_tag, str(save_p.resolve()))
            dpg.set_value("_detect_ok", f"Game found in prefix {aid}, paths filled.")
            dpg.configure_item("_detect_ok", show=True)
            if not save_p.exists():
                dpg.configure_item("_detect_nosave", show=True)
        else:
            dpg.set_value("_detect_err", f"Game exe not found in prefix {aid}.")
            dpg.configure_item("_detect_err", show=True)

    dpg.add_text("", tag="_detect_ok", parent="content_group",
                 color=(45, 164, 78), wrap=700, show=False)
    if sys.platform != "win32" and not linux_native:
        dpg.add_button(label="Auto-detect paths from selected prefix",
                       callback=_autodetect, width=-1, height=28,
                       parent="content_group")
        dpg.add_text("", tag="_detect_err", parent="content_group",
                     color=(207, 34, 46),  wrap=700, show=False)
        dpg.add_spacer(height=4, parent="content_group")

    dpg.add_text("Paths", parent="content_group")

    def _exe_start():
        if sys.platform == "win32":
            return os.environ.get("PROGRAMFILES", r"C:\Program Files")
        aid = dpg.get_value(aid_tag).strip() if dpg.does_item_exist(aid_tag) else ""
        if aid and native_steam:
            game_dir = find_steam_game_dir(aid)
            if game_dir:
                return str(game_dir)
        if aid:
            pf = proton_drive_c(aid) / "Program Files"
            return str(pf) if pf.exists() else str(proton_drive_c(aid))
        return str(Path.home())

    def _save_start():
        if sys.platform == "win32":
            return os.environ.get("APPDATA", str(Path.home()))
        if linux_native:
            return str(Path.home())
        aid = dpg.get_value(aid_tag).strip() if dpg.does_item_exist(aid_tag) else ""
        if aid:
            r = proton_drive_c(aid) / "users" / "steamuser" / "AppData" / "Roaming"
            return str(r) if r.exists() else str(proton_drive_c(aid))
        return str(Path.home())

    if native_steam or linux_native:
        dpg.add_text(
            "Executable: provided automatically by Steam at launch -- no path needed.",
            parent="content_group", color=(130, 130, 155),
        )
        dpg.add_input_text(tag=exe_tag, default_value="", width=1,
                           show=False, parent="content_group")
    else:
        add_path_row("Executable", exe_tag, False, _exe_start)
    add_path_row("Save folder", save_tag, True, _save_start)
    dpg.add_text(
        "Save folder not found yet, it will likely be created"
        " the first time you save in game."
        if sys.platform == "win32" else
        "Save folder not found in this prefix yet, it will likely be created"
        " the first time you save in game.",
        tag="_detect_nosave", parent="content_group", color=(200, 170, 80),
        wrap=700, show=False,
    )
    if auto_exe_val:
        dpg.set_value(exe_tag, auto_exe_val)
    if auto_save_val:
        dpg.set_value(save_tag, auto_save_val)
    if auto_aid_val:
        status_msg = f"Game found automatically in prefix {auto_aid_val}, paths pre-filled."
        if native_steam:
            status_msg = f"Paths resolved from App ID {auto_aid_val}."
        dpg.set_value("_detect_ok", status_msg)
        dpg.configure_item("_detect_ok", show=True)
        if auto_save_val and not Path(auto_save_val).exists():
            dpg.configure_item("_detect_nosave", show=True)
    if sys.platform == "win32" and native_steam and auto_save_val:
        exists = Path(auto_save_val).exists()
        dpg.set_value("_detect_ok",
                      "Save path pre-filled." if exists
                      else "Save path pre-filled (folder not yet created -- normal before first play).")
        dpg.configure_item("_detect_ok", show=True)
        if not exists:
            dpg.configure_item("_detect_nosave", show=True)

    if sys.platform != "win32":
        dpg.add_spacer(height=4, parent="content_group")
        dpg.add_text("Env vars (optional)", parent="content_group")
        dpg.add_input_text(tag=env_tag, hint="DXVK_ASYNC=1 ...",
                           default_value=game_cfg.get("env_vars", ""),
                           width=-1, parent="content_group")
    else:
        dpg.add_input_text(tag=env_tag, default_value=game_cfg.get("env_vars", ""),
                           width=1, show=False, parent="content_group")

    existing_mc = get_local_config(game_cfg["id"]) or {}
    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_text("Disc Image (optional -- auto-mount ISO on launch)", parent="content_group")
    add_path_row("Disc image (.iso)", disc_tag, False, lambda: str(Path.home()))
    if existing_mc.get("disc_image"):
        dpg.set_value(disc_tag, existing_mc["disc_image"])

    def _run():
        exe_p  = dpg.get_value(exe_tag).strip() if dpg.does_item_exist(exe_tag) else ""
        save_p = dpg.get_value(save_tag).strip()
        if (not exe_p and not native_steam and not linux_native) or not save_p:
            return
        exe_real  = Path(exe_p) if exe_p else None
        save_real = Path(save_p)
        if sys.platform == "win32":
            if native_steam:
                aid = native_app_id or ""
            else:
                raw = ns_entry.get("appid") or 0
                aid = str(raw & 0xFFFFFFFF) if raw else None
            mc = {"platform": "windows", "save_path": str(save_real), "app_id": aid}
            if exe_real:
                mc["exe_path"] = str(exe_real)
            if native_steam:
                mc["native_steam"] = True
            ev = None
        else:
            aid = dpg.get_value(aid_tag).strip()
            ev = dpg.get_value(env_tag).strip()
            if linux_native:
                import ludusavi
                mc = {"platform": "linux_native", "app_id": aid,
                      "save_path": str(save_real.resolve())}
                if ns_entry:
                    _orig_exe = ns_entry.get("exe", "").strip().strip('"')
                    if _orig_exe:
                        mc["shortcut_exe"] = _orig_exe
                save_rel = ludusavi.normalize_path_for_storage(save_real.resolve())
            else:
                if not aid:
                    return
                mc = {"platform": "linux", "app_id": aid}
                if native_steam:
                    mc["native_steam"] = True
        disc = dpg.get_value(disc_tag).strip() if dpg.does_item_exist(disc_tag) else ""
        _assign_run(game_cfg, ns_entry, shortcuts_data, vdf_path,
                    exe_real, save_real, mc, aid,
                    env_vars=ev, disc_image=disc, native_steam=native_steam,
                    linux_native=linux_native)

    def _back():
        if native_steam:
            _assign_s2_native(game_cfg)
        else:
            _assign_s2_shortcut(game_cfg)

    add_action_bar("Configure -->", _run, back_cb=_back)


def _assign_run(game_cfg, ns_entry, shortcuts_data, vdf_path,
                exe_real: Path | None, save_real: Path, machine_cfg: dict,
                steam_app_id: str | None, env_vars: str | None = None,
                disc_image: str = "", native_steam: bool = False,
                linux_native: bool = False):
    from steam import _to_drive_c_rel, make_save_symlink, make_save_symlink_native
    name = game_cfg["name"]
    show_progress(f"Configuring '{name}'...")

    def _work():
        log = []
        if native_steam or linux_native:
            shutdown_steam_sync()
        if sys.platform != "win32":
            if linux_native:
                ok, msg = make_save_symlink_native(game_cfg["id"], save_real.resolve())
                log.append((f"Save symlink {'created' if ok else 'failed'}: {msg}", ok))
            else:
                try:
                    drive_c  = proton_drive_c(steam_app_id)
                    save_rel = str(save_real.resolve().relative_to(drive_c.resolve()))
                except ValueError:
                    save_rel = _to_drive_c_rel(game_cfg["save_path"])
                ok, msg = make_save_symlink(game_cfg["id"], steam_app_id, save_rel)
                log.append((f"Save symlink {'created' if ok else 'failed'}: {msg}", ok))

        # Save machine config before pull/push so get_local_save_path resolves
        # the correct Windows save directory (not the SYNC_ROOT fallback).
        if disc_image:
            machine_cfg["disc_image"] = disc_image
        elif "disc_image" in machine_cfg:
            del machine_cfg["disc_image"]
        set_local_config(game_cfg["id"], machine_cfg)

        ok1, _ = rclone_sync_pull(game_cfg["id"], game_cfg)
        ok2, _ = rclone_sync_push(game_cfg["id"], game_cfg)
        log.append(("Initial sync", ok1 and ok2))

        if native_steam:
            ok3, msg3 = update_native_game_launch(
                app_id=steam_app_id,
                game_name=game_cfg["name"],
                savesync_bin=SAVESYNC_BIN,
                game_cfg=game_cfg,
                disc_image_override=disc_image,
            )
            log.append((f"Steam launch options {'updated' if ok3 else 'failed: ' + msg3}", ok3))
        else:
            ok3, msg3 = update_shortcut_launch(
                shortcuts_data, vdf_path,
                shortcut_index=ns_entry["index"],
                game_name=game_cfg["name"],
                savesync_bin=SAVESYNC_BIN,
                real_exe=str(exe_real) if exe_real else "",
                start_dir=ns_entry["start_dir"],
                game_cfg=game_cfg,
                disc_image_override=disc_image,
                linux_native=linux_native,
            )
            log.append((f"Steam shortcut {'updated' if ok3 else 'failed: ' + msg3}", ok3))

        rclone_mod.rclone_pull_games_json()
        all_g = load_games()
        if env_vars is not None:
            for g in all_g:
                if g["id"] == game_cfg["id"]:
                    g["env_vars"] = env_vars
        save_games(all_g)
        ok_p, msg_p = rclone_mod.rclone_push_games_json()
        log.append(("Config saved to cloud storage" if ok_p
                    else f"Config push failed: {msg_p}", ok_p))

        remote_art = list_remote_art(game_cfg["id"])
        if remote_art and steam_app_id:
            pull_art_from_nextcloud(game_cfg["id"], steam_app_id)
            log.append((f"Artwork downloaded: {', '.join(remote_art)}", True))
        return log

    def _done(log):
        stop_progress()
        all_ok = all(ok for _, ok in log)
        lines  = [("OK  " if ok else "X  ") + msg for msg, ok in log]
        show_done(f"'{name}' configured!" if all_ok else "Configured with warnings",
                  lines, success=all_ok, show_restart_steam=True)

    run_async(_work, (), _done)
