"""HuriLauncher: where the huri-modules checkout, its binaries and the
launchable configs are found, and the command it runs."""

import asyncio
import stat
from pathlib import Path

import pytest

from huri_launcher import HuriLauncher, resolve_bin, resolve_modules_path

REPO_ROOT = Path(__file__).resolve().parents[2]
UNREACHABLE = "http://127.0.0.1:9"


def _checkout(path: Path, configs=("huri.yaml", "huri_cpu.yaml")) -> Path:
    (path / "config").mkdir(parents=True)
    for name in configs:
        (path / "config" / name).write_text("applications: []\n")
    return path


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# --- resolve_modules_path ------------------------------------------------------------


def test_explicit_path_wins(tmp_path):
    _checkout(tmp_path / "site" / "huri-modules")
    env = {"HURI_MODULES_PATH": "/opt/huri-modules"}
    assert resolve_modules_path(env, tmp_path / "site") == Path("/opt/huri-modules")


def test_submodule_then_sibling(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    _checkout(tmp_path / "huri-modules")
    assert resolve_modules_path({}, site) == tmp_path / "huri-modules"
    _checkout(site / "huri-modules")
    assert resolve_modules_path({}, site) == site / "huri-modules"


def test_uninitialized_submodule_falls_back_to_its_path(tmp_path):
    site = tmp_path / "site"
    (site / "huri-modules").mkdir(parents=True)  # empty: `git submodule update` not run
    assert resolve_modules_path({}, site) == site / "huri-modules"


# --- binaries ----------------------------------------------------------------------------


def test_bin_override_then_installer_venv_then_path(tmp_path):
    modules = _checkout(tmp_path / "huri-modules")
    assert resolve_bin({}, "HURI_SERVE_BIN", modules, "serve") == "serve"
    serve = _executable(modules / ".venv" / "bin" / "serve", "exit 0\n")
    assert resolve_bin({}, "HURI_SERVE_BIN", modules, "serve") == str(serve)
    env = {"HURI_SERVE_BIN": "/usr/local/bin/serve"}
    assert resolve_bin(env, "HURI_SERVE_BIN", modules, "serve") == "/usr/local/bin/serve"


# --- configs ------------------------------------------------------------------------------


def test_configs_from_huri_modules_and_presets(tmp_path):
    modules = _checkout(
        tmp_path / "huri-modules",
        ("huri.yaml", "huri_cpu.yaml", "huri_local.generated.yaml", "client_template.yaml"),
    )
    presets = tmp_path / "presets"
    for name in ("F1/huri.yaml", "F1/huri_cpu.yaml", "F9/huri.yaml", "F9/rag_only.json"):
        (presets / name).parent.mkdir(parents=True, exist_ok=True)
        (presets / name).write_text("{}\n")
    launcher = HuriLauncher(modules, UNREACHABLE, presets_dir=presets, environ={})
    assert launcher.list_configs() == [
        "F1/huri.yaml",
        "F1/huri_cpu.yaml",
        "F9/huri.yaml",
        "huri.yaml",
        "huri_cpu.yaml",
        "huri_local.generated.yaml",
    ]
    configs = launcher._discover_configs()
    assert configs["huri.yaml"] == modules / "config" / "huri.yaml"
    assert configs["F9/huri.yaml"] == presets / "F9" / "huri.yaml"


def test_configs_without_a_checkout(tmp_path):
    launcher = HuriLauncher(
        tmp_path / "missing", UNREACHABLE, presets_dir=REPO_ROOT / "presets", environ={}
    )
    configs = launcher.list_configs()
    assert "F1/huri_cpu.yaml" in configs
    assert all(c.startswith("F") for c in configs)


def test_repo_presets_are_all_launchable():
    launcher = HuriLauncher(
        resolve_modules_path({}, REPO_ROOT),
        UNREACHABLE,
        presets_dir=REPO_ROOT / "presets",
        environ={},
    )
    configs = launcher.list_configs()
    assert {f"F{i}/huri.yaml" for i in range(1, 12)} <= set(configs)
    if (REPO_ROOT / "huri-modules" / "config").is_dir():
        assert {"huri.yaml", "huri_cpu.yaml"} <= set(configs)


# --- command --------------------------------------------------------------------------------


def test_command_without_installer_env(tmp_path):
    modules = _checkout(tmp_path / "huri-modules")
    launcher = HuriLauncher(modules, UNREACHABLE, environ={"HURI_SERVE_BIN": "srv"})
    config = modules / "config" / "huri.yaml"
    assert launcher.command_for(config) == ["srv", "run", str(config)]


def test_command_sources_the_installer_env(tmp_path):
    modules = _checkout(tmp_path / "huri-modules")
    env_sh = modules / ".huri-local" / "env.sh"
    env_sh.parent.mkdir()
    env_sh.write_text("export HURI_MODULES=rag\n")
    serve = _executable(modules / ".venv" / "bin" / "serve", "exit 0\n")
    launcher = HuriLauncher(modules, UNREACHABLE, environ={})
    config = tmp_path / "presets" / "F9" / "huri.yaml"
    argv = launcher.command_for(config)
    assert argv[:2] == ["bash", "-c"]
    assert argv[3:] == [str(env_sh), str(serve), str(config)]


def test_start_runs_serve_from_the_checkout(tmp_path):
    """The real start(): the process runs in the huri-modules checkout with
    the installer environment, and stop() tears it down."""
    modules = _checkout(tmp_path / "huri-modules")
    env_sh = modules / ".huri-local" / "env.sh"
    env_sh.parent.mkdir()
    env_sh.write_text("export HURI_MODULES=vad,stt\n")
    _executable(
        modules / ".venv" / "bin" / "serve",
        'echo "cwd=$PWD args=$* modules=$HURI_MODULES"\nexec sleep 30\n',
    )
    _executable(modules / ".venv" / "bin" / "ray", "exit 0\n")

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE, environ={})
        await launcher.start("huri_cpu.yaml")
        for _ in range(100):
            if launcher.logs():
                break
            await asyncio.sleep(0.05)
        status = await launcher.status()
        logs = launcher.logs()
        await launcher.stop()
        return status, logs, await launcher.status()

    status, logs, after = asyncio.run(scenario())
    assert status["status"] == "starting" and status["config"] == "huri_cpu.yaml"
    config = modules / "config" / "huri_cpu.yaml"
    assert logs[0].endswith(f"cwd={modules} args=run {config} modules=vad,stt")
    assert after["status"] == "stopped"


def test_unknown_config_is_refused(tmp_path):
    from huri_launcher import UnknownConfigError

    modules = _checkout(tmp_path / "huri-modules")
    launcher = HuriLauncher(modules, UNREACHABLE, environ={})
    with pytest.raises(UnknownConfigError):
        asyncio.run(launcher.start("../../etc/passwd"))


def test_default_environ_is_the_process_one(monkeypatch, tmp_path):
    monkeypatch.setenv("HURI_SERVE_BIN", "from-env")
    launcher = HuriLauncher(_checkout(tmp_path / "m"), UNREACHABLE)
    assert launcher.serve_bin == "from-env"
