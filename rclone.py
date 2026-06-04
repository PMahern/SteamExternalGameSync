"""
ExternalGameSync — rclone availability, remote setup, and games.json push/pull.
"""

from __future__ import annotations

import json
import re
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
        r = _run(
            [rclone_cmd(), "authorize", provider_type],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        return False, "Authorization timed out (5 min limit)"
    if r.returncode != 0:
        return False, r.stderr.strip() or "Authorization failed or was cancelled"
    token = _parse_oauth_token(r.stdout)
    if not token:
        return False, "Could not read authorization token from rclone output"
    args = ([rclone_cmd(), "config", "create", NC_REMOTE_NAME, provider_type,
             "token", token]
            + _OAUTH_EXTRA_PARAMS.get(provider_type, []))
    r = _run(args, capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""


def _parse_oauth_token(output: str) -> str:
    m = re.search(r'--->\s*(\{.*?\})\s*<---', output, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r'\{[^{}]*"access_token"[^{}]*\}', output)
    if m:
        return m.group(0)
    return ""


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
    r = _run([rclone_cmd(), "mkdir", RCLONE_REMOTE], capture_output=True, text=True)
    return r.returncode == 0


# ── games.json push / pull ────────────────────────────────────────────────────

def rclone_pull_games_json() -> tuple[bool, str]:
    """Copy games.json from remote to local SYNC_ROOT."""
    remote_file = f"{RCLONE_REMOTE}/games.json"
    r = _run(
        [rclone_cmd(), "copyto", remote_file, str(GAMES_JSON)],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        if any(p in r.stderr.lower() for p in ("not found", "no such", "404", "doesn't exist", "does not exist")):
            return True, "no existing config (fresh start)"
        return False, r.stderr.strip()
    return True, "pulled existing config"


_MACHINE_FIELDS = ("machine_configs", "machine_app_ids")

def _strip_machine_fields(g: dict) -> dict:
    return {k: v for k, v in g.items() if k not in _MACHINE_FIELDS}

def merge_games(local: list[dict], remote: list[dict]) -> list[dict]:
    """Union two games lists by id. Machine-specific fields are kept out of the shared config."""
    remote_by_id = {g["id"]: g for g in remote}
    local_by_id  = {g["id"]: g for g in local}
    result = []
    for gid, rg in remote_by_id.items():
        if gid in local_by_id:
            merged = {**_strip_machine_fields(rg), **_strip_machine_fields(local_by_id[gid])}
            result.append(merged)
        else:
            result.append(_strip_machine_fields(rg))
    for gid, lg in local_by_id.items():
        if gid not in remote_by_id:
            result.append(_strip_machine_fields(lg))
    return result


def _fetch_remote_games() -> list[dict]:
    """Fetch games.json from remote into a temp file. Returns [] on failure."""
    remote_file = f"{RCLONE_REMOTE}/games.json"
    tmp = Path(tempfile.mktemp(suffix=".json"))
    try:
        r = _run([rclone_cmd(), "copyto", remote_file, str(tmp)],
                 capture_output=True, text=True)
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
    r = _run(
        [rclone_cmd(), "copyto", str(GAMES_JSON), remote_file],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, ""
