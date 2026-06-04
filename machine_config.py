"""
ExternalGameSync -- per-machine local config (machine_configs.json).
Stores this machine's game configs outside the shared games.json so that
games.json stays clean and shareable across machines and users.
"""

from __future__ import annotations

import json
from pathlib import Path

from config import APP_CONFIG_DIR, GAMES_JSON, hostname, log, is_configured

MACHINE_CONFIGS_FILE = APP_CONFIG_DIR / "machine_configs.json"


def load_local_configs() -> dict:
    """Return {game_id: cfg} for this machine."""
    if MACHINE_CONFIGS_FILE.exists():
        return json.loads(MACHINE_CONFIGS_FILE.read_text())
    return {}


def save_local_configs(configs: dict):
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MACHINE_CONFIGS_FILE.write_text(json.dumps(configs, indent=2))


def get_local_config(game_id: str) -> dict | None:
    """Return this machine's config for a game, or None if not configured."""
    return load_local_configs().get(game_id)


def set_local_config(game_id: str, cfg: dict):
    """Write this machine's config for a game to the local file."""
    configs = load_local_configs()
    configs[game_id] = cfg
    save_local_configs(configs)


def migrate_from_games_json():
    """
    One-time migration from the old format where machine configs lived inside games.json.
    Reads the LOCAL games.json (not the remote) so the data is available even if
    another machine has already pushed a clean version to the server.
    Must be called before any rclone pull.
    """
    if MACHINE_CONFIGS_FILE.exists():
        return

    if not GAMES_JSON.exists():
        save_local_configs({})
        return

    try:
        data = json.loads(GAMES_JSON.read_text())
    except Exception:
        save_local_configs({})
        return

    host = hostname()
    configs = {}
    for game in data.get("games", []):
        game_id = game.get("id")
        if not game_id:
            continue
        mc = game.get("machine_configs", {}).get(host)
        if mc:
            configs[game_id] = mc
            continue
        # Legacy Linux format
        app_id = game.get("machine_app_ids", {}).get(host)
        if app_id:
            configs[game_id] = {"platform": "linux", "app_id": str(app_id)}

    save_local_configs(configs)

    had_machine_data = any(
        "machine_configs" in g or "machine_app_ids" in g
        for g in data.get("games", [])
    )
    if had_machine_data and is_configured():
        # Push immediately so the server gets a clean games.json without machine data.
        # Each machine has its own copy of its config locally, so nothing is lost.
        from rclone import rclone_push_games_json
        rclone_push_games_json()
        log(f"Migration complete: pushed clean games.json ({len(configs)} game(s) extracted locally)")
