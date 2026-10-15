"""The browser <-> HuRI bridge: name mapping, handshake, and whole sessions
through main.app against a fake HuRI server (fake_huri.py)."""

import asyncio
import base64
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from huri_events import Audio, Emotion, Motion, RAGQuestion, Token, Transcript

import huri_bridge
import main
from fake_huri import OUTPUTS
from huri_bridge import (
    InvalidHandshake,
    browser_module_names,
    build_session_modules,
    describe_config,
    subscriptions,
    to_browser_name,
    to_server_name,
)
from huri_presets import load_presets

# --- names ----------------------------------------------------------------------


def test_legacy_names_map_to_curated_modules():
    assert to_server_name("mic") == "vad"
    assert to_server_name("gesture") == "mov"
    for name in ("stt", "tag", "emo", "eag", "qag", "rag", "vad", "mov"):
        assert to_server_name(name) == name


def test_tts_resolves_to_the_engine_the_server_runs():
    assert to_server_name("tts") == "tts"
    assert to_server_name("tts", ["tts", "tts_cosyvoice"]) == "tts"
    assert to_server_name("tts", ["rag", "tts_cosyvoice"]) == "tts_cosyvoice"
    # Unknown to the server either way: its session_error says so.
    assert to_server_name("tts", ["rag"]) == "tts"
    assert to_server_name("tts_cosyvoice", ["tts"]) == "tts_cosyvoice"


def test_server_modules_in_browser_names():
    assert to_browser_name("vad") == "mic"
    assert browser_module_names(
        ["eag", "emo", "mov", "qag", "rag", "stt", "tag", "tts", "tts_cosyvoice", "vad"]
    ) == ["eag", "emo", "gesture", "mic", "qag", "rag", "stt", "tag", "tts"]


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
                "mic": {"name": "mic", "args": {"vad_agressiveness": 2}},
                "tts": {"name": "tts"},
                "servo": {"module": "my_pkg.servo:Servo"},
            }
        },
        ["vad", "tts_cosyvoice"],
    )
    assert modules == {
        "mic": {"name": "vad", "args": {"vad_agressiveness": 2}},
        "tts": {"name": "tts_cosyvoice", "args": {}},
        "servo": {"module": "my_pkg.servo:Servo", "args": {}},
    }


def test_subscriptions_follow_the_producers():
    modules = {
        "a": {"name": "vad"},
        "b": {"name": "emo"},
        "c": {"name": "qag"},
        "d": {"name": "tts_cosyvoice"},
        "e": {"name": "mov"},
    }
    assert subscriptions(modules) == ["huri.audio.out", "huri.motion", "huri.question"]
    assert subscriptions({"r": {"name": "rag"}}) == ["huri.token"]


def test_session_config_speaks_the_frontend_names():
    config = describe_config(
        {"m": {"name": "vad", "args": {}}, "g": {"name": "mov", "args": {}}}
    )
    assert config["modules"] == {
        "m": {"name": "mic", "args": {}},
        "g": {"name": "gesture", "args": {}},
    }
    assert config["huri_modules"]["g"]["name"] == "mov"
    assert config["hooks"] == {
        "motion": {"name": "web_motion", "topics": ["huri.motion"], "args": {}}
    }
    assert set(config["senders"]) == {"audio", "text", "token"}


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
        "mic": {"name": "mic", "args": {"block_duration": 0.03}},
        "stt": {"name": "stt", "args": {}},
        "tag": {"name": "tag", "args": {}},
        "emo": {"name": "emo", "args": {}},
        "eag": {"name": "eag", "args": {}},
        "qag": {"name": "qag", "args": {}},
        "rag": {"name": "rag", "args": {}},
        "tts": {"name": "tts", "args": {}},
        "gesture": {"name": "gesture", "args": {}},
    }
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": browser_modules})
            config = ws.receive_json()["config"]
            # The frontend keys its mic button / avatar on these names.
            assert config["modules"] == browser_modules
            ws.send_bytes(b"\x01\x00\x02\x00")
            received = {}
            for _ in range(3):
                msg = ws.receive_json()
                received[msg["type"]] = msg

    sent = huri_env.handshakes[0]["modules"]
    assert sent["mic"]["name"] == "vad"
    assert sent["gesture"]["name"] == "mov"
    assert sent["tts"]["name"] == "tts_cosyvoice"
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


PRESETS = load_presets(main.PRESETS_DIR)
COSYVOICE_ONLY = sorted(set(OUTPUTS) - {"tts"})


@pytest.mark.parametrize("server", [sorted(OUTPUTS), COSYVOICE_ONLY], ids=["all", "cosyvoice"])
@pytest.mark.parametrize("key", sorted(PRESETS))
def test_every_client_preset_opens_a_session(huri_env, key, server):
    """Each presets/F*/*.json handshake passes the fake's copy of huri-core's
    session wiring check, so the bridge sends and subscribes what it needs."""
    huri_env.modules = server
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
    assert "no producer for ['tts']" in reply["message"]


def test_tts_is_remapped_when_the_module_list_is_unknown(huri_env, monkeypatch):
    """GET /modules failed: the browser's "tts" reaches a CosyVoice-only
    server unmapped, is refused, and the bridge retries with the server's
    module list."""
    huri_env.modules = COSYVOICE_ONLY

    async def unknown():
        return None

    monkeypatch.setattr(main, "_server_module_names", unknown)
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": {"tts": {"name": "tts", "args": {}}}})
            reply = ws.receive_json()
    assert reply["type"] == "session_config"
    assert reply["config"]["huri_modules"]["tts"]["name"] == "tts_cosyvoice"
    assert [h["modules"]["tts"]["name"] for h in huri_env.handshakes] == [
        "tts",
        "tts_cosyvoice",
    ]


def test_module_list_is_cached_and_survives_a_failed_fetch(huri_env, monkeypatch):
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("HuRI is slow")
        return {"modules": ["rag", "tts_cosyvoice"]}

    monkeypatch.setattr(main, "_fetch_server_modules", fetch)
    monkeypatch.setattr(main, "_MODULE_NAMES_CACHE", {})

    async def scenario():
        first = await main._server_module_names()
        cached = await main._server_module_names()  # within the TTL: no fetch
        monkeypatch.setattr(main, "_MODULE_NAMES_TTL", 0.0)
        after_failure = await main._server_module_names()
        return first, cached, after_failure

    first, cached, after_failure = asyncio.run(scenario())
    assert first == cached == after_failure == ["rag", "tts_cosyvoice"]
    assert len(calls) == 2


def test_refused_session_reaches_the_browser(huri_env):
    huri_env.modules = ["rag"]
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"modules": {"gesture": {"name": "gesture", "args": {}}}})
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


def test_huri_modules_in_frontend_names(huri_env):
    huri_env.modules = ["mov", "rag", "tts_cosyvoice", "vad"]
    with TestClient(main.app) as client:
        body = client.get("/huri-modules").json()
    assert body["modules"] == ["gesture", "mic", "rag", "tts"]
    assert body["server_modules"] == ["mov", "rag", "tts_cosyvoice", "vad"]
    assert body["details"]["rag"]["output"] == "huri.token"
    assert body["huri_core"] == "0.4.0"


def test_huri_modules_unreachable(monkeypatch):
    monkeypatch.setenv("HURI_URL", "ws://127.0.0.1:9/session")
    with TestClient(main.app) as client:
        body = client.get("/huri-modules").json()
    assert body["modules"] is None


def test_presets_are_served_in_frontend_names():
    with TestClient(main.app) as client:
        presets = client.get("/presets").json()
    full = presets["F4/voice_with_speech"]
    names = {m["name"] for m in full.values()}
    assert {"mic", "gesture", "tts"} <= names
    assert not names & {"vad", "mov"}
