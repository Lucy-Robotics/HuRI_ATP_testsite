"""Bridges one browser websocket to a HuRI session, through huri-core's client
SDK (``huri_core.core.client.HuRIClient``).

The browser protocol on ``/ws`` is this website's own and is unchanged by the
move to the SDK (the frontend and its built bundle in ``static/`` are not
touched):

  1. the browser sends one handshake: ``{"modules": {tag: {name, args}}}``;
  2. the backend opens the HuRI session and replies
     ``{"type": "session_config", "config": {...}}`` (:func:`describe_config`),
     or ``{"type": "error", "message": ...}`` when HuRI refuses it;
  3. inbound: binary frames are mic PCM (int16, 16 kHz: ``huri.audio.in``);
     JSON frames are ``{"topic": "question"|"token", "text": ...}``, the
     Composer's event dropdown ("rag.in" vs "rag.out" in the ATP's terms);
  4. outbound: ``{"type": "token"|"audio"|"motion"|"question", ...}``, each
     passed through ``transform_outbound`` (main.py turns ``"motion"`` into
     this frontend's ``"segment"`` rig frames).

Module names: the frontend still speaks the names of the HuRI monorepo
(``mic``, ``gesture``, ``tts``), while the curated modules of huri-modules are
``vad``, ``mov`` and ``tts`` (Piper, CPU) / ``tts_cosyvoice`` (CosyVoice3,
GPU). :func:`to_server_name` and :func:`to_browser_name` translate both ways
at this boundary, so the frontend, its presets dropdown and the HuRI server
each keep their own vocabulary. The preset files under ``presets/`` use the
server's names (the same ``modules:`` block a huri-cli client config holds).
"""

import asyncio
import base64
import json
import logging
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from huri_core.core.client import HuRIClient, HuRISessionError
from huri_core.core.events import BytesEvent
from huri_events import Audio, Motion, RAGQuestion, Token, Transcript

logger = logging.getLogger(__name__)

# Curated topics (huri-events) this bridge sends and receives.
TOPIC_AUDIO_IN = "huri.audio.in"
TOPIC_QUESTION = "huri.question"
TOPIC_TOKEN = "huri.token"
TOPIC_AUDIO_OUT = "huri.audio.out"
TOPIC_MOTION = "huri.motion"

# Browser (HuRI monorepo) name -> huri-modules name. "tts" is not here: it
# names both the browser's speech module and huri-modules' Piper engine, and
# resolves against the server (see to_server_name).
_SERVER_NAME = {"mic": "vad", "gesture": "mov"}
# huri-modules name -> browser name: the inverse, plus CosyVoice shown as "tts".
_BROWSER_NAME = {"vad": "mic", "mov": "gesture", "tts_cosyvoice": "tts"}
_TTS_ENGINES = ("tts", "tts_cosyvoice")

# Server module -> the topic the browser subscribes to when the module is in
# the session. HuRI rejects a subscription to a topic no module of the session
# produces, so subscriptions follow the requested modules.
#
# No "emo" -> "huri.emotion" entry: that is EMO's raw per-chunk read, which
# only travels between modules. The utterance-final read QAG links onto
# RAGQuestion.emotion reaches the browser with the "question" message.
_SUBSCRIPTION_BY_PRODUCER = {
    "rag": TOPIC_TOKEN,
    "tts": TOPIC_AUDIO_OUT,
    "tts_cosyvoice": TOPIC_AUDIO_OUT,
    "mov": TOPIC_MOTION,
    "qag": TOPIC_QUESTION,
}

# Key of each subscription in the session_config's "hooks" (the names the
# HuRI monorepo's web interface used).
_HOOK_KEY = {
    TOPIC_TOKEN: "token",
    TOPIC_AUDIO_OUT: "audio",
    TOPIC_MOTION: "motion",
    TOPIC_QUESTION: "question",
}

# Always offered: a sent topic nothing in the session consumes is accepted by
# HuRI and dropped, which lets a tester target huri.token directly (rag.out)
# even when rag/qag are not part of the session.
SENDS = [TOPIC_AUDIO_IN, TOPIC_QUESTION, TOPIC_TOKEN]

Outbound = Dict[str, Any]
TransformOutbound = Callable[[Outbound], Outbound]


class InvalidHandshake(ValueError):
    """The browser's handshake does not describe a session."""


def to_server_name(name: str, server_modules: Optional[Iterable[str]] = None) -> str:
    """The huri-modules name of a module the browser asked for.

    The browser's ``tts`` becomes whichever TTS engine the server enabled:
    ``tts`` (Piper) when it has it or when the server's modules are unknown,
    else ``tts_cosyvoice`` (config/huri.yaml enables only CosyVoice). New
    names (``vad``, ``mov``, ``tts_cosyvoice``...) pass through unchanged.
    """
    if name == "tts" and server_modules is not None:
        available = set(server_modules)
        if "tts" not in available and "tts_cosyvoice" in available:
            return "tts_cosyvoice"
    return _SERVER_NAME.get(name, name)


def to_browser_name(name: str) -> str:
    """The frontend's name of a huri-modules module (``vad`` -> ``mic``)."""
    return _BROWSER_NAME.get(name, name)


def browser_module_names(server_modules: Iterable[str]) -> List[str]:
    """A server's module list (GET /modules) in the frontend's vocabulary,
    sorted and without duplicates (both TTS engines show as ``tts``)."""
    return sorted({to_browser_name(name) for name in server_modules})


def to_browser_modules(modules: Mapping[str, Any]) -> Dict[str, Any]:
    """A ``modules`` block (a preset file, or a session) renamed for the
    frontend. Entries that are not ``{name, args}`` objects pass through."""
    renamed: Dict[str, Any] = {}
    for tag, entry in modules.items():
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            entry = {**entry, "name": to_browser_name(entry["name"])}
        renamed[tag] = entry
    return renamed


def build_session_modules(
    handshake: Any, server_modules: Optional[Iterable[str]] = None
) -> Dict[str, Dict[str, Any]]:
    """The HuRI ``modules`` block of a browser handshake.

    ``handshake["modules"]`` is ``{tag: {"name": <module>, "args": {...}}}``
    (or ``{"module": "pkg.mod:Cls"}`` for an unpackaged module). Which names
    the server accepts is its call: an unknown one comes back as a
    ``session_error`` listing the server's modules.
    """
    if not isinstance(handshake, Mapping):
        raise InvalidHandshake("the handshake must be a JSON object")
    requested = handshake.get("modules")
    if not isinstance(requested, Mapping) or not requested:
        raise InvalidHandshake("the handshake needs a non-empty 'modules' object")

    server_list = list(server_modules) if server_modules is not None else None
    modules: Dict[str, Dict[str, Any]] = {}
    for tag, entry in requested.items():
        if not isinstance(entry, Mapping):
            raise InvalidHandshake(f"module {tag!r} must be an object")
        args = entry.get("args")
        if args is None:
            args = {}
        if not isinstance(args, Mapping):
            raise InvalidHandshake(f"module {tag!r}: 'args' must be an object")
        name, path = entry.get("name"), entry.get("module")
        if isinstance(path, str) and name is None:
            modules[str(tag)] = {"module": path, "args": dict(args)}
        elif isinstance(name, str) and path is None:
            modules[str(tag)] = {
                "name": to_server_name(name, server_list),
                "args": dict(args),
            }
        else:
            raise InvalidHandshake(
                f"module {tag!r} needs exactly one of 'name' or 'module'"
            )
    return modules


def subscriptions(modules: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """Topics the browser receives for this session's modules."""
    names = {m.get("name") for m in modules.values()}
    return sorted(
        {topic for name, topic in _SUBSCRIPTION_BY_PRODUCER.items() if name in names}
    )


def describe_config(modules: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The ``session_config`` echoed to the browser: the session's modules in
    the frontend's names (it renders the mic button, the avatar... from them),
    the senders and hooks in the shape the HuRI monorepo's web interface gave,
    and the modules as HuRI actually runs them."""
    topics = subscriptions(modules)
    return {
        "modules": to_browser_modules(modules),
        "senders": {
            "audio": {"name": "web_audio", "topic": TOPIC_AUDIO_IN, "args": {}},
            "text": {"name": "web_text", "topic": TOPIC_QUESTION, "args": {}},
            "token": {"name": "web_token", "topic": TOPIC_TOKEN, "args": {}},
        },
        "hooks": {
            _HOOK_KEY[topic]: {
                "name": "web_" + _HOOK_KEY[topic],
                "topics": [topic],
                "args": {},
            }
            for topic in topics
        },
        "huri_modules": {k: dict(v) for k, v in modules.items()},
    }


# --- HuRI events -> browser messages ----------------------------------------


def token_message(event: Token) -> Outbound:
    """Streamed RAG answer text."""
    return {"type": "token", "text": event.text, "end": bool(event.end)}


def audio_message(event: Audio) -> Outbound:
    """Streamed TTS speech: little-endian float32 PCM, base64 for JSON."""
    pcm = np.ascontiguousarray(event.data, dtype="<f4")
    return {
        "type": "audio",
        "pts": event.pts,
        "sample_rate": event.sample_rate,
        "end": bool(event.end),
        "data": base64.b64encode(pcm.tobytes()).decode("ascii"),
    }


def motion_message(event: Motion) -> Outbound:
    """Gesture frames as raw SMPL-X arrays; mapping them onto this site's rig
    is main.py's ``transform_outbound`` (pipeline.py)."""
    return {
        "type": "motion",
        "pts": event.pts,
        "fps": event.fps,
        "poses": np.asarray(event.poses).tolist(),
        "expressions": np.asarray(event.expressions).tolist(),
        "trans": np.asarray(event.trans).tolist(),
    }


def question_message(event: RAGQuestion) -> Outbound:
    """The aggregated question right before RAG: the transcript and its
    linked emotion (ATP F9 "full question", F11 transcript + emotion)."""
    emotion = event.emotion
    return {
        "type": "question",
        "text": event.transcript.text,
        "emotion": (
            {
                "label": emotion.label,
                "confidence": emotion.confidence,
                "scores": emotion.scores,
            }
            if emotion
            else None
        ),
    }


_HANDLERS = {
    TOPIC_TOKEN: (Token, token_message),
    TOPIC_AUDIO_OUT: (Audio, audio_message),
    TOPIC_MOTION: (Motion, motion_message),
    TOPIC_QUESTION: (RAGQuestion, question_message),
}


# --- session ------------------------------------------------------------------


def make_client(
    huri_url: str,
    modules: Mapping[str, Mapping[str, Any]],
    user_id: str,
    outbound: "asyncio.Queue[Outbound]",
) -> HuRIClient:
    """A HuRI client for the session whose every subscribed event is turned
    into its browser message and queued on ``outbound``."""
    client = HuRIClient(huri_url, modules=modules, sends=SENDS, user_id=user_id)
    for topic in subscriptions(modules):
        event_cls, to_message = _HANDLERS[topic]

        async def forward(event: Any, to_message: Callable = to_message) -> None:
            await outbound.put(to_message(event))

        client.on(topic, event_cls, forward)
    return client


async def _pump_outbound(
    outbound: "asyncio.Queue[Outbound]",
    ws: WebSocket,
    transform_outbound: Optional[TransformOutbound],
) -> None:
    # One writer: Starlette websockets are not safe for concurrent sends.
    while True:
        payload = await outbound.get()
        if transform_outbound is not None:
            payload = transform_outbound(payload)
        await ws.send_json(payload)


async def _pump_inbound(ws: WebSocket, client: HuRIClient) -> None:
    """Browser input to HuRI: binary frames are mic PCM, JSON frames typed
    text for the topic the tester picked."""
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            raise WebSocketDisconnect()

        raw_bytes = msg.get("bytes")
        if raw_bytes is not None:
            await client.send(TOPIC_AUDIO_IN, BytesEvent(data=raw_bytes))
            continue

        raw_text = msg.get("text")
        if raw_text is None:
            continue
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        text = str(data.get("text", ""))
        if data.get("topic") == "token":
            # rag.out: straight at TTS/gesture, bypassing RAG (ATP F7/F8).
            await client.send(TOPIC_TOKEN, Token(text=text, end=False))
            await client.send(TOPIC_TOKEN, Token(text="", end=True))
        else:
            # rag.in: through the full RAG pipeline (ATP F9).
            await client.send(
                TOPIC_QUESTION, RAGQuestion(Transcript(text=text, end=True), None)
            )


# Teardowns in flight, referenced until done (asyncio keeps only weak ones).
_TEARDOWNS: "set[asyncio.Task]" = set()


async def _teardown(client: HuRIClient, tasks: Iterable["asyncio.Task"]) -> None:
    tasks = list(tasks)
    for task in tasks:
        task.cancel()
    await asyncio.wait(tasks)
    await client.close()


async def _connect(
    huri_url: str,
    handshake: Any,
    modules: Dict[str, Dict[str, Any]],
    user_id: str,
    outbound: "asyncio.Queue[Outbound]",
) -> HuRIClient:
    """A connected client for ``modules``. When HuRI refuses the session and
    its ``server_modules`` map the handshake to other names (the browser's
    ``tts`` on a CosyVoice-only server, when the module list was unknown
    beforehand), retries once with those."""
    client = make_client(huri_url, modules, user_id, outbound)
    try:
        await client.connect()
        return client
    except HuRISessionError as exc:
        if not exc.server_modules:
            raise
        remapped = build_session_modules(handshake, exc.server_modules)
        if remapped == modules:
            raise
    logger.info("Retrying the HuRI session with the server's module names")
    client = make_client(huri_url, remapped, user_id, outbound)
    await client.connect()
    return client


async def run_browser_session(
    ws: WebSocket,
    user_id: str,
    huri_url: str,
    server_modules: Optional[Callable[[], Any]] = None,
    transform_outbound: Optional[TransformOutbound] = None,
) -> None:
    """Drive one browser<->HuRI session end to end. ``ws`` is already accepted
    (the caller owns auth).

    ``server_modules``, if given, is an async callable returning the module
    names the HuRI server enabled (or None when unknown): it picks the TTS
    engine the browser's ``tts`` means.
    """
    try:
        handshake = await ws.receive_json()
    except Exception:
        await ws.close(code=1002)
        return

    available = await server_modules() if server_modules is not None else None
    try:
        modules = build_session_modules(handshake, available)
    except InvalidHandshake as exc:
        await ws.send_json(
            {"type": "error", "message": f"invalid or missing session config: {exc}"}
        )
        await ws.close(code=1008)
        return

    outbound: "asyncio.Queue[Outbound]" = asyncio.Queue()
    try:
        client = await _connect(huri_url, handshake, modules, user_id, outbound)
    except HuRISessionError as exc:
        message = f"HuRI refused the session: {exc.error}"
        if exc.server_modules:
            message += f" (server modules: {', '.join(exc.server_modules)})"
        logger.warning("HuRI refused the session: %s", exc.error)
        await ws.send_json({"type": "error", "message": message})
        await ws.close(code=1011)
        return
    except Exception as exc:  # unreachable server, bad URL...
        logger.warning("Could not reach HuRI at %s: %s", huri_url, exc)
        await ws.send_json(
            {"type": "error", "message": f"Could not reach HuRI at {huri_url}: {exc}"}
        )
        await ws.close(code=1011)
        return
    modules = client.modules

    await ws.send_json({"type": "session_config", "config": describe_config(modules)})

    huri_task = asyncio.create_task(client.wait_closed())
    inbound_task = asyncio.create_task(_pump_inbound(ws, client))
    writer_task = asyncio.create_task(
        _pump_outbound(outbound, ws, transform_outbound)
    )
    try:
        # Race the browser's input against HuRI closing the session under us,
        # so the browser hears about it instead of waiting on a dead session.
        done, _ = await asyncio.wait(
            {huri_task, inbound_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if huri_task in done:
            logger.warning("HuRI closed the session (user_id=%s)", user_id)
            try:
                await ws.send_json(
                    {"type": "error", "message": "HuRI session ended: server closed"}
                )
            except Exception:
                pass
        for task in (inbound_task, writer_task):
            if task in done and not task.cancelled():
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    logger.error("browser session error", exc_info=exc)
    finally:
        # Shielded: the HuRI session is closed even when the ASGI server
        # cancels this handler mid-teardown (shutdown, or a test client
        # closing), and that cancellation reaches the handler once, as the
        # server's own cancel scope expects.
        teardown = asyncio.create_task(
            _teardown(client, (huri_task, inbound_task, writer_task))
        )
        _TEARDOWNS.add(teardown)
        teardown.add_done_callback(_TEARDOWNS.discard)
        await asyncio.shield(teardown)
