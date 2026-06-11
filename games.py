"""
ExternalGameSync -- games.json registry and per-machine config helpers.
"""

from __future__ import annotations

import hashlib
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


def hash_file(path: Path) -> str:
    """Partial SHA-256 fingerprint: first 4 MB of content + 8-byte little-endian file size.
    Fast for large installers; equivalent to a full hash for files under 4 MB."""
    CHUNK = 4 * 1024 * 1024
    h = hashlib.sha256()
    size = path.stat().st_size
    with open(path, "rb") as f:
        h.update(f.read(CHUNK))
    h.update(size.to_bytes(8, "little"))
    return f"sha256p:{h.hexdigest()}"


def add_game_hash(game_id: str, hash_val: str, field: str) -> bool:
    """Append hash_val to field ('exe_hashes' or 'installer_hashes') if not already present.
    Saves games.json locally and returns True if the file was modified."""
    games = load_games()
    for g in games:
        if g["id"] == game_id:
            hashes = g.get(field, [])
            if hash_val in hashes:
                return False
            g[field] = hashes + [hash_val]
            save_games(games)
            return True
    return False


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
