"""Ludusavi manifest integration: community game save path database."""

from __future__ import annotations

import difflib
import os
import sys
import time
from pathlib import Path

MANIFEST_URL = (
    "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest"
    "/refs/heads/master/data/manifest.yaml"
)
_MANIFEST_MAX_AGE_DAYS = 7

# Folders present in a stock Proton prefix — anything outside these is a game install
_BASELINE_DRIVE_C = frozenset({
    "Program Files", "Program Files (x86)", "windows", "users", "ProgramData",
})
_BASELINE_PROGRAM_FILES = frozenset({
    "Common Files", "Internet Explorer", "Windows Media Player",
    "Windows NT", "WindowsPowerShell",
})
_BASELINE_PROGRAM_FILES_X86 = frozenset({
    "Common Files", "Internet Explorer", "Windows Media Player",
    "Windows NT", "WindowsPowerShell",
})

# Path variable sets
_FIXED_VARS = frozenset({
    "<winAppData>", "<winLocalAppData>", "<winDocuments>",
    "<winPublicDocuments>", "<winDesktop>", "<winProgramData>",
    "<home>", "<xdgData>", "<xdgConfig>", "<xdgCache>",
})
_INSTALL_VARS = frozenset({"<base>", "<game>", "<root>"})

_manifest_cache: dict | None = None


def _cache_path() -> Path:
    from config import APP_CONFIG_DIR
    return APP_CONFIG_DIR / "ludusavi-manifest.yaml"


def _processed_path() -> Path:
    from config import APP_CONFIG_DIR
    return APP_CONFIG_DIR / "ludusavi-processed.json"


def ensure_manifest() -> Path:
    """Return path to manifest YAML, downloading it if missing or stale."""
    import urllib.request
    p = _cache_path()
    if p.exists():
        age_days = (time.time() - p.stat().st_mtime) / 86400
        if age_days < _MANIFEST_MAX_AGE_DAYS:
            return p
    p.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(MANIFEST_URL, p)
    # Invalidate processed cache so it gets rebuilt from the new YAML
    pp = _processed_path()
    if pp.exists():
        pp.unlink()
    return p


def _build_processed(raw: dict) -> dict:
    """
    Strip the raw manifest down to only what we need and write a compact JSON.
    Keeps only games that have at least one save-tagged file entry, and drops
    all fields we never read (launch, registry, id, cloud, etc.).
    """
    import json
    games = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        files = entry.get("files", {})
        if not isinstance(files, dict):
            continue
        save_files = {
            path: fe for path, fe in files.items()
            if isinstance(fe, dict) and "save" in fe.get("tags", [])
        }
        if not save_files:
            continue
        games[name] = {
            "files":      save_files,
            "installDir": entry.get("installDir", {}),
            "steam":      entry.get("steam", {}),
        }
    processed = {"version": 1, "games": games}
    pp = _processed_path()
    with open(pp, "w", encoding="utf-8") as f:
        json.dump(processed, f, separators=(",", ":"))
    return games


def load_manifest() -> dict:
    """
    Load the manifest optimised for search.  On first run after a fresh YAML
    download the full YAML is parsed and a compact processed JSON is written
    alongside it.  On all subsequent loads only the small JSON is read.
    Result is cached in-process so repeated searches within a session are free.
    """
    global _manifest_cache
    if _manifest_cache is not None:
        return _manifest_cache

    import json

    yaml_path = _cache_path()
    proc_path = _processed_path()

    # Use processed JSON if it exists and is at least as new as the YAML
    if (proc_path.exists() and yaml_path.exists()
            and proc_path.stat().st_mtime >= yaml_path.stat().st_mtime):
        with open(proc_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _manifest_cache = data.get("games", data)
        return _manifest_cache

    # Fall back to parsing the full YAML, then build the processed cache
    import yaml
    yaml_path = ensure_manifest()
    with open(yaml_path, "r", encoding="utf-8") as f:
        try:
            raw = yaml.load(f, Loader=yaml.CSafeLoader)
        except AttributeError:
            raw = yaml.safe_load(f)
    _manifest_cache = _build_processed(raw)
    return _manifest_cache


def search_games(manifest: dict, query: str, n: int = 12) -> list[tuple[str, dict]]:
    """Fuzzy-search manifest by game name. Returns (name, entry) pairs."""
    if not query.strip():
        return []
    q = query.lower()
    names = list(manifest.keys())
    substring = [nm for nm in names if q in nm.lower()]
    fuzzy = difflib.get_close_matches(query, names, n=n * 3, cutoff=0.45)
    combined = list(dict.fromkeys(substring + fuzzy))[:n]
    return [(nm, manifest[nm]) for nm in combined]


# ── Path resolution ───────────────────────────────────────────────────────────

def _is_save(file_entry) -> bool:
    return isinstance(file_entry, dict) and "save" in file_entry.get("tags", [])


def _os_ok(file_entry: dict, target_os: str) -> bool:
    when = file_entry.get("when")
    if not when:
        return True
    return any(c.get("os") in (None, target_os) for c in when)


def _needs_install_var(path_str: str) -> bool:
    return any(v in path_str for v in _INSTALL_VARS)


def _resolve(path_str: str, var_map: dict) -> str | None:
    s = path_str
    for k, v in var_map.items():
        s = s.replace(k, v)
    s = s.replace("/", os.sep)
    return None if "<" in s else s


def _fixed_var_map(app_id: str | None) -> dict:
    if sys.platform == "win32":
        ad  = os.environ.get("APPDATA", "")
        lad = os.environ.get("LOCALAPPDATA", "")
        pd  = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        h   = str(Path.home())
        return {
            "<winAppData>":         ad,
            "<winLocalAppData>":    lad,
            "<winProgramData>":     pd,
            "<winDocuments>":       str(Path.home() / "Documents"),
            "<winPublicDocuments>": str(Path(pd).parent / "Users" / "Public" / "Documents"),
            "<winDesktop>":         str(Path.home() / "Desktop"),
            "<home>":               h,
        }
    else:
        if not app_id:
            return {}
        from steam import proton_drive_c
        dc = proton_drive_c(app_id)
        su = dc / "users" / "steamuser"
        h  = Path.home()
        return {
            "<winAppData>":         str(su / "AppData" / "Roaming"),
            "<winLocalAppData>":    str(su / "AppData" / "Local"),
            "<winProgramData>":     str(dc / "ProgramData"),
            "<winDocuments>":       str(su / "Documents"),
            "<winPublicDocuments>": str(dc / "users" / "Public" / "Documents"),
            "<winDesktop>":         str(su / "Desktop"),
            "<home>":               str(h),
            "<xdgData>":            str(h / ".local" / "share"),
            "<xdgConfig>":          str(h / ".config"),
            "<xdgCache>":           str(h / ".cache"),
        }


def get_fixed_save_paths(entry: dict, app_id: str | None) -> list[str]:
    """Resolve save paths that don't need the install directory."""
    vm = _fixed_var_map(app_id)
    if not vm:
        return []
    results = []
    for path_str, fe in entry.get("files", {}).items():
        if not _is_save(fe):
            continue
        if not _os_ok(fe, "windows"):
            continue
        if _needs_install_var(path_str):
            continue
        r = _resolve(path_str, vm)
        if r:
            results.append(r)
    return results


def get_install_relative_save_paths(entry: dict, install_dir: Path) -> list[str]:
    """Resolve save paths that use <base>/<game>/<root> given a known install dir."""
    vm = {
        "<base>": str(install_dir),
        "<game>": str(install_dir),
        "<root>": str(install_dir),
    }
    results = []
    for path_str, fe in entry.get("files", {}).items():
        if not _is_save(fe):
            continue
        if not _os_ok(fe, "windows"):
            continue
        if not _needs_install_var(path_str):
            continue
        r = _resolve(path_str, vm)
        if r:
            results.append(r)
    return results


def has_install_relative_paths(entry: dict) -> bool:
    for path_str, fe in entry.get("files", {}).items():
        if _is_save(fe) and _needs_install_var(path_str):
            return True
    return False


# ── Proton prefix install dir search ─────────────────────────────────────────

def _non_baseline_dirs(parent: Path, baseline: frozenset) -> list[Path]:
    if not parent.exists():
        return []
    return [d for d in parent.iterdir() if d.is_dir() and d.name not in baseline]


def find_install_candidates(app_id: str) -> list[Path]:
    """Return non-baseline dirs in drive_c, Program Files, and Program Files (x86)."""
    from steam import proton_drive_c
    dc = proton_drive_c(app_id)
    return (
        _non_baseline_dirs(dc, _BASELINE_DRIVE_C)
        + _non_baseline_dirs(dc / "Program Files", _BASELINE_PROGRAM_FILES)
        + _non_baseline_dirs(dc / "Program Files (x86)", _BASELINE_PROGRAM_FILES_X86)
    )


def find_install_dir(app_id: str, install_dir_hints: list[str]) -> Path | None:
    """
    Find the game install dir in the Proton prefix.
    Tries manifest installDir hints first (including one level inside each
    candidate, e.g. Program Files (x86)/Eidos/Hitman Blood Money), then
    falls back to the sole non-baseline candidate.
    """
    candidates = find_install_candidates(app_id)
    if not candidates:
        return None
    if install_dir_hints:
        hints_lower = [h.lower() for h in install_dir_hints]
        # Direct match first
        for c in candidates:
            if c.name.lower() in hints_lower:
                return c
        # One level deeper (publisher/studio wrapper folders like Eidos/, Ubisoft/)
        for c in candidates:
            try:
                for child in c.iterdir():
                    if child.is_dir() and child.name.lower() in hints_lower:
                        return child
            except PermissionError:
                continue
    return candidates[0] if len(candidates) == 1 else None


# Substrings that identify non-game executables to deprioritise
_EXE_NOISE = ("redist", "vcredist", "setup", "unins", "install", "crash",
              "report", "register", "dxsetup", "directx")


def find_exe_in_dir(install_dir: Path, game_name: str) -> Path | None:
    """Find the most likely game exe in an install directory."""
    if not install_dir.exists():
        return None
    exes = [
        e for e in install_dir.rglob("*.exe")
        if not any(n in e.name.lower() for n in _EXE_NOISE)
    ]
    if not exes:
        return None
    name_norm = game_name.lower().replace(" ", "")
    scored = sorted(
        ((difflib.SequenceMatcher(None, name_norm, e.stem.lower().replace(" ", "")).ratio(), e)
         for e in exes),
        reverse=True,
    )
    best_score, best_exe = scored[0]
    return best_exe if best_score > 0.2 else exes[0]
