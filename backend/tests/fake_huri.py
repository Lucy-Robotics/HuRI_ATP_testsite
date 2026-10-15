"""A fake HuRI server for the bridge tests: huri-core's ``GET /modules`` and
``/session`` websocket (handshake, JSON and binary frames), with no module
behind it. Each test scripts the replies with :attr:`FakeHuri.on_frame`."""

import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from huri_core.core.client import decode, encode

# What each curated module emits, for the subscription check.
OUTPUTS = {
    "vad": "huri.voice",
    "stt": "huri.transcript",
    "tag": "huri.partial_question",
    "emo": "huri.emotion",
    "eag": "huri.partial_question",
    "qag": "huri.question",
    "rag": "huri.token",
    "tts": "huri.audio.out",
    "tts_cosyvoice": "huri.audio.out",
    "mov": "huri.motion",
}

Reply = Callable[["FakeHuri", str, Any], List[Tuple[str, Any]]]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeHuri:
    def __init__(self, modules: Optional[List[str]] = None):
        self.modules = sorted(modules or list(OUTPUTS))
        self.port = _free_port()
        self.handshakes: List[Dict[str, Any]] = []
        self.frames: List[Tuple[str, Any]] = []
        # (topic, raw wire data) -> [(topic, EventData)] sent back.
        self.on_frame: Optional[Reply] = None
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self.app = self._build_app()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/session"

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/modules")
        async def modules() -> Dict[str, Any]:
            return {
                "modules": self.modules,
                "details": {
                    m: {"input": "x", "output": OUTPUTS.get(m), "actuation": False}
                    for m in self.modules
                },
                "huri_core": "0.4.0",
            }

        @app.websocket("/session")
        async def session(ws: WebSocket) -> None:
            await ws.accept()
            handshake = await ws.receive_json()
            self.handshakes.append(handshake)
            names = [m.get("name") for m in handshake["modules"].values()]
            unknown = [n for n in names if n not in self.modules]
            produced = {OUTPUTS[n] for n in names if n in OUTPUTS}
            unsubscribable = [t for t in handshake["subscribe"] if t not in produced]
            if unknown or unsubscribable:
                await ws.send_json(
                    {
                        "type": "session_error",
                        "error": f"unknown {unknown} / unsubscribable {unsubscribable}",
                        "server_modules": self.modules,
                    }
                )
                await ws.close(code=1008)
                return
            await ws.send_json(
                {"type": "session_init", "user_id": handshake["user_id"]}
            )
            try:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    raw = msg.get("bytes")
                    topic, data = decode(raw if raw is not None else msg["text"])
                    self.frames.append((topic, data))
                    replies = self.on_frame(self, topic, data) if self.on_frame else []
                    for out_topic, event in replies:
                        frame = encode(out_topic, event)
                        if isinstance(frame, bytes):
                            await ws.send_bytes(frame)
                        else:
                            await ws.send_text(frame)
            except WebSocketDisconnect:
                return

        return app

    def start(self) -> None:
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake HuRI did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        assert self._server is not None and self._thread is not None
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def wait_frames(self, count: int, timeout: float = 5.0) -> List[Tuple[str, Any]]:
        deadline = time.time() + timeout
        while len(self.frames) < count:
            if time.time() > deadline:
                raise AssertionError(f"expected {count} frames, got {self.frames}")
            time.sleep(0.02)
        return self.frames
