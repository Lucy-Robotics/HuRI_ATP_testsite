"""The presets/ tree: client presets name curated modules, and the copies
that the ATP sheet points at from several feature folders stay identical."""

import tomllib
from pathlib import Path

import pytest

from fake_huri import INPUTS, OUTPUTS
from huri_presets import load_presets

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESETS = REPO_ROOT / "presets"
MANIFESTS = sorted((REPO_ROOT / "huri-modules" / "packages").glob("*/*/**/huri_module.toml"))

# Each ATP feature folder holds the preset its sheet row points at, so a
# preset shared by several features exists once per folder. They must not
# drift apart.
SHARED_COPIES = [
    ("F2/full_preset.json", "F4/voice_with_speech.json"),
    ("F2/rag_only.json", "F9/rag_only.json"),
    ("F5/voice_to_question.json", "F6/voice_to_question.json"),
]


def test_client_presets_use_curated_module_names():
    presets = load_presets(PRESETS)
    # F1 and F3 are pure launch tests: no client preset.
    assert len(presets) == 10
    assert not any(k.startswith(("F1/", "F3/")) for k in presets)
    for key, preset in presets.items():
        for tag, entry in preset.items():
            assert entry["name"] in OUTPUTS, f"{key}: {tag} -> {entry['name']}"
            assert isinstance(entry.get("args", {}), dict)


def test_presets_hold_no_server_configs():
    """Server configs are huri-modules' config/huri*.yaml, never copies here."""
    assert not list(PRESETS.rglob("*.yaml"))


@pytest.mark.parametrize("first,second", SHARED_COPIES)
def test_shared_presets_are_identical(first, second):
    assert (PRESETS / first).read_bytes() == (PRESETS / second).read_bytes()


def test_invalid_files_are_skipped(tmp_path):
    (tmp_path / "ok.json").write_text('{"rag": {"name": "rag"}}')
    (tmp_path / "list.json").write_text("[]")
    (tmp_path / "broken.json").write_text("{")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.json").write_text("{}")
    assert load_presets(tmp_path) == {"ok": {"rag": {"name": "rag"}}, "sub/deep": {}}
    assert load_presets(tmp_path / "missing") == {}


@pytest.mark.skipif(not MANIFESTS, reason="huri-modules submodule not checked out")
def test_fake_server_wiring_matches_the_manifests():
    """fake_huri's INPUTS/OUTPUTS are the curated modules' manifest topics."""
    inputs, outputs = {}, {}
    for path in MANIFESTS:
        manifest = tomllib.loads(path.read_text())
        name = manifest.get("name") or path.parent.name
        inputs[name] = manifest["input"]["topic"]
        outputs[name] = (manifest.get("output") or {}).get("topic")
    assert inputs == INPUTS
    assert outputs == OUTPUTS
