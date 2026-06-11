"""
ExternalGameSync -- rclone availability, remote setup, and games.json push/pull.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from config import _run, log_err, GAMES_JSON
from games import load_games, save_games

NC_REMOTE_NAME = "externalgamesync_nc"
NC_FOLDER      = "ExternalGameSync"
RCLONE_REMOTE  = f"{NC_REMOTE_NAME}:{NC_FOLDER}"


# ── Binary discovery ──────────────────────────────────────────────────────────

def rclone_available() -> bool:
    if shutil.which("rclone"):
        return True
    return (Path.home() / ".local" / "bin" / "rclone").exists()


def rclone_cmd() -> str:
    """Return the rclone binary path."""
    which = shutil.which("rclone")
    if which:
        return which
    local_bin = Path.home() / ".local" / "bin" / "rclone"
    if local_bin.exists():
        return str(local_bin)
    return "rclone"


# ── Remote setup ──────────────────────────────────────────────────────────────

def rclone_setup_webdav(url: str, vendor: str, username: str, password: str) -> tuple[bool, str]:
    """Create a WebDAV rclone remote. vendor: nextcloud | owncloud | other"""
    if not rclone_available():
        return False, "rclone is not installed"
    r = _run(
        [rclone_cmd(), "config", "create", NC_REMOTE_NAME, "webdav",
         "url", url, "vendor", vendor, "user", username, "pass", password],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


_OAUTH_EXTRA_PARAMS: dict[str, list[str]] = {
    "drive": ["scope", "drive"],
}


def rclone_setup_oauth(provider_type: str) -> tuple[bool, str]:
    """Authorize a browser-based OAuth provider (dropbox/drive/onedrive) and create rclone remote."""
    if not rclone_available():
        return False, "rclone is not installed"
    try:
        args = ([rclone_cmd(), "config", "create", NC_REMOTE_NAME, provider_type]
                + _OAUTH_EXTRA_PARAMS.get(provider_type, []))
        r = _run(args, capture_output=True, text=True, timeout=300,
                 stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "Authorization timed out (5 min limit)"
    if r.returncode != 0:
        return False, r.stderr.strip() or "Authorization failed or was cancelled"
    return True, ""


def rclone_setup_sftp(host: str, port: int, username: str, password: str) -> tuple[bool, str]:
    """Create an SFTP rclone remote."""
    if not rclone_available():
        return False, "rclone is not installed"
    r = _run(
        [rclone_cmd(), "config", "create", NC_REMOTE_NAME, "sftp",
         "host", host, "port", str(port), "user", username, "pass", password],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


def rclone_test() -> tuple[bool, str]:
    """Test the rclone remote connection."""
    r = _run(
        [rclone_cmd(), "lsd", f"{NC_REMOTE_NAME}:"],
        capture_output=True, text=True, timeout=15
    )
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


def rclone_ensure_remote_folder() -> bool:
    """Create the ExternalGameSync folder on the remote if it doesn't exist."""
    try:
        r = _run([rclone_cmd(), "mkdir", RCLONE_REMOTE],
                 capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0


# ── games.json push / pull ────────────────────────────────────────────────────

def rclone_pull_games_json() -> tuple[bool, str]:
    """Pull games.json from remote and merge with local using timestamps (newer wins)."""
    remote_file = f"{RCLONE_REMOTE}/games.json"
    local_games = load_games()
    remote_games = _fetch_remote_games()
    if not remote_games:
        # Nothing on server yet -- nothing to pull
        return True, "no existing config (fresh start)"
    merged = merge_games(local_games, remote_games)
    save_games(merged)
    return True, "pulled existing config"


_MACHINE_FIELDS = ("machine_configs", "machine_app_ids")

def _strip_machine_fields(g: dict) -> dict:
    return {k: v for k, v in g.items() if k not in _MACHINE_FIELDS}

def merge_games(local: list[dict], remote: list[dict]) -> list[dict]:
    """Merge two game lists by id using 'added' timestamp -- newer entry wins.
    Games present on only one side are kept as-is. Ties go to local."""
    from datetime import datetime

    def _ts(g: dict):
        try:
            return datetime.fromisoformat(g.get("added", ""))
        except (ValueError, TypeError):
            return datetime.min

    remote_by_id = {g["id"]: g for g in remote}
    local_by_id  = {g["id"]: g for g in local}
    result = []
    for gid in set(remote_by_id) | set(local_by_id):
        lg = local_by_id.get(gid)
        rg = remote_by_id.get(gid)
        if lg is None:
            result.append(_strip_machine_fields(rg))
        elif rg is None:
            result.append(_strip_machine_fields(lg))
        else:
            winner = lg if _ts(lg) >= _ts(rg) else rg
            result.append(_strip_machine_fields(winner))
    return result


def _fetch_remote_games() -> list[dict]:
    """Fetch games.json from remote into a temp file. Returns [] on failure."""
    remote_file = f"{RCLONE_REMOTE}/games.json"
    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        r = _run([rclone_cmd(), "copyto", remote_file, str(tmp)],
                 capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        return json.loads(tmp.read_text()).get("games", [])
    except Exception:
        return []
    finally:
        tmp.unlink(missing_ok=True)


def rclone_sync_games_json() -> tuple[bool, str]:
    """Pull games.json from remote. If none exists there, push local copy up."""
    ok, msg = rclone_pull_games_json()
    if not ok:
        return False, msg
    if msg == "no existing config (fresh start)":
        ok, push_err = rclone_push_games_json()
        if not ok:
            return False, f"No remote config found; local push also failed: {push_err}"
        return True, "No config found on server - pushed local game list up"
    return True, ""


def rclone_push_games_json() -> tuple[bool, str]:
    """Merge local games.json with the current remote version, then push.
    Prevents concurrent edits from different machines overwriting each other."""
    remote_file   = f"{RCLONE_REMOTE}/games.json"
    remote_games  = _fetch_remote_games()
    if remote_games:
        merged = merge_games(load_games(), remote_games)
        save_games(merged)
    try:
        r = _run(
            [rclone_cmd(), "copyto", str(GAMES_JSON), remote_file],
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return False, "Connection timed out (30 s) -- check VPN / server"
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""
