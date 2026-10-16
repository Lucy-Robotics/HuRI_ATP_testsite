# HuRI ATP test website

A browser-based testing console for [HuRI](https://github.com/Lucy-Robotics/huri-core) — lets
a tester pick any module combination (voice in, RAG, TTS, gesture, emotion...), launch HuRI itself
with a chosen deployment config, and drive a live session from a chat-style UI instead of a
terminal. Built to exercise every row of the ATP, the test plan this whole site is designed
around.

It is a HuRI use case: the backend is a client of a HuRI server through huri-core's client SDK
(`huri-core[client]` and the curated events `huri-events`, installed from git by
`backend/requirements.txt`), and the server it launches comes from
[huri-modules](https://github.com/Lucy-Robotics/huri-modules), vendored as the git submodule
`huri-modules/`. Compatibility: huri-core 0.4.x / huri-modules 0.1.x.

## Prerequisites

Before anything else:

- **Python 3.12.** Both huri-modules' installer and this site's backend build their own venv
  from it. (The installer accepts 3.10–3.12 and this backend needs 3.11+; 3.12 is the one
  version that satisfies both, so that is what we require.)
- **Git LFS**, installed *before* cloning (`git lfs install`). The avatar's 3D model
  (`model.fbx`, in `backend/static/` and `frontend/public/`) is stored in LFS — without it you
  get a small text pointer file instead of the model and the avatar never renders. Already
  cloned without it? `git lfs install && git lfs pull`.
- **An NVIDIA GPU is needed for the GPU-heavy modules, CosyVoice TTS and gesture** — and even
  then only if enough VRAM is free (roughly 4.5 GiB for CosyVoice + 2.2 GiB for gesture, on top
  of anything else on the card). huri-modules' installer plans per module: without that
  headroom, or on AMD/CPU machines, TTS falls back to Piper on CPU and gesture (`mov`) is
  dropped. Presets that need a missing module are greyed out in the Event Configuration modal;
  that is expected, not a bug. `huri-modules/install.sh --plan-only` shows what your machine
  will run; see [FULL_INSTALLATION.md](FULL_INSTALLATION.md) for the numbers.

Then:

1. **The huri-modules submodule checked out.** The HuRI server is vendored at `huri-modules/`
   as a git submodule, not a plain folder — if it's empty, run:
   ```bash
   git submodule update --init
   ```

2. **HuRI itself, installed** — venv (huri-core, the curated modules, huri-cli), downloaded
   models, and a generated Ray Serve config. Run huri-modules' installer *inside the
   submodule*, before starting the site:
   ```bash
   cd huri-modules
   ./install.sh --yes
   ```
   This also sets up the two things HuRI needs at runtime:
   - **Qdrant** (RAG's vector memory) — started as a local Docker container (`huri-qdrant`) on
     `localhost:6333`, unless you pass `--skip-services` because one is already running (e.g.
     from another HuRI checkout on the same machine — Qdrant/Ollama are shared, machine-wide
     services, not per-checkout).
   - **Ollama** (LLM + embeddings) — installed and pulled with whatever models the capability
     plan picks (e.g. `mistral:7b`, `bge-m3`), reachable at `localhost:11434`.

   If you already have a working huri-modules checkout elsewhere with Qdrant/Ollama already
   running, you can skip re-provisioning those two: `./install.sh --yes --skip-services`, or
   point the site at that checkout with `HURI_MODULES_PATH`. See `./install.sh --help` for
   GPU/CPU profile flags.

3. **Node.js** (for the frontend). This site's own backend uses the Python 3.12 from above,
   in a venv separate from HuRI's (see below).

## Running it

The one-shot way:

```bash
./scripts/run_all.sh
```

This starts the backend (`:8001`) and frontend (`:5173`) and waits on both — Ctrl-C stops them
cleanly (including HuRI itself, if you started it from the Control Panel in the meantime). It
does **not** start HuRI automatically: open `http://localhost:5173`, use the **HuRI Control
Panel** button in the top bar, pick a config from the dropdown, and hit Start. That's deliberate —
see "Configuring HuRI" below for why launching HuRI is a UI action rather than baked into the
script.

To run the pieces separately (e.g. while iterating on just one of them):

```bash
./scripts/run_backend.sh    # FastAPI on :8001, auto-creates .venv at the repo root
./scripts/run_frontend.sh   # Vite dev server on :5173, auto-runs npm install
```

The backend finds the huri-modules checkout (`HURI_MODULES_PATH`, else the submodule at
`huri-modules/`, else a sibling `../huri-modules` checkout) and the HuRI Control Panel runs
`bash -c 'source .huri-local/env.sh && exec serve run <config>'` from it: the installer's
environment (venv, model paths, GPU environment, LLM API key) is the one contract it relies on.

By default `REQUIRE_AUTH=0` (`run_all.sh` sets this) — no Authelia needed, every visitor is
treated as one open `anonymous` session. See **Auth** below to turn on the real login flow.

## Configuring HuRI (the Control Panel)

HuRI itself is launched and stopped from inside the website, not from a terminal — that's
`backend/huri_launcher.py` spawning/supervising a `serve run <config>.yaml` subprocess (in the
huri-modules checkout) on the machine running the backend, and the **HuRI Control Panel** (top bar button) is its UI: pick a
config, watch it come up, tail its logs, jump to the Ray dashboard, and open a pre-configured
client tab.

The config dropdown lists huri-modules' `config/huri*.yaml`: `huri.yaml` (GPU, CosyVoice),
`huri_cpu.yaml` (CPU, Piper) and the installer's `huri_local.generated.yaml`, the one fitted to
your machine. `presets/README.md` says which one each ATP feature uses.

Only one HuRI instance can run at a time (it binds fixed local ports), so Start is disabled while
one is already up. Stop sends SIGINT to the `serve run` process group (SIGKILL after a grace
period), then runs `ray stop` only if `serve run` started the Ray head (none was up before
Start). When HuRI crashes or never gets ready, the output of `huri-modules/.huri-local/status.sh`
(Qdrant, LLM, Ray, Serve app) lands in the log tail. The backend also stops a HuRI it started
when it shuts down itself.

## Testing a session

Once HuRI is running, the app's default connection uses a minimal `rag`-only handshake so it
comes up immediately. From there:

- **Event Configuration** (gear icon) — pick a named preset or a custom module combination for
  the session. The module list is the server's own (`GET /huri-modules`, a proxy of huri-core's
  `GET /modules`), in its names (`vad`, `stt`, ..., `tts` or `tts_cosyvoice`, `mov`); presets
  needing a module it does not serve (e.g. `mov` on a machine with no GPU) are greyed out.
- **Composer** — type text or use the mic; the event dropdown picks which topic a typed message
  targets (`rag.in`, i.e. `huri.question`, through the full pipeline, or `rag.out`, i.e.
  `huri.token`, straight at TTS/gesture, bypassing RAG — see the ATP's F7-F9 rows).
- **Chat panel** — each message can be expanded to show its linked emotion reading and the
  RAG-augmented prompt that was actually sent to the LLM.
- **Avatar** — only rendered when the active session includes the `mov` module.

`presets/` holds the module-combination JSON files behind the Event Configuration dropdown,
organized to mirror the ATP's feature rows one-to-one (`F2/` through `F11/`) — see
`presets/README.md` for the full convention, including how to add a new preset (drop a `.json`
file in, no restart needed).

## Tests

The backend tests run against a fake HuRI server (no Ray, no models): whole browser sessions
through the bridge, the preset files, and the launcher (config discovery, and the process
lifecycle against fake `serve`/`ray` scripts).

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest tests
```

## Auth

Three modes, picked by what's set in the backend's environment (see `backend/main.py`):

| Mode | Set | Behaviour |
|---|---|---|
| Open (local dev) | `REQUIRE_AUTH=0` | No login screen; every session is `user_id=anonymous`. |
| Authelia (OIDC) | `REQUIRE_AUTH=1`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET` | Real login via `/auth/login`; the OIDC `sub` becomes the HuRI `user_id` (scopes RAG memory per user). |
| Magic link (demo) | `MAGIC_LINK_SECRET` | A signed token redeemed at `/auth/magic` drops the same session an OIDC login would, without running Authelia. Mint one with `backend/tools/make_magic_qr.py` (see FULL_INSTALLATION.md §6.7). |

`SESSION_SECRET` is required whenever `REQUIRE_AUTH=1` — the backend refuses to start without one
rather than fall back to a public default, since it's what makes the session cookie unforgeable.

## Key environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HURI_MODULES_PATH` | submodule at `huri-modules/`, else sibling `../huri-modules` | The huri-modules checkout the Control Panel runs `serve run` from, and where it looks for `config/huri*.yaml` |
| `HURI_URL` | `ws://localhost:8000/session` | The HuRI server the backend's client SDK connects to (its HTTP origin serves `GET /modules`) |
| `HURI_PRESETS_DIR` | `<repo root>/presets` | Where `/presets` is scanned from |
| `VITE_BACKEND_URL` | `http://localhost:8001` | Backend origin the frontend talks to (build-time env var) |
| `REQUIRE_AUTH` | `0` in `run_all.sh`, `1` otherwise | See **Auth** above |

## Known gaps

- `backend/Dockerfile` isn't wired up for a real deployment yet — it doesn't include the
  repo-root `presets/` folder in its build context (see the `TODO(deploy)` comment at its top).
  Local dev via the scripts above is unaffected.
- `backend/requirements.txt` pins huri-core (`huri-core[client]`, tag `0.4.0`) and huri-events
  (huri-modules tag `0.1.0`), and the `huri-modules` submodule is at the `0.1.0` tag. To try
  local checkouts of either instead, install them editable in `.venv` and start the scripts with
  `SKIP_PIP_INSTALL=1`, so that they keep the venv as is.
- The Control Panel runs `serve run` with huri-modules' `.huri-local/env.sh`, not the full
  `.huri-local/start.sh`: it does not start Qdrant/Ollama (run `start.sh` once, then `stop.sh`
  without `--all`, to leave them up), and the Ray head `serve run` autostarts is sized to the
  whole machine. See FULL_INSTALLATION.md §6.5.
