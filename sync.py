"""
ExternalGameSync — save snapshots, conflict detection, backup, and sync operations.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from config import _run, log, log_err, log_ok, APP_CONFIG_DIR, BACKUP_ROOT, LOCK_DIR
from games import find_game, get_local_save_path
from rclone import rclone_cmd, RCLONE_REMOTE

if sys.platform != "win32":
    import fcntl

# ── Pull result codes ─────────────────────────────────────────────────────────

PULL_OK            = "ok"
PULL_NO_REMOTE     = "no_remote"
PULL_CONFLICT      = "conflict"
PULL_FAILED        = "failed"
PULL_NO_CONNECTION = "no_connection"


# ── Snapshots ─────────────────────────────────────────────────────────────────

SNAPSHOT_DIR   = APP_CONFIG_DIR / "snapshots"
SYNC_STATE_FILE = APP_CONFIG_DIR / "sync_states.json"


def save_sync_state(game_id: str, status: str) -> None:
    """Persist the last known sync status for a game to disk."""
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(SYNC_STATE_FILE.read_text()) if SYNC_STATE_FILE.exists() else {}
    except Exception:
        data = {}
    data[game_id] = {"status": status, "updated": datetime.now().isoformat()}
    try:
        SYNC_STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log_err(f"save_sync_state: {e}")


def load_sync_state(game_id: str) -> str | None:
    """Return the last known sync status for a game, or None if unavailable."""
    if not SYNC_STATE_FILE.exists():
        return None
    try:
        data = json.loads(SYNC_STATE_FILE.read_text())
        return data.get(game_id, {}).get("status")
    except Exception:
        return None


def _snapshot_path(game_id: str) -> Path:
    return SNAPSHOT_DIR / f"{game_id}.json"

def _local_snapshot_path(game_id: str) -> Path:
    return SNAPSHOT_DIR / f"{game_id}_local.json"

def clear_remote_snapshots():
    """Invalidate all cached remote snapshots. Call when switching cloud providers."""
    for p in SNAPSHOT_DIR.glob("*.json"):
        if not p.name.endswith("_local.json"):
            p.unlink(missing_ok=True)


def _filter_args(game: dict | None) -> list[str]:
    """Return rclone --include/--exclude args for a game's save_filter, or []."""
    f = (game or {}).get("save_filter", "").strip()
    if not f:
        return []
    return ["--include", f, "--exclude", "*"]


def _take_remote_snapshot(game_id: str, filter_args: list[str] | None = None) -> dict | None:
    """Use rclone lsjson to snapshot the remote save folder.
    Returns {filename: {size, mod_time}} or None on failure."""
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
    try:
        r = _run(
            [rclone_cmd(), "lsjson", "--recursive", remote_path] + (filter_args or []),
            capture_output=True, text=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        log_err(f"_take_remote_snapshot: timeout for '{game_id}'")
        return None
    if r.returncode != 0:
        return None
    try:
        files = json.loads(r.stdout)
        return {
            f["Path"]: {"size": f["Size"], "mod_time": f["ModTime"]}
            for f in files if not f.get("IsDir")
        }
    except Exception as e:
        log_err(f"snapshot parse failed: {e}")
        return None


def _take_local_snapshot(game_id: str, game: dict | None = None) -> dict:
    """Snapshot local save folder."""
    local  = get_local_save_path(game_id, game)
    result = {}
    if not local.exists():
        return result
    for f in local.rglob("*"):
        if f.is_file():
            stat = f.stat()
            result[str(f.relative_to(local))] = {
                "size": stat.st_size, "mod_time": stat.st_mtime
            }
    return result


def _load_snapshot(game_id: str) -> dict | None:
    p = _snapshot_path(game_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_snapshot(game_id: str, snapshot: dict):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    _snapshot_path(game_id).write_text(json.dumps(snapshot, indent=2))


def _load_local_snapshot(game_id: str) -> dict | None:
    p = _local_snapshot_path(game_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _save_local_snapshot(game_id: str, game: dict | None = None):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = _take_local_snapshot(game_id, game)
    _local_snapshot_path(game_id).write_text(json.dumps(snapshot, indent=2))


def _is_true_conflict(last_remote: dict, current_remote: dict,
                      last_local: dict | None, current_local: dict) -> bool:
    """True if both sides changed the same file since the last snapshot."""
    if last_local is None:
        return False

    for path in last_remote:
        remote_changed = (
            path not in current_remote
            or last_remote[path]["size"] != current_remote[path].get("size")
            or last_remote[path].get("mod_time") != current_remote[path].get("mod_time")
        )
        if not remote_changed:
            continue
        local_last = last_local.get(path)
        local_curr = current_local.get(path)
        if local_last is None:
            continue
        if local_curr is None:
            return True
        if (local_last["size"] != local_curr["size"]
                or local_last.get("mod_time") != local_curr.get("mod_time")):
            return True

    new_on_remote = set(current_remote) - set(last_remote)
    new_on_local  = set(current_local)  - set(last_local)
    return bool(new_on_remote & new_on_local)


def _snapshots_differ(a: dict, b: dict) -> bool:
    """True if files from snapshot a have changed or been deleted in b."""
    for key in a:
        if key not in b:
            return True
        if a[key]["size"] != b[key]["size"]:
            return True
        if a[key].get("mod_time") != b[key].get("mod_time"):
            return True
    return False


def get_sync_status(game_id: str, game: dict | None = None) -> str:
    """
    Determine sync status without transferring save data.
    The remote check uses rclone lsjson (metadata listing only).
    Returns one of: "in_sync", "cloud_ahead", "local_ahead", "conflict",
                    "unknown", "no_connection", "error"
    "unknown" means no snapshots exist yet (game hasn't been synced from this machine).
    """
    if game is None:
        game = find_game(game_id)

    last_remote = _load_snapshot(game_id)
    last_local  = _load_local_snapshot(game_id)
    if last_remote is None or last_local is None:
        return "unknown"

    fa = _filter_args(game)

    current_local = _take_local_snapshot(game_id, game)
    local_changed = (
        _snapshots_differ(last_local, current_local)
        or bool(set(current_local) - set(last_local))
    )

    current_remote = _take_remote_snapshot(game_id, fa)
    if current_remote is None:
        try:
            r = _run(
                [rclone_cmd(), "lsd", f"{RCLONE_REMOTE.split(':')[0]}:"],
                capture_output=True, text=True, timeout=10,
            )
            return "no_connection" if r.returncode != 0 else "error"
        except subprocess.TimeoutExpired:
            return "no_connection"

    remote_changed = (
        _snapshots_differ(last_remote, current_remote)
        or bool(set(current_remote) - set(last_remote))
    )

    if local_changed and remote_changed:
        status = "conflict"
    elif remote_changed:
        status = "cloud_ahead"
    elif local_changed:
        status = "local_ahead"
    else:
        status = "in_sync"
    save_sync_state(game_id, status)
    return status


# ── Backup ────────────────────────────────────────────────────────────────────

def _backup_local_saves(game_id: str) -> Path:
    """Back up local save folder to <BACKUP_ROOT>/<game_id>/<timestamp>/."""
    game   = find_game(game_id)
    local  = get_local_save_path(game_id, game)
    backup = BACKUP_ROOT / game_id / datetime.now().strftime("%Y%m%d_%H%M%S")
    if local.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(local), str(backup))
        log(f"Backed up local saves to {backup}")
    return backup


def _backup_remote_saves(game_id: str) -> bool:
    """Back up remote saves to ExternalGameSync/saves_backup/<game_id>/<timestamp>/."""
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{RCLONE_REMOTE}/saves_backup/{game_id}/{timestamp}"
    r = _run(
        [rclone_cmd(), "copy", remote_path, backup_path],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode == 0:
        log(f"Backed up remote saves to {backup_path}")
    else:
        log_err(f"Remote backup failed: {r.stderr.strip()}")
    return r.returncode == 0


# ── Conflict detection ────────────────────────────────────────────────────────

def check_for_conflict(game_id: str, game: dict | None = None) -> tuple[str, dict | None]:
    """
    Compare the current remote snapshot against the last known snapshot.
    Returns (status, current_remote_snapshot) where status is one of:
      "no_connection", "no_remote", "no_snapshot", "clean", "conflict", "error"
    """
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
    try:
        check = _run(
            [rclone_cmd(), "lsd", remote_path],
            capture_output=True, text=True, timeout=15
        )
    except subprocess.TimeoutExpired:
        log_err(f"check_for_conflict: timeout reaching '{remote_path}'")
        return "no_connection", None

    if check.returncode != 0:
        try:
            conn_check = _run(
                [rclone_cmd(), "lsd", f"{RCLONE_REMOTE.split(':')[0]}:"],
                capture_output=True, text=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            return "no_connection", None
        if conn_check.returncode != 0:
            return "no_connection", None
        return "no_remote", None

    fa = _filter_args(game)
    current_remote = _take_remote_snapshot(game_id, fa)
    if current_remote is None:
        return "error", None

    last_snapshot = _load_snapshot(game_id)
    if last_snapshot is None:
        return "no_snapshot", current_remote

    last_local = _load_local_snapshot(game_id)
    if last_local is not None:
        current_local = _take_local_snapshot(game_id, game)
        remote_changed = (
            _snapshots_differ(last_snapshot, current_remote)
            or bool(set(current_remote) - set(last_snapshot))
        )
        local_changed = (
            _snapshots_differ(last_local, current_local)
            or bool(set(current_local) - set(last_local))
        )
        if remote_changed and local_changed:
            return "conflict", current_remote

    return "clean", current_remote


# ── Sync operations ───────────────────────────────────────────────────────────

def rclone_sync_pull(game_id: str, game: dict | None = None) -> tuple[bool, str]:
    """Pull saves from remote before launch. Returns (success, status_code)."""
    if game is None:
        game = find_game(game_id)
    local       = str(get_local_save_path(game_id, game))
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"

    if sys.platform != "win32":
        lp = Path(local)
        if lp.exists() and not lp.is_symlink():
            msg = (f"save path '{local}' is a plain directory, not a symlink — "
                   f"run 'externalgamesync gui' and use Relink to fix this before syncing")
            log_err(msg)
            return False, PULL_FAILED

    Path(local).mkdir(parents=True, exist_ok=True)

    fa = _filter_args(game)
    status, current_remote = check_for_conflict(game_id, game)

    if status == "no_connection":
        log_err(f"pull: cannot reach remote for '{game_id}'")
        return False, PULL_NO_CONNECTION

    if status == "no_remote":
        log(f"pull: no remote folder yet for '{game_id}', skipping")
        return True, PULL_NO_REMOTE

    if status == "error":
        log_err(f"pull: could not reach remote for '{game_id}'")
        return False, PULL_FAILED

    if status == "conflict":
        log(f"pull: conflict detected for '{game_id}' — remote changed since last sync")
        save_sync_state(game_id, "conflict")
        return True, PULL_CONFLICT

    if status == "no_snapshot":
        # No sync history on this machine yet. Compare content directly to avoid
        # a destructive sync that could wipe existing local saves.
        current_local = _take_local_snapshot(game_id, game)
        if current_local and not current_remote:
            # Local has files, remote is empty — local is ahead; skip pull so push
            # can establish the remote without destroying local saves.
            log(f"pull: no snapshot, remote empty but local has saves — skipping pull for '{game_id}'")
            return True, PULL_NO_REMOTE
        if current_local and current_remote:
            # Both sides have files with no shared baseline — treat as conflict.
            log(f"pull: no snapshot, both sides have saves for '{game_id}' — treating as conflict")
            save_sync_state(game_id, "conflict")
            return True, PULL_CONFLICT
        # Local is empty: safe to pull whatever's on the remote.

    log(f"pull: remote -> local '{game_id}' ({status})")
    cmd = [rclone_cmd(), "sync", "--ignore-size", remote_path, local, "-v"] + fa
    try:
        result = _run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log_err(f"pull: copy timed out for '{game_id}'")
        return False, PULL_FAILED

    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            log(f"  rclone: {line}")

    if result.returncode == 0:
        if current_remote is not None:
            _save_snapshot(game_id, current_remote)
        _save_local_snapshot(game_id, game)
        save_sync_state(game_id, "in_sync")
        log_ok(f"pull OK: saves/{game_id}")
        return True, PULL_OK

    log_err(f"pull failed (rc={result.returncode}): saves/{game_id}")
    return False, PULL_FAILED


def rclone_sync_pull_force(game_id: str, keep: str,
                           game: dict | None = None) -> tuple[bool, str]:
    """Resolve a conflict: keep='remote' overwrites local; keep='local' keeps local."""
    log(f"conflict resolve: keep={keep} for '{game_id}'")
    if game is None:
        game = find_game(game_id)

    _backup_local_saves(game_id)
    _backup_remote_saves(game_id)

    fa = _filter_args(game)

    if keep == "remote":
        local       = str(get_local_save_path(game_id, game))
        remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
        cmd = [rclone_cmd(), "sync", remote_path, local, "-v"] + fa
        try:
            result = _run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            log_err(f"pull_force: sync timed out for '{game_id}'")
            return False, PULL_FAILED
        for line in (result.stdout + result.stderr).splitlines():
            if line.strip():
                log(f"  rclone: {line}")
        if result.returncode == 0:
            snap = _take_remote_snapshot(game_id, fa)
            if snap:
                _save_snapshot(game_id, snap)
            _save_local_snapshot(game_id, game)
            save_sync_state(game_id, "in_sync")
            log_ok(f"conflict resolved (kept remote): saves/{game_id}")
            return True, PULL_OK
        return False, PULL_FAILED

    # keep == "local"
    log(f"conflict resolved (kept local): saves/{game_id} — will push on exit")
    save_sync_state(game_id, "in_sync")
    return True, PULL_OK


def rclone_sync_push(game_id: str, game: dict | None = None) -> tuple[bool, str]:
    """Push saves from local to remote after exit."""
    if game is None:
        game = find_game(game_id)
    local       = str(get_local_save_path(game_id, game))
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"

    if sys.platform != "win32":
        lp = Path(local)
        if lp.exists() and not lp.is_symlink():
            msg = (f"save path '{local}' is a plain directory, not a symlink — "
                   f"run 'externalgamesync gui' and use Relink to fix this before syncing")
            log_err(msg)
            return False, "failed"

    last_local = _load_local_snapshot(game_id)
    if last_local:
        current_local = _take_local_snapshot(game_id, game)
        if not current_local:
            msg = (f"push blocked for '{game_id}': local save folder is empty but previous sync had files — "
                   f"run pull first to restore saves before pushing")
            log_err(msg)
            return False, "push blocked (local empty, cloud has saves)"

    try:
        _run([rclone_cmd(), "mkdir", remote_path], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log_err(f"push: mkdir timed out for '{game_id}' — server unreachable")
        return False, "push failed (mkdir timed out)"

    fa  = _filter_args(game)
    cmd = [rclone_cmd(), "sync", "--ignore-size", local, remote_path, "-v"] + fa
    log(f"push: local -> remote '{game_id}' (sync)")
    try:
        result = _run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log_err(f"push: sync timed out for '{game_id}'")
        return False, "push failed (sync timed out)"

    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            log(f"  rclone: {line}")

    if result.returncode == 0:
        snap = _take_remote_snapshot(game_id, fa)
        if snap is not None:
            _save_snapshot(game_id, snap)
        _save_local_snapshot(game_id, game)
        save_sync_state(game_id, "in_sync")
        log_ok(f"push OK: saves/{game_id}")
        return True, "ok"

    msg = f"push failed (rc={result.returncode})"
    log_err(f"{msg}: saves/{game_id}")
    return False, msg


def rclone_bisync(game_id: str, resync: bool = False) -> tuple[bool, str]:
    """Compatibility shim — pull then push."""
    ok1, msg1 = rclone_sync_pull(game_id)
    if not ok1 or msg1 == PULL_CONFLICT:
        return ok1, msg1
    ok2, msg2 = rclone_sync_push(game_id)
    if ok2:
        return True, "ok"
    return False, msg2


# ── Lock file ─────────────────────────────────────────────────────────────────

class GameLock:
    def __init__(self, game_id: str):
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOCK_DIR / f"{game_id}.lock"
        self.fh   = None

    def acquire(self, timeout=10) -> bool:
        self.fh = open(self.path, "w")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fh.write(str(os.getpid()))
                self.fh.flush()
                return True
            except OSError:
                time.sleep(0.5)
        return False

    def release(self):
        if self.fh:
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
