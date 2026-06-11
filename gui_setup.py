"""
ExternalGameSync GUI -- cloud storage setup flow.
"""

from __future__ import annotations

import threading

import dearpygui.dearpygui as dpg

from config import load_settings, save_settings, is_configured
import rclone as rclone_mod
from sync import clear_remote_snapshots
from gui_common import (
    set_nav_active, set_nav_enabled, clear_content, add_header,
    run_async, _prog_stop,
)

_PROVIDER_LABELS    = ["Nextcloud", "Owncloud", "WebDAV (other)", "SFTP",
                       "Dropbox", "Google Drive", "OneDrive"]
_PROVIDER_KEY_MAP   = {"Nextcloud": "nextcloud", "Owncloud": "owncloud",
                       "WebDAV (other)": "webdav", "SFTP": "sftp",
                       "Dropbox": "dropbox", "Google Drive": "drive", "OneDrive": "onedrive"}
_PROVIDER_LABEL_MAP = {v: k for k, v in _PROVIDER_KEY_MAP.items()}
_WEBDAV_VENDOR_MAP  = {"nextcloud": "nextcloud", "owncloud": "owncloud", "webdav": "other"}
_OAUTH_PROVIDERS    = {"dropbox", "drive", "onedrive"}


def _setup_provider_changed():
    label    = dpg.get_value("_setup_provider")
    key      = _PROVIDER_KEY_MAP.get(label, "nextcloud")
    is_sftp  = (key == "sftp")
    is_oauth = (key in _OAUTH_PROVIDERS)
    if dpg.does_item_exist("_setup_webdav_fields"):
        dpg.configure_item("_setup_webdav_fields", show=not is_sftp and not is_oauth)
    if dpg.does_item_exist("_setup_sftp_fields"):
        dpg.configure_item("_setup_sftp_fields", show=is_sftp)
    if dpg.does_item_exist("_setup_oauth_fields"):
        dpg.configure_item("_setup_oauth_fields", show=is_oauth)
    if dpg.does_item_exist("_setup_btn"):
        dpg.configure_item("_setup_btn",
                           label="Connect - opens browser" if is_oauth else "Test & Save Connection")


def flow_setup():
    set_nav_active(None)
    clear_content()

    if not rclone_mod.rclone_available():
        dpg.add_spacer(height=40, parent="content_group")
        dpg.add_text("rclone not found", parent="content_group", color=(207, 34, 46))
        dpg.add_spacer(height=8, parent="content_group")
        dpg.add_text("Install rclone first, then reopen ExternalGameSync:",
                     parent="content_group")
        dpg.add_text("  curl https://rclone.org/install.sh | bash",
                     parent="content_group", color=(130, 130, 155))
        return

    settings      = load_settings()
    provider_key  = settings.get("provider_type", "nextcloud")
    current_label = _PROVIDER_LABEL_MAP.get(provider_key, "Nextcloud")
    is_sftp       = (provider_key == "sftp")
    is_oauth      = (provider_key in _OAUTH_PROVIDERS)

    add_header("Cloud Storage Setup",
               "Connect to your cloud storage server to enable save syncing.")

    with dpg.group(horizontal=True, parent="content_group"):
        dpg.add_text("Provider")
        dpg.add_combo(tag="_setup_provider", items=_PROVIDER_LABELS,
                      default_value=current_label, width=200,
                      callback=_setup_provider_changed)

    dpg.add_spacer(height=8, parent="content_group")

    with dpg.group(tag="_setup_webdav_fields", parent="content_group", show=not is_sftp and not is_oauth):
        with dpg.table(header_row=False, policy=dpg.mvTable_SizingFixedFit):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=110)
            dpg.add_table_column()
            with dpg.table_row():
                dpg.add_text("WebDAV URL")
                dpg.add_input_text(tag="_setup_url", width=480,
                                   default_value=settings.get("webdav_url", ""),
                                   hint="https://cloud.example.com/remote.php/dav/files/USER/")
            with dpg.table_row():
                dpg.add_text("Username")
                dpg.add_input_text(tag="_setup_user", width=480,
                                   default_value=settings.get("webdav_user", ""))
            with dpg.table_row():
                dpg.add_text("Password")
                dpg.add_input_text(tag="_setup_pw", width=480, password=True)

    with dpg.group(tag="_setup_sftp_fields", parent="content_group", show=is_sftp):
        with dpg.table(header_row=False, policy=dpg.mvTable_SizingFixedFit):
            dpg.add_table_column(width_fixed=True, init_width_or_weight=110)
            dpg.add_table_column()
            with dpg.table_row():
                dpg.add_text("Host")
                dpg.add_input_text(tag="_setup_sftp_host", width=480,
                                   default_value=settings.get("sftp_host", ""),
                                   hint="myserver.example.com or 192.168.1.10")
            with dpg.table_row():
                dpg.add_text("Port")
                dpg.add_input_int(tag="_setup_sftp_port", width=100,
                                  default_value=int(settings.get("sftp_port", 22)),
                                  min_value=1, max_value=65535)
            with dpg.table_row():
                dpg.add_text("Username")
                dpg.add_input_text(tag="_setup_sftp_user", width=480,
                                   default_value=settings.get("sftp_user", ""))
            with dpg.table_row():
                dpg.add_text("Password")
                dpg.add_input_text(tag="_setup_sftp_pw", width=480, password=True)

    with dpg.group(tag="_setup_oauth_fields", parent="content_group", show=is_oauth):
        dpg.add_text("Your browser will open to sign in. Return here once complete.",
                     color=(130, 130, 155))

    dpg.add_spacer(height=6, parent="content_group")
    dpg.add_text("", tag="_setup_status", parent="content_group", color=(130, 130, 155))
    dpg.add_progress_bar(default_value=0.0, width=480, height=6,
                         tag="_setup_prog", parent="content_group", show=False)
    dpg.add_spacer(height=8, parent="content_group")

    with dpg.group(horizontal=True, parent="content_group"):
        btn_label = "Connect - opens browser" if is_oauth else "Test & Save Connection"
        dpg.add_button(label=btn_label, width=220, height=32,
                       tag="_setup_btn", callback=_setup_connect)
        if is_configured():
            from gui_home import refresh_and_home
            dpg.add_button(label="Cancel", width=80, height=32,
                           callback=refresh_and_home)


def _setup_connect():
    provider_label = dpg.get_value("_setup_provider") if dpg.does_item_exist("_setup_provider") else "Nextcloud"
    provider_key   = _PROVIDER_KEY_MAP.get(provider_label, "nextcloud")

    if provider_key == "sftp":
        host = dpg.get_value("_setup_sftp_host").strip()
        port = int(dpg.get_value("_setup_sftp_port"))
        user = dpg.get_value("_setup_sftp_user").strip()
        pw   = dpg.get_value("_setup_sftp_pw")
        if not (host and user and pw):
            dpg.set_value("_setup_status", "All fields are required.")
            dpg.configure_item("_setup_status", color=(207, 34, 46))
            return
        saved           = {"provider_type": "sftp", "sftp_host": host, "sftp_port": port, "sftp_user": user}
        connecting_label = "Connecting..."
        status_text      = "Testing connection..."
        reset_label      = "Test & Save Connection"
        def _work():
            ok, err = rclone_mod.rclone_setup_sftp(host, port, user, pw)
            if ok:
                ok, err = rclone_mod.rclone_test()
            return ok, err
    elif provider_key in _OAUTH_PROVIDERS:
        saved            = {"provider_type": provider_key}
        connecting_label = "Waiting for browser..."
        status_text      = "Browser opened -- please sign in and return here..."
        reset_label      = "Connect - opens browser"
        def _work():
            ok, err = rclone_mod.rclone_setup_oauth(provider_key)
            if ok:
                ok, err = rclone_mod.rclone_test()
            return ok, err
    else:
        url    = dpg.get_value("_setup_url").strip()
        user   = dpg.get_value("_setup_user").strip()
        pw     = dpg.get_value("_setup_pw")
        vendor = _WEBDAV_VENDOR_MAP.get(provider_key, "nextcloud")
        if not url.endswith("/"):
            url += "/"
        if not (url.strip("/") and user and pw):
            dpg.set_value("_setup_status", "All fields are required.")
            dpg.configure_item("_setup_status", color=(207, 34, 46))
            return
        saved            = {"provider_type": provider_key, "webdav_url": url, "webdav_user": user}
        connecting_label = "Connecting..."
        status_text      = "Testing connection..."
        reset_label      = "Test & Save Connection"
        def _work():
            ok, err = rclone_mod.rclone_setup_webdav(url, vendor, user, pw)
            if ok:
                ok, err = rclone_mod.rclone_test()
            return ok, err

    dpg.configure_item("_setup_btn", enabled=False, label=connecting_label)
    dpg.set_value("_setup_status", status_text)
    dpg.configure_item("_setup_status", color=(130, 130, 155))
    dpg.configure_item("_setup_prog", show=True)
    set_nav_enabled(False)
    _prog_stop.clear()

    def _anim():
        while not _prog_stop.is_set():
            if dpg.does_item_exist("_setup_prog"):
                val = (dpg.get_value("_setup_prog") + 0.02) % 1.0
                dpg.set_value("_setup_prog", val)
            _prog_stop.wait(0.05)

    threading.Thread(target=_anim, daemon=True).start()

    def _done(res):
        ok, err = res
        _prog_stop.set()
        if dpg.does_item_exist("_setup_prog"):
            dpg.configure_item("_setup_prog", show=False)
        if dpg.does_item_exist("_setup_btn"):
            dpg.configure_item("_setup_btn", enabled=True, label=reset_label)
        set_nav_enabled(True)
        if not ok:
            if dpg.does_item_exist("_setup_status"):
                dpg.set_value("_setup_status", f"Connection failed: {err}")
                dpg.configure_item("_setup_status", color=(207, 34, 46))
            return
        s = load_settings()
        prev_provider = s.get("provider_type")
        s["configured"] = True
        s.update(saved)
        save_settings(s)
        if prev_provider != saved.get("provider_type"):
            clear_remote_snapshots()
        rclone_mod.rclone_ensure_remote_folder()
        rclone_mod.rclone_pull_games_json()
        from games import load_games
        n_games = len(load_games())
        if dpg.does_item_exist("_setup_status"):
            dpg.set_value("_setup_status",
                          f"Connected! Found {n_games} game config(s) on {provider_label}.")
            dpg.configure_item("_setup_status", color=(45, 164, 78))
        if dpg.does_item_exist("_setup_btn"):
            dpg.configure_item("_setup_btn", label="Connected!", enabled=False)
        from gui_home import refresh_and_home
        dpg.set_frame_callback(dpg.get_frame_count() + 110, refresh_and_home)

    run_async(_work, (), _done)
