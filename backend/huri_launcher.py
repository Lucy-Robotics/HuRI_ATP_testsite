"""Launches and supervises one HuRI (Ray Serve) process from this website's
backend, so a tester can pick a server config and bring an instance up/down
without a terminal (ATP F1, and a server for F2-F11 to point a client at).

The server comes from huri-modules (https://github.com/Lucy-Robotics/huri-modules),
vendored as the git submodule ``huri-modules/`` and installed by its own
``./install.sh``. The launcher relies on that installer's contract only:

* ``.huri-local/env.sh`` configures a shell like a Serve replica (venv, model
  paths, GPU environment, secrets). Every command the launcher runs goes
  through it: ``bash -c 'source env.sh && exec serve run <config>'``, from the
  checkout (the configs' relative ``assets/...`` paths resolve there).
* ``.huri-local/status.sh`` reports the state of every dependency (Qdrant,
  the LLM, Ray, the Serve app). When HuRI crashes or never becomes ready, its
  output goes into the log tail as the hint for what is missing.

Starting Qdrant/Ollama and sizing the Ray head are ``.huri-local/start.sh``'s
job, not this panel's: ``start.sh`` ends in ``serve deploy``, which detaches,
so it cannot be the supervised process. ``serve run`` joins a Ray head that is
already up, else starts one sized to the whole machine.

Single instance by design: HuRI binds fixed local ports. The process runs in
its own process group, stopped with SIGINT (``serve run``'s controlled
shutdown), then SIGKILL after a grace period. ``ray stop`` runs afterwards only
when no Ray head was up before Start, i.e. when ``serve run`` started the one
it ran on.

This spawns real local processes, so every route that touches it sits behind
auth (main.py's ``_require_auth``).
"""

import asyncio
import logging
import os
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_LOG_MAXLEN = 2000
_READY_POLL_INTERVAL = 1.0
_READY_TIMEOUT = 90.0
_STOP_GRACE_PERIOD = 20.0
_COMMAND_TIMEOUT = 30.0
_PROBE_TIMEOUT = 2.0
# Shorter than the frontend's 2s status poll, so every poll sees a fresh-enough
# answer while N open tabs still collapse into ~one request at HuRI.
_PROBE_CACHE_TTL = 1.5


def resolve_modules_path(environ: Mapping[str, str], repo_root: Path) -> Path:
    """The huri-modules checkout the server is run from: ``HURI_MODULES_PATH``
    when set, else the ``huri-modules/`` submodule of this repo when it is
    checked out, else a sibling ``../huri-modules`` checkout (the submodule
    path again when neither exists, so errors name the expected place)."""
    explicit = environ.get("HURI_MODULES_PATH")
    if explicit:
        return Path(explicit)
    submodule = repo_root / "huri-modules"
    sibling = repo_root.parent / "huri-modules"
    for candidate in (submodule, sibling):
        if (candidate / "config").is_dir():
            return candidate
    return submodule


async def _app_is_serving(client: httpx.AsyncClient, base_url: str) -> bool:
    """Is HuRI's Serve app deployed and serving at ``base_url``? huri-core's
    ``GET /modules`` only answers once the app itself is up, not when just
    Ray Serve's proxy is (it answers 404 while the app is still deploying)."""
    return (await client.get(f"{base_url}/modules")).is_success


class AlreadyRunningError(RuntimeError):
    pass


class NotRunningError(RuntimeError):
    pass


class NotInstalledError(RuntimeError):
    pass


class UnknownConfigError(ValueError):
    pass


class HuriLauncher:
    """Owns the lifecycle of one ``serve run`` subprocess.

    State is in-memory: fine for this site, which runs the backend as a
    single uvicorn process (see backend/Dockerfile's CMD).
    """

    def __init__(self, modules_path: Path, probe_url: str):
        self.modules_path = modules_path
        self.config_dir = modules_path / "config"
        self.env_script = modules_path / ".huri-local" / "env.sh"
        self.status_script = modules_path / ".huri-local" / "status.sh"
        # HuRI's HTTP ingress, where readiness is probed.
        self.probe_url = probe_url
        self.dashboard_url = os.environ.get(
            "HURI_DASHBOARD_URL", "http://127.0.0.1:8265/"
        )

        self._process: Optional[asyncio.subprocess.Process] = None
        self._config_name: Optional[str] = None
        self._status = "stopped"  # stopped | starting | running | stopping | crashed
        self._started_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._last_error: Optional[str] = None
        # Whether a Ray head was up before Start (Stop then leaves it alone).
        self._ray_was_up = True
        self._log_lines: Deque[Tuple[float, str]] = deque(maxlen=_LOG_MAXLEN)
        self._tasks: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        # (checked_at, is_up) for the "is *some* HuRI answering?" probe.
        self._probe_cache: Optional[Tuple[float, bool]] = None

    # --- installation -------------------------------------------------------

    def install_problem(self) -> Optional[str]:
        """Why this checkout cannot launch HuRI, or None when it can."""
        if not self.config_dir.is_dir():
            return (
                f"No huri-modules checkout at {self.modules_path}: run "
                "'git submodule update --init', or set HURI_MODULES_PATH."
            )
        if not self.env_script.is_file():
            return (
                f"huri-modules at {self.modules_path} is not installed (no "
                ".huri-local/env.sh): run its ./install.sh first."
            )
        return None

    def list_configs(self) -> List[str]:
        """huri-modules' launchable Serve configs: ``config/huri*.yaml``
        (``huri.yaml``, ``huri_cpu.yaml`` and the installer's
        ``huri_local.generated.yaml``). ``client_*.yaml`` are client configs."""
        return sorted(self._discover_configs())

    def _discover_configs(self) -> Dict[str, Path]:
        if not self.config_dir.is_dir():
            return {}
        return {path.name: path for path in self.config_dir.glob("huri*.yaml")}

    def command_for(self, *argv: str) -> List[str]:
        """``argv`` run in the installer's environment (``env.sh`` sourced),
        the command replacing the shell so its pid is the one supervised."""
        return ["bash", "-c", 'source "$0" && exec "$@"', str(self.env_script), *argv]

    async def _run(self, *argv: str) -> Tuple[Optional[int], str]:
        """Run a short command in the installer's environment: (exit code,
        output), exit code None when it did not finish in time."""
        proc = await asyncio.create_subprocess_exec(
            *self.command_for(*argv),
            cwd=str(self.modules_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), _COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None, ""
        return proc.returncode, out.decode(errors="replace")

    async def _ray_head_is_up(self) -> bool:
        """Is a Ray head already running? A ``ray status`` that times out
        counts as yes, so Stop never tears down a head it did not start."""
        code, _ = await self._run("ray", "status")
        return code is None or code == 0

    def _log(self, line: str) -> None:
        self._log_lines.append((time.time(), line))

    async def _dependency_hint(self) -> None:
        """Append ``.huri-local/status.sh``'s report (Qdrant, LLM, Ray, Serve
        app) to the log tail, after a crash or a start that never got ready."""
        if not self.status_script.is_file():
            return
        try:
            code, out = await self._run("bash", str(self.status_script))
        except OSError as exc:
            logger.warning("Could not run %s: %s", self.status_script, exc)
            return
        self._log("[launcher] .huri-local/status.sh:")
        for line in out.splitlines():
            self._log(f"[status.sh] {line}")
        if code is None:
            self._log("[status.sh] (timed out)")

    # --- status -------------------------------------------------------------

    async def _external_is_up(self, fresh: bool = False) -> bool:
        """Is *some* HuRI answering at ``probe_url``, whether or not this
        backend started it (a ``serve run`` in a terminal, or an instance that
        outlived a backend restart)? The frontend only opens its session once
        the status says running, so this keeps that tied to reality."""
        now = time.time()
        cached = self._probe_cache
        if not fresh and cached is not None and now - cached[0] < _PROBE_CACHE_TTL:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
                up = await _app_is_serving(client, self.probe_url)
        except Exception:
            up = False
        self._probe_cache = (time.time(), up)
        return up

    def _owns_process(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def status(self) -> Dict[str, Any]:
        owned = self._owns_process()
        pid = self._process.pid if owned else None
        status = self._status
        external = False
        # Our own bookkeeping distinguishes starting/stopping/crashed, which a
        # single HTTP probe cannot: probe only without a live process.
        if status not in ("starting", "running", "stopping") and await self._external_is_up():
            status = "running"
            external = True
        return {
            "status": status,
            # HuRI is up but not ours: the UI offers to use it, not to stop it.
            "external": external,
            "config": None if external else self._config_name,
            "pid": pid,
            "started_at": None if external else self._started_at,
            "uptime_seconds": (
                time.time() - self._started_at if self._started_at and pid else None
            ),
            "dashboard_url": self.dashboard_url,
            "exit_code": self._exit_code,
            "last_error": self._last_error,
        }

    def logs(self, tail: int = 200) -> List[str]:
        lines = list(self._log_lines)[-max(tail, 0):]
        return [f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {line}" for ts, line in lines]

    # --- lifecycle ----------------------------------------------------------

    async def start(self, config_name: str) -> None:
        async with self._lock:
            if self._status in ("starting", "running", "stopping"):
                raise AlreadyRunningError(
                    f"HuRI is already {self._status} (config={self._config_name})."
                )
            configs = self._discover_configs()
            if config_name not in configs:
                raise UnknownConfigError(
                    f"Unknown config {config_name!r}; must be one of {sorted(configs)}."
                )
            problem = self.install_problem()
            if problem:
                raise NotInstalledError(problem)
            # A second `serve run` next to one started by hand would collide on
            # HuRI's ports. Fresh probe: a cached answer could predate it.
            if await self._external_is_up(fresh=True):
                raise AlreadyRunningError(
                    f"A HuRI instance is already answering at {self.probe_url}, "
                    "started outside this backend: connect to it as is, or stop "
                    "it where it was started."
                )

            self._log_lines.clear()
            self._exit_code = None
            self._last_error = None
            self._ray_was_up = await self._ray_head_is_up()
            if self._ray_was_up:
                self._log("[launcher] a Ray head is already up: serve run joins it")
            self._config_name = config_name
            self._status = "starting"
            self._started_at = time.time()
            self._process = await asyncio.create_subprocess_exec(
                *self.command_for("serve", "run", str(configs[config_name])),
                cwd=str(self.modules_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own process group, so Stop reaches every child of it.
                start_new_session=True,
            )
            self._tasks = [
                asyncio.create_task(self._read_logs()),
                asyncio.create_task(self._reap()),
                asyncio.create_task(self._wait_ready()),
            ]

    async def stop(self) -> None:
        async with self._lock:
            if self._status not in ("starting", "running"):
                if await self._external_is_up():
                    raise NotRunningError(
                        f"HuRI is running at {self.probe_url}, but it wasn't started "
                        "by this backend: stop it where you started it."
                    )
                raise NotRunningError(f"HuRI is not running (status={self._status}).")
            self._status = "stopping"
            proc = self._process
        assert proc is not None
        await self._terminate(proc)
        # Let _reap record the exit (and the log reader drain) first.
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._ray_was_up:
            self._log("[launcher] leaving the Ray head running (it was up before Start)")
        else:
            code, _ = await self._run("ray", "stop")
            self._log(f"[launcher] ray stop (serve run started that head): exit {code}")
        # The instance just stopped may sit in a cached "up" answer.
        self._probe_cache = None

    async def shutdown(self) -> None:
        """Stop the process this launcher owns, if any (backend shutdown)."""
        if self._owns_process() and self._status in ("starting", "running"):
            try:
                await self.stop()
            except NotRunningError:
                pass

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """SIGINT the process group (Ctrl-C: serve run shuts down cleanly),
        SIGKILL it after the grace period."""
        self._signal_group(proc, signal.SIGINT)
        try:
            await asyncio.wait_for(proc.wait(), _STOP_GRACE_PERIOD)
        except asyncio.TimeoutError:
            logger.warning("HuRI did not exit within %.0fs of SIGINT; killing.", _STOP_GRACE_PERIOD)
            self._log(f"[launcher] still running {_STOP_GRACE_PERIOD:.0f}s after SIGINT: SIGKILL")
            self._signal_group(proc, signal.SIGKILL)
            await proc.wait()

    async def _read_logs(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            async for raw_line in self._process.stdout:
                self._log(raw_line.decode(errors="replace").rstrip("\n"))
        except Exception:
            logger.exception("HuRI log reader crashed")

    async def _wait_ready(self) -> None:
        deadline = time.time() + _READY_TIMEOUT
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            while time.time() < deadline:
                if self._status != "starting":
                    return  # crashed, or stop requested
                try:
                    if await _app_is_serving(client, self.probe_url):
                        self._status = "running"
                        return
                except Exception:
                    pass
                await asyncio.sleep(_READY_POLL_INTERVAL)
        if self._status == "starting":
            self._last_error = (
                f"HuRI did not answer at {self.probe_url} within {_READY_TIMEOUT:.0f}s "
                "(still starting, or a dependency is down): see the logs."
            )
            logger.warning(self._last_error)
            await self._dependency_hint()

    async def _reap(self) -> None:
        assert self._process is not None
        returncode = await self._process.wait()
        self._exit_code = returncode
        if self._status == "stopping":
            self._status = "stopped"
            return
        self._status = "crashed"
        self._last_error = f"HuRI exited unexpectedly (code {returncode}): see the logs."
        logger.warning("HuRI process exited unexpectedly (code %s)", returncode)
        await self._dependency_hint()
