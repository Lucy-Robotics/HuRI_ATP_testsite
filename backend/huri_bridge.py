"""Bridges one browser websocket to a HuRI session, through huri-core's client
SDK (``huri_core.core.client.HuRIClient``).

The browser protocol on ``/ws`` (this file is its reference):

  1. the browser sends one handshake: ``{"modules": {tag: {name, args}}}``,
     the ``modules`` block of a HuRI session with the server's module names
     (``vad``, ``stt``, ``rag``, ``tts`` / ``tts_cosyvoice``, ``mov``...);
  2. the backend opens the HuRI session and replies
     ``{"type": "session_config", "config": {"modules": {...}}}``, or
     ``{"type": "error", "message": ...}`` when it cannot;
  3. inbound: binary frames are mic PCM (int16, 16 kHz: ``huri.audio.in``);
     JSON frames are ``{"topic": "question"|"token", "text": ...}``, the
     Composer's event dropdown ("rag.in" vs "rag.out" in the ATP's terms);
  4. outbound: ``{"type": "token"|"audio"|"segment"|"question"|"error", ...}``.
     The bridge emits ``"motion"`` (raw SMPL-X arrays); main.py's
     ``transform_outbound`` turns it into this frontend's ``"segment"`` rig
     frames before it reaches the browser.

The browser receives the outputs of the session's modules that it has a
message for: each module's output topic comes from the server's
``GET /modules`` details.
"""

import asyncio
import base64
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect
from huri_core.core.client import HuRIClient, HuRISessionError
from huri_core.core.events import BytesEvent
from huri_events import Audio, Motion, RAGQuestion, Token, Transcript

logger = logging.getLogger(__name__)

TOPIC_AUDIO_IN = "huri.audio.in"
TOPIC_QUESTION = "huri.question"
TOPIC_TOKEN = "huri.token"
TOPIC_AUDIO_OUT = "huri.audio.out"
TOPIC_MOTION = "huri.motion"

# Always offered: a sent topic nothing in the session consumes is accepted by
# HuRI and dropped, which lets a tester target huri.token directly (rag.out)
# even when rag/qag are not part of the session.
SENDS = [TOPIC_AUDIO_IN, TOPIC_QUESTION, TOPIC_TOKEN]

Outbound = Dict[str, Any]
TransformOutbound = Callable[[Outbound], Outbound]
# GET /modules "details": {module name: {"input", "output", "actuation"}}.
ModuleDetails = Mapping[str, Mapping[str, Any]]


class InvalidHandshake(ValueError):
    """The browser's handshake does not describe a session."""


def build_session_modules(handshake: Any) -> Dict[str, Dict[str, Any]]:
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
            modules[str(tag)] = {"name": name, "args": dict(args)}
        else:
            raise InvalidHandshake(
                f"module {tag!r} needs exactly one of 'name' or 'module'"
            )
    return modules


def describe_config(modules: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """The ``session_config`` echoed to the browser: the session's modules
    (it renders the mic button, the avatar... from them)."""
    return {"modules": {tag: dict(entry) for tag, entry in modules.items()}}


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


# The topics the browser has a message for. Not huri.emotion: that is EMO's
# raw per-chunk read; the utterance-final one rides on RAGQuestion.emotion.
_HANDLERS = {
    TOPIC_TOKEN: (Token, token_message),
    TOPIC_AUDIO_OUT: (Audio, audio_message),
    TOPIC_MOTION: (Motion, motion_message),
    TOPIC_QUESTION: (RAGQuestion, question_message),
}


def subscriptions(
    modules: Mapping[str, Mapping[str, Any]], details: ModuleDetails
) -> List[str]:
    """Topics the browser receives: the output topics (per the server's
    ``GET /modules`` details) of the session's modules that the bridge has a
    browser message for. HuRI refuses a subscription to a topic no module of
    the session produces, so this follows the requested modules."""
    outputs = {
        (details.get(entry.get("name")) or {}).get("output")
        for entry in modules.values()
    }
    return sorted(outputs & _HANDLERS.keys())


# --- session ------------------------------------------------------------------


def make_client(
    huri_url: str,
    modules: Mapping[str, Mapping[str, Any]],
    topics: List[str],
    user_id: str,
    outbound: "asyncio.Queue[Outbound]",
) -> HuRIClient:
    """A HuRI client for the session whose every event on ``topics`` is
    turned into its browser message and queued on ``outbound``."""
    client = HuRIClient(huri_url, modules=modules, sends=SENDS, user_id=user_id)
    for topic in topics:
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


async def _teardown(client: HuRIClient, tasks: List["asyncio.Task"]) -> None:
    """Cancel the session's tasks (any that were created) and close HuRI."""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks)
    await client.close()


async def _refuse(ws: WebSocket, message: str, code: int) -> None:
    await ws.send_json({"type": "error", "message": message})
    await ws.close(code=code)


async def run_browser_session(
    ws: WebSocket,
    user_id: str,
    huri_url: str,
    module_details: Callable[[], Awaitable[Optional[ModuleDetails]]],
    transform_outbound: Optional[TransformOutbound] = None,
) -> None:
    """Drive one browser<->HuRI session end to end. ``ws`` is already accepted
    (the caller owns auth). ``module_details`` returns the server's
    ``GET /modules`` details, or None when they cannot be read."""
    try:
        handshake = await ws.receive_json()
    except Exception:
        await ws.close(code=1002)
        return

    try:
        modules = build_session_modules(handshake)
    except InvalidHandshake as exc:
        await _refuse(ws, f"invalid or missing session config: {exc}", 1008)
        return

    details = await module_details()
    if details is None:
        await _refuse(ws, f"Could not reach HuRI's module list (GET /modules) for {huri_url}", 1011)
        return

    outbound: "asyncio.Queue[Outbound]" = asyncio.Queue()
    client = make_client(huri_url, modules, subscriptions(modules, details), user_id, outbound)
    try:
        await client.connect()
    except HuRISessionError as exc:
        message = f"HuRI refused the session: {exc.error}"
        if exc.server_modules:
            message += f" (server modules: {', '.join(exc.server_modules)})"
        logger.warning("HuRI refused the session: %s", exc.error)
        await _refuse(ws, message, 1011)
        return
    except Exception as exc:  # unreachable server, bad URL...
        logger.warning("Could not reach HuRI at %s: %s", huri_url, exc)
        await _refuse(ws, f"Could not reach HuRI at {huri_url}: {exc}", 1011)
        return

    tasks: List[asyncio.Task] = []
    try:
        await ws.send_json(
            {"type": "session_config", "config": describe_config(client.modules)}
        )
        huri_task = asyncio.create_task(client.wait_closed())
        tasks.append(huri_task)
        inbound_task = asyncio.create_task(_pump_inbound(ws, client))
        tasks.append(inbound_task)
        writer_task = asyncio.create_task(
            _pump_outbound(outbound, ws, transform_outbound)
        )
        tasks.append(writer_task)
        # Race the browser's input against HuRI closing the session under us,
        # so the browser hears about it instead of waiting on a dead session.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
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
        teardown = asyncio.create_task(_teardown(client, tasks))
        _TEARDOWNS.add(teardown)
        teardown.add_done_callback(_TEARDOWNS.discard)
        await asyncio.shield(teardown)
