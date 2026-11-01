"""
ExternalGameSync -- unassign a game from this machine, or delete its config everywhere.
"""

from __future__ import annotations

from datetime import datetime

from config import _run, log
from games import delete_game, load_games
from machine_config import get_local_config, remove_local_config, load_local_configs
import rclone as rclone_mod
from steam import launch_wrapper_path, restore_steam_launch, saves_link_path
from sync import clear_local_sync_state


def orphaned_local_games() -> list[dict]:
    """Games assigned on this machine whose config no longer exists (e.g. deleted
    from another machine). Returns minimal game dicts, named from the tombstone."""
    live  = {g["id"] for g in load_games()}
    names = {g["id"]: g.get("name", g["id"]) for g in load_games(include_deleted=True)}
    return [{"id": gid, "name": names.get(gid, gid)}
            for gid in load_local_configs() if gid not in live]


def unassign_game(game_cfg: dict) -> list[tuple[str, bool]]:
    """Remove a game's assignment from this machine. Saves on disk are never touched:
    only the Steam hook, launch wrapper, save symlink and local sync state go."""
    game_id = game_cfg["id"]
    mc = get_local_config(game_id)
    if not mc:
        return [("Not assigned on this machine", True)]

    out = []
    try:
        ok, msg = restore_steam_launch(game_cfg, mc)
    except Exception as e:
        ok, msg = False, f"Failed to restore Steam launch: {e}"
    out.append((msg, ok))

    wrapper = launch_wrapper_path(game_cfg["name"])
    if wrapper.exists():
        wrapper.unlink()
        out.append(("Launch wrapper removed", True))

    # Linux: SYNC_ROOT/saves/<id> is a symlink into the real save folder. Only the
    # link is removed; if it's a real directory something unexpected is going on.
    link = saves_link_path(game_id)
    if link.is_symlink():
        link.unlink()
        out.append(("Save symlink removed (save files left in place)", True))

    clear_local_sync_state(game_id)
    remove_local_config(game_id)
    out.append(("Machine config removed", True))
    log(f"Unassigned '{game_cfg['name']}' ({game_id}) from this machine")
    return out


def _remove_cloud_data(game_id: str) -> list[tuple[str, bool]]:
    """Move cloud saves to saves_backup/ (recoverable) and drop cloud artwork."""
    out = []
    remote  = rclone_mod.RCLONE_REMOTE
    rclone  = rclone_mod.rclone_cmd()
    backup  = f"{remote}/saves_backup/{game_id}/deleted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    r = _run([rclone, "lsf", f"{remote}/saves/{game_id}"], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        r = _run([rclone, "move", f"{remote}/saves/{game_id}", backup, "--delete-empty-src-dirs"],
                 capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            _run([rclone, "rmdir", f"{remote}/saves/{game_id}"], capture_output=True, text=True, timeout=30)
            out.append((f"Cloud saves moved to saves_backup/{game_id}/", True))
        else:
            out.append((f"Could not move cloud saves: {r.stderr.strip()}", False))

    r = _run([rclone, "lsf", f"{remote}/art/{game_id}"], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        r = _run([rclone, "purge", f"{remote}/art/{game_id}"], capture_output=True, text=True, timeout=60)
        out.append(("Cloud artwork removed" if r.returncode == 0
                    else f"Could not remove cloud artwork: {r.stderr.strip()}", r.returncode == 0))
    return out


def delete_game_everywhere(game_cfg: dict, remove_cloud_data: bool = False) -> list[tuple[str, bool]]:
    """Unassign locally, then tombstone the config in games.json and push it so
    every machine drops it on its next sync."""
    game_id = game_cfg["id"]
    out = unassign_game(game_cfg) if get_local_config(game_id) else []

    rclone_mod.rclone_pull_games_json()
    if not delete_game(game_id):
        out.append(("Config not found in games.json", False))
        return out
    ok, msg = rclone_mod.rclone_push_games_json()
    out.append(("Config deleted from cloud" if ok else f"Cloud push failed: {msg}", ok))

    if remove_cloud_data:
        out += _remove_cloud_data(game_id)
    log(f"Deleted game config '{game_cfg['name']}' ({game_id})")
    return out
