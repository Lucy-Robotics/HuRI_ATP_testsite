"""The presets/ tree: client presets name curated modules, and every server
config is one huri-core can serve."""

from pathlib import Path

import pytest
import yaml

from fake_huri import OUTPUTS
from huri_presets import load_presets

PRESETS = Path(__file__).resolve().parents[2] / "presets"
SERVER_CONFIGS = sorted(PRESETS.rglob("huri*.yaml"))

# Deployment name of each module's Serve deployment (huri-modules' *Handle
# classes); modules without a shared actor have none.
HANDLE_BY_MODULE = {
    "stt": "STTHandle",
    "emo": "EMOHandle",
    "rag": "RAGHandle",
    "tts": "TTSHandle",
    "tts_cosyvoice": "CosyVoiceTTSHandle",
    "mov": "MOVHandle",
}


def test_every_feature_folder_is_there():
    folders = sorted(p.name for p in PRESETS.iterdir() if p.is_dir())
    assert folders == sorted(f"F{i}" for i in range(1, 12))
    for folder in folders:
        assert (PRESETS / folder / "huri.yaml").is_file()
    assert (PRESETS / "F1" / "huri_cpu.yaml").is_file()


def test_client_presets_use_curated_module_names():
    presets = load_presets(PRESETS)
    # F1 and F3 are pure launch tests: no client preset.
    assert len(presets) == 10
    assert not any(k.startswith(("F1/", "F3/")) for k in presets)
    for key, preset in presets.items():
        for tag, entry in preset.items():
            assert entry["name"] in OUTPUTS, f"{key}: {tag} -> {entry['name']}"
            assert isinstance(entry.get("args", {}), dict)


def test_presets_load_in_browser_names():
    presets = load_presets(PRESETS, browser_names=True)
    assert presets["F8/text_to_avatar"] == {
        "tts": {"name": "tts", "args": {}},
        "mov": {"name": "gesture", "args": {}},
    }
    assert presets["F5/voice_to_question"]["vad"]["name"] == "mic"


@pytest.mark.parametrize("path", SERVER_CONFIGS, ids=lambda p: str(p.relative_to(PRESETS)))
def test_server_configs_run_huri_core(path):
    config = yaml.safe_load(path.read_text())
    (app,) = config["applications"]
    assert app["import_path"] == "huri_core.app:app"
    env = app["runtime_env"]["env_vars"]
    enabled = [m.strip() for m in env["HURI_MODULES"].split(",")]
    assert set(enabled) <= set(OUTPUTS)
    deployments = {d["name"] for d in app["deployments"]}
    expected = {"HuRI"} | {HANDLE_BY_MODULE[m] for m in enabled if m in HANDLE_BY_MODULE}
    assert deployments == expected

