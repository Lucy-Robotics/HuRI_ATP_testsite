"""Launches and supervises a HuRI (Ray Serve) process from this website's
backend, so a tester can pick a config from the HuRI submodule and bring an
instance up/down without a terminal — covering HuRI/ATP.xlsx F1 ("Configure
HuRI") and giving F2-F11 something real to point a client at.

Single-instance by design: HuRI binds fixed local ports (Ray Serve's HTTP
ingress and the Ray dashboard), so running two at once would just collide.
``HuriLauncher`` therefore tracks exactly one subprocess, started with
``serve run <config path>`` (cwd'd into the HuRI checkout, matching the usage
comment at the top of HuRI/config/huri.yaml) and torn down with SIGINT (the
same as Ctrl-C, which `serve run` handles as a controlled shutdown) followed
by ``ray stop`` to reclaim the local Ray head it may have started. The config
path can point at either a bare ``HuRI/config/huri*.yaml`` or one of its
per-ATP-feature copies under the website's own ``presets/`` folder (see
``presets/README.md`` and :meth:`HuriLauncher._discover_configs`).

This spawns real local processes on the machine running the backend, so every
route that touches it must sit behind auth (see main.py's ``_require_auth``)
— treat it as a trusted-operator control panel, not a public API.
"""

import asyncio
import logging
import os
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_LOG_MAXLEN = 2000
_READY_POLL_INTERVAL = 1.0
_READY_TIMEOUT = 90.0
_STOP_GRACE_PERIOD = 20.0
_PROBE_TIMEOUT = 2.0
# Shorter than the frontend's 2s status poll, so every poll sees a fresh-enough
# answer while N open tabs still collapse into ~one request at HuRI.
_PROBE_CACHE_TTL = 1.5


async def _app_is_serving(client: httpx.AsyncClient, base_url: str) -> bool:
    """Is HuRI's Serve app actually deployed and serving at ``base_url``?

    Prefers HuRI's own ``/modules`` route, but that only exists on HuRI
    branches that carry the web interface — a HuRI started from an older
    checkout 404s there while its ``/session`` websocket works fine, and
    treating that as "down" kept the frontend from ever opening its socket.
    Fall back to Ray Serve's built-in ``/-/routes``, which every version
    has: it lists ``{route_prefix: app}`` and stays empty until the app
    finishes deploying, so it keeps the "proxy up but app still loading"
    case (see :meth:`HuriLauncher._wait_ready`) reported as not ready.
    """
    resp = await client.get(f"{base_url}/modules")
    if resp.is_success:
        return True
    if resp.status_code != 404:
        return False
    routes = await client.get(f"{base_url}/-/routes")
    return routes.is_success and bool(routes.json())


class AlreadyRunningError(RuntimeError):
    pass


class NotRunningError(RuntimeError):
    pass


class UnknownConfigError(ValueError):
    pass


class HuriLauncher:
    """Owns the lifecycle of one ``serve run`` subprocess.

    Not safe to share across multiple backend worker processes (state is
    in-memory) — fine for this site, which runs the backend as a single
    uvicorn process (see backend/Dockerfile's CMD).
    """

    def __init__(self, repo_path: Path, probe_url: str, presets_dir: Optional[Path] = None):
        self.repo_path = repo_path
        self.config_dir = repo_path / "config"
        # Per-ATP-feature copies of a huri*.yaml config (see presets/README.md)
        # so a tester can pick "F1/huri_cpu.yaml" straight off the sheet
        # instead of cross-referencing which bare config it happens to be.
        self.presets_dir = presets_dir
        # Where readiness is probed (HuRI's own HTTP ingress, not the Ray
        # dashboard) — any response at all means the app is up and serving.
        self.probe_url = probe_url
        self.dashboard_url = os.environ.get(
            "HURI_DASHBOARD_URL", "http://127.0.0.1:8265/"
        )
        self.serve_bin = os.environ.get("HURI_SERVE_BIN", "serve")
        self.ray_bin = os.environ.get("HURI_RAY_BIN", "ray")

        self._process: Optional[asyncio.subprocess.Process] = None
        self._config_name: Optional[str] = None
        self._status = "stopped"  # stopped | starting | running | stopping | crashed
        self._started_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._last_error: Optional[str] = None
        self._log_lines: Deque[Tuple[float, str]] = deque(maxlen=_LOG_MAXLEN)
        self._log_task: Optional[asyncio.Task] = None
        self._reap_task: Optional[asyncio.Task] = None
        self._ready_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        # (checked_at, is_up) for the "is *some* HuRI answering?" probe — see
        # _external_is_up.
        self._probe_cache: Optional[Tuple[float, bool]] = None

    def _discover_configs(self) -> Dict[str, Path]:
        """Every launchable server-side HuRI config, keyed by what the
        dropdown/API shows: bare ``config/huri*.yaml`` files under the plain
        name (``"huri.yaml"``), plus any per-ATP-feature copies under
        ``presets/`` keyed by their path relative to it (``"F1/huri.yaml"``)
        — see presets/README.md. (``client_*.yaml`` files are the *other*
        kind of config, for a client connecting to an already-running HuRI,
        and aren't launchable here.)"""
        configs: Dict[str, Path] = {}
        if self.config_dir.is_dir():
            for path in self.config_dir.glob("huri*.yaml"):
                configs[path.name] = path
        if self.presets_dir is not None and self.presets_dir.is_dir():
            for path in self.presets_dir.rglob("huri*.yaml"):
                key = path.relative_to(self.presets_dir).as_posix()
                configs[key] = path
        return configs

    def list_configs(self) -> List[str]:
        """Server-side HuRI configs available to launch (see
        :meth:`_discover_configs`)."""
        return sorted(self._discover_configs())

    async def _external_is_up(self, fresh: bool = False) -> bool:
        """Is *some* HuRI answering at ``probe_url``, whether or not this
        backend started it?

        HuRI is just as often launched by hand (``serve run config/huri.yaml``
        in a terminal) as from the control panel, and a backend restart — which
        includes every ``uvicorn --reload`` — wipes the in-memory bookkeeping of
        an instance this process *did* start. Reporting "stopped" in either
        case is worse than cosmetic: the frontend gates its client websocket on
        that flag (``App.jsx`` passes ``enabled: status === "running"``), so it
        never opens the socket and the composer sits at "Connecting to HuRI…"
        against a perfectly healthy instance. Probing the same ``/modules``
        route :meth:`_wait_ready` uses ties the reported status to reality
        rather than to who spawned the process.
        """
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

    async def status(self) -> Dict[str, Any]:
        owned = self._process is not None and self._process.returncode is None
        pid = self._process.pid if owned else None
        status = self._status
        external = False
        # Only fall back to the probe when we have no live process of our own:
        # our own bookkeeping distinguishes starting/stopping/crashed, which a
        # single HTTP probe cannot.
        if status not in ("starting", "running", "stopping") and await self._external_is_up():
            status = "running"
            external = True
        return {
            "status": status,
            # True when HuRI is up but this backend doesn't own the process, so
            # the UI can offer to use it without offering a Stop that can't work.
            "external": external,
            # These describe *our* subprocess; they say nothing about an
            # instance someone else started.
            "config": None if external else self._config_name,
            "pid": pid,
            "started_at": None if external else self._started_at,
            "uptime_seconds": (time.time() - self._started_at) if self._started_at and pid else None,
            "dashboard_url": self.dashboard_url,
            "exit_code": self._exit_code,
            "last_error": self._last_error,
        }

    def logs(self, tail: int = 200) -> List[str]:
        lines = list(self._log_lines)[-max(tail, 0):]
        return [f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {line}" for ts, line in lines]

    async def start(self, config_name: str) -> None:
        async with self._lock:
            if self._status in ("starting", "running", "stopping"):
                raise AlreadyRunningError(
                    f"HuRI is already {self._status} (config={self._config_name})."
                )
            # HuRI binds fixed local ports, so a second `serve run` against an
            # instance someone started by hand would just collide. Probe fresh:
            # a cached answer from before the operator brought one up would let
            # exactly the launch this guards against through.
            if await self._external_is_up(fresh=True):
                raise AlreadyRunningError(
                    f"A HuRI instance is already answering at {self.probe_url}, "
                    "started outside this backend — connect to it as is, or stop "
                    "it where it was started."
                )
            configs = self._discover_configs()
            if config_name not in configs:
                raise UnknownConfigError(
                    f"Unknown config {config_name!r}; must be one of {sorted(configs)}."
                )
            config_path = configs[config_name]

            self._log_lines.clear()
            self._exit_code = None
            self._last_error = None
            self._config_name = config_name
            self._status = "starting"
            self._started_at = time.time()

            try:
                self._process = await asyncio.create_subprocess_exec(
                    self.serve_bin,
                    "run",
                    str(config_path),
                    cwd=str(self.repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError as exc:
                self._status = "crashed"
                self._last_error = (
                    f"Could not exec {self.serve_bin!r}: {exc}. Is HuRI's Python "
                    "environment (ray[serve]) active for this backend process? "
                    "Override with the HURI_SERVE_BIN env var."
                )
                raise RuntimeError(self._last_error) from exc

            self._log_task = asyncio.create_task(self._read_logs())
            self._reap_task = asyncio.create_task(self._reap())
            self._ready_task = asyncio.create_task(self._wait_ready())

    async def stop(self) -> None:
        async with self._lock:
            if self._status not in ("starting", "running"):
                if await self._external_is_up():
                    raise NotRunningError(
                        f"HuRI is running at {self.probe_url}, but it wasn't started "
                        "by this backend — stop it where you started it."
                    )
                raise NotRunningError(f"HuRI is not running (status={self._status}).")
            self._status = "stopping"
            proc = self._process

        assert proc is not None
        try:
            proc.send_signal(signal.SIGINT)  # same as Ctrl-C: serve run shuts down cleanly
        except ProcessLookupError:
            pass

        waited = 0.0
        while proc.returncode is None and waited < _STOP_GRACE_PERIOD:
            await asyncio.sleep(0.5)
            waited += 0.5

        if proc.returncode is None:
            logger.warning("HuRI did not exit within %.0fs of SIGINT; killing.", _STOP_GRACE_PERIOD)
            proc.kill()
            await proc.wait()

        # Best-effort cleanup of any local Ray head `serve run` autostarted —
        # harmless if none is running.
        try:
            cleanup = await asyncio.create_subprocess_exec(
                self.ray_bin, "stop",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.wait(), timeout=15)
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            logger.warning("`%s stop` cleanup skipped: %s", self.ray_bin, exc)

        # The instance we just tore down may still be inside a cached "up"
        # answer; drop it so the next status() re-probes instead of reporting
        # the corpse as an external instance.
        self._probe_cache = None

    async def _read_logs(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            async for raw_line in self._process.stdout:
                line = raw_line.decode(errors="replace").rstrip("\n")
                self._log_lines.append((time.time(), line))
        except Exception:
            logger.exception("HuRI log reader crashed")

    async def _wait_ready(self) -> None:
        # Probing self.probe_url (HuRI's bare HTTP origin) isn't enough: Ray
        # Serve's proxy binds and answers *before* the actual app finishes
        # deploying, responding "Path '/' not found... route table is not
        # populated yet" — a real HTTP response, so the old plain
        # `client.get(self.probe_url)` treated that as ready and declared
        # "running" while the app (still loading STT/RAG/etc. models) would
        # reject every session. Probe a route that only 200s once the app
        # itself is serving (see HuRI/src/core/huri.py's "/modules").
        deadline = time.time() + _READY_TIMEOUT
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.time() < deadline:
                if self._status != "starting":
                    return  # already resolved (crashed, or stop requested)
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
                "(still starting, or misconfigured — check logs)."
            )
            logger.warning(self._last_error)

    async def _reap(self) -> None:
        assert self._process is not None
        returncode = await self._process.wait()
        self._exit_code = returncode
        if self._status == "stopping":
            self._status = "stopped"
        else:
            self._status = "crashed"
            if not self._last_error:
                self._last_error = f"HuRI exited unexpectedly (code {returncode}); see logs."
            logger.warning("HuRI process exited unexpectedly (code %s)", returncode)
