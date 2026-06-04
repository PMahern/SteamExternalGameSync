"""
ExternalGameSync — games.json registry and per-machine config helpers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from config import SYNC_ROOT, GAMES_JSON


def load_games() -> list[dict]:
    """Load game configs from the synced games.json."""
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    if GAMES_JSON.exists():
        data = json.loads(GAMES_JSON.read_text())
        return data.get("games", [])
    return []

def save_games(games: list[dict]):
    """Write games list back to games.json (will be picked up by next sync)."""
    SYNC_ROOT.mkdir(parents=True, exist_ok=True)
    GAMES_JSON.write_text(json.dumps({"games": games}, indent=2))

def find_game(identifier: str) -> dict | None:
    """Find a game config by id or name (case-insensitive)."""
    ident = identifier.lower()
    for g in load_games():
        if g["id"] == identifier or g["name"].lower() == ident:
            return g
    return None

def game_id_from_name(name: str) -> str:
    return name.lower().replace(" ", "_").replace(":", "").replace("'", "").replace("/", "")


def get_local_save_path(game_id: str, game: dict | None = None) -> Path:
    """
    Return the local directory rclone should sync saves to/from.
    Linux: SYNC_ROOT/saves/<game_id>  (symlink to Proton save dir)
    Windows: the actual save folder stored in machine_configs.json
    """
    if sys.platform == "win32":
        from machine_config import get_local_config
        cfg = get_local_config(game_id)
        if cfg and cfg.get("save_path"):
            return Path(cfg["save_path"])
    return SYNC_ROOT / "saves" / game_id
