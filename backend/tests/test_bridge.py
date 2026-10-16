"""The browser <-> HuRI bridge: handshake, subscriptions, and whole sessions
through main.app against a fake HuRI server (fake_huri.py)."""

import asyncio
import base64
import json

import numpy as np
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from huri_events import Audio, Emotion, Motion, RAGQuestion, Token, Transcript

import huri_bridge
import main
from fake_huri import INPUTS, OUTPUTS
from huri_bridge import (
    InvalidHandshake,
    build_session_modules,
    describe_config,
    subscriptions,
)
from huri_presets import load_presets

@pytest.mark.parametrize(
    "handshake",
    [
        None,
        [],
        {},
        {"modules": {}},
        {"modules": {"rag": "rag"}},
        {"modules": {"rag": {"args": {}}}},
        {"modules": {"rag": {"name": "rag", "module": "a.b:C"}}},
        {"modules": {"rag": {"name": "rag", "args": []}}},
    ],
)
def test_invalid_handshakes_are_refused(handshake):
    with pytest.raises(InvalidHandshake):
        build_session_modules(handshake)


def test_handshake_to_session_modules():
    modules = build_session_modules(
        {
            "modules": {
                "mic": {"name": "vad", "args": {"vad_agressiveness": 2}},
                "tts": {"name": "tts_cosyvoice"},
                "servo": {"module": "my_pkg.servo:Servo"},
            }
        }
    )
    assert modules == {
        "mic": {"name": "vad", "args": {"vad_agressiveness": 2}},
        "tts": {"name": "tts_cosyvoice", "args": {}},
        "servo": {"module": "my_pkg.servo:Servo", "args": {}},
    }


DETAILS = {name: {"input": INPUTS[name], "output": out} for name, out in OUTPUTS.items()}


def test_subscriptions_follow_the_server_details():
    modules = {
        "a": {"name": "vad"},
        "b": {"name": "emo"},
        "c": {"name": "qag"},
        "d": {"name": "tts_cosyvoice"},
        "e": {"name": "mov"},
        "f": {"module": "my_pkg.servo:Servo"},
    }
    assert subscriptions(modules, DETAILS) == [
        "huri.audio.out",
        "huri.motion",
        "huri.question",
    ]
    assert subscriptions({"r": {"name": "rag"}}, DETAILS) == ["huri.token"]
    # A module the server does not describe contributes nothing.
    assert subscriptions({"r": {"name": "rag"}}, {}) == []


def test_session_config_echoes_the_modules():
    modules = {"m": {"name": "vad", "args": {}}, "g": {"name": "mov", "args": {}}}
    assert describe_config(modules) == {"modules": modules}


# --- sessions through main.app -----------------------------------------------------

RAG_ONLY = {"rag": {"name": "rag", "args": {"response_format": "short"}}}


def _open(client, modules):
    ws = client.websocket_connect("/ws")
    session = ws.__enter__()
    session.send_text(json.dumps({"modules": modules}))
    return ws, session


def test_typed_question_round_trip(huri_env):
    huri_env.on_frame = lambda srv, topic, data: [
        ("huri.token", Token(text="Hello", end=False)),
        ("huri.token", Token(text="", end=True)),
    ]
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": RAG_ONLY})
            reply = ws.receive_json()
            assert reply["type"] == "session_config"
            assert reply["config"]["modules"] == RAG_ONLY

            ws.send_json({"topic": "question", "text": "hi there"})
            assert ws.receive_json() == {"type": "token", "text": "Hello", "end": False}
            assert ws.receive_json() == {"type": "token", "text": "", "end": True}

    handshake = huri_env.handshakes[0]
    assert handshake["user_id"] == "anonymous"
    assert handshake["modules"] == RAG_ONLY
    assert handshake["subscribe"] == ["huri.token"]
    assert set(handshake["send"]) == {"huri.audio.in", "huri.question", "huri.token"}
    topic, data = huri_env.frames[0]
    assert topic == "huri.question"
    assert RAGQuestion.from_wire(data).transcript == Transcript("hi there", True)


def test_typed_tokens_go_straight_to_tts(huri_env):
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": {"tts": {"name": "tts", "args": {}}}})
            assert ws.receive_json()["type"] == "session_config"
            ws.send_json({"topic": "token", "text": "say this"})
            frames = huri_env.wait_frames(2)
    assert [(t, d) for t, d in frames] == [
        ("huri.token", {"text": "say this", "end": False}),
        ("huri.token", {"text": "", "end": True}),
    ]


def test_voice_session_with_speech_and_avatar(huri_env):
    huri_env.modules = sorted(set(huri_env.modules) - {"tts"})  # CosyVoice only
    samples = np.linspace(-1, 1, 8, dtype=np.float32)
    poses = np.zeros((2, 165), dtype=np.float32)
    poses[1, 0] = 0.5

    def reply(srv, topic, data):
        assert topic == "huri.audio.in"
        return [
            (
                "huri.question",
                RAGQuestion(
                    Transcript("what time is it", True),
                    Emotion("happy", 0.9, {"happy": 0.9, "sad": 0.1}, True),
                ),
            ),
            ("huri.audio.out", Audio(data=samples, sample_rate=22050, pts=0.5)),
            (
                "huri.motion",
                Motion(
                    poses=poses,
                    expressions=np.zeros((2, 100), dtype=np.float32),
                    trans=np.ones((2, 3), dtype=np.float32),
                    fps=30,
                    pts=0.5,
                ),
            ),
        ]

    huri_env.on_frame = reply
    browser_modules = {
        "mic": {"name": "vad", "args": {"block_duration": 0.03}},
        "stt": {"name": "stt", "args": {}},
        "tag": {"name": "tag", "args": {}},
        "emo": {"name": "emo", "args": {}},
        "eag": {"name": "eag", "args": {}},
        "qag": {"name": "qag", "args": {}},
        "rag": {"name": "rag", "args": {}},
        "tts": {"name": "tts_cosyvoice", "args": {}},
        "gesture": {"name": "mov", "args": {}},
    }
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": browser_modules})
            config = ws.receive_json()["config"]
            assert config["modules"] == browser_modules
            ws.send_bytes(b"\x01\x00\x02\x00")
            received = {}
            for _ in range(3):
                msg = ws.receive_json()
                received[msg["type"]] = msg

    assert huri_env.handshakes[0]["modules"] == browser_modules
    assert huri_env.handshakes[0]["subscribe"] == [
        "huri.audio.out",
        "huri.motion",
        "huri.question",
        "huri.token",
    ]
    assert huri_env.frames[0] == ("huri.audio.in", b"\x01\x00\x02\x00")

    question = received["question"]
    assert question["text"] == "what time is it"
    assert question["emotion"]["label"] == "happy"
    assert question["emotion"]["scores"] == {"happy": 0.9, "sad": 0.1}

    audio = received["audio"]
    assert audio["sample_rate"] == 22050 and audio["pts"] == 0.5
    decoded = np.frombuffer(base64.b64decode(audio["data"]), dtype="<f4")
    np.testing.assert_allclose(decoded, samples)

    segment = received["segment"]
    assert segment["pts"] == 0.5
    assert len(segment["frames"]) == 2
    assert segment["frames"][1]["t"] == pytest.approx(0.5 + 1 / 30)
    assert segment["frames"][0]["positions"] == {"pelvis": [1.0, 1.0, 1.0]}
    assert set(segment["frames"][0]["rotations"]) >= {"pelvis", "jaw", "right_thumb3"}


PRESETS = load_presets()


@pytest.mark.parametrize("key", sorted(PRESETS))
def test_every_client_preset_opens_a_session(huri_env, key):
    """Each presets/F*/*.json handshake passes the fake's copy of huri-core's
    session wiring check, so the bridge sends and subscribes what it needs."""
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": PRESETS[key]})
            reply = ws.receive_json()
    assert reply["type"] == "session_config", reply


def test_a_session_missing_its_input_is_refused(huri_env, monkeypatch):
    """Text-to-voice (F7) needs the bridge to send huri.token: without it TTS
    has no producer and the server refuses the session."""
    monkeypatch.setattr(huri_bridge, "SENDS", ["huri.audio.in", "huri.question"])
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": PRESETS["F7/text_to_voice"]})
            reply = ws.receive_json()
    assert reply["type"] == "error"
    assert "no producer for ['tts_cosyvoice']" in reply["message"]


def test_module_details_are_cached_and_survive_a_failed_fetch(monkeypatch):
    calls = []
    details = {"rag": {"input": "huri.question", "output": "huri.token"}}

    async def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("HuRI is slow")
        return {"modules": ["rag"], "details": details}

    monkeypatch.setattr(main, "_fetch_server_modules", fetch)
    monkeypatch.setattr(main, "_MODULE_DETAILS_CACHE", {})

    async def scenario():
        first = await main._module_details()
        cached = await main._module_details()  # within the TTL: no fetch
        monkeypatch.setattr(main, "_MODULE_DETAILS_TTL", 0.0)
        after_failure = await main._module_details()
        return first, cached, after_failure

    first, cached, after_failure = asyncio.run(scenario())
    assert first == cached == after_failure == details
    assert len(calls) == 2


def test_module_details_unknown_refuses_the_session(huri_env, monkeypatch):
    async def unknown():
        return None

    monkeypatch.setattr(main, "_module_details", unknown)
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": RAG_ONLY})
            reply = ws.receive_json()
    assert reply["type"] == "error"
    assert "GET /modules" in reply["message"]
    assert huri_env.handshakes == []


class _GoneBrowser:
    """A browser websocket that sends its handshake, then is gone: every
    send raises WebSocketDisconnect."""

    def __init__(self, handshake):
        self.handshake = handshake
        self.closed_with = None

    async def receive_json(self):
        return self.handshake

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def send_json(self, payload):
        raise WebSocketDisconnect()

    async def close(self, code=1000):
        self.closed_with = code


def test_huri_session_is_closed_when_the_browser_left_before_session_config(
    huri_env, monkeypatch
):
    """The session_config send fails: the HuRI session opened just before is
    still closed (teardown with no task created yet)."""
    closed = []
    real_close = huri_bridge.HuRIClient.close

    async def close(self):
        closed.append(self)
        await real_close(self)

    monkeypatch.setattr(huri_bridge.HuRIClient, "close", close)

    async def scenario():
        with pytest.raises(WebSocketDisconnect):
            await huri_bridge.run_browser_session(
                _GoneBrowser({"modules": RAG_ONLY}),
                user_id="u",
                huri_url=huri_env.ws_url,
                module_details=main._module_details,
            )

    asyncio.run(scenario())
    assert len(huri_env.handshakes) == 1
    assert len(closed) == 1


def test_refused_session_reaches_the_browser(huri_env):
    huri_env.modules = ["rag"]
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": {"gesture": {"name": "mov", "args": {}}}})
            reply = ws.receive_json()
    assert reply["type"] == "error"
    assert "HuRI refused the session" in reply["message"]
    assert "server modules: rag" in reply["message"]


def test_invalid_handshake_is_refused_before_huri(huri_env):
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": {}})
            reply = ws.receive_json()
    assert reply["type"] == "error"
    assert "invalid or missing session config" in reply["message"]
    assert huri_env.handshakes == []


def test_unreachable_huri(monkeypatch):
    monkeypatch.setenv("HURI_URL", "ws://127.0.0.1:9/session")
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": RAG_ONLY})
            reply = ws.receive_json()
    assert reply["type"] == "error"
    assert "Could not reach HuRI" in reply["message"]


# --- HTTP routes ------------------------------------------------------------------------


def test_huri_modules_is_the_server_answer(huri_env):
    huri_env.modules = ["mov", "rag", "tts_cosyvoice", "vad"]
    with TestClient(main.app) as client:
        body = client.get("/huri-modules").json()
    assert body["modules"] == ["mov", "rag", "tts_cosyvoice", "vad"]
    assert body["details"]["rag"]["output"] == "huri.token"
    assert body["huri_core"] == "0.4.0"


def test_huri_modules_unreachable(monkeypatch):
    monkeypatch.setenv("HURI_URL", "ws://127.0.0.1:9/session")
    with TestClient(main.app) as client:
        body = client.get("/huri-modules").json()
    assert body["modules"] is None


def test_presets_are_served_as_written():
    with TestClient(main.app) as client:
        presets = client.get("/presets").json()
    assert presets == load_presets()
    names = {m["name"] for m in presets["F4/voice_with_speech"].values()}
    assert {"vad", "mov", "tts_cosyvoice"} <= names
