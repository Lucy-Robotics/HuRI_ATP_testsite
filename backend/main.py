"""
Backend bridge between the browser frontend and a HuRI server.

The HuRI session itself goes through huri-core's client SDK
(``huri_core.core.client.HuRIClient``, installed with ``huri-core[client]``
and the curated events of ``huri-events``, see requirements.txt), driven by
``huri_bridge.py``. This file owns everything that is specific to *this
website*: Authelia/magic-link auth, CORS, serving the built SPA, the
module-combination presets, the HuRI launcher, and reshaping gesture output for
this frontend's 3D rig.

Frontend <-> backend protocol on ``/ws`` (unchanged by the move to the SDK):

  1. the frontend sends one handshake: ``{"modules": {tag: {name, args}}}``
     (see the repo-root ``presets/`` folder, loaded by ``huri_presets.py``,
     for ready-made combinations);
  2. the backend replies ``{"type": "session_config", "config": {...}}``;
  3. inbound: binary frames are mic PCM (always ``huri.audio.in``); JSON
     frames are ``{"topic": "question"|"token", "text": ...}`` — the
     frontend's event dropdown picks which one a given typed message targets
     ("rag.in" vs "rag.out" in HuRI/ATP.xlsx's terms);
  4. outbound: ``{"type": "token"|"audio"|"segment"|"question"|"error", ...}``,
     where a ``"segment"`` message carries HuRI's raw pose/expression/
     translation arrays already converted into this frontend's ``{t,
     rotations, blendshapes, positions}`` frame format by ``pipeline.py``.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from huri_bridge import browser_module_names, run_browser_session
from huri_launcher import (
    AlreadyRunningError,
    HuriLauncher,
    NotRunningError,
    UnknownConfigError,
    resolve_modules_path,
)
from huri_presets import PRESETS_DIR, load_presets
from pipeline import motion_arrays_to_frames

_REPO_ROOT = Path(__file__).resolve().parents[1]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Authelia OIDC (relying party) -----------------------------------------
# The website is the OIDC relying party for Authelia. After login, the user's
# stable opaque `sub` is stored in the signed session and pinned as the HuRI
# `user_id`, so RAG retrieval is scoped to the real authenticated identity
# instead of a throwaway UUID. Set REQUIRE_AUTH=0 to fall back to the local
# persisted-UUID behavior for development without Authelia.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "huri-website")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH", "1") not in ("0", "false", "False")


def _is_falsey(value: str) -> bool:
    return value.strip().lower() in ("0", "false", "no", "off", "")


# The session cookie carries the authenticated OIDC `sub`, which the /ws handler
# pins as the HuRI `user_id` / RAG partition key. A missing or guessable secret
# therefore lets anyone forge a cookie for any `sub` and read another user's
# ingested documents — so with auth enabled we refuse to start without a real
# one rather than silently falling back to a public default.
_DEV_SESSION_SECRET = "dev-insecure-session-secret"
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")
if not SESSION_SECRET or SESSION_SECRET == _DEV_SESSION_SECRET:
    if REQUIRE_AUTH:
        raise RuntimeError(
            "SESSION_SECRET is unset (or left at the insecure dev default) while "
            "REQUIRE_AUTH is on. Generate one with `openssl rand -hex 32`, or set "
            "REQUIRE_AUTH=0 for local development without Authelia."
        )
    SESSION_SECRET = _DEV_SESSION_SECRET
    logger.warning("SESSION_SECRET unset; using an insecure dev secret (auth disabled).")

# Cookie flags. Over HTTPS the session cookie must be Secure, so default it on
# whenever auth is required. same_site="lax" is correct when the frontend and
# backend share a site (the OIDC callback is a top-level navigation that still
# sends the cookie); use COOKIE_SAMESITE=none with COOKIE_SECURE=1 for a
# cross-site frontend.
COOKIE_SECURE = not _is_falsey(
    os.environ.get("COOKIE_SECURE", "1" if REQUIRE_AUTH else "0")
)
COOKIE_SAMESITE = os.environ.get("COOKIE_SAMESITE", "lax").lower()

# --- Magic-link auto-login (passwordless QR sign-in for demos) --------------
# A signed token — baked into a QR code by tools/make_magic_qr.py — is redeemed
# at /auth/magic, which drops the same signed session an OIDC login would, so the
# /ws handler and RAG partitioning are identical. Holding the token IS the
# credential: anyone who scans the QR is logged in as the encoded identity, so
# only ever mint one for a throwaway demo `sub`, never a real user. The token is
# signed (not encrypted) with MAGIC_LINK_SECRET — unset it to disable the route.
MAGIC_LINK_SECRET = os.environ.get("MAGIC_LINK_SECRET", "")
# Seconds a token stays valid; 0 (the default) = never expires, for a QR you can
# print once and reuse. Set it to time-box the link.
MAGIC_LINK_MAX_AGE = int(os.environ.get("MAGIC_LINK_MAX_AGE", "0") or "0")
# Namespaces the signature so a magic-link token can't be swapped in for another
# itsdangerous token signed with the same secret. Must match make_magic_qr.py.
MAGIC_LINK_SALT = "huri-magic-login"

_magic_serializer = None
if MAGIC_LINK_SECRET:
    from itsdangerous import URLSafeTimedSerializer

    _magic_serializer = URLSafeTimedSerializer(MAGIC_LINK_SECRET, salt=MAGIC_LINK_SALT)

_oauth = None
if OIDC_ISSUER:
    from authlib.integrations.starlette_client import OAuth

    _oauth = OAuth()
    _oauth.register(
        name="authelia",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=f"{OIDC_ISSUER}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
            "code_challenge_method": "S256",  # Authelia client requires PKCE
        },
    )
elif REQUIRE_AUTH:
    logger.warning(
        "REQUIRE_AUTH is on but OIDC_ISSUER is unset — websocket connections "
        "will be rejected. Set OIDC_ISSUER or REQUIRE_AUTH=0."
    )

app = FastAPI(title="HuRI Website")

# SessionMiddleware backs the signed cookie that carries the OIDC `sub`; it is
# read on both the HTTP auth routes and the /ws handshake.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site=COOKIE_SAMESITE,
    https_only=COOKIE_SECURE,
)

# Credentialed CORS: the browser attaches the session cookie on cross-origin
# requests, so the allowlist must name the real frontend origin(s) — the
# hardcoded localhost-only list silently breaks the deployed site. Dev origins
# stay allowed; FRONTEND_URL adds wherever the SPA is actually served.
_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
ALLOWED_ORIGINS = sorted({FRONTEND_URL, *_DEV_ORIGINS})

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/auth/login")
async def auth_login(request: Request):
    """Kick off the OIDC authorization-code flow at Authelia."""
    if _oauth is None:
        return JSONResponse({"error": "OIDC not configured"}, status_code=503)
    redirect_uri = os.environ.get("OIDC_REDIRECT_URI") or str(
        request.url_for("auth_callback")
    )
    return await _oauth.authelia.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Exchange the code, persist the user's `sub`, and return to the frontend."""
    if _oauth is None:
        return JSONResponse({"error": "OIDC not configured"}, status_code=503)
    try:
        token = await _oauth.authelia.authorize_access_token(request)
    except Exception:
        logger.exception("OIDC token exchange failed")
        return RedirectResponse(f"{FRONTEND_URL}/?auth=error")
    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    if not sub:
        return RedirectResponse(f"{FRONTEND_URL}/?auth=error")
    request.session["sub"] = sub
    request.session["email"] = userinfo.get("email")
    request.session["name"] = userinfo.get("name") or userinfo.get("preferred_username")
    logger.info("OIDC login: sub=%s email=%s", sub, userinfo.get("email"))
    return RedirectResponse(f"{FRONTEND_URL}/?auth=ok")


@app.get("/auth/magic")
async def auth_magic(request: Request):
    """Redeem a signed magic-link token into a session (passwordless demo login).

    The token — carried in a QR minted by tools/make_magic_qr.py — encodes the
    {sub, email, name} identity to grant. We verify it with MAGIC_LINK_SECRET,
    then store the same session an OIDC login would, so everything downstream
    (the /ws handshake, the HuRI user_id / RAG partition) is unchanged. Holding
    the token IS the credential; keep it scoped to a demo identity.
    """
    if _magic_serializer is None:
        return JSONResponse(
            {"error": "magic-link login not configured"}, status_code=503
        )
    token = request.query_params.get("t") or request.query_params.get("token")
    if not token:
        return JSONResponse({"error": "missing token"}, status_code=400)

    from itsdangerous import BadSignature, SignatureExpired

    try:
        data = _magic_serializer.loads(token, max_age=MAGIC_LINK_MAX_AGE or None)
    except SignatureExpired:
        logger.info("magic-link login rejected: token expired")
        return RedirectResponse(f"{FRONTEND_URL}/?auth=expired")
    except BadSignature:
        logger.warning("magic-link login rejected: bad signature")
        return RedirectResponse(f"{FRONTEND_URL}/?auth=error")

    sub = (data or {}).get("sub")
    if not sub:
        return RedirectResponse(f"{FRONTEND_URL}/?auth=error")
    request.session["sub"] = sub
    request.session["email"] = data.get("email")
    request.session["name"] = data.get("name")
    logger.info("magic-link login: sub=%s email=%s", sub, data.get("email"))
    return RedirectResponse(f"{FRONTEND_URL}/?auth=ok")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse(f"{FRONTEND_URL}/?auth=loggedout")


@app.get("/auth/me")
async def auth_me(request: Request):
    """Tell the frontend whether the visitor is signed in."""
    sub = request.session.get("sub")
    return {
        "authenticated": bool(sub),
        "auth_required": REQUIRE_AUTH,
        "sub": sub,
        "email": request.session.get("email"),
        "name": request.session.get("name"),
    }


@app.get("/presets")
async def presets() -> Dict[str, Any]:
    """Module-combination presets for the Event Configuration modal — a
    starting point for a session's ``modules``, covering HuRI/ATP.xlsx F1/F2
    without a server restart. Read fresh from the repo-root ``presets/``
    folder on every call (see huri_presets.py), so a preset file added,
    edited, or removed there shows up on the next request with no backend
    restart. The files use huri-modules' module names (``vad``, ``mov``);
    they are served in the frontend's (``mic``, ``gesture``)."""
    return load_presets(browser_names=True)


def _huri_ws_url() -> str:
    return os.environ.get("HURI_URL", "ws://localhost:8000/session")


def _huri_http_base() -> str:
    """HuRI's plain HTTP origin, derived from the same HURI_URL the websocket
    session connects to — ``/session`` and ``/modules`` live on the one
    FastAPI ingress huri-core's server binds."""
    http_url = _huri_ws_url().replace("wss://", "https://").replace("ws://", "http://")
    return http_url.rsplit("/", 1)[0]  # drop the trailing "/session"


async def _fetch_server_modules() -> Dict[str, Any]:
    """HuRI's ``GET /modules``: ``{"modules": [...], "details": {...},
    "huri_core": "..."}`` (huri-core >= 0.4; older servers only give
    ``modules``)."""
    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(f"{_huri_http_base()}/modules")
        resp.raise_for_status()
        return resp.json()


async def _server_module_names() -> Optional[List[str]]:
    """The modules the HuRI server enabled, or None when it can't be asked."""
    try:
        return list((await _fetch_server_modules()).get("modules") or [])
    except Exception as e:
        logger.info("Could not list HuRI's modules: %s", e)
        return None


async def _require_auth(request: Request) -> str:
    """Gate for routes that do more than read (right now: the HuRI launcher,
    which spawns real local processes) — same bar as the /ws handshake: a
    signed-in session when REQUIRE_AUTH is on, open otherwise (local dev
    without Authelia)."""
    sub = request.session.get("sub")
    if REQUIRE_AUTH and not sub:
        raise HTTPException(status_code=401, detail="Not signed in")
    return sub or "anonymous"


# Manages a single local `serve run` subprocess, run from the huri-modules
# checkout (the submodule at huri-modules/, see huri_launcher.py) — covers
# HuRI/ATP.xlsx F1 ("launch HuRI with different config files, check the
# dashboard") without a terminal.
_launcher = HuriLauncher(
    resolve_modules_path(os.environ, _REPO_ROOT),
    probe_url=_huri_http_base(),
    presets_dir=PRESETS_DIR,
)


class _LauncherStartRequest(BaseModel):
    config: str


@app.get("/launcher/huri/configs")
async def launcher_configs(_sub: str = Depends(_require_auth)) -> Dict[str, Any]:
    """Server-side HuRI configs (huri-modules' config/huri*.yaml, and the
    presets/F*/huri*.yaml copies) that can be launched — as opposed to the client_*.yaml configs, which are for a
    client connecting to an already-running HuRI, not for launching one."""
    return {"configs": _launcher.list_configs()}


@app.get("/launcher/huri/status")
async def launcher_status(_sub: str = Depends(_require_auth)) -> Dict[str, Any]:
    return await _launcher.status()


@app.get("/launcher/huri/logs")
async def launcher_logs(tail: int = 200, _sub: str = Depends(_require_auth)) -> Dict[str, Any]:
    return {"lines": _launcher.logs(tail)}


@app.post("/launcher/huri/start")
async def launcher_start(
    body: _LauncherStartRequest, _sub: str = Depends(_require_auth)
) -> Dict[str, Any]:
    try:
        await _launcher.start(body.config)
    except UnknownConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AlreadyRunningError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return await _launcher.status()


@app.post("/launcher/huri/stop")
async def launcher_stop(_sub: str = Depends(_require_auth)) -> Dict[str, Any]:
    try:
        await _launcher.stop()
    except NotRunningError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return await _launcher.status()


@app.get("/huri-modules")
async def huri_modules() -> Dict[str, Any]:
    """Which modules the connected HuRI instance actually has deployed
    (huri-core's ``GET /modules``) — lets the Event Configuration modal
    disable module combinations that would otherwise only fail once a tester
    hits Apply (e.g. "tts"/"gesture" on a machine with no GPU, per
    huri-modules' install.sh device plan).

    ``modules`` is in the frontend's names (``vad`` -> ``mic``, ``mov`` ->
    ``gesture``, either TTS engine -> ``tts``); ``server_modules``,
    ``details`` and ``huri_core`` are HuRI's answer as is.

    ``modules: null`` means HuRI couldn't be reached at all (distinct from an
    empty list, which would mean it's up but has nothing registered) — the
    frontend should let everything through rather than lock the UI when it
    can't tell what's actually available.
    """
    try:
        info = await _fetch_server_modules()
    except Exception as e:
        logger.warning("Could not reach HuRI at %s/modules: %s", _huri_http_base(), e)
        return {"modules": None, "error": str(e)}
    server_modules = list(info.get("modules") or [])
    return {
        "modules": browser_module_names(server_modules),
        "server_modules": server_modules,
        "details": info.get("details", {}),
        "huri_core": info.get("huri_core"),
    }


def _transform_outbound(message: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a raw HuRI ``"motion"`` message into this frontend's per-frame
    rig format. Every other message type passes through unchanged.

    This is the one place gesture output is specific to *this* 3D asset
    (SMPL-X pose/FLAME expression arrays -> named-bone quaternions +
    blendshapes, see pipeline.py) — huri_bridge.py only forwards HuRI's
    huri.motion events as raw arrays.
    """
    if message.get("type") != "motion":
        return message
    frames = motion_arrays_to_frames(
        message["poses"],
        message["expressions"],
        message["trans"],
        pts=message["pts"],
        fps=message["fps"],
    )
    return {"type": "segment", "pts": message["pts"], "frames": frames}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # SessionMiddleware also populates ws.session from the signed cookie, so the
    # authenticated OIDC `sub` is available before we open the HuRI session.
    sub = ws.session.get("sub")
    if REQUIRE_AUTH and not sub:
        await ws.accept()
        await ws.send_json(
            {"type": "error", "message": "Not signed in — visit /auth/login first."}
        )
        await ws.close(code=4401)  # application-level "unauthorized"
        return

    await ws.accept()
    user_id = sub or "anonymous"
    logger.info("Frontend connected (user_id=%s)", user_id)

    try:
        await run_browser_session(
            ws,
            user_id=user_id,
            huri_url=_huri_ws_url(),
            server_modules=_server_module_names,
            transform_outbound=_transform_outbound,
        )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Bridge error")
    logger.info("Frontend disconnected (user_id=%s)", user_id)


# Serve the built SPA (single-origin deploy). Mounted LAST so the API routes and
# the /ws handshake above take precedence; StaticFiles(html=True) returns
# index.html for "/" and serves the hashed assets + /model.fbx. Absent in local
# dev (Vite serves the SPA on :5173), so guard on the directory existing.
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="spa")
    logger.info("Serving SPA from %s", _STATIC_DIR)
else:
    logger.info("No static/ dir; SPA not served by the backend (dev mode).")
