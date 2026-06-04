"""
ExternalGameSync GUI — shared state, theme, navigation, widgets, and async helpers.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path

import dearpygui.dearpygui as dpg

from config import is_configured, _run
from machine_config import get_local_config
from steam import get_proton_base, proton_drive_c, _find_all_steamapps_dirs

# ── Constants ─────────────────────────────────────────────────────────────────

TITLE        = "ExternalGameSync"
WIN_W, WIN_H = 960, 660
MIN_W, MIN_H = 820, 540
SIDEBAR_W    = 210

if sys.platform == "win32":
    _APP_DIR     = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ExternalGameSync"
    SAVESYNC_BIN = str(_APP_DIR / "externalgamesync.bat")
else:
    SAVESYNC_BIN = str(Path.home() / ".local/bin" / "externalgamesync")


# ── Nav state (module-level, shared across all flows) ─────────────────────────

_theme_nav_active: int = 0
_theme_nav_normal: int = 0
_nav_btn_ids: dict[str, int] = {}
_prog_stop = threading.Event()

# ── Sync status cache ─────────────────────────────────────────────────────────
# Keyed by game_id; values are get_sync_status() return strings.
# Populated by pre-check chains; avoids re-fetching on every tab navigation.

_sync_status_cache: dict[str, str] = {}


def get_cached_status(game_id: str) -> str | None:
    return _sync_status_cache.get(game_id)


def set_cached_status(game_id: str, status: str) -> None:
    _sync_status_cache[game_id] = status


def evict_cached_status(game_id: str) -> None:
    _sync_status_cache.pop(game_id, None)


# ── Theme ─────────────────────────────────────────────────────────────────────

def setup_theme():
    with dpg.theme() as t:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg,        (22,  23,  38))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg,         (22,  23,  38))
            dpg.add_theme_color(dpg.mvThemeCol_PopupBg,         (30,  31,  50))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg,         (36,  38,  58))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered,  (46,  50,  74))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive,   (56,  60,  90))
            dpg.add_theme_color(dpg.mvThemeCol_Button,          (31, 111, 235))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered,   (56, 139, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,    (18,  88, 200))
            dpg.add_theme_color(dpg.mvThemeCol_Header,          (48,  80, 130))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered,   (60,  95, 155))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive,    (36,  66, 110))
            dpg.add_theme_color(dpg.mvThemeCol_Text,            (220, 220, 235))
            dpg.add_theme_color(dpg.mvThemeCol_TextDisabled,    (110, 110, 130))
            dpg.add_theme_color(dpg.mvThemeCol_Border,          (50,  52,  74))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg,     (22,  23,  38))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab,   (60,  62,  90))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive,   (28,  30,  46))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg,      (22,  23,  38))
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt,   (30,  31,  50))
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding,   0)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding,    6)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding,    4)
            dpg.add_theme_style(dpg.mvStyleVar_GrabRounding,     4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding,    10, 10)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding,     8, 5)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing,      8, 6)
    dpg.bind_theme(t)

    global _theme_nav_active, _theme_nav_normal
    with dpg.theme() as _theme_nav_active:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (44,  72, 120))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (56,  90, 148))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (36,  60, 100))
    with dpg.theme() as _theme_nav_normal:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (22,  23,  38, 0))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (40,  45,  65))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (30,  33,  52))


# ── Navigation helpers ────────────────────────────────────────────────────────

def set_nav_active(key: str | None):
    for k, bid in _nav_btn_ids.items():
        if dpg.does_item_exist(bid):
            dpg.bind_item_theme(bid, _theme_nav_active if k == key else _theme_nav_normal)


def set_nav_enabled(enabled: bool):
    for bid in _nav_btn_ids.values():
        if dpg.does_item_exist(bid):
            dpg.configure_item(bid, enabled=enabled)


def clear_content():
    _prog_stop.set()
    if dpg.does_item_exist("content_group"):
        dpg.delete_item("content_group", children_only=True)


# ── Common widgets ────────────────────────────────────────────────────────────

def add_header(title: str, subtitle: str = ""):
    dpg.add_text(title, parent="content_group")
    if subtitle:
        dpg.add_text(subtitle, parent="content_group", color=(130, 130, 155))
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")


def add_action_bar(primary_label: str, primary_cb, back_cb=None, cancel_cb=None):
    dpg.add_separator(parent="content_group")
    dpg.add_spacer(height=4, parent="content_group")
    if cancel_cb is None:
        from gui_home import refresh_and_home
        cancel_cb = refresh_and_home
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label=primary_label, callback=primary_cb, width=140, height=32)
        if back_cb:
            dpg.add_button(label="<-- Back", callback=back_cb, width=90, height=32)
        dpg.add_button(label="Cancel", callback=cancel_cb, width=80, height=32)


# ── Async + progress ──────────────────────────────────────────────────────────

def run_async(fn, args: tuple, on_done):
    result_box = [None]
    done_flag  = [False]

    def _worker():
        try:
            result_box[0] = fn(*args)
        except Exception as exc:
            result_box[0] = [(str(exc), False)]
        done_flag[0] = True

    def _check():
        if done_flag[0]:
            on_done(result_box[0])
        else:
            dpg.set_frame_callback(dpg.get_frame_count() + 2, _check)

    threading.Thread(target=_worker, daemon=True).start()
    dpg.set_frame_callback(dpg.get_frame_count() + 2, _check)


def show_progress(message: str):
    clear_content()
    set_nav_enabled(False)
    _prog_stop.clear()

    dpg.add_spacer(height=60, parent="content_group")
    dpg.add_text(message, parent="content_group", tag="_prog_msg")
    dpg.add_spacer(height=8, parent="content_group")
    dpg.add_progress_bar(default_value=0.0, width=-1, height=10,
                         tag="_prog_bar", parent="content_group")

    def _anim():
        while not _prog_stop.is_set():
            if dpg.does_item_exist("_prog_bar"):
                val = (dpg.get_value("_prog_bar") + 0.018) % 1.0
                dpg.set_value("_prog_bar", val)
            _prog_stop.wait(0.05)

    threading.Thread(target=_anim, daemon=True).start()


def stop_progress():
    _prog_stop.set()
    set_nav_enabled(True)


def shutdown_steam_sync() -> bool:
    """Close Steam and wait for it to exit. Returns True if Steam was running."""
    import subprocess, shutil, time
    if sys.platform == "win32":
        check = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"],
                               capture_output=True, text=True, creationflags=0x08000000)
        if "steam.exe" not in check.stdout.lower():
            return False
        subprocess.run(["taskkill", "/IM", "steam.exe", "/F"],
                       capture_output=True, creationflags=0x08000000)
        time.sleep(3)
        return True
    else:
        if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode != 0:
            return False
        steam_exe = shutil.which("steam") or "steam"
        subprocess.run([steam_exe, "-shutdown"], capture_output=True)
        for _ in range(20):
            if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode != 0:
                break
            time.sleep(0.5)
        return True


def restart_steam():
    import subprocess
    import threading
    if sys.platform == "win32":
        import os
        import time
        from config import find_steam_root_windows
        steam_root = find_steam_root_windows()
        steam_exe  = str(steam_root / "steam.exe") if steam_root else r"C:\Program Files (x86)\Steam\steam.exe"
        def _do():
            subprocess.run(["taskkill", "/IM", "steam.exe", "/F"],
                           capture_output=True, creationflags=0x08000000)
            time.sleep(3)
            os.startfile(steam_exe)
        threading.Thread(target=_do, daemon=True).start()
    else:
        import shutil
        steam_exe = shutil.which("steam") or "steam"
        subprocess.Popen(f'"{steam_exe}" -shutdown; sleep 5; "{steam_exe}" &',
                         shell=True, start_new_session=True)


def show_done(title: str, lines: list[str], success: bool = True, on_done=None,
              show_restart_steam: bool = False):
    clear_content()
    stop_progress()
    if on_done is None:
        from gui_home import refresh_and_home
        on_done = refresh_and_home
    color  = (45, 164, 78) if success else (207, 34, 46)
    prefix = "OK  " if success else "X  "
    dpg.add_spacer(height=30, parent="content_group")
    dpg.add_text(prefix + title, parent="content_group", color=color)
    dpg.add_spacer(height=10, parent="content_group")
    for line in lines:
        dpg.add_text(line, parent="content_group", wrap=800)
    dpg.add_spacer(height=20, parent="content_group")
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_button(label="Done", width=120, height=32, callback=on_done)
        if show_restart_steam:
            dpg.add_spacer(width=8)
            dpg.add_button(label="Restart Steam", width=140, height=32,
                           callback=restart_steam)


def show_error(title: str, message: str, on_done=None):
    show_done(title, [message], success=False, on_done=on_done)


# ── File picker ───────────────────────────────────────────────────────────────

def browse_into(input_tag: str, dir_only: bool, start: str | None):
    def _cb(sender, app_data, user_data):
        if dir_only:
            # For directory_selector, file_path_name is the selected directory.
            # selections can contain the dir keyed by its own name, which causes
            # doubling if used directly (e.g. {"Saves": "/path/to/Saves"} → Saves/Saves).
            path = app_data.get("file_path_name", "").rstrip("/\\")
        else:
            selections = app_data.get("selections", {})
            path = next(iter(selections.values()), "") if selections else app_data.get("file_path_name", "")
        if path and dpg.does_item_exist(user_data):
            dpg.set_value(user_data, path)

    kw: dict = {}
    if start and Path(start).exists():
        kw["default_path"] = start

    with dpg.file_dialog(label="Select " + ("Folder" if dir_only else "File"),
                         callback=_cb, user_data=input_tag,
                         directory_selector=dir_only,
                         width=700, height=450, modal=True, **kw):
        if not dir_only:
            dpg.add_file_extension(".*")
            dpg.add_file_extension(".exe", color=(100, 255, 100, 255))


def add_path_row(label: str, input_tag: str, dir_only: bool, start_dir_fn):
    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_text(label, indent=0)
        dpg.add_input_text(tag=input_tag, width=-80, hint="(not set)")
        dpg.add_button(label="...", width=60, height=22,
                       callback=lambda: browse_into(input_tag, dir_only, start_dir_fn()))


# ── Selectable table ──────────────────────────────────────────────────────────

def add_table(columns: list[str], rows: list[list],
              col_weights: list[int] | None = None,
              height: int = -50,
              filterable: bool = False,
              parent_tag: str = "content_group") -> dict:
    """Build a selectable table. Returns state dict with sel_tags and row_tags."""
    state: dict = {"selected": None, "sel_tags": [], "row_tags": [], "_rows": rows}

    def _on_sel(sender, app_data, user_data):
        if not app_data:
            return
        state["selected"] = user_data
        for t in state["sel_tags"]:
            if t != sender and dpg.does_item_exist(t):
                dpg.set_value(t, False)

    if filterable:
        def _on_filter(sender, app_data, user_data):
            q = (app_data or "").lower()
            for row_tag, row in zip(state["row_tags"], state["_rows"]):
                show = not q or any(q in str(c).lower() for c in row)
                if dpg.does_item_exist(row_tag):
                    dpg.configure_item(row_tag, show=show)
        dpg.add_input_text(
            hint="Filter by name...", width=-1,
            parent=parent_tag,
            callback=_on_filter,
        )

    weights = col_weights or [1] * len(columns)
    with dpg.table(
        parent=parent_tag,
        header_row=True, row_background=True, scrollY=True,
        borders_outerH=True, borders_innerV=True,
        height=height,
        policy=dpg.mvTable_SizingStretchProp,
    ):
        for col, w in zip(columns, weights):
            dpg.add_table_column(label=col, width_stretch=True,
                                 init_width_or_weight=float(w))

        for i, row in enumerate(rows):
            sel_tag = f"_sel_{id(state)}_{i}"
            row_tag = f"_row_{id(state)}_{i}"
            state["sel_tags"].append(sel_tag)
            state["row_tags"].append(row_tag)
            with dpg.table_row(tag=row_tag):
                dpg.add_selectable(
                    label=str(row[0]), tag=sel_tag,
                    callback=_on_sel, user_data=row,
                    span_columns=False, height=24,
                )
                for cell in row[1:]:
                    dpg.add_text(str(cell))

    return state


# ── Windows / Proton path helpers (used by assign and add flows) ──────────────

def win_exe_candidates(path_str: str) -> list[Path]:
    if not path_str:
        return []
    norm = path_str.replace("/", "\\").lstrip("\\")
    if re.match(r'^[A-Za-z]:\\', norm):
        return [Path(norm)]
    norm = re.sub(r'^[A-Za-z]:[\\]', '', norm)
    candidates = []
    for drive in ["C:", "D:", "E:"]:
        candidates.append(Path(f"{drive}\\{norm}"))
        if "Program Files\\" in norm:
            alt = norm.replace("Program Files\\", "Program Files (x86)\\", 1)
            candidates.append(Path(f"{drive}\\{alt}"))
    return candidates


def win_save_candidates(path_str: str) -> list[Path]:
    if not path_str:
        return []
    norm = path_str.replace("/", "\\").lstrip("\\")
    if re.match(r'^[A-Za-z]:\\', norm):
        return [Path(norm)]
    norm        = re.sub(r'^[A-Za-z]:[\\]', '', norm)
    appdata     = Path(os.environ.get("APPDATA",      str(Path.home() / "AppData" / "Roaming")))
    localappdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    home        = Path.home()
    candidates  = []
    m = re.match(r'^[Uu]sers\\[^\\]+\\[Aa]pp[Dd]ata\\[Rr]oaming\\(.*)', norm)
    if m:
        candidates.append(appdata / m.group(1))
    m = re.match(r'^[Uu]sers\\[^\\]+\\[Aa]pp[Dd]ata\\[Ll]ocal\\(.*)', norm)
    if m:
        candidates.append(localappdata / m.group(1))
    m = re.match(r'^[Uu]sers\\[^\\]+\\(?:[Mm]y )?[Dd]ocuments\\(.*)', norm)
    if m:
        candidates.append(home / "Documents" / m.group(1))
    if not candidates:
        for drive in ["C:", "D:", "E:"]:
            candidates.append(Path(f"{drive}\\{norm}"))
    return candidates


def try_resolve_windows_paths(game_cfg: dict) -> tuple[Path | None, Path | None]:
    exe_found = save_found = None
    for c in win_exe_candidates(game_cfg.get("exe_path", "")):
        if c.exists():
            exe_found = c
            break
    for c in win_save_candidates(game_cfg.get("save_path", "")):
        if c.exists():
            save_found = c
            break
    return exe_found, save_found


def _all_compatdata_dirs() -> list[Path]:
    """Return all compatdata/<app_id> dirs across every Steam library folder."""
    dirs = []
    for steamapps in _find_all_steamapps_dirs():
        compatdata = steamapps / "compatdata"
        if compatdata.exists():
            dirs.extend(d for d in compatdata.iterdir() if d.is_dir() and d.name.isdigit())
    return dirs


def find_proton_prefix_for_shortcut(ns_entry: dict, game_cfg: dict) -> str | None:
    exe_rel = game_cfg.get("exe_path", "")
    if not exe_rel:
        return None
    for d in _all_compatdata_dirs():
        if (d / "pfx" / "drive_c" / exe_rel).exists():
            return d.name
    return None


def find_proton_prefix_for_ns_exe(exe_str: str) -> str | None:
    exe_name = Path(exe_str.strip('"')).name.lower()
    for d in _all_compatdata_dirs():
        drive_c = d / "pfx" / "drive_c"
        if not drive_c.exists():
            continue
        for candidate in drive_c.rglob("*.exe"):
            if candidate.name.lower() == exe_name:
                return d.name
    return None


def list_proton_prefixes() -> list[list[str]]:
    dirs = sorted(_all_compatdata_dirs(), key=lambda d: d.stat().st_mtime, reverse=True)
    rows = []
    for d in dirs:
        pf   = d / "pfx" / "drive_c" / "Program Files"
        hint = ""
        if pf.exists():
            subdirs = [x.name for x in sorted(pf.iterdir()) if x.is_dir()][:4]
            hint    = ", ".join(subdirs)
        rows.append([d.name, hint])
    return rows
