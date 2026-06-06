"""
ExternalGameSync GUI — Community tab.

Shows the signed-in user's shared configs and lets them share new ones.
"""

from __future__ import annotations

from pathlib import Path

import dearpygui.dearpygui as dpg

from gui_common import (
    set_nav_active, clear_content, add_header, add_action_bar,
    add_table, show_done, show_error, show_progress, stop_progress, run_async,
)


def flow_community():
    set_nav_active("community")
    _community_home()


def _community_home():
    from community.client import is_signed_in, signed_in_provider

    clear_content()
    add_header("Community", "Share game configs and see your contributions")

    if not is_signed_in():
        dpg.add_text("Sign in to share and manage your community game configs.",
                     parent="content_group", color=(130, 130, 155), wrap=700)
        dpg.add_spacer(height=8, parent="content_group")
        dpg.add_button(label="Go to Settings to Sign In", parent="content_group",
                       callback=lambda: __import__("gui_setup").flow_setup())
        return

    provider = signed_in_provider() or "community"
    dpg.add_text(f"Signed in via {provider}",
                 parent="content_group", color=(80, 180, 80))
    dpg.add_spacer(height=8, parent="content_group")

    dpg.add_text("Loading your shared configs...", tag="_comm_home_status",
                 parent="content_group", color=(130, 130, 155))
    dpg.add_group(tag="_comm_home_list", parent="content_group")

    dpg.add_spacer(height=12, parent="content_group")
    dpg.add_button(label="Share a Config  -->", height=28,
                   parent="content_group", callback=_community_share_s1)

    def _work():
        from community.client import get_my_configs
        return get_my_configs()

    def _done(results):
        if not dpg.does_item_exist("_comm_home_status"):
            return
        if results and isinstance(results[0], tuple):
            dpg.set_value("_comm_home_status", f"Error: {results[0][0]}")
            dpg.configure_item("_comm_home_status", color=(207, 34, 46))
            return
        if not results:
            dpg.set_value("_comm_home_status",
                          "You haven't shared any configs yet. Click 'Share a Config' below.")
            return
        dpg.set_value("_comm_home_status", f"{len(results)} shared config(s):")
        rows = [
            [r.get("name", ""),
             str(r.get("votes", 0)),
             (r.get("created_at", "") or "")[:10],
             "Yes" if r.get("hidden") else "No"]
            for r in results if isinstance(r, dict)
        ]
        add_table(["Name", "Votes", "Shared On", "Hidden"], rows,
                  col_weights=[3, 1, 2, 1], height=200,
                  parent_tag="_comm_home_list")

    run_async(_work, (), _done)


def _community_share_s1():
    from games import load_games

    games = load_games()
    unshared = [g for g in games if not g.get("community_id")]

    clear_content()
    add_header("Share a Config",
               "Select a local game config to share with the community")

    if not unshared:
        dpg.add_text("All your local configs have already been shared.",
                     parent="content_group", color=(130, 130, 155))
        add_action_bar(back_cb=_community_home)
        return

    rows = [[g["name"], g.get("exe_path", ""), g.get("save_path", "")]
            for g in unshared]
    state = add_table(["Name", "Exe Path", "Save Path"], rows,
                      col_weights=[2, 3, 3], height=-80)

    def _share():
        sel = state["selected"]
        if not sel:
            return
        game = next((g for g in unshared if g["name"] == sel[0]), None)
        if game:
            _community_do_share(game)

    add_action_bar("Share  -->", _share, back_cb=_community_home)


def _community_do_share(game: dict):
    show_progress(f'Sharing "{game["name"]}"...')

    def _work():
        from community.client import submit_config
        from games import hash_file, load_install_hashes
        import sys

        exe_hash = None
        installer_hash = None
        platform = "windows" if sys.platform == "win32" else "linux"

        try:
            from machine_config import get_local_config
            cfg = get_local_config(game["id"])
            if cfg and cfg.get("exe_path"):
                try:
                    exe_hash = hash_file(Path(cfg["exe_path"]))
                except Exception:
                    pass
            if cfg and cfg.get("app_id"):
                hashes = load_install_hashes(str(cfg["app_id"]))
                installer_hash = hashes[0] if hashes else None
        except Exception:
            pass

        return submit_config(game, exe_hash, installer_hash, platform)

    def _done(result):
        stop_progress()
        if isinstance(result, list) and result and isinstance(result[0], tuple):
            show_error("Share Failed", result[0][0])
            return
        ok, msg, community_id = result
        if ok:
            if community_id is not None:
                from games import load_games, save_games
                games = load_games()
                for g in games:
                    if g["id"] == game["id"]:
                        g["community_id"] = community_id
                        break
                save_games(games)
            _community_home()
        else:
            show_error("Share Failed", msg)

    run_async(_work, (), _done)
