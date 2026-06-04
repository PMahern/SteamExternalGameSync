"""
ExternalGameSync Decky Plugin — Python backend.

Imports from the installed app directory (~/.local/share/externalgamesync/)
so all sync logic stays in one place; this file only bridges to Decky's API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import decky_plugin

logger = logging.getLogger(__name__)

_APP_DIR = os.path.expanduser("~/.local/share/externalgamesync")


def _ensure_path() -> bool:
    """Add the installed app directory to sys.path if present."""
    if not os.path.isfile(os.path.join(_APP_DIR, "sync.py")):
        logger.error("ExternalGameSync not found at %s", _APP_DIR)
        return False
    if _APP_DIR not in sys.path:
        sys.path.insert(0, _APP_DIR)
    return True


class Plugin:
    async def _main(self) -> None:
        logger.info("ExternalGameSync plugin loaded (app_dir=%s)", _APP_DIR)

    async def _unload(self) -> None:
        logger.info("ExternalGameSync plugin unloaded")

    # ── get_games ─────────────────────────────────────────────────────────────

    async def get_games(self) -> list[dict[str, Any]]:
        """Return all games with whether they're assigned to this machine."""
        if not _ensure_path():
            return []
        try:
            from games import load_games
            from machine_config import load_local_configs
            games = load_games()
            local_cfgs = load_local_configs()
            return [
                {
                    "id": g["id"],
                    "name": g["name"],
                    "assigned": g["id"] in local_cfgs,
                }
                for g in games
            ]
        except Exception as exc:
            logger.error("get_games: %s", exc)
            return []

    # ── sync_all ──────────────────────────────────────────────────────────────

    async def sync_all(self) -> dict[str, Any]:
        """
        Pull then push every assigned game.

        Emits 'sync_progress' events during the run:
          {"game_id": str, "name": str, "status": "syncing"|"ok"|"error"|"conflict"}

        Returns:
          {"results": [...], "conflicts": [...], "error": str|None}
        """
        if not _ensure_path():
            return {"error": "ExternalGameSync not installed", "results": [], "conflicts": []}

        loop = asyncio.get_running_loop()

        try:
            from games import load_games
            from sync import (
                rclone_sync_pull,
                rclone_sync_push,
                PULL_CONFLICT,
            )
            from rclone import rclone_push_games_json
        except ImportError as exc:
            return {"error": str(exc), "results": [], "conflicts": []}

        games = load_games()
        results: list[dict[str, Any]] = []
        conflicts: list[dict[str, str]] = []

        for g in games:
            game_id: str = g["id"]
            name: str = g["name"]

            await decky_plugin.emit_event("sync_progress", {
                "game_id": game_id,
                "name": name,
                "status": "syncing",
            })

            try:
                pull_ok, pull_msg = await loop.run_in_executor(
                    None,
                    lambda gid=game_id, gm=g: rclone_sync_pull(gid, gm),
                )

                if pull_msg == PULL_CONFLICT:
                    conflicts.append({"id": game_id, "name": name})
                    status, final_msg = "conflict", "conflict"
                elif pull_ok:
                    push_ok, push_msg = await loop.run_in_executor(
                        None,
                        lambda gid=game_id, gm=g: rclone_sync_push(gid, gm),
                    )
                    status = "ok" if push_ok else "error"
                    final_msg = push_msg
                else:
                    status, final_msg = "error", pull_msg

            except Exception as exc:
                logger.error("sync error for %s: %s", game_id, exc)
                status, final_msg = "error", str(exc)

            await decky_plugin.emit_event("sync_progress", {
                "game_id": game_id,
                "name": name,
                "status": status,
                "msg": final_msg,
            })

            results.append({"id": game_id, "name": name, "status": status, "msg": final_msg})

        try:
            await loop.run_in_executor(None, rclone_push_games_json)
        except Exception as exc:
            logger.error("push games.json: %s", exc)

        return {"results": results, "conflicts": conflicts, "error": None}

    # ── sync_game ─────────────────────────────────────────────────────────────

    async def sync_game(self, game_id: str) -> dict[str, Any]:
        """
        Pull then push a single assigned game.
        Returns: {"status": "ok"|"error"|"conflict", "msg": str}
        """
        try:
            if not _ensure_path():
                return {"status": "error", "msg": "ExternalGameSync not installed"}

            loop = asyncio.get_running_loop()

            try:
                from games import find_game
                from sync import rclone_sync_pull, rclone_sync_push, PULL_CONFLICT
            except ImportError as exc:
                return {"status": "error", "msg": str(exc)}

            game = find_game(game_id)
            if not game:
                return {"status": "error", "msg": f"Game not found: {game_id}"}

            name: str = game["name"]

            try:
                pull_ok, pull_msg = await loop.run_in_executor(
                    None, lambda gid=game_id, gm=game: rclone_sync_pull(gid, gm)
                )

                if pull_msg == PULL_CONFLICT:
                    status, final_msg = "conflict", "conflict"
                elif pull_ok:
                    push_ok, push_msg = await loop.run_in_executor(
                        None, lambda gid=game_id, gm=game: rclone_sync_push(gid, gm)
                    )
                    status = "ok" if push_ok else "error"
                    final_msg = push_msg
                else:
                    status, final_msg = "error", pull_msg

            except Exception as exc:
                logger.error("sync_game error for %s: %s", game_id, exc)
                status, final_msg = "error", str(exc)

            try:
                await decky_plugin.emit_event("sync_progress", {
                    "game_id": game_id,
                    "name": name,
                    "status": status,
                    "msg": final_msg,
                })
            except Exception as exc:
                logger.error("sync_game emit_event error: %s", exc)

            return {"status": status, "msg": final_msg}

        except Exception as exc:
            logger.error("sync_game unhandled: %s", exc, exc_info=True)
            return {"status": "error", "msg": str(exc)}

    # ── get_sync_statuses ─────────────────────────────────────────────────────

    async def get_sync_statuses(self) -> dict[str, Any]:
        """
        Return sync status for every assigned game without transferring save data.
        Uses rclone lsjson (metadata only) to check the remote.
        Returns {game_id: {"status": str, "last_known_status": str|None}} where
        status is one of: "in_sync", "cloud_ahead", "local_ahead", "conflict",
          "unknown", "no_connection", "error"
        last_known_status is set (from sync_states.json) when status is transient.
        """
        if not _ensure_path():
            return {}

        loop = asyncio.get_running_loop()

        try:
            from games import load_games
            from machine_config import load_local_configs
            from sync import get_sync_status, load_sync_state
        except ImportError as exc:
            logger.error("get_sync_statuses import: %s", exc)
            return {}

        games      = load_games()
        local_cfgs = load_local_configs()
        assigned   = [g for g in games if g["id"] in local_cfgs]
        _transient = {"no_connection", "error"}

        async def _check(g: dict) -> tuple[str, dict]:
            game_id = g["id"]
            try:
                status = await loop.run_in_executor(
                    None, lambda gm=g: get_sync_status(gm["id"], gm)
                )
            except Exception as exc:
                logger.error("get_sync_statuses %s: %s", game_id, exc)
                status = "error"
            last_known = load_sync_state(game_id) if status in _transient else None
            return game_id, {"status": status, "last_known_status": last_known}

        pairs = await asyncio.gather(*[_check(g) for g in assigned])
        return dict(pairs)

    # ── get_sync_status_for_appid ─────────────────────────────────────────────

    async def get_sync_status_for_appid(self, app_id: str) -> dict[str, Any]:
        """
        Return sync status for a game identified by its Steam app ID.
        Used by the game-page overlay to show status without opening the QAM.
        Returns {"status": "...", "name": "..."} or {"status": "none"} if not
        configured on this machine.
        """
        if not _ensure_path():
            return {"status": "none"}

        loop = asyncio.get_running_loop()

        def _work() -> dict[str, Any]:
            from machine_config import load_local_configs
            from games import find_game
            from sync import get_sync_status, load_sync_state

            configs = load_local_configs()
            game_id = next(
                (gid for gid, cfg in configs.items()
                 if str(cfg.get("app_id", "")) == str(app_id)),
                None,
            )
            if not game_id:
                return {"status": "none"}
            game = find_game(game_id)
            if not game:
                return {"status": "none"}
            try:
                status = get_sync_status(game_id, game)
                result: dict[str, Any] = {"status": status, "name": game["name"], "game_id": game_id}
                if status in ("no_connection", "error"):
                    result["last_known_status"] = load_sync_state(game_id)
                return result
            except Exception as exc:
                logger.error("get_sync_status_for_appid %s: %s", app_id, exc)
                return {"status": "error", "last_known_status": load_sync_state(game_id)}

        return await loop.run_in_executor(None, _work)

    # ── get_decky_settings ────────────────────────────────────────────────────

    async def get_decky_settings(self) -> dict[str, Any]:
        """Return Decky plugin behaviour settings: polling_enabled, poll_auto_pull."""
        if not _ensure_path():
            return {"polling_enabled": True, "poll_auto_pull": False}
        try:
            from config import load_settings
            s = load_settings()
            return {
                "polling_enabled": bool(s.get("polling_enabled", True)),
                "poll_auto_pull":  bool(s.get("poll_auto_pull",  False)),
            }
        except Exception as exc:
            logger.error("get_decky_settings: %s", exc)
            return {"polling_enabled": True, "poll_auto_pull": False}

    # ── set_decky_settings ────────────────────────────────────────────────────

    async def set_decky_settings(self, polling_enabled: bool, poll_auto_pull: bool) -> dict[str, Any]:
        """Persist Decky plugin behaviour settings."""
        if not _ensure_path():
            return {"ok": False}
        try:
            from config import load_settings, save_settings
            s = load_settings()
            s["polling_enabled"] = bool(polling_enabled)
            s["poll_auto_pull"]  = bool(poll_auto_pull) and bool(polling_enabled)
            save_settings(s)
            return {"ok": True}
        except Exception as exc:
            logger.error("set_decky_settings: %s", exc)
            return {"ok": False}

    # ── auto_pull_if_no_conflict ──────────────────────────────────────────────

    async def auto_pull_if_no_conflict(self) -> dict[str, Any]:
        """
        For every assigned game whose cloud save is ahead with no conflict,
        pull then push automatically. Used by background polling.
        Returns {game_id: {"status": str, "last_known_status": str|None}}
        """
        if not _ensure_path():
            return {}

        loop = asyncio.get_running_loop()

        try:
            from games import load_games
            from machine_config import load_local_configs
            from sync import (
                get_sync_status,
                load_sync_state,
                rclone_sync_pull,
                rclone_sync_push,
                PULL_CONFLICT,
            )
        except ImportError as exc:
            logger.error("auto_pull_if_no_conflict import: %s", exc)
            return {}

        games      = load_games()
        local_cfgs = load_local_configs()
        assigned   = [g for g in games if g["id"] in local_cfgs]
        _transient = {"no_connection", "error"}

        result: dict[str, Any] = {}

        for g in assigned:
            game_id = g["id"]
            try:
                status = await loop.run_in_executor(
                    None, lambda gm=g: get_sync_status(gm["id"], gm)
                )
            except Exception as exc:
                logger.error("auto_pull_if_no_conflict status %s: %s", game_id, exc)
                status = "error"

            if status == "cloud_ahead":
                try:
                    pull_ok, pull_msg = await loop.run_in_executor(
                        None, lambda gid=game_id, gm=g: rclone_sync_pull(gid, gm)
                    )
                    if pull_msg == PULL_CONFLICT:
                        status = "conflict"
                    elif pull_ok:
                        push_ok, _ = await loop.run_in_executor(
                            None, lambda gid=game_id, gm=g: rclone_sync_push(gid, gm)
                        )
                        status = "in_sync" if push_ok else "error"
                    else:
                        status = "error"
                except Exception as exc:
                    logger.error("auto_pull_if_no_conflict sync %s: %s", game_id, exc)
                    status = "error"

            last_known = load_sync_state(game_id) if status in _transient else None
            result[game_id] = {"status": status, "last_known_status": last_known}

        return result

    # ── resolve_conflict ──────────────────────────────────────────────────────

    async def resolve_conflict(self, game_id: str, keep: str) -> dict[str, Any]:
        """
        Resolve a save conflict.

        keep='local'  — keep local saves, push them to cloud
        keep='remote' — overwrite local saves with cloud copy
        """
        if not _ensure_path():
            return {"ok": False, "msg": "ExternalGameSync not installed"}

        loop = asyncio.get_running_loop()

        try:
            from games import find_game
            from sync import rclone_sync_pull_force, rclone_sync_push
        except ImportError as exc:
            return {"ok": False, "msg": str(exc)}

        game = find_game(game_id)
        if not game:
            return {"ok": False, "msg": f"Game not found: {game_id}"}

        try:
            ok, msg = await loop.run_in_executor(
                None,
                lambda gid=game_id, k=keep, gm=game: rclone_sync_pull_force(gid, k, gm),
            )
            if not ok:
                return {"ok": False, "msg": msg}

            if keep == "local":
                push_ok, push_msg = await loop.run_in_executor(
                    None,
                    lambda gid=game_id, gm=game: rclone_sync_push(gid, gm),
                )
                return {"ok": push_ok, "msg": push_msg}

            return {"ok": True, "msg": "ok"}

        except Exception as exc:
            logger.error("resolve_conflict %s keep=%s: %s", game_id, keep, exc)
            return {"ok": False, "msg": str(exc)}
