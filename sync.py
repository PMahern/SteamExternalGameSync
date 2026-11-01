"""
ExternalGameSync -- save snapshots, conflict detection, backup, and sync operations.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from config import _run, log, log_err, log_ok, load_settings, APP_CONFIG_DIR, BACKUP_ROOT, LOCK_DIR
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

def clear_local_sync_state(game_id: str):
    """Drop snapshots and last-known status for a game (used when unassigning it)."""
    _snapshot_path(game_id).unlink(missing_ok=True)
    _local_snapshot_path(game_id).unlink(missing_ok=True)
    try:
        data = json.loads(SYNC_STATE_FILE.read_text()) if SYNC_STATE_FILE.exists() else {}
        if data.pop(game_id, None) is not None:
            SYNC_STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log_err(f"clear_local_sync_state: {e}")

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

# Backups are timestamped folders: local ones under BACKUP_ROOT/<game_id>/, cloud
# ones under saves_backup/<game_id>/. Only plain-timestamp folders count toward the
# retention limit; "deleted_<ts>" folders from remove.py are never pruned.

_TS_FMT     = "%Y%m%d_%H%M%S"
_TS_PATTERN = re.compile(r"^(deleted_)?(\d{8}_\d{6})$")


def history_keep() -> int:
    """How many timestamped backups to keep per game (each side). Setting: save_history_keep."""
    try:
        return max(1, int(load_settings().get("save_history_keep", 10)))
    except (TypeError, ValueError):
        return 10


def _remote_backup_root(game_id: str) -> str:
    return f"{RCLONE_REMOTE}/saves_backup/{game_id}"


def _unique_backup_name(existing: set[str]) -> str:
    name = datetime.now().strftime(_TS_FMT)
    while name in existing:
        time.sleep(1)
        name = datetime.now().strftime(_TS_FMT)
    return name


def _prune_names(names: list[str]) -> list[str]:
    """Return the timestamped backup names that fall outside the retention limit."""
    dated = sorted(n for n in names if _TS_PATTERN.match(n) and not n.startswith("deleted_"))
    return dated[:-history_keep()] if len(dated) > history_keep() else []


def _prune_local_backups(game_id: str):
    root = BACKUP_ROOT / game_id
    if not root.is_dir():
        return
    for name in _prune_names([p.name for p in root.iterdir() if p.is_dir()]):
        shutil.rmtree(root / name, ignore_errors=True)
        log(f"Pruned old local backup {root / name}")


def _backup_local_saves(game_id: str, game: dict | None = None,
                        prune: bool = True) -> Path | None:
    """Back up local save folder to <BACKUP_ROOT>/<game_id>/<timestamp>/ and prune old ones."""
    if game is None:
        game = find_game(game_id)
    local = get_local_save_path(game_id, game)
    if not _take_local_snapshot(game_id, game):
        return None
    root = BACKUP_ROOT / game_id
    root.mkdir(parents=True, exist_ok=True)
    backup = root / _unique_backup_name({p.name for p in root.iterdir()})
    shutil.copytree(str(local), str(backup))
    log(f"Backed up local saves to {backup}")
    if prune:
        _prune_local_backups(game_id)
    return backup


def _remote_backup_names(game_id: str) -> list[str] | None:
    r = _run([rclone_cmd(), "lsf", "--dirs-only", _remote_backup_root(game_id)],
             capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return None
    return [l.strip().rstrip("/") for l in r.stdout.splitlines() if l.strip()]


def _prune_remote_backups(game_id: str, names: list[str] | None = None):
    if names is None:
        names = _remote_backup_names(game_id) or []
    for name in _prune_names(names):
        p = _run([rclone_cmd(), "purge", f"{_remote_backup_root(game_id)}/{name}"],
                 capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            log(f"Pruned old cloud backup saves_backup/{game_id}/{name}")
        else:
            log_err(f"Could not prune cloud backup {name}: {p.stderr.strip()}")


def _backup_remote_saves(game_id: str, current_remote: dict | None = None,
                         prune: bool = True) -> bool:
    """Back up remote saves to saves_backup/<game_id>/<timestamp>/ and prune old ones.
    Skipped when the cloud is empty."""
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
    if current_remote is None:
        current_remote = _take_remote_snapshot(game_id)
    if not current_remote:
        return True

    names = _remote_backup_names(game_id) or []
    backup_path = f"{_remote_backup_root(game_id)}/{_unique_backup_name(set(names))}"
    r = _run(
        [rclone_cmd(), "copy", remote_path, backup_path],
        capture_output=True, text=True, timeout=120
    )
    if r.returncode != 0:
        log_err(f"Remote backup failed: {r.stderr.strip()}")
        return False
    log(f"Backed up remote saves to {backup_path}")
    if prune:
        _prune_remote_backups(game_id, names + [backup_path.rsplit("/", 1)[1]])
    return True


# ── Save history / restore ────────────────────────────────────────────────────

def list_save_history(game_id: str) -> tuple[list[dict], bool]:
    """List every backup of a game's saves, newest first.
    Each entry: {source, name, when, files, size, newest_file}.
    Returns (entries, cloud_ok); cloud_ok is False if the cloud listing failed."""
    entries: list[dict] = []

    def _entry(source, name, files):
        m = _TS_PATTERN.match(name)
        return {
            "source":      source,
            "name":        name,
            "when":        datetime.strptime(m.group(2), _TS_FMT) if m else None,
            "deleted":     bool(m and m.group(1)),
            "files":       len(files),
            "size":        sum(s for s, _ in files),
            "newest_file": max((t for _, t in files if t), default=None),
        }

    root = BACKUP_ROOT / game_id
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir():
                files = [(f.stat().st_size, datetime.fromtimestamp(f.stat().st_mtime))
                         for f in d.rglob("*") if f.is_file()]
                entries.append(_entry("local", d.name, files))

    cloud_ok = True
    try:
        r = _run([rclone_cmd(), "lsjson", "--recursive", "--files-only", _remote_backup_root(game_id)],
                 capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        r = None
    if r is not None and r.returncode == 0:
        grouped: dict[str, list] = {}
        for f in json.loads(r.stdout or "[]"):
            top, _, rest = f["Path"].partition("/")
            if not rest:
                continue
            # rclone gives nanoseconds and sometimes "Z"; fromisoformat wants <=6 digits, +00:00
            iso = re.sub(r"(\.\d{6})\d+", r"\1", f["ModTime"].replace("Z", "+00:00"))
            try:
                mt = datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
            except ValueError:
                mt = None
            grouped.setdefault(top, []).append((f["Size"], mt))
        for name, files in grouped.items():
            entries.append(_entry("cloud", name, files))
    elif r is None or "directory not found" not in (r.stderr or "").lower():
        cloud_ok = False

    entries.sort(key=lambda e: e["when"] or datetime.min, reverse=True)
    return entries, cloud_ok


def _push_transfer(game_id: str, game: dict, local: str) -> tuple[bool, str]:
    """rclone sync local -> cloud and record snapshots. No safety checks."""
    remote_path = f"{RCLONE_REMOTE}/saves/{game_id}"
    try:
        _run([rclone_cmd(), "mkdir", remote_path], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log_err(f"push: mkdir timed out for '{game_id}' -- server unreachable")
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


def restore_save_backup(game_id: str, entry: dict,
                        game: dict | None = None) -> list[tuple[str, bool]]:
    """Make a backup the current save everywhere: back up what's there now, copy the
    backup into the local save folder, bump every file's mtime to now, and force-push
    it to the cloud. Other machines then see the cloud as changed and pull it on their
    next launch (or get a conflict prompt if they have unsynced changes of their own)."""
    if game is None:
        game = find_game(game_id)
    out: list[tuple[str, bool]] = []

    lock = GameLock(game_id)
    if not lock.acquire(timeout=2):
        return [("Game appears to be running -- close it and try again", False)]
    try:
        local = get_local_save_path(game_id, game)
        if sys.platform != "win32" and local.exists() and not local.is_symlink():
            return [(f"Save path '{local}' is not a symlink -- use Relink Saves first", False)]
        local.mkdir(parents=True, exist_ok=True)

        if entry["source"] == "cloud":
            source = f"{_remote_backup_root(game_id)}/{entry['name']}"
        else:
            source = str(BACKUP_ROOT / game_id / entry["name"])

        # Back up both sides first so the restore itself can be undone. Pruning waits
        # until the end so it can't evict the backup we're restoring from.
        lb = _backup_local_saves(game_id, game, prune=False)
        out.append((f"Current local saves backed up ({lb.name})" if lb
                    else "No current local saves to back up", True))
        rb = _backup_remote_saves(game_id, prune=False)
        out.append(("Current cloud saves backed up" if rb
                    else "Could not back up current cloud saves", rb))
        if not rb:
            return out

        fa = _filter_args(game)
        try:
            r = _run([rclone_cmd(), "sync", source, str(local), "-v"] + fa,
                     capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return out + [("Copying backup into save folder timed out", False)]
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                log(f"  rclone: {line}")
        if r.returncode != 0:
            return out + [(f"Copying backup failed: {r.stderr.strip()[-200:]}", False)]
        out.append((f"Backup {entry['name']} copied into {local}", True))

        # Fresh mtimes so the restore can't lose to newer-looking files anywhere
        now = time.time()
        for f in local.rglob("*"):
            if f.is_file():
                os.utime(f, (now, now))

        ok, msg = _push_transfer(game_id, game, str(local))
        out.append(("Restored saves uploaded to cloud" if ok else f"Upload failed: {msg}", ok))
        if ok:
            log_ok(f"restore: '{game_id}' restored from {entry['source']} backup {entry['name']}")
            _prune_local_backups(game_id)
            _prune_remote_backups(game_id)
        return out
    finally:
        lock.release()


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
            msg = (f"save path '{local}' is a plain directory, not a symlink -- "
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
        log(f"pull: conflict detected for '{game_id}' -- remote changed since last sync")
        save_sync_state(game_id, "conflict")
        return True, PULL_CONFLICT

    if status == "no_snapshot":
        # No sync history on this machine yet. Compare content directly to avoid
        # a destructive sync that could wipe existing local saves.
        current_local = _take_local_snapshot(game_id, game)
        if current_local and not current_remote:
            # Local has files, remote is empty -- local is ahead; skip pull so push
            # can establish the remote without destroying local saves.
            log(f"pull: no snapshot, remote empty but local has saves -- skipping pull for '{game_id}'")
            return True, PULL_NO_REMOTE
        if current_local and current_remote:
            # Both sides have files with no shared baseline -- treat as conflict.
            log(f"pull: no snapshot, both sides have saves for '{game_id}' -- treating as conflict")
            save_sync_state(game_id, "conflict")
            return True, PULL_CONFLICT
        # Local is empty: safe to pull whatever's on the remote.

    current_local = _take_local_snapshot(game_id, game)
    last_remote   = _load_snapshot(game_id)
    last_local    = _load_local_snapshot(game_id)
    if (status == "clean" and last_remote is not None and current_remote is not None
            and current_local
            and not _snapshots_differ(last_remote, current_remote)
            and not set(current_remote) - set(last_remote)):
        # Nothing new on the cloud. Pulling anyway would revert any local changes
        # made since the last sync (e.g. saves copied in by hand); push sends them up.
        log(f"pull: cloud unchanged since last sync for '{game_id}' -- keeping local saves")
        return True, PULL_OK

    if current_local and (last_local is None
                          or _snapshots_differ(last_local, current_local)
                          or set(current_local) - set(last_local)):
        # Local has changes that never reached the cloud; keep a copy before overwriting
        _backup_local_saves(game_id, game)

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

    # Only local can be overwritten here; the push that follows keep=local backs up the cloud
    _backup_local_saves(game_id, game)

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
    log(f"conflict resolved (kept local): saves/{game_id} -- will push on exit")
    save_sync_state(game_id, "in_sync")
    return True, PULL_OK


def rclone_sync_push(game_id: str, game: dict | None = None) -> tuple[bool, str]:
    """Push saves from local to remote after exit."""
    if game is None:
        game = find_game(game_id)
    local = str(get_local_save_path(game_id, game))

    if sys.platform != "win32":
        lp = Path(local)
        if lp.exists() and not lp.is_symlink():
            msg = (f"save path '{local}' is a plain directory, not a symlink -- "
                   f"run 'externalgamesync gui' and use Relink to fix this before syncing")
            log_err(msg)
            return False, "failed"

    last_local    = _load_local_snapshot(game_id)
    current_local = _take_local_snapshot(game_id, game)
    if last_local:
        if not current_local:
            msg = (f"push blocked for '{game_id}': local save folder is empty but previous sync had files -- "
                   f"run pull first to restore saves before pushing")
            log_err(msg)
            return False, "push blocked (local empty, cloud has saves)"
        if (_load_snapshot(game_id) is not None
                and not _snapshots_differ(last_local, current_local)
                and not set(current_local) - set(last_local)):
            # Pushing an unchanged local copy could only revert newer cloud saves
            log(f"push: no local changes since last sync for '{game_id}' -- nothing to push")
            return True, "ok"

    # Keep the cloud version we're about to replace in saves_backup/ (history)
    current_remote = _take_remote_snapshot(game_id)
    if current_remote:
        _backup_remote_saves(game_id, current_remote)

    return _push_transfer(game_id, game, local)


def initial_sync(game_id: str, game: dict) -> tuple[str, bool]:
    """First sync after assigning/adding a game. Returns a (message, ok) log line.
    Never pushes over a conflict -- that's how fresh empty saves overwrite real ones."""
    ok1, msg1 = rclone_sync_pull(game_id, game)
    if msg1 == PULL_CONFLICT:
        return ("Initial sync skipped: this machine and the cloud both have saves. "
                "Resolve it on the Sync page (or launch the game) and pick which to keep.",
                False)
    if not ok1:
        return f"Initial sync: pull failed ({msg1})", False
    ok2, msg2 = rclone_sync_push(game_id, game)
    return ("Initial sync", True) if ok2 else (f"Initial sync: push failed ({msg2})", False)


def rclone_bisync(game_id: str, resync: bool = False) -> tuple[bool, str]:
    """Compatibility shim -- pull then push."""
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
