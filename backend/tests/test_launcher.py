"""HuriLauncher: where the huri-modules checkout and its launchable configs
are found, and the lifecycle of the ``serve run`` process it supervises
(started through the installer's env.sh, stopped as a process group, Ray
stopped only when this run started it)."""

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

import huri_launcher
from huri_launcher import (
    HuriLauncher,
    NotInstalledError,
    NotRunningError,
    UnknownConfigError,
    resolve_modules_path,
)

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


def _installed(tmp_path: Path, serve: str, ray_status_code: int = 1) -> Path:
    """A huri-modules checkout whose env.sh puts fake ``serve``/``ray`` on
    PATH. ``ray status`` exits ``ray_status_code``; ``ray stop`` is recorded
    in ``ray.log``."""
    modules = _checkout(tmp_path / "huri-modules")
    bin_dir = tmp_path / "bin"
    _executable(bin_dir / "serve", serve)
    _executable(
        bin_dir / "ray",
        f'echo "$*" >> "{tmp_path}/ray.log"\n'
        f'[ "$1" = status ] && exit {ray_status_code}\nexit 0\n',
    )
    local = modules / ".huri-local"
    local.mkdir()
    (local / "env.sh").write_text(
        f'export PATH="{bin_dir}:$PATH"\nexport HURI_MODULES=vad,stt\n'
    )
    return modules


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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


# --- configs and installation -------------------------------------------------------------


def test_configs_are_huri_modules_serve_configs(tmp_path):
    modules = _checkout(
        tmp_path / "huri-modules",
        ("huri.yaml", "huri_cpu.yaml", "huri_local.generated.yaml", "client_template.yaml"),
    )
    launcher = HuriLauncher(modules, UNREACHABLE)
    assert launcher.list_configs() == [
        "huri.yaml",
        "huri_cpu.yaml",
        "huri_local.generated.yaml",
    ]


def test_configs_without_a_checkout(tmp_path):
    launcher = HuriLauncher(tmp_path / "missing", UNREACHABLE)
    assert launcher.list_configs() == []
    assert "No huri-modules checkout" in launcher.install_problem()


def test_repo_submodule_configs():
    modules = resolve_modules_path({}, REPO_ROOT)
    if not (modules / "config").is_dir():
        pytest.skip("huri-modules submodule not checked out")
    assert {"huri.yaml", "huri_cpu.yaml"} <= set(HuriLauncher(modules, UNREACHABLE).list_configs())


def test_uninstalled_checkout_is_refused(tmp_path):
    launcher = HuriLauncher(_checkout(tmp_path / "huri-modules"), UNREACHABLE)
    assert "run its ./install.sh" in launcher.install_problem()
    with pytest.raises(NotInstalledError):
        asyncio.run(launcher.start("huri.yaml"))


def test_unknown_config_is_refused(tmp_path):
    launcher = HuriLauncher(_checkout(tmp_path / "huri-modules"), UNREACHABLE)
    with pytest.raises(UnknownConfigError):
        asyncio.run(launcher.start("../../etc/passwd"))


def test_command_sources_the_installer_env(tmp_path):
    modules = _checkout(tmp_path / "huri-modules")
    launcher = HuriLauncher(modules, UNREACHABLE)
    argv = launcher.command_for("serve", "run", "cfg.yaml")
    assert argv[:2] == ["bash", "-c"]
    assert argv[3:] == [str(modules / ".huri-local" / "env.sh"), "serve", "run", "cfg.yaml"]


# --- lifecycle ------------------------------------------------------------------------------


def test_start_runs_serve_from_the_checkout(tmp_path):
    """The process runs in the huri-modules checkout with env.sh sourced, and
    stop() tears it down."""
    modules = _installed(
        tmp_path, 'echo "cwd=$PWD args=$* modules=$HURI_MODULES"\nexec sleep 30\n'
    )

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri_cpu.yaml")
        await _wait_for(lambda: launcher.logs())
        status, logs = await launcher.status(), launcher.logs()
        await launcher.stop()
        return status, logs, await launcher.status()

    status, logs, after = asyncio.run(scenario())
    assert status["status"] == "starting" and status["config"] == "huri_cpu.yaml"
    config = modules / "config" / "huri_cpu.yaml"
    assert logs[-1].endswith(f"cwd={modules} args=run {config} modules=vad,stt")
    assert after["status"] == "stopped"


def test_stop_kills_the_whole_process_group(tmp_path, monkeypatch):
    """A serve run that ignores SIGINT is SIGKILLed after the grace period,
    with the children it started."""
    monkeypatch.setattr(huri_launcher, "_STOP_GRACE_PERIOD", 0.5)
    child_pid = tmp_path / "child.pid"
    modules = _installed(
        tmp_path,
        "trap '' INT\n"
        f'sleep 30 &\necho $! > "{child_pid}"\necho ready\nwait\n',
    )

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri.yaml")
        await _wait_for(lambda: any(line.endswith("ready") for line in launcher.logs()))
        pid = int(child_pid.read_text())
        assert _alive(pid)
        await launcher.stop()
        return pid, launcher.logs(), await launcher.status()

    pid, logs, status = asyncio.run(scenario())
    assert any("SIGKILL" in line for line in logs)
    assert status["status"] == "stopped"
    assert not _alive(pid)


def test_ray_head_started_by_serve_run_is_stopped(tmp_path):
    modules = _installed(tmp_path, "exec sleep 30\n", ray_status_code=1)

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri.yaml")
        await launcher.stop()
        return launcher.logs()

    logs = asyncio.run(scenario())
    assert (tmp_path / "ray.log").read_text().splitlines() == ["status", "stop"]
    assert any("ray stop" in line for line in logs)


def test_ray_head_up_before_start_is_left_running(tmp_path):
    modules = _installed(tmp_path, "exec sleep 30\n", ray_status_code=0)

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri.yaml")
        await launcher.stop()
        return launcher.logs()

    logs = asyncio.run(scenario())
    assert (tmp_path / "ray.log").read_text().splitlines() == ["status"]
    assert any("leaving the Ray head running" in line for line in logs)


def test_stop_when_nothing_runs(tmp_path):
    launcher = HuriLauncher(_checkout(tmp_path / "huri-modules"), UNREACHABLE)
    with pytest.raises(NotRunningError):
        asyncio.run(launcher.stop())
    asyncio.run(launcher.shutdown())  # backend shutdown: nothing to stop


def test_shutdown_stops_the_owned_process(tmp_path):
    modules = _installed(tmp_path, "exec sleep 30\n")

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri.yaml")
        proc = launcher._process
        await launcher.shutdown()
        return proc.returncode, await launcher.status()

    returncode, status = asyncio.run(scenario())
    assert returncode is not None
    assert status["status"] == "stopped"


def test_crash_appends_the_status_report(tmp_path):
    modules = _installed(tmp_path, 'echo "boom"\nexit 3\n')
    (modules / ".huri-local" / "status.sh").write_text('echo "qdrant: down"\n')

    async def scenario():
        launcher = HuriLauncher(modules, UNREACHABLE)
        await launcher.start("huri.yaml")
        await _wait_for(lambda: any("[status.sh]" in line for line in launcher.logs()))
        return await launcher.status(), launcher.logs()

    status, logs = asyncio.run(scenario())
    assert status["status"] == "crashed" and status["exit_code"] == 3
    assert any(line.endswith("[status.sh] qdrant: down") for line in logs)
