"""
ExternalGameSync -- Steam grid artwork push/pull via cloud storage.
"""

from __future__ import annotations

from pathlib import Path

from config import _run, log, log_err, log_ok
from rclone import rclone_cmd, RCLONE_REMOTE
from steam import find_shortcuts_vdf

ART_TYPES = {
    "portrait":  "p",      # vertical cover     e.g. 123456789p.png
    "landscape": "",       # horizontal banner  e.g. 123456789.png
    "hero":      "_hero",  # hero banner
    "logo":      "_logo",  # logo
    "icon":      "_icon",  # icon
}


def find_grid_dir() -> Path | None:
    """Find Steam's grid artwork folder under the same user as shortcuts.vdf."""
    vdf_path = find_shortcuts_vdf()
    if vdf_path:
        return vdf_path.parent / "grid"
    return None


def find_art_files(appid: str) -> dict[str, Path]:
    """Find all existing art files for a given Steam appid."""
    grid = find_grid_dir()
    if not grid:
        return {}
    found = {}
    for art_type, suffix in ART_TYPES.items():
        for ext in ["png", "jpg", "jpeg", "gif", "webp"]:
            candidate = grid / f"{appid}{suffix}.{ext}"
            if candidate.exists():
                found[art_type] = candidate
                break
    return found


def push_art_to_nextcloud(game_id: str, appid: str) -> dict[str, bool]:
    """Copy art from local Steam grid to remote ExternalGameSync/art/<game_id>/."""
    art_files  = find_art_files(appid)
    remote_art = f"{RCLONE_REMOTE}/art/{game_id}"
    _run([rclone_cmd(), "mkdir", remote_art], capture_output=True, text=True)

    results = {}
    for art_type, local_path in art_files.items():
        remote_file = f"{remote_art}/{art_type}{local_path.suffix}"
        r = _run(
            [rclone_cmd(), "copyto", str(local_path), remote_file],
            capture_output=True, text=True
        )
        results[art_type] = r.returncode == 0
        if r.returncode == 0:
            log_ok(f"art push: {art_type} for {game_id}")
        else:
            log_err(f"art push failed: {art_type} for {game_id}: {r.stderr.strip()}")
    return results


def pull_art_from_nextcloud(game_id: str, appid: str) -> dict[str, bool]:
    """Download art from remote and place into local Steam grid with appid-based filenames."""
    grid = find_grid_dir()
    if not grid:
        log_err("pull_art: Steam grid folder not found")
        return {}
    grid.mkdir(parents=True, exist_ok=True)
    log(f"pull_art: grid folder -> {grid}")

    remote_art = f"{RCLONE_REMOTE}/art/{game_id}"
    check = _run([rclone_cmd(), "lsd", remote_art], capture_output=True, text=True, timeout=10)
    if check.returncode != 0:
        log(f"pull_art: no remote art for {game_id}")
        return {}

    ls = _run([rclone_cmd(), "ls", remote_art], capture_output=True, text=True, timeout=15)
    if ls.returncode != 0:
        return {}

    results = {}
    for line in ls.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        filename   = parts[1].strip()
        stem       = Path(filename).stem
        ext        = Path(filename).suffix
        if stem not in ART_TYPES:
            continue
        local_path = grid / f"{appid}{ART_TYPES[stem]}{ext}"
        r = _run(
            [rclone_cmd(), "copyto", f"{remote_art}/{filename}", str(local_path)],
            capture_output=True, text=True
        )
        results[stem] = r.returncode == 0
        if r.returncode == 0:
            log_ok(f"art pull: {stem} -> {local_path.name}")
        else:
            log_err(f"art pull failed: {stem}: {r.stderr.strip()}")
    return results


def list_remote_art(game_id: str) -> list[str]:
    """List art types available on remote for a game."""
    remote_art = f"{RCLONE_REMOTE}/art/{game_id}"
    ls = _run([rclone_cmd(), "ls", remote_art], capture_output=True, text=True, timeout=15)
    if ls.returncode != 0:
        return []
    types = []
    for line in ls.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        stem = Path(parts[1].strip()).stem
        if stem in ART_TYPES:
            types.append(stem)
    return types
