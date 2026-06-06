"""
ExternalGameSync — Supabase community config client.

Provides anonymous hash-based lookup and authenticated config submission.
Set SUPABASE_URL and SUPABASE_ANON_KEY below after creating your project.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from supabase import create_client
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

from config import APP_CONFIG_DIR

# ── Fill these in after creating your Supabase project ───────────────────────
SUPABASE_URL      = "https://swcxosmopriwsjcmcxqz.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InN3Y3hvc21vcHJpd3NqY21jeHF6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA2ODA4OTksImV4cCI6MjA5NjI1Njg5OX0.9NjgO_EZ87aQSm6vwLLO0WgXry3NWCJVQsBxXM5wuLo"
# ─────────────────────────────────────────────────────────────────────────────

_SESSION_FILE = APP_CONFIG_DIR / "community_session.json"
_OAUTH_PORT   = 54321
_REDIRECT_URI = f"http://localhost:{_OAUTH_PORT}/callback"

# Small HTML page served to the browser after OAuth redirect.
# The fragment (#access_token=...) is client-side only, so JavaScript reads it
# and forwards the tokens to our local /token endpoint.
_FRAGMENT_PAGE = """<!DOCTYPE html><html><body>
<p>Completing sign-in&hellip;</p>
<script>
  var p = new URLSearchParams(window.location.hash.slice(1));
  var q = new URLSearchParams();
  ['access_token','refresh_token'].forEach(function(k){ if(p.get(k)) q.set(k, p.get(k)); });
  fetch('/token?' + q.toString()).then(function(){
    document.body.innerHTML = '<p>Signed in! You can close this tab.</p>';
  });
</script>
</body></html>"""


def available() -> bool:
    """True if supabase package is installed and URL has been configured."""
    return _AVAILABLE and not SUPABASE_URL.startswith("https://YOUR_")


def _load_session() -> dict | None:
    if _SESSION_FILE.exists():
        try:
            return json.loads(_SESSION_FILE.read_text())
        except Exception:
            pass
    return None


def _save_session(session: dict | None) -> None:
    if session is None:
        _SESSION_FILE.unlink(missing_ok=True)
    else:
        APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(json.dumps(session))


def _get_valid_session() -> dict | None:
    """Return a session with a fresh access token.

    Delegates JWT validation and refresh to supabase-py's GoTrue client
    (set_session checks expiry and calls the refresh endpoint if needed).
    """
    if not _AVAILABLE:
        return None
    sess = _load_session()
    if not sess:
        return None
    try:
        sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        result = sb.auth.set_session(sess["access_token"], sess.get("refresh_token", ""))
        new = result.session
        if not new:
            return None
        updated = {
            "access_token":  new.access_token,
            "refresh_token": new.refresh_token,
            "provider":      sess.get("provider"),
        }
        _save_session(updated)
        return updated
    except Exception:
        return None


def _get_client():
    if not _AVAILABLE:
        raise RuntimeError("supabase package not installed — run: pip install supabase")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def _authed_post(table: str, data: dict, *, ignore_conflict: bool = False) -> tuple[int, str]:
    """POST a row to a Supabase table using the stored user token.

    supabase-py 2.x doesn't reliably propagate the auth token to PostgREST,
    so authenticated writes go direct via httpx (which is already a supabase-py
    dependency). Returns (status_code, response_text).
    """
    import httpx
    sess = _get_valid_session()
    if not sess:
        raise RuntimeError("Not signed in to community service — call sign_in() first")
    prefer = "resolution=ignore-duplicates,return=minimal" if ignore_conflict else "return=minimal"
    resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={
            "apikey":        SUPABASE_ANON_KEY,
            "Authorization": f"Bearer {sess['access_token']}",
            "Content-Type":  "application/json",
            "Prefer":        prefer,
        },
        json=data,
    )
    return resp.status_code, resp.text


def is_signed_in() -> bool:
    return _load_session() is not None


def signed_in_provider() -> str | None:
    """Return the OAuth provider name used to sign in ('github', 'google', …) or None."""
    sess = _load_session()
    return sess.get("provider") if sess else None


def sign_in(provider: str = "discord") -> tuple[bool, str]:
    """Open a browser OAuth flow and wait for the callback (2-minute timeout).

    Supported providers: discord, github, google (must be enabled in Supabase
    dashboard under Authentication → Providers).
    Also add http://localhost:54321/callback to Authentication → URL Configuration
    → Redirect URLs.

    Handles both PKCE (code in query params, newer Supabase default) and implicit
    (token in URL fragment) flows. The same sb instance is used throughout so the
    PKCE code verifier is available for exchange_code_for_session.
    """
    if not _AVAILABLE:
        return False, "supabase package not installed"

    sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    res = sb.auth.sign_in_with_oauth({
        "provider": provider,
        "options": {"redirect_to": _REDIRECT_URI},
    })
    webbrowser.open(res.url)

    received: dict = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                qs = parse_qs(parsed.query)
                code = qs.get("code", [""])[0]
                if code:
                    # PKCE flow: exchange auth code using the same client that
                    # generated the auth URL (it holds the code verifier).
                    try:
                        session = sb.auth.exchange_code_for_session({"auth_code": code})
                        received["access_token"]  = session.session.access_token
                        received["refresh_token"] = session.session.refresh_token
                        body = b"<html><body><p>Signed in! You can close this tab.</p></body></html>"
                    except Exception as e:
                        received["error"] = str(e)
                        body = f"<html><body><p>Sign-in error: {e}</p></body></html>".encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    done.set()
                else:
                    # Implicit flow: serve JS page that reads the fragment and
                    # forwards tokens to /token.
                    body = _FRAGMENT_PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif parsed.path == "/token":
                # Implicit flow token receipt from JS fragment capture.
                qs = parse_qs(parsed.query)
                received["access_token"]  = qs.get("access_token",  [""])[0]
                received["refresh_token"] = qs.get("refresh_token", [""])[0]
                body = b"<html><body>Done</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()

        def log_message(self, *_):
            pass

    server = HTTPServer(("localhost", _OAUTH_PORT), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        if not done.wait(timeout=120):
            return False, "Sign-in timed out or was cancelled"
        if received.get("error"):
            return False, f"Sign-in failed: {received['error']}"
        if not received.get("access_token"):
            return False, "No token received — sign-in may have failed"
        received["provider"] = provider
        _save_session(received)
        return True, "Signed in"
    finally:
        server.shutdown()


def sign_out() -> None:
    _save_session(None)


# ── Anonymous queries ─────────────────────────────────────────────────────────

def lookup_by_hash(hash_val: str) -> list[dict]:
    """Return approved game configs whose exe or installer hash matches hash_val.

    Each result dict is a game_configs row with an extra '_hash_type' key
    ('exe' or 'installer') indicating which hash matched.
    Returns [] on any error or if the community service is not configured.
    """
    if not available():
        return []
    try:
        sb = _get_client()
        rows = (
            sb.table("game_hashes")
              .select("hash_type, game_configs(id, name, exe_path, save_path, save_filter, env_vars, steam_app_id, votes)")
              .eq("hash", hash_val)
              .execute()
        )
        results = []
        for row in (rows.data or []):
            if not isinstance(row, dict):
                continue
            cfg = row.get("game_configs")
            if isinstance(cfg, dict) and cfg.get("id") is not None:
                cfg["_hash_type"] = row["hash_type"]
                results.append(cfg)
        return results
    except Exception:
        return []


def search_by_name(query: str, limit: int = 30) -> list[dict]:
    """Search approved community configs by name (case-insensitive substring match).

    Returns [] on any error or if the community service is not configured.
    """
    if not available() or not query.strip():
        return []
    try:
        sb = _get_client()
        rows = (
            sb.table("game_configs")
              .select("id, name, exe_path, save_path, save_filter, env_vars, steam_app_id, votes")
              .ilike("name", f"%{query.strip()}%")
              .order("votes", desc=True)
              .limit(limit)
              .execute()
        )
        return [r for r in (rows.data or []) if isinstance(r, dict) and r.get("id") is not None]
    except Exception:
        return []


def lookup_by_steam_app_id(steam_app_id: str) -> list[dict]:
    """Return approved game configs matching a Steam native app ID.

    Returns [] on any error or if the community service is not configured.
    """
    if not available():
        return []
    try:
        sb = _get_client()
        rows = (
            sb.table("game_configs")
              .select("id, name, exe_path, save_path, save_filter, env_vars, steam_app_id, votes")
              .eq("steam_app_id", steam_app_id)
              .order("votes", desc=True)
              .execute()
        )
        return [r for r in (rows.data or []) if isinstance(r, dict) and r.get("id") is not None]
    except Exception:
        return []


# ── Authenticated reads ───────────────────────────────────────────────────────

def get_my_configs() -> list[dict]:
    """Return all configs submitted by the signed-in user, including hidden ones.

    Uses the user's JWT so RLS allows reading their own hidden records.
    Returns [] if not signed in, not configured, or on any error.
    """
    if not available():
        return []
    sess = _get_valid_session()
    if not sess:
        return []
    try:
        import httpx, base64 as _b64, json as _json
        payload = sess["access_token"].split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        user_id = _json.loads(_b64.b64decode(payload))["sub"]
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/game_configs",
            headers={
                "apikey":        SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {sess['access_token']}",
            },
            params={
                "select":       "id,name,exe_path,save_path,votes,hidden,created_at",
                "submitted_by": f"eq.{user_id}",
                "order":        "created_at.desc",
            },
        )
        if resp.status_code != 200:
            return []
        return [r for r in resp.json() if isinstance(r, dict) and r.get("id") is not None]
    except Exception:
        return []


# ── Authenticated writes ──────────────────────────────────────────────────────

def submit_config(
    game: dict,
    exe_hash: str | None = None,
    installer_hash: str | None = None,
    platform: str | None = None,
) -> tuple[bool, str, int | None]:
    """Submit a game config to the community database for moderator review.

    Requires the user to be signed in. The config lands in 'pending' status and
    will not appear in public lookups until approved.

    Returns (ok, message, community_id) where community_id is the server-assigned
    integer primary key of the new record (None on failure).
    """
    if not available():
        return False, "Community service not configured", None
    row: dict = {
        "name":        game["name"],
        "exe_path":    game.get("exe_path", ""),
        "save_path":   game.get("save_path", ""),
        "save_filter": game.get("save_filter", ""),
        "env_vars":    game.get("env_vars", ""),
    }
    if game.get("steam_app_id"):
        row["steam_app_id"] = str(game["steam_app_id"])

    try:
        import httpx, json as _json
        sess = _get_valid_session()
        if not sess:
            return False, "Not signed in to community service", None
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/game_configs",
            headers={
                "apikey":        SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {sess['access_token']}",
                "Content-Type":  "application/json",
                "Prefer":        "return=representation",
            },
            json=row,
        )
    except Exception as e:
        return False, str(e), None

    if resp.status_code not in (200, 201):
        text = resp.text
        if "42501" in text or "security policy" in text:
            return False, "Permission denied — are you signed in?", None
        if "limit" in text.lower() or "daily" in text.lower():
            return False, "Daily submission limit reached — try again tomorrow", None
        return False, f"Submit failed ({resp.status_code}): {text}", None

    try:
        community_id: int = resp.json()[0]["id"]
    except Exception:
        return True, "Config submitted — pending moderator review", None

    for h, htype in [(exe_hash, "exe"), (installer_hash, "installer")]:
        if h:
            try:
                _authed_post("game_hashes", {
                    "game_id":   community_id,
                    "hash":      h,
                    "hash_type": htype,
                    "platform":  platform,
                }, ignore_conflict=True)
            except Exception:
                pass

    return True, "Config submitted — pending moderator review", community_id


def cast_vote(community_id: int, vote: int) -> tuple[bool, str]:
    """Record or update the signed-in user's vote for a community config.

    vote must be 1 (working) or -1 (not working). Upserts so calling again
    with a different value changes the vote; calling with vote=0 is not valid
    (delete the row instead — not yet exposed here).
    Requires sign-in.
    """
    if not available():
        return False, "Community service not configured"
    if vote not in (1, -1):
        return False, "vote must be 1 or -1"
    try:
        import httpx
        sess = _get_valid_session()
        if not sess:
            return False, "Not signed in"
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/game_config_votes",
            headers={
                "apikey":        SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {sess['access_token']}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates,return=minimal",
            },
            json={"game_config_id": community_id, "vote": vote},
        )
    except Exception as e:
        return False, str(e)
    if resp.status_code in (200, 201):
        return True, "Vote recorded"
    return False, f"Vote failed ({resp.status_code}): {resp.text}"


def contribute_hash(
    community_id: int,
    hash_val: str,
    hash_type: str,
    platform: str | None = None,
) -> tuple[bool, str]:
    """Associate a new exe or installer hash with an existing approved config.

    community_id is the server-assigned integer primary key stored as
    community_id in the local games.json entry. Requires sign-in.
    Returns (True, ...) even if the hash was already known.
    """
    if not available():
        return False, "Community service not configured"
    try:
        status, text = _authed_post("game_hashes", {
            "game_id":   community_id,
            "hash":      hash_val,
            "hash_type": hash_type,
            "platform":  platform,
        }, ignore_conflict=True)
    except RuntimeError as e:
        return False, str(e)

    if status in (200, 201):
        return True, "Hash contributed"
    if "23505" in text or "duplicate" in text.lower():
        return True, "Hash already known"
    if "foreign" in text.lower() or "game_configs" in text.lower():
        return False, "Game not found in community database"
    return False, f"Contribute failed ({status}): {text}"
