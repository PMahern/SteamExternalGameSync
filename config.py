"""
ExternalGameSync — paths, subprocess helpers, logging, and settings.
Everything else imports from here; this module has no internal dependencies.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from datetime import datetime

if sys.platform != "win32":
    import fcntl

_CNW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _run(cmd, **kw):
    """subprocess.run wrapper that suppresses console windows on Windows."""
    kw.update({k: v for k, v in _CNW.items() if k not in kw})
    return subprocess.run(cmd, **kw)


# ── Paths ─────────────────────────────────────────────────────────────────────

if sys.platform == "win32":
    _APP_DATA      = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    APP_CONFIG_DIR = _APP_DATA / "ExternalGameSync"
else:
    APP_CONFIG_DIR = Path.home() / ".config" / "externalgamesync"

SETTINGS_FILE = APP_CONFIG_DIR / "settings.json"
SYNC_ROOT     = Path.home() / "ExternalGameSync"
BACKUP_ROOT   = APP_CONFIG_DIR / "backups"
LOG_FILE      = APP_CONFIG_DIR / "sync.log"
LOCK_DIR      = APP_CONFIG_DIR / "locks"
GAMES_JSON    = SYNC_ROOT / "games.json"


def hostname() -> str:
    return platform.node()


def find_steam_root_linux() -> Path | None:
    """Return the Steam root directory on Linux, checking all common install locations."""
    candidates = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".steam/debian-installation",
        Path.home() / ".steam/root",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",  # Flatpak
    ]
    for p in candidates:
        if (p / "userdata").exists():
            return p
    return None


def find_steam_root_windows() -> Path | None:
    try:
        import winreg
        for hive, sub in [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_CURRENT_USER,  r"Software\Valve\Steam"),
        ]:
            try:
                key = winreg.OpenKey(hive, sub)
                path, _ = winreg.QueryValueEx(key, "InstallPath")
                winreg.CloseKey(key)
                p = Path(path)
                if p.exists():
                    return p
            except OSError:
                continue
    except ImportError:
        pass
    default = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"
    return default if default.exists() else None


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO"):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    return line

def log_err(msg): return log(msg, "ERROR")
def log_ok(msg):  return log(msg, "OK")


# ── Settings (local machine only) ─────────────────────────────────────────────

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text())
    return {}

def save_settings(s: dict):
    APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))

def is_configured() -> bool:
    return bool(load_settings().get("configured"))
