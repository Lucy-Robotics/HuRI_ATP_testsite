# HuRI ATP test website

A browser-based testing console for [HuRI](https://github.com/Lucy-Robotics/HuRI-landing) — lets
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
./scripts/run_backend.sh    # FastAPI on :8001, auto-creates backend/.venv
./scripts/run_frontend.sh   # Vite dev server on :5173, auto-runs npm install
```

The backend auto-detects the huri-modules checkout (the submodule at `huri-modules/`, falling
back to a sibling `../huri-modules` checkout) and the HuRI Control Panel runs `serve run` from
it, with its installer environment (`huri-modules/.huri-local/env.sh`: venv, model paths, LLM
API key) and its venv's `serve`/`ray` binaries. Override with `HURI_MODULES_PATH` if yours lives
somewhere else.

By default `REQUIRE_AUTH=0` (`run_all.sh` sets this) — no Authelia needed, every visitor is
treated as one open `anonymous` session. See **Auth** below to turn on the real login flow.

## Configuring HuRI (the Control Panel)

HuRI itself is launched and stopped from inside the website, not from a terminal — that's
`backend/huri_launcher.py` spawning/supervising a `serve run <config>.yaml` subprocess (in the
huri-modules checkout) on the machine running the backend, and the **HuRI Control Panel** (top bar button) is its UI: pick a
config, watch it come up, tail its logs, jump to the Ray dashboard, and open a pre-configured
client tab.

The config dropdown lists two kinds of files:
- the bare `huri-modules/config/huri*.yaml` files (`huri.yaml` with CosyVoice, `huri_cpu.yaml`
  with Piper, and the installer's `huri_local.generated.yaml` — the one that matches your
  machine), and
- per-ATP-feature copies under this repo's own `presets/F*/huri*.yaml` — see `presets/README.md`
  for which folder copies which config and why.

Only one HuRI instance can run at a time (it binds fixed local ports), so Start is disabled while
one is already up.

## Testing a session

Once HuRI is running, the app's default connection uses a minimal `rag`-only handshake so it
comes up immediately. From there:

- **Event Configuration** (gear icon) — pick a named preset or a custom module combination for
  the session. Modules HuRI hasn't actually deployed (e.g. `gesture` on a machine with no GPU)
  are greyed out and can't be selected — checked live against `GET /huri-modules`, which
  proxies huri-core's own `GET /modules` (enabled modules, their topics, the huri-core version).
  The frontend keeps the HuRI monorepo's module names (`mic`, `gesture`, `tts`); the backend
  maps them to huri-modules' (`vad`, `mov`, and `tts` or `tts_cosyvoice`, whichever TTS engine
  the server runs).
- **Composer** — type text or use the mic; the event dropdown picks which topic a typed message
  targets (`rag.in`, i.e. `huri.question`, through the full pipeline, or `rag.out`, i.e.
  `huri.token`, straight at TTS/gesture, bypassing RAG — see the ATP's F7-F9 rows).
- **Chat panel** — each message can be expanded to show its linked emotion reading and the
  RAG-augmented prompt that was actually sent to the LLM.
- **Avatar** — only rendered when the active session includes the `gesture` module.

`presets/` holds the module-combination JSON files behind the Event Configuration dropdown,
organized to mirror the ATP's feature rows one-to-one (`F2/` through `F11/`) — see
`presets/README.md` for the full convention, including how to add a new preset (drop a `.json`
file in, no restart needed).

## Tests

The backend tests run against a fake HuRI server (no Ray, no models): whole browser sessions
through the bridge, the preset files, and the launcher's config resolution.

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
| Magic link (demo) | `MAGIC_LINK_SECRET` | A signed token redeemed at `/auth/magic` drops the same session an OIDC login would, without running Authelia. The minting tool `main.py` refers to (`tools/make_magic_qr.py`) doesn't exist yet — see **Known gaps**. |

`SESSION_SECRET` is required whenever `REQUIRE_AUTH=1` — the backend refuses to start without one
rather than fall back to a public default, since it's what makes the session cookie unforgeable.

## Key environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HURI_MODULES_PATH` | submodule at `huri-modules/`, else sibling `../huri-modules` | The huri-modules checkout the Control Panel runs `serve run` from, and where it looks for `config/huri*.yaml` |
| `HURI_URL` | `ws://localhost:8000/session` | The HuRI server the backend's client SDK connects to (its HTTP origin serves `GET /modules`) |
| `HURI_SERVE_BIN` / `HURI_RAY_BIN` | `<HURI_MODULES_PATH>/.venv/bin/{serve,ray}` when that venv exists, else plain `serve`/`ray` (PATH) | Binaries the Control Panel uses to launch/stop HuRI |
| `HURI_PRESETS_DIR` | `<repo root>/presets` | Where `/presets` and the Control Panel's config list are scanned from |
| `VITE_BACKEND_URL` | `http://localhost:8001` | Backend origin the frontend talks to (build-time env var) |
| `REQUIRE_AUTH` | `0` in `run_all.sh`, `1` otherwise | See **Auth** above |

## Known gaps

- `backend/Dockerfile` isn't wired up for a real deployment yet — it doesn't include the
  repo-root `presets/` folder in its build context (see the `TODO(deploy)` comment at its top).
  Local dev via the scripts above is unaffected.
- `backend/requirements.txt` installs huri-core and huri-events at their release tags (`0.4.0`,
  `0.1.0`); the `huri-modules` submodule pins a huri-modules commit of `dev` until the `0.1.0`
  tag exists, and must then be moved to it (`git -C huri-modules checkout 0.1.0`, commit the
  gitlink).
- The magic-link auth mode's redemption endpoint (`/auth/magic`) works, but the tool to mint a
  valid token (`tools/make_magic_qr.py`, referenced in `main.py`'s comments) hasn't been written
  yet — that mode isn't usable until it exists.
