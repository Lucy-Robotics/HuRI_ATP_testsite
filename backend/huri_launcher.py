"""Launches and supervises a HuRI (Ray Serve) process from this website's
backend, so a tester can pick a server config and bring an instance up/down
without a terminal — covering HuRI/ATP.xlsx F1 ("Configure HuRI") and giving
F2-F11 something real to point a client at.

The server comes from huri-modules (https://github.com/Lucy-Robotics/huri-modules),
vendored as the git submodule ``huri-modules/`` and installed by its own
``./install.sh`` (venv in ``huri-modules/.venv``, environment in
``huri-modules/.huri-local/env.sh``). :func:`resolve_modules_path` finds that
checkout, and :meth:`HuriLauncher.command_for` builds a *supervised*
``serve run <config>`` command: cwd'd into the checkout (the configs' relative
``assets/...`` paths resolve there), with ``.huri-local/env.sh`` sourced first
when the installer wrote one (the venv, the model paths and the LLM API key of
``secrets.env``).

That is less than huri-modules' generated ``.huri-local/start.sh`` does, and on
purpose: ``start.sh`` ends in ``serve deploy``, which detaches and returns, so
running it would lose the subprocess this launcher tails and stops. What the
panel does and does not do compared with ``start.sh``:

* Qdrant and Ollama: started only when huri-modules ships them as a separate
  ``.huri-local/services.sh``, which is run before ``serve run``. Without that
  file they are the operator's job; the launcher then probes Qdrant's
  ``/readyz`` and Ollama's ``/api/tags`` (the URLs of the config's
  ``RAGHandle`` user_config, else the installer's ``plan.env``, else the
  localhost defaults) and reports a dead one in the log tail and
  ``last_error`` — RAG otherwise "silently returns nothing".
* The GPU pin: ``HIP_VISIBLE_DEVICES`` is applied when ``env.sh`` exports it,
  or, for an older ``env.sh`` that does not, when ``start.sh`` does
  (:meth:`HuriLauncher.gpu_pin`).
* Ray sizing: not reproduced. ``serve run`` autostarts a local Ray head sized
  to the whole machine (every CPU and GPU), not to the installer's
  ``--num-gpus=$P_RAY_GPUS``. Start a head by hand (``ray start --head
  --num-gpus=N``) before **Start** to size it; ``serve run`` then joins it.

Single-instance by design: HuRI binds fixed local ports (Ray Serve's HTTP
ingress and the Ray dashboard), so running two at once would just collide.
``HuriLauncher`` therefore tracks exactly one subprocess, torn down with SIGINT
(the same as Ctrl-C, which `serve run` handles as a controlled shutdown)
followed by ``ray stop`` to reclaim the local Ray head it may have started.
The config can be a bare ``huri-modules/config/huri*.yaml`` or one of its
per-ATP-feature copies under the website's own ``presets/`` folder (see
``presets/README.md`` and :meth:`HuriLauncher._discover_configs`).

This spawns real local processes on the machine running the backend, so every
route that touches it must sit behind auth (see main.py's ``_require_auth``)
— treat it as a trusted-operator control panel, not a public API.
"""

import asyncio
import logging
import os
import re
import shlex
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

import httpx
import yaml

logger = logging.getLogger(__name__)

_LOG_MAXLEN = 2000
_READY_POLL_INTERVAL = 1.0
_READY_TIMEOUT = 90.0
_STOP_GRACE_PERIOD = 20.0
_PROBE_TIMEOUT = 2.0
# Shorter than the frontend's 2s status poll, so every poll sees a fresh-enough
# answer while N open tabs still collapse into ~one request at HuRI.
_PROBE_CACHE_TTL = 1.5
_SERVICE_PROBE_TIMEOUT = 2.0
# RAGHandle's own defaults (huri_rag.rag.RAGConfig), used when neither the
# config nor the installer's plan.env names the endpoints.
_DEFAULT_QDRANT_URL = "http://localhost:6333"
_DEFAULT_LLM_URL = "http://localhost:11434"
# Exports of a generated start.sh that the GPU pin consists of.
_GPU_PIN_VARS = ("HIP_VISIBLE_DEVICES",)
_EXPORT_RE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(\S*)\s*$")


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


def resolve_bin(
    environ: Mapping[str, str], variable: str, modules_path: Path, name: str
) -> str:
    """A HuRI binary (``serve``, ``ray``): the ``variable`` override, else the
    one in huri-modules' installer venv, else ``name`` on PATH."""
    explicit = environ.get(variable)
    if explicit:
        return explicit
    candidate = modules_path / ".venv" / "bin" / name
    if os.access(candidate, os.X_OK):
        return str(candidate)
    return name


def _read_env_file(path: Path) -> Dict[str, str]:
    """``KEY=value`` lines of a generated ``.env`` file (plan.env, huri.env);
    empty when it is missing or unreadable."""
    values: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parts = shlex.split(value)
        except ValueError:
            continue
        values[key.strip()] = parts[0] if parts else ""
    return values


def _read_exports(path: Path) -> Dict[str, str]:
    """``export NAME=value`` lines of a shell script (no expansion)."""
    exports: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return exports
    for line in text.splitlines():
        match = _EXPORT_RE.match(line)
        if match:
            exports[match.group(1)] = match.group(2).strip("\"'")
    return exports


def rag_endpoints(config_path: Path, plan: Mapping[str, str]) -> Optional[Dict[str, Any]]:
    """The external services a Serve config's RAG talks to:
    ``{"qdrant_url", "llm_url", "ollama"}``, or None when the config serves
    no RAG (no ``RAGHandle`` deployment) or cannot be read.

    URLs come from the ``RAGHandle`` user_config, else the installer's
    ``plan.env`` (``HURI_QDRANT_URL``, ``HURI_LLM_URL``), else RAGHandle's
    defaults. ``ollama`` says whether the LLM is an Ollama the operator has
    to run (provider ``ollama``, or no provider and Ollama's port).
    """
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    rag = None
    for app in config.get("applications") or []:
        for deployment in (app or {}).get("deployments") or []:
            if (deployment or {}).get("name") == "RAGHandle":
                rag = deployment
    if rag is None:
        return None
    user_config = rag.get("user_config") or {}
    qdrant_url = (
        user_config.get("qdrant_url") or plan.get("HURI_QDRANT_URL") or _DEFAULT_QDRANT_URL
    )
    llm_url = user_config.get("llm_url") or plan.get("HURI_LLM_URL") or _DEFAULT_LLM_URL
    provider = user_config.get("llm_provider") or plan.get("HURI_LLM_PROVIDER")
    ollama = provider == "ollama" if provider else ":11434" in str(llm_url)
    return {
        "qdrant_url": str(qdrant_url).rstrip("/"),
        "llm_url": str(llm_url).rstrip("/"),
        "ollama": ollama,
    }


async def _answers(client: httpx.AsyncClient, url: str) -> bool:
    try:
        return (await client.get(url)).is_success
    except Exception:
        return False


async def probe_services(endpoints: Mapping[str, Any]) -> List[str]:
    """Warnings for the services of :func:`rag_endpoints` that do not answer
    (Qdrant's ``/readyz``, Ollama's ``/api/tags``); empty when all do."""
    warnings: List[str] = []
    async with httpx.AsyncClient(timeout=_SERVICE_PROBE_TIMEOUT, verify=False) as client:
        qdrant = endpoints["qdrant_url"]
        if not await _answers(client, f"{qdrant}/readyz"):
            warnings.append(
                f"Qdrant is not answering at {qdrant}: RAG retrieval and memory "
                "will silently return nothing. This panel does not start it; run "
                "huri-modules/.huri-local/start.sh once, or start Qdrant yourself."
            )
        if endpoints["ollama"]:
            llm = endpoints["llm_url"]
            if not await _answers(client, f"{llm}/api/tags"):
                warnings.append(
                    f"Ollama is not answering at {llm}: RAG cannot generate "
                    "answers. This panel does not start it; run `ollama serve`."
                )
    return warnings


async def _app_is_serving(client: httpx.AsyncClient, base_url: str) -> bool:
    """Is HuRI's Serve app actually deployed and serving at ``base_url``?

    Prefers HuRI's own ``/modules`` route (huri-core >= 0.4), but a HuRI
    started from an older checkout may 404 there while its ``/session``
    websocket works fine, and treating that as "down" kept the frontend from
    ever opening its socket. Fall back to Ray Serve's built-in ``/-/routes``, which every version
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

    def __init__(
        self,
        modules_path: Path,
        probe_url: str,
        presets_dir: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        environ = os.environ if environ is None else environ
        # The huri-modules checkout (see resolve_modules_path): `serve run`
        # runs from there, and its config/huri*.yaml are launchable.
        self.modules_path = modules_path
        self.config_dir = modules_path / "config"
        # Written by huri-modules' ./install.sh: venv, model paths, API key.
        self.env_script = modules_path / ".huri-local" / "env.sh"
        # Qdrant/Ollama bring-up split out of start.sh by newer installers;
        # run before `serve run` when present (see the module docstring).
        self.services_script = modules_path / ".huri-local" / "services.sh"
        # Read for its GPU pin only: it ends in `serve deploy`, which detaches.
        self.start_script = modules_path / ".huri-local" / "start.sh"
        # The installer's plan: endpoint URLs when a config does not name them.
        self.plan_file = modules_path / ".huri-local" / "plan.env"
        # Per-ATP-feature copies of a huri*.yaml config (see presets/README.md)
        # so a tester can pick "F1/huri_cpu.yaml" straight off the sheet
        # instead of cross-referencing which bare config it happens to be.
        self.presets_dir = presets_dir
        # Where readiness is probed (HuRI's own HTTP ingress, not the Ray
        # dashboard) — any response at all means the app is up and serving.
        self.probe_url = probe_url
        self.dashboard_url = environ.get(
            "HURI_DASHBOARD_URL", "http://127.0.0.1:8265/"
        )
        self.serve_bin = resolve_bin(environ, "HURI_SERVE_BIN", modules_path, "serve")
        self.ray_bin = resolve_bin(environ, "HURI_RAY_BIN", modules_path, "ray")

        self._process: Optional[asyncio.subprocess.Process] = None
        self._config_name: Optional[str] = None
        self._status = "stopped"  # stopped | starting | running | stopping | crashed
        self._started_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._last_error: Optional[str] = None
        # Pre-launch service warnings (see probe_services), kept in last_error.
        self._preflight: Optional[str] = None
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
        dropdown/API shows: huri-modules' bare ``config/huri*.yaml`` files
        (``huri.yaml``, ``huri_cpu.yaml``, and the installer's
        ``huri_local.generated.yaml``) under the plain name, plus any per-ATP-feature copies under
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

    def command_for(self, config_path: Path) -> List[str]:
        """The argv launching HuRI with ``config_path``, run with cwd
        :attr:`modules_path`: ``serve run`` straight, or through bash after
        sourcing huri-modules' ``.huri-local/env.sh`` when the installer
        wrote one, and running its ``.huri-local/services.sh`` (Qdrant,
        Ollama) when it has one. ``serve run`` stays the process this
        launcher supervises (``exec``)."""
        if self.env_script.is_file():
            if self.services_script.is_file():
                return [
                    "bash",
                    "-c",
                    'source "$0" && bash "$3" && exec "$1" run "$2"',
                    str(self.env_script),
                    self.serve_bin,
                    str(config_path),
                    str(self.services_script),
                ]
            return [
                "bash",
                "-c",
                'source "$0" && exec "$1" run "$2"',
                str(self.env_script),
                self.serve_bin,
                str(config_path),
            ]
        return [self.serve_bin, "run", str(config_path)]

    def gpu_pin(self) -> Dict[str, str]:
        """The GPU pin a generated ``start.sh`` exports
        (``HIP_VISIBLE_DEVICES``) but ``env.sh`` does not — older installers
        wrote it into start.sh only — to set in the launched process's
        environment. Empty when there is none to add."""
        pinned = _read_exports(self.start_script)
        in_env = _read_exports(self.env_script)
        return {
            name: pinned[name]
            for name in _GPU_PIN_VARS
            if name in pinned and name not in in_env
        }

    async def preflight(self, config_path: Path) -> List[str]:
        """Warnings about the external services ``config_path``'s RAG needs,
        when no ``services.sh`` will start them (see :func:`probe_services`)."""
        if self.services_script.is_file():
            return []
        endpoints = rag_endpoints(config_path, _read_env_file(self.plan_file))
        if endpoints is None:
            return []
        return await probe_services(endpoints)

    def _set_error(self, message: str) -> None:
        """``last_error``, keeping any pre-launch service warning after it."""
        self._last_error = f"{message} {self._preflight}" if self._preflight else message

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
            self._preflight = None
            warnings = await self.preflight(config_path)
            for warning in warnings:
                self._log_lines.append((time.time(), f"[launcher] WARNING: {warning}"))
                logger.warning(warning)
            if warnings:
                self._preflight = " ".join(warnings)
                self._last_error = self._preflight
            env = None
            pin = self.gpu_pin()
            if pin:
                env = {**os.environ, **pin}
                self._log_lines.append(
                    (time.time(), f"[launcher] GPU pin from start.sh: {pin}")
                )
            self._config_name = config_name
            self._status = "starting"
            self._started_at = time.time()

            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command_for(config_path),
                    cwd=str(self.modules_path),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError as exc:
                self._status = "crashed"
                self._set_error(
                    f"Could not exec {self.serve_bin!r} in {self.modules_path}: "
                    f"{exc}. Install the server first (huri-modules/install.sh, "
                    "see README.md), or point HURI_MODULES_PATH / HURI_SERVE_BIN "
                    "at an existing install."
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
        # itself is serving (huri-core's "/modules").
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
            message = (
                f"HuRI did not answer at {self.probe_url} within {_READY_TIMEOUT:.0f}s "
                "(still starting, or misconfigured — check logs)."
            )
            self._set_error(message)
            logger.warning(message)

    async def _reap(self) -> None:
        assert self._process is not None
        returncode = await self._process.wait()
        self._exit_code = returncode
        if self._status == "stopping":
            self._status = "stopped"
        else:
            self._status = "crashed"
            if not self._last_error or self._last_error == self._preflight:
                self._set_error(f"HuRI exited unexpectedly (code {returncode}); see logs.")
            logger.warning("HuRI process exited unexpectedly (code %s)", returncode)
