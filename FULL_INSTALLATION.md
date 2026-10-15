# Installation guide — HuRI + the ATP test site

This is the **full** install: from a bare Linux machine to a browser session that drives the
complete HuRI pipeline (voice in → transcription → emotion → RAG/LLM → speech + gesture out)
from the ATP test site. It covers, in order:

1. [What gets installed](#1-what-gets-installed) and how the pieces talk to each other
2. [Requirements](#2-requirements)
3. [Getting the code](#3-get-the-code)
4. [Installing HuRI](#4-install-huri) with huri-modules' `install.sh`, flag by flag
5. [The three external services](#5-the-external-services-in-depth) HuRI depends on — the
   **LLM**, the **embedding model** and **Qdrant** — what they are and how to provide each one
   (local Ollama, a self-hosted OpenAI-compatible server, or a hosted API with a key)
6. [Installing and running the ATP test site](#6-install-the-atp-test-site) on top
7. [Day-to-day operation](#7-operating-it)
8. [Troubleshooting](#8-troubleshooting)

If you only want the commands, start with the [Quick path](#quick-path). The rest of the
document explains *why* those commands look the way they do, so you can adapt them.

---

## Quick path

The reference layout used by this project: HuRI runs on a workstation with one NVIDIA GPU, and
the LLM, the embedding model and Qdrant run on other machines on the LAN behind self-signed
HTTPS. The GPU is then free for TTS and gesture generation.

```bash
# 1. Code (huri-modules, the HuRI server, is a git submodule; the avatar model is in Git LFS)
git lfs install
git clone --recurse-submodules https://github.com/Lucy-Robotics/HuRI_ATP_testsite.git
cd HuRI_ATP_testsite

# 2. HuRI — installed *inside* the huri-modules submodule
cd huri-modules
./install.sh \
  --llm-url https://llm.huri.lan --llm-model Qwen3.5-4B-GGUF \
  --embed-url https://embedding.huri.lan --embed-model bge-large-en-v1.5-gguf-Q4_K_M \
  --no-verify-ssl \
  --qdrant-url https://qdrant.pommier.lan \
  --voice-sample ../assets/voice.wav      # default voice shipped in this repo, no transcript flag needed
cd ..

# 3. ATP site (backend venv + frontend node_modules are created on first run)
./scripts/run_all.sh
```

Then open <http://localhost:5173>, click **HuRI Control Panel**, pick
`huri_local.generated.yaml` and hit **Start**. Read [§6.5](#65-launch-huri-from-the-control-panel--which-config-to-pick)
before picking any other config.

If you have no remote LLM/embedding/Qdrant hosts, replace step 2 with the all-local variant in
[§4.4 A](#a-everything-local-with-ollama-simplest) — the installer will set up Ollama and a
Qdrant container for you.

---

## 1. What gets installed

```
 browser ───► ATP frontend   (Vite dev server, :5173)
                  │  /ws  /presets  /launcher/huri/*  /huri-modules
                  ▼
              ATP backend    (FastAPI + uvicorn, :8001)
                      │  spawns  `serve run <huri config>.yaml`  (from huri-modules/)
                  │  bridges  ws://localhost:8000/session    (huri-core client SDK)
                  ▼
              HuRI           (huri-core on Ray Serve, HTTP/WS :8000 · Ray dashboard :8265)
               ├─ vad/stt/tag   WebRTC VAD, faster-whisper   local — CPU or GPU
               ├─ emo/eag       hubert prosody model         local — CPU, per session
               ├─ qag/rag       ──► embeddings   POST {embed_url}/v1/embeddings
               │                ──► LLM          POST {llm_url}/api/chat  (ollama)
               │                                 POST {llm_url}/v1/chat/completions (vllm | api)
               │                ──► Qdrant       vector DB: documents + conversation memory
               ├─ tts_cosyvoice CosyVoice3-0.5B              local — NVIDIA GPU (or forced CPU)
               ├─ tts           Piper                        local — CPU (when CosyVoice does not fit)
               └─ mov           EMAGE gestures               local — NVIDIA GPU (or forced CPU)
```

Three processes run on the test machine: the **frontend**, the **backend** and **HuRI**
itself. The three services on the right — LLM, embeddings, Qdrant — are network endpoints. Each
one can be on this machine (the installer then installs and starts it) or somewhere else (the
installer then only checks it answers). Which is which is decided purely by the URL you give:
anything that is not `localhost`/`127.0.0.1`/this host's name is treated as *already running
elsewhere*.

| Port | Who | What |
|---|---|---|
| 5173 | ATP frontend | Vite dev server (the UI) |
| 8001 | ATP backend | FastAPI: auth, presets, HuRI launcher, browser↔HuRI websocket bridge |
| 8000 | HuRI | Ray Serve HTTP ingress: `GET /modules`, `WS /session` |
| 8265 | HuRI | Ray dashboard (linked from the Control Panel) |
| 6333 / 6334 | Qdrant | REST / gRPC — only if Qdrant runs locally |
| 11434 | Ollama | LLM + embeddings — only if Ollama runs locally |

The repositories involved:

- **huri-modules** (<https://github.com/Lucy-Robotics/huri-modules>) — the HuRI server: the
  curated modules and events, the installer and the Serve configs. Vendored here as the git
  submodule `huri-modules/`; its installer also installs **huri-core**
  (<https://github.com/Lucy-Robotics/huri-core>), the framework, at the version it pins.
- **HuRI_ATP_testsite** (this repo) — the browser test console built to exercise every row of the
  ATP (acceptance test plan). Its backend is a HuRI client: it installs `huri-core[client]` and
  `huri-events` (huri-core 0.4.x / huri-modules 0.1.x) and needs no HuRI checkout to run.

The site's frontend still names the modules as the HuRI monorepo did (`mic`, `gesture`, `tts`);
the backend translates to and from huri-modules' names (`vad`, `mov`, `tts`/`tts_cosyvoice`).

---

## 2. Requirements

| | Requirement | Notes |
|---|---|---|
| OS | Linux x86_64 | Ubuntu/Debian, Fedora, Arch, openSUSE are handled by the installer (apt/dnf/pacman/zypper). WSL2 works with an NVIDIA GPU; AMD ROCm does **not** work under WSL2. |
| Python | **3.12** | HuRI's installer accepts 3.10–3.12 and the ATP backend needs 3.11+; 3.12 is the version that satisfies both, so it is the requirement. Each side builds its own venv. On Arch, install 3.12 via pyenv/uv/AUR and pass `--python` to the installer. |
| Node.js | 20+ with npm | Frontend only (Vite 5, React 18). Tested with Node 24. |
| git | any recent | Submodules; the backend installs huri-core and huri-events from git. |
| Git LFS | installed **before** cloning | The avatar's 3D model (`model.fbx` in `backend/static/` and `frontend/public/`) is an LFS object. Without LFS the clone contains a small text pointer instead, and the avatar never renders. |
| sudo | for system packages | `build-essential`/gcc, `ffmpeg`, `libsndfile1`, `libportaudio2`, `python3-dev`, `python3.X-venv`. Pass `--skip-system` if you install those yourself. |
| Container runtime | Docker or Podman — optional | Only for a **local** Qdrant. Without one the installer downloads a standalone Qdrant binary instead. |
| GPU — full pipeline | NVIDIA, ≥ ~9 GiB free VRAM | TTS ≈ 4.5 GiB fp16, gesture ≈ 2.2 GiB, STT-base ≈ 1 GiB, 0.7 GiB kept free for the driver. Driver must support CUDA 12.1 (≥ 525.60). |
| GPU — with a local LLM too | add the LLM's VRAM | e.g. mistral:7b ≈ 5.5 GiB, llama3.2:3b ≈ 2.8 GiB, plus ≈ 1.3 GiB for local embeddings. On a 12 GiB card the full pipeline only leaves room for a 3B model — that is why the reference layout moves the LLM off-box. |
| GPU — AMD ROCm | STT and Ollama only | CosyVoice/EMAGE are not built for ROCm; the plan drops TTS/gesture (or `--force-tts`/`--force-gesture` runs them on CPU). |
| No GPU | text pipeline | Speech/text in, text out. `--force-tts` works but is far slower than realtime. |
| RAM | ≥ 8 GiB, 16 GiB comfortable | 3 GiB base (Ray + Serve) + 0.7 GiB STT-base + 1.6 GiB emotion **per session**, plus the LLM if it runs on CPU (mistral:7b ≈ 6.5 GiB). |
| Disk | ≈ 16 GiB for the full NVIDIA install | venv 2.5 + CUDA torch 5 + CosyVoice 6 + EMAGE 1.5 + emotion 1.3 + whisper-base 0.2. Add ≈ 3 GiB + model weights if Ollama is local (mistral:7b ≈ 4.4 GiB, bge-m3 ≈ 1.2 GiB). CPU-only text install ≈ 5 GiB. |
| Network | outbound HTTPS | Hugging Face Hub, ModelScope (CosyVoice3 weights), GitHub, PyPI, ollama.com. |
| Audio (optional) | microphone | Only for the CLI client / mic tests; the browser uses its own mic. |

`./install.sh --plan-only` (run inside `huri-modules/`) prints exactly what fits on the
current machine without installing anything — run it first on any new box.

---

## 3. Get the code

```bash
git lfs install          # once per machine, before the clone
git clone --recurse-submodules https://github.com/Lucy-Robotics/HuRI_ATP_testsite.git
cd HuRI_ATP_testsite
```

Already cloned without submodules? The `huri-modules/` folder is then empty:

```bash
git submodule update --init
```

Cloned before installing Git LFS? The `model.fbx` files are pointers; fetch the real objects:

```bash
git lfs install && git lfs pull
```

The submodule is pinned to a specific huri-modules commit (see `git submodule status`): a
huri-modules 0.1.x release, compatible with the huri-core 0.4.x client the backend installs.
To move it to the commit the ATP site expects after a `git pull`, run `git submodule update`
again; to move it to another release, `git -C huri-modules checkout <tag>` and commit the new
gitlink (then re-run the installer stages that changed, see [§7](#7-operating-it)).

### Where HuRI lives: two layouts

- **A. Inside the submodule (recommended).** Install HuRI in `huri-modules/`. The ATP backend
  finds it automatically, runs `serve run` from it with its installer environment
  (`.huri-local/env.sh`) and its venv's `serve`/`ray` binaries. The rest of this guide assumes
  this.
- **B. An existing huri-modules checkout elsewhere** (e.g. you already ran the installer in
  another clone; a sibling `../huri-modules` is found automatically). Do not install twice;
  point the site at it instead:
  ```bash
  export HURI_MODULES_PATH=/path/to/your/huri-modules
  ```
  before `./scripts/run_all.sh`. The launcher will run from that checkout, use
  `$HURI_MODULES_PATH/.venv/bin/serve` and list `$HURI_MODULES_PATH/config/huri*.yaml`. Note
  that Qdrant and Ollama are machine-wide services, not per-checkout: two checkouts on one
  machine share them.

---

## 4. Install HuRI

### 4.1 How the installer works

`huri-modules/install.sh` (a wrapper around its `scripts/install_local.sh`, every flag applies)
is the bare-metal counterpart of the Docker images and Helm chart. It runs in stages:

1. **Detect** — OS, package manager, CPU/RAM/disk, GPU vendor + VRAM (`nvidia-smi` /
   `rocm-smi`), Python, Docker/Podman, Ollama.
2. **Plan** — decides per module whether it runs on GPU, on CPU, remotely, or not at all, and
   prints a table with a verdict: `full` (voice + gesture out), `voice` (no gesture), `text`,
   or `blocked`. Modules that do not fit are dropped from `HURI_MODULES`, so no Serve
   deployment is ever created for them.
3. **system** — apt/dnf/pacman/zypper packages.
4. **python** — a venv at `huri-modules/.venv` with the right torch build (CUDA 12.1, ROCm, or
   CPU), huri-core and huri-cli at the refs pinned in `versions.env`, the curated module
   packages, the CosyVoice/EMAGE stack when needed, and a clone of the CosyVoice source tree
   into `huri-modules/assets/cosyvoice`.
5. **models** — weights into `huri-modules/assets/models/`: faster-whisper (Hugging Face), CosyVoice3
   (ModelScope), EMAGE (Hugging Face), the emotion classifier (HF cache), and your voice sample.
6. **services** — a local Qdrant (Docker container `huri-qdrant`, or a binary) and Ollama with
   the planned models — **only for endpoints that resolve to this machine**.
7. **config** — the generated Ray Serve config, client config, env files and run scripts.
8. **verify** — imports, GPU visibility, CosyVoice/EMAGE importability, voice sample presence,
   endpoint reachability.

Useful switches: `--plan-only` (stop after 2), `-n/--dry-run` (print every command),
`-y/--yes` (no prompts — this also auto-runs the official Ollama installer if Ollama is needed
and missing), `--only system,python,models,services,config,verify` (re-run selected stages),
`--skip-system|python|models|services`. Everything it writes lives in
`huri-modules/.huri-local/`, `huri-modules/assets/` and `huri-modules/config/*.generated.yaml`,
so an install is inspectable and removable.

### 4.2 Choose where the LLM, embeddings and Qdrant live

Before running it, decide the three URLs. The rule is simple: **a URL on localhost is
installed and started by the installer; any other URL is assumed to be already running and
is only probed.**

| Service | Flag | Default | Local means | Remote means |
|---|---|---|---|---|
| LLM | `--llm-url` | `http://localhost:11434` (Ollama) | Ollama is installed, the model is pulled (`--llm-model`, or a size picked from free VRAM/RAM) | Nothing installed; `--llm-model` becomes **mandatory** and must be the name the endpoint serves |
| Embeddings | `--embed-url` | same as `--llm-url` | Ollama pulls `--embed-model` (default `bge-m3`) | Nothing installed |
| Qdrant | `--qdrant-url` | `http://localhost:6333` | Container/binary started, data in `.huri-local/qdrant` | Nothing installed |

Two more flags shape the endpoints:

- `--llm-provider ollama|vllm|api` — the **wire protocol**. If omitted it is inferred: a URL
  containing `:11434` → `ollama`; otherwise `api` if an API key was given, else `vllm`. Details
  in [§5.2](#52-the-llm).
- `--no-verify-ssl` — disables TLS verification for **all three** endpoints at once (self-signed
  LAN certificates). Leave it out for public hosted APIs.

### 4.3 The reference full install, explained

```bash
cd huri-modules
./install.sh \
  --llm-url https://llm.huri.lan --llm-model Qwen3.5-4B-GGUF \
  --embed-url https://embedding.huri.lan --embed-model bge-large-en-v1.5-gguf-Q4_K_M \
  --no-verify-ssl \
  --qdrant-url https://qdrant.pommier.lan \
  --voice-sample ../assets/voice.wav
```

| Flag | Effect |
|---|---|
| `--llm-url https://llm.huri.lan` | Remote LLM. No key was given and the port is not 11434, so the provider is inferred as `vllm`: HuRI will `POST https://llm.huri.lan/v1/chat/completions` with no auth header. Any OpenAI-compatible server (vLLM, llama.cpp `llama-server`, LM Studio, LiteLLM…) works. |
| `--llm-model Qwen3.5-4B-GGUF` | Sent as the `model` field of every request. Must be the exact name the server exposes (`curl https://llm.huri.lan/v1/models`). |
| `--embed-url https://embedding.huri.lan` | Remote embeddings: `POST https://embedding.huri.lan/v1/embeddings`. |
| `--embed-model bge-large-en-v1.5-gguf-Q4_K_M` | The `model` field of embedding requests. llama.cpp ignores it (it serves one model); Ollama uses it to pick the model. Must be the **same model that was used to ingest the documents** in Qdrant (see [§5.1](#51-qdrant)). |
| `--no-verify-ssl` | Private CA / self-signed certs on all three hosts. |
| `--qdrant-url https://qdrant.pommier.lan` | Remote Qdrant over HTTPS. No port → 443 (HuRI derives the port from the scheme). |
| `--voice-sample ../assets/voice.wav` | The zero-shot reference voice for TTS (CosyVoice), copied to `huri-modules/assets/voice.wav`. `assets/voice.wav` is the **default voice shipped in this repo** and it says the default transcript, so no `--voice-transcript` is needed. For another voice pass your own file **and** its transcript — see [§4.5](#45-the-voice-sample). |

With every service remote, the plan on a 12 GiB RTX 3060 is: TTS on GPU (fp16), gesture on
GPU, STT-base on GPU, emotion on CPU, LLM/embeddings/memory remote → verdict **full**, and no
Ollama or Qdrant is installed locally.

### 4.4 Other layouts

#### A. Everything local with Ollama (simplest)

```bash
cd huri-modules
./install.sh --yes --voice-sample ../assets/voice.wav
```

The installer starts a `huri-qdrant` Docker container (or a binary), installs Ollama, and pulls
the largest LLM tier that fits the VRAM left after TTS/gesture (`qwen2.5:14b` → `mistral:7b` →
`llama3.2:3b` → `qwen2.5:1.5b`, falling back to CPU inference from RAM) plus `bge-m3` for
embeddings. Force a tag with `--llm-model mistral:7b`. Expect a smaller LLM, or TTS/gesture
being dropped, on cards under 16 GiB — check `--plan-only` first.

#### B. A hosted API with a key (OpenAI, Mistral, Groq, OpenRouter, any OpenAI-compatible)

```bash
cd huri-modules
./install.sh \
  --llm-url https://api.openai.com --llm-provider api \
  --llm-model gpt-4o-mini --llm-api-key "$OPENAI_API_KEY" \
  --embed-url http://localhost:11434 --embed-model bge-m3 \
  --voice-sample ../assets/voice.wav
```

- The key is written to `huri-modules/.huri-local/secrets.env` (mode `0600`) and exported as
  `HURI_LLM_API_KEY`; it is never written into the generated YAML. Only the `api` provider
  attaches it as `Authorization: Bearer …` — the installer warns if you pass a key with another
  provider.
- **Embeddings stay local** on purpose: HuRI's embedding call sends *no* authorization header,
  so a hosted `/v1/embeddings` that needs a key would answer 401. Ollama serves an
  OpenAI-compatible `/v1/embeddings` without a key, so the installer installs Ollama for
  embeddings only (the LLM is remote, so no LLM model is pulled). To use a hosted embedding
  model anyway, put a keyless LiteLLM proxy in front of it — see [§5.3](#53-the-embedding-model).
- `--llm-model` is the provider's model id (use whatever your account offers).
- Rotate the key later without reinstalling: `./install.sh --only config
  --llm-api-key "$NEW_KEY"`, then restart HuRI (a running Ray cluster keeps the old
  environment).

#### C. Remote Ollama on another machine

```bash
./install.sh --llm-url http://192.168.1.20:11434 --llm-model mistral:7b \
  --embed-model bge-m3 --voice-sample ../assets/voice.wav
```

The `:11434` in the URL selects the `ollama` provider (`/api/chat`); embeddings default to the
same host (`/v1/embeddings`). On the Ollama host, bind it to all interfaces:
`OLLAMA_HOST=0.0.0.0 ollama serve` (or `Environment="OLLAMA_HOST=0.0.0.0"` in a systemd
override), and run `ollama pull mistral:7b` and `ollama pull bge-m3` there.

#### D. No GPU / CPU-only (text pipeline)

```bash
./install.sh --profile cpu --stt-model base --llm-model qwen2.5:1.5b
```

STT runs int8 on CPU (realtime for `base`/`small`), the LLM is served from RAM by Ollama, TTS
runs on Piper (module `tts`, ONNX on CPU, faster than realtime) and gesture is dropped. Presets
that need `gesture` will be greyed out in the site's Event Configuration modal, which is
expected. `--force-gesture` runs EMAGE on CPU anyway (slowly).

### 4.5 The voice sample

This applies to CosyVoice3 (`tts_cosyvoice`, NVIDIA only); Piper (`tts`) speaks with a stock
voice (`--piper-voice`). CosyVoice3 is zero-shot: it clones the voice of a reference recording, and needs that
recording plus the exact transcript of what it says.

**Default voice — use it unless you want another one.** This repo ships `assets/voice.wav`
(5 s, mono, 44.1 kHz), a recording of the default transcript *"Instinct creates its own
oppressors and bids us rise up against them."* Pass it as `--voice-sample ../assets/voice.wav`
(the path is relative to `huri-modules/`, where the installer runs); the installer copies it to
`huri-modules/assets/voice.wav` and the default `--voice-transcript` already matches, so nothing
else is needed. It lives in this repo rather than in huri-modules because huri-modules'
`assets/` folder is git-ignored (this repo's `.gitignore` has an explicit `!assets/voice.wav`
exception). Once copied, the file stays in `huri-modules/assets/`, so later `--only config`
re-runs do not need the flag.

**Custom voice.** Pass two flags that **must match each other**:

- `--voice-sample PATH` — a WAV of one speaker, clean, no music or background noise, roughly
  5–15 seconds (CosyVoice resamples it, so 16 kHz or 44.1 kHz mono are both fine). Copied to
  `huri-modules/assets/voice.wav`, replacing whatever was there.
- `--voice-transcript "…"` — the **exact words spoken in that file**. Without the flag the
  default transcript above is used, which is only right for the default voice. A wrong
  transcript degrades or garbles synthesis.

The generated config stores it as
`HURI_VOICE_TRANSCRIPT: "You are a helpful assistant.<|endofprompt|><your transcript>"`. The
`<|endofprompt|>` marker is a CosyVoice3 contract: the transcript must come *after* it or the
model treats it as an instruction and sometimes speaks it aloud.

To add or replace the sample after installing: `./install.sh --only models,config
--voice-sample new.wav --voice-transcript "…"` (`--only config` alone does not copy the file —
`models` is the stage that does). Without any `huri-modules/assets/voice.wav`, verification fails on
purpose: every TTS request would fail silently at runtime.

### 4.6 What the installer produced

| Path (under `huri-modules/`) | What |
|---|---|
| `config/huri_local.generated.yaml` | The Ray Serve config for **this machine**: absolute model paths, `HURI_MODULES`, GPU fractions, and the `RAGHandle` `user_config` (Qdrant/LLM/embedding URLs, provider, model names, `verify_ssl`). **This is the config to launch from the ATP Control Panel.** |
| `config/client_local.generated.yaml` | A matching config for the reference CLI client (`huri-cli --config …`). |
| `.huri-local/plan.env` | The hardware plan it acted on (profile, devices per module, endpoints). |
| `.huri-local/huri.env` | The same runtime env vars as a sourceable `.env`. |
| `.huri-local/secrets.env` | `HURI_LLM_API_KEY`, mode 0600. Present even when empty. |
| `.huri-local/env.sh` | `source` it for a shell configured like a Serve replica: venv + `huri.env` + `secrets.env`. Needed for ingestion and for `python -m huri_core.launch_huri`; the ATP launcher sources it before `serve run`. |
| `.huri-local/start.sh` / `stop.sh` / `status.sh` | Run the stack outside the ATP site (see §4.7). |
| `.huri-local/install.log` | Everything the installer ran. |
| `.huri-local/qdrant/`, `.huri-local/bin/qdrant` | Local Qdrant data / binary, only for a local Qdrant. |
| `.venv/` | HuRI's Python environment: huri-core, the modules, huri-cli (`serve`, `ray`, `huri-cli` binaries live in `.venv/bin`). |
| `assets/voice.wav`, `assets/cosyvoice/`, `assets/models/…` | Voice sample (copied from this repo's default `assets/voice.wav`, or your `--voice-sample`), CosyVoice source, weights. |

These paths are git-ignored by huri-modules.

### 4.7 Start HuRI on its own and check it

You do not need this for the ATP site (the Control Panel launches HuRI), but it is the fastest
way to validate the install:

```bash
cd huri-modules
.huri-local/start.sh          # local services (if any) + ray head + serve deploy
.huri-local/status.sh         # qdrant / llm / ray / huri up-or-down + `serve status`
curl -s http://localhost:8000/modules      # the modules this instance actually deployed
```

First start takes a while: CosyVoice3 and EMAGE load, then TTS runs a warm-up synthesis. The
Ray dashboard at <http://127.0.0.1:8265> shows each deployment's state and logs. Talk to it from
the terminal:

```bash
source .huri-local/env.sh
huri-cli --config config/client_local.generated.yaml
```

Stop with `.huri-local/stop.sh` (Serve + Ray) or `.huri-local/stop.sh --all` (also the local
Qdrant container/binary and `ollama serve`).

### 4.8 Change endpoints or the key later

Re-run only the config stage; nothing is reinstalled:

```bash
./install.sh --only config \
  --llm-url https://other.lan --llm-model other-model --llm-provider vllm \
  --embed-url … --embed-model … --qdrant-url … --no-verify-ssl
.huri-local/stop.sh && .huri-local/start.sh      # or Stop/Start in the Control Panel
```

Pass the complete set of endpoint flags each time: `--only config` regenerates the whole file
from the flags given (the API key is the one exception — it is reloaded from `secrets.env` when
not passed). Note that the plan is recomputed too, so moving the LLM from local to remote frees
VRAM and can promote TTS/gesture from `off` to `gpu`; in that case run `--only
python,models,config` so the extra packages and weights get installed.

---

## 5. The external services in depth

### 5.1 Qdrant

**What it is.** Qdrant is an open-source vector database: it stores embeddings (fixed-length
float vectors) together with a JSON payload, and answers "which stored vectors are closest to
this query vector" filtered by payload fields. It is the memory behind RAG (retrieval-augmented
generation): text is turned into vectors by the embedding model, stored in Qdrant, and at
question time the question's vector retrieves the most similar chunks, which are pasted into
the LLM prompt.

**How HuRI uses it.** Two collections, both created automatically with cosine distance and the
embedding model's dimension:

- `documents` — knowledge ingested with the ingestion tool (PDFs, text, profile facts). Every
  point carries a `_user_id` payload; retrieval only returns chunks whose `_user_id` is the
  querying user's or the reserved shared id `__shared__`, so memory is partitioned per user.
- `conversations` — long-term conversational memory. HuRI writes summaries of what was said,
  re-ranks them at retrieval time by relevance × recency × importance, reinforces the ones it
  reuses, and runs a maintenance pass every `memory_maintenance_check_hours` (6 h in the
  generated config) that consolidates and prunes old memories.

The user id comes from the ATP backend: `anonymous` when `REQUIRE_AUTH=0` (the default in
`run_all.sh`), or the OIDC `sub` when logged in through Authelia.

**The one rule.** The vectors in a collection and the vectors used to query it **must come from
the same embedding model** (same model → same dimension and same vector space). Swapping
`--embed-model` after ingesting means re-ingesting; a dimension mismatch fails outright, a
different model of the same dimension silently returns garbage.

**Install options.**

| Option | How |
|---|---|
| Local, managed by the installer | Leave `--qdrant-url` at its default. With Docker/Podman: container `huri-qdrant` from `qdrant/qdrant:v1.12.4`, ports 6333/6334, data in `huri-modules/.huri-local/qdrant`. Without: the standalone binary in `.huri-local/bin`, started by `start.sh`. |
| Self-hosted on another machine | `docker run -d --name qdrant --restart unless-stopped -p 6333:6333 -p 6334:6334 -v /srv/qdrant:/qdrant/storage qdrant/qdrant:v1.12.4`, then `--qdrant-url http://that-host:6333`. For HTTPS put a reverse proxy in front (Caddy, Traefik, nginx) and pass `https://qdrant.example.lan` (no port → 443). |
| Qdrant Cloud | Not supported out of the box: HuRI's client is built from a URL only and does not send an API key. Use it only behind a proxy that injects the key, or restrict access at the network level instead. |

Check any instance with `curl -k https://<host>/readyz` (→ `all shards are ready`) and browse
its collections at `https://<host>/dashboard`. Keep the server at 1.12 or newer; HuRI's client
library is 1.18.

### 5.2 The LLM

HuRI does not run the LLM itself; the `RAGHandle` deployment streams from an HTTP endpoint.
`--llm-provider` selects the protocol:

| Provider | Endpoint called | Auth | Typical servers |
|---|---|---|---|
| `ollama` | `POST {llm_url}/api/chat` (Ollama native, NDJSON stream) | none | Ollama, local or on the LAN |
| `vllm` | `POST {llm_url}/v1/chat/completions` (OpenAI SSE stream) | none | vLLM, llama.cpp `llama-server`, LM Studio, text-generation-inference, a keyless LiteLLM proxy — also Ollama's own `/v1` if you prefer |
| `api` | `POST {llm_url}/v1/chat/completions` | `Authorization: Bearer $HURI_LLM_API_KEY` | OpenAI, Mistral, Groq, OpenRouter, Anthropic's OpenAI-compatible endpoint, a LiteLLM proxy with a master key |

Inference when the flag is omitted: `:11434` in the URL → `ollama`; a key given → `api`;
otherwise `vllm`. The `model` field of each request is `--llm-model`, so it must be a name the
endpoint serves (`ollama list`, or `curl {llm_url}/v1/models`).

**Option 1 — Ollama (no key).** The installer does this for a localhost URL. By hand:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull mistral:7b            # or qwen2.5:14b, llama3.2:3b, qwen2.5:1.5b …
ollama pull bge-m3                # embeddings, see §5.3
ollama serve                      # if not already running as a service
curl -s http://localhost:11434/api/tags
```

Ollama has no API keys; it trusts whoever can reach port 11434. Expose it to the LAN with
`OLLAMA_HOST=0.0.0.0` and firewall it.

**Option 2 — a self-hosted OpenAI-compatible server (no key).** This is what `llm.huri.lan`
is in the reference layout. Examples:

```bash
# vLLM (GPU)
vllm serve Qwen/Qwen2.5-7B-Instruct --port 8080
# llama.cpp (GPU or CPU, GGUF weights)
llama-server -hf Qwen/Qwen2.5-7B-Instruct-GGUF:Q4_K_M --port 8080 --alias Qwen2.5-7B
```

Then `--llm-url http://host:8080 --llm-model <served name>`; the provider is inferred as
`vllm`. Add `--no-verify-ssl` when it sits behind a self-signed HTTPS proxy.

**Option 3 — a hosted API with a key.** `--llm-url https://api.openai.com --llm-provider api
--llm-model <model> --llm-api-key "$KEY"` (see [§4.4 B](#b-a-hosted-api-with-a-key-openai-mistral-groq-openrouter-any-openai-compatible)).
The base URL is everything before `/v1/chat/completions`, e.g. `https://api.mistral.ai`,
`https://api.groq.com/openai`, `https://openrouter.ai/api`. Where the key lives and how it
reaches the replicas:

- stored in `huri-modules/.huri-local/secrets.env` (0600), exported as `HURI_LLM_API_KEY`;
- `start.sh` sources it before `ray start`, so replicas inherit it;
- **when HuRI is launched from the ATP Control Panel**, the launcher sources
  `huri-modules/.huri-local/env.sh` (which loads `secrets.env`) before `serve run`, exactly as
  `start.sh` does, so nothing needs exporting. Without that file (a manual install), export the
  key in the shell that starts the site instead.
  A Ray cluster that is already up keeps its old environment: stop it before changing the key.

### 5.3 The embedding model

**What it is.** An embedding model maps a piece of text to a vector such that semantically
similar texts land close together. HuRI uses it twice: at ingestion (chunks → vectors stored in
Qdrant) and at question time (question → vector → nearest chunks and memories). It is small
(bge-large is ~335 M parameters, ~200 MB at Q4) and runs fine on CPU.

**How HuRI calls it.** Always the OpenAI shape, regardless of `--llm-provider`:
`POST {embed_url}/v1/embeddings` with `{"model": "<embed model>", "input": "<text>"}`, expecting
`data[0].embedding`. **No authorization header is sent.** `--embed-url` defaults to
`--llm-url`, which is why an Ollama LLM gets Ollama embeddings for free.

**Ways to provide it.**

| Option | Command | Flags |
|---|---|---|
| Ollama (default) | `ollama pull bge-m3` (1024-dim, multilingual). Also fine: `nomic-embed-text`, `mxbai-embed-large`. | `--embed-url http://localhost:11434 --embed-model bge-m3` — the installer pulls it for a localhost URL |
| llama.cpp server with a GGUF (what `embedding.huri.lan` runs) | `llama-server -hf CompendiumLabs/bge-large-en-v1.5-gguf:Q4_K_M --embedding --pooling cls -c 512 --port 8081 --alias bge-large-en-v1.5-gguf-Q4_K_M` | `--embed-url http://host:8081 --embed-model bge-large-en-v1.5-gguf-Q4_K_M` (the name is informational for llama.cpp) |
| Hugging Face text-embeddings-inference | `docker run -p 8081:80 ghcr.io/huggingface/text-embeddings-inference:cpu-latest --model-id BAAI/bge-large-en-v1.5` | `--embed-url http://host:8081 --embed-model BAAI/bge-large-en-v1.5` |
| Hosted embeddings behind a key (OpenAI `text-embedding-3-*`, Mistral, …) | Run a **keyless** LiteLLM proxy that holds the provider key: `pip install 'litellm[proxy]'`, a config with `model_list: [{model_name: my-embed, litellm_params: {model: openai/text-embedding-3-small, api_key: os.environ/OPENAI_API_KEY}}]` and `general_settings: {master_key: null}`, then `litellm --config litellm.yaml --port 4000`. | `--embed-url http://<LAN-IP-or-hostname>:4000 --embed-model my-embed`. Use a non-`localhost` name even if the proxy is on this machine, otherwise the installer treats the URL as an Ollama it must install. The same proxy can front the LLM (`--llm-provider vllm`). |

Whatever you choose, use the **same** `--embed-url`/`--embed-model` for ingestion
([§5.4](#54-feeding-the-rag-memory-ingestion)) and for HuRI, and do not change it without
re-ingesting.

### 5.4 Feeding the RAG memory (ingestion)

An empty Qdrant makes RAG answer from the LLM alone (plus conversation memory). To give it
knowledge, ingest documents with the same embedding endpoint HuRI uses:

```bash
cd huri-modules
source .huri-local/env.sh
python -m huri_rag.ingestion \
  --qdrant-url https://qdrant.pommier.lan --no-verify-ssl \
  --embedding-url https://embedding.huri.lan --embedding-model bge-large-en-v1.5-gguf-Q4_K_M \
  --chunking fixed \
  --user-id __shared__ \
  pdf docs/*.pdf
```

- `--user-id __shared__` makes the documents visible to every user; `--user-id anonymous`
  targets the ATP site's default open session; an OIDC `sub` targets one logged-in user.
  Without the flag the tool uses (and creates) `~/.huri_user_id`, which the site never uses.
- `--chunking fixed` is required with a remote `--embedding-url` (semantic chunking needs a
  local sentence-transformers model). Keep chunks under the model's context (512 tokens for
  bge).
- Sub-commands: `pdf FILES…`, `text FILES…` (`.txt`/`.md`), `write --title T` (interactive),
  `profile --name N --fact "…"` (always-on facts about the user), `list`, `delete --source S`.
- For a local Ollama: `--embedding-url http://localhost:11434 --embedding-model bge-m3`.

---

## 6. Install the ATP test site

### 6.1 Backend

`scripts/run_backend.sh` creates the venv on first run; you can also do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt     # fastapi, uvicorn, numpy, scipy, authlib, httpx…, huri-core[client], huri-events
```

`requirements.txt` installs the HuRI client SDK and the curated events from git, at their
releases (huri-core `0.4.0`, huri-events from huri-modules `0.1.0`); no Ray, no torch. This venv
is deliberately separate from `huri-modules/.venv`: the backend only talks to HuRI over its
websocket, and delegates `serve`/`ray` to huri-modules' own venv binaries.

To run the backend tests (a fake HuRI server stands in for the real one):

```bash
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest tests
```

### 6.2 Frontend

`scripts/run_frontend.sh` runs `npm install` on first run; by hand:

```bash
cd frontend && npm install
```

### 6.3 Environment variables

All are read from the backend's process environment (the ATP scripts do **not** load a `.env`
file; export them in the shell, or `source` your own file first).

| Variable | Default | Purpose |
|---|---|---|
| `HURI_MODULES_PATH` | `huri-modules/` submodule if checked out, else `../huri-modules` | huri-modules checkout the Control Panel runs `serve run` from (sourcing its `.huri-local/env.sh`), and where `config/huri*.yaml` are listed. |
| `HURI_SERVE_BIN` / `HURI_RAY_BIN` | `$HURI_MODULES_PATH/.venv/bin/{serve,ray}` when that venv exists, else `serve`/`ray` from `PATH` | Binaries the Control Panel uses to launch/stop HuRI. |
| `HURI_URL` | `ws://localhost:8000/session` | Where browser sessions connect; its HTTP form serves `GET /modules`, which the launcher probes for readiness and `/huri-modules` proxies. |
| `HURI_PRESETS_DIR` | `<repo>/presets` | Client presets (`*.json`) and per-feature HuRI config copies (`F*/huri*.yaml`). |
| `HURI_DASHBOARD_URL` | `http://127.0.0.1:8265/` | Link shown in the Control Panel. |
| `VITE_BACKEND_URL` | `http://localhost:8001` (set by the scripts) | Backend origin baked into the frontend. |
| `REQUIRE_AUTH` | `0` in `run_all.sh`, `1` otherwise | See §6.7. |
| `SESSION_SECRET`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `FRONTEND_URL`, `COOKIE_SECURE`, `COOKIE_SAMESITE`, `MAGIC_LINK_SECRET` | — | Auth only (§6.7). |
| `HURI_LLM_API_KEY` | — | Not read by the site itself, but inherited by the `serve run` it spawns — required when HuRI uses `--llm-provider api` and no `.huri-local/env.sh` provides it (§5.2). |

### 6.4 Run it

```bash
./scripts/run_all.sh          # backend :8001 + frontend :5173, Ctrl-C stops both (and HuRI, if the panel started it)
# or separately
./scripts/run_backend.sh
./scripts/run_frontend.sh
```

`run_all.sh` prints the resolved `HURI_MODULES_PATH`; if it warns that huri-modules could not
be found, the submodule is not checked out ([§3](#3-get-the-code)); if it warns that it is not
installed, run `huri-modules/install.sh` ([§4](#4-install-huri)). HuRI itself is **not** started by
these scripts.

### 6.5 Launch HuRI from the Control Panel — which config to pick

Open <http://localhost:5173>, click **HuRI Control Panel** (top bar; it opens automatically
when nothing is running), choose a config, **Start**. The backend runs `serve run <config>`
with the huri-modules checkout as working directory (after sourcing its `.huri-local/env.sh`), tails its logs into the panel, and reports
*running* once `GET /modules` answers (up to 90 s). Only one instance can run at a time.

The dropdown lists every `config/huri*.yaml` in the huri-modules checkout plus the copies under
`presets/F*/`. **Not all of them are runnable on your machine**:

| Entry | Use it? |
|---|---|
| `huri_local.generated.yaml` | **Yes.** Generated by the installer for this machine: correct absolute model paths, only the modules that fit, and your LLM/embedding/Qdrant settings. |
| `huri.yaml`, `huri_cpu.yaml` | Only after editing. They are huri-modules' hand-written templates with another machine's paths (`/home/fifster/…`, `/models/…`) and a RAG block in an older format (`llm_base_url`, no `llm_provider`) that the current `RAGHandle` ignores — it would then fall back to a local Ollama at `localhost:11434` with `mistral:7b`. `huri.yaml` serves CosyVoice (`tts_cosyvoice`), `huri_cpu.yaml` Piper (`tts`). |
| `F1/huri.yaml`, `F2/huri.yaml`, … `F11/huri.yaml`, `F1/huri_cpu.yaml` | Byte-for-byte copies of the two above (see `presets/README.md`), so the same caveat applies. |

To make the per-feature entries launchable as the ATP sheet describes, copy the generated
config over them (they are documented as convenience copies, not the source of truth):

```bash
for d in presets/F*; do cp huri-modules/config/huri_local.generated.yaml "$d/huri.yaml"; done
```

For `F1/huri_cpu.yaml` (the GPU-vs-CPU comparison), generate a CPU variant into a second
huri-modules checkout (`./install.sh --profile cpu`), or edit the copy's paths to match `huri_local.generated.yaml` and set `num_gpus: 0`
everywhere.

Alternatively start HuRI in a terminal with `huri-modules/.huri-local/start.sh`: the panel detects an
instance it did not start (shown as *external*), the client connects to it as usual, and only
**Stop** is disabled (stop it where you started it).

### 6.6 End-to-end check

```bash
curl -s http://localhost:8001/presets | head -c 300          # backend up, presets scanned
curl -s http://localhost:8001/launcher/huri/status           # {"status":"running", …} once HuRI is up
curl -s http://localhost:8001/huri-modules                   # modules HuRI actually deployed
curl -s http://localhost:8000/modules                        # same, straight from HuRI
```

Then in the UI: the composer switches from *Connecting to HuRI…* to ready; open **Event
Configuration** (gear), pick `F9/rag_only`, type a question, get a streamed answer. Pick a
`voice_with_speech`-style preset to test mic → TTS → gesture (the avatar only renders when the
session includes `gesture`, i.e. huri-modules' `mov`). Presets needing modules this machine did not deploy are greyed
out — that is the plan from [§4.1](#41-how-the-installer-works), not a bug.

### 6.7 Authentication

Local testing runs open (`REQUIRE_AUTH=0`, everyone is `user_id=anonymous`, so everyone shares
one RAG partition). For per-user memory or a public deployment:

- **Authelia (OIDC)** — `REQUIRE_AUTH=1`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
  `OIDC_CLIENT_SECRET`, and a `SESSION_SECRET` (`openssl rand -hex 32`; the backend refuses to
  start without one). The OIDC `sub` becomes the HuRI user id. `deploy/` holds a Terraform
  setup for Authelia on GCP.
- **Magic link** — set `MAGIC_LINK_SECRET`, mint a link/QR with
  `backend/tools/make_magic_qr.py --origin http://localhost:8001 --sub demo-user`, and open
  it; the visitor is signed in as that `sub` without a password (demo use only).

---

## 7. Operating it

**Start order.** Remote services first (they must answer before `RAGHandle` initialises), then
`./scripts/run_all.sh`, then **Start** in the Control Panel (or `huri-modules/.huri-local/start.sh`).
`huri-modules/.huri-local/status.sh` shows every dependency in one screen.

**Stop.** Ctrl-C in `run_all.sh` asks the launcher to stop HuRI (SIGINT to `serve run`, then
`ray stop`) before exiting. A stack started with `start.sh` is stopped with `stop.sh`
(`--all` also stops the local Qdrant container and `ollama serve`). The panel's Stop ends with
`ray stop`, so any Ray head on the machine goes down with it.

**Update.**

```bash
git pull
git submodule update            # huri-modules to the commit this repo pins
.venv/bin/pip install -r backend/requirements.txt    # the client SDK at the pinned release
cd huri-modules
./install.sh --only python,config      # new pins / new config keys
# add "models" if the pinned model ids changed, "services" if the Qdrant/Ollama versions did
```

The installer is idempotent: existing venv, weights and containers are reused.

**Remove.** Inside `huri-modules/`: `.huri-local/stop.sh --all`, then `rm -rf .huri-local .venv assets
config/*.generated.yaml`; `docker rm -f huri-qdrant` for the local container (its data was in
`.huri-local/qdrant`); `ollama rm <model>` for pulled models. In this repo: `rm -rf .venv
frontend/node_modules`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Panel: `Could not exec 'serve'`, or logs end with `serve: command not found` | huri-modules is not installed where the site looks (`HURI_MODULES_PATH`). Run `huri-modules/install.sh`, or export `HURI_MODULES_PATH` / `HURI_SERVE_BIN`. |
| Panel: `A HuRI instance is already answering … started outside this backend` | Something already serves `:8000` (a `start.sh` stack, a previous crash). Use it as is, or `huri-modules/.huri-local/stop.sh`. |
| Panel stuck on *starting*, logs show `Path '/' not found … route table is not populated yet` | Normal while models load; TTS warm-up on first start can take minutes. Watch the Ray dashboard. |
| Composer stays at *Connecting to HuRI…* | HuRI is not answering `GET /modules` at `HURI_URL`'s host. Check `curl localhost:8000/modules` and the panel logs. |
| `RAG FAILED during embedding … HTTP 401/403` | The embedding endpoint needs a key HuRI never sends. Serve embeddings from Ollama/llama.cpp/TEI, or a keyless LiteLLM proxy (§5.3). |
| `RAG FAILED during embedding … Connection refused` | Wrong `--embed-url`, or it defaulted to the LLM URL which has no `/v1/embeddings`. Re-run `--only config` with an explicit `--embed-url`. |
| Qdrant call times out although the host is up; looks like a TLS error | Give the scheme explicitly (`https://host` → port 443). Older client versions fell back to 6333 without it. |
| Qdrant: `Vector dimension error: expected dim: 1024, got 768` | The collection was created with another embedding model. Delete `documents`/`conversations` in the Qdrant dashboard and re-ingest, or switch back to the original `--embed-model`. |
| Answers ignore the ingested documents | Ingested under a different `--user-id` than the session's (`anonymous` when auth is off). Re-ingest with `--user-id __shared__`. |
| `Unknown llm_provider` / LLM answers 404 | `--llm-provider` does not match the server: Ollama native is `ollama`, everything OpenAI-shaped is `vllm` (no key) or `api` (key). |
| LLM answers 401 with `--llm-provider vllm` | The endpoint wants a key: `--llm-provider api --llm-api-key …`. |
| LLM 401 only when launched from the panel | `HURI_LLM_API_KEY` did not reach `serve run`: no `huri-modules/.huri-local/env.sh` (manual install) — `source` your `secrets.env` before `run_all.sh` — or a stale Ray head kept its old environment (stop it). |
| Installer: `--llm-url points at … — pass --llm-model` | A remote LLM has no auto-picked tier; name the model the server serves. |
| Installer: `no suitable Python found (need 3.10–3.12)` | Install 3.12 (deadsnakes / pyenv / uv) and pass `--python /path/to/python3.12`. |
| Installer: `torch cannot see the GPU` | Driver too old for CUDA 12.1, or on WSL2 the Windows NVIDIA driver is missing. `nvidia-smi` must work in the same shell. |
| Installer: `no assets/voice.wav — TTS is enabled but has no reference voice` | The installer ran without `--voice-sample`. Default voice: `--only models,config --voice-sample ../assets/voice.wav`; own voice: add `--voice-transcript "…"` (§4.5). |
| TTS speaks a strange sentence before the answer | The transcript ended up before `<|endofprompt|>`; regenerate the config (`--only config`) rather than hand-editing `HURI_VOICE_TRANSCRIPT`. |
| `RuntimeError: HURI_VOICE_TRANSCRIPT is not set` | Launched a hand-written config without that env var; use `huri_local.generated.yaml`. |
| Event Configuration: `HuRI refused the session: … (server modules: …)` | The session asks for a module this server did not enable (`HURI_MODULES` of the launched config); the message lists the ones it has. |
| `ModuleNotFoundError: No module named 'pkg_resources'` at STT start | setuptools ≥ 82 removed it; huri-modules pins `setuptools<82` in `constraints.txt`. `--only python` after updating the submodule. |
| Replica crash: `'FieldDescriptor' object has no attribute 'label'` | protobuf ≥ 5 got installed over the pin; `--only python` reinstalls with `constraints.txt` (`protobuf==4.25.8`). |
| `TypeError: cannot pickle '_thread.lock'` on deploy | FastAPI ≥ 0.139.2 with this Ray version; constraints pin `fastapi<0.139.2`, re-run `--only python`. |
| Ollama: `not answering on :11434` | `ollama serve` is not running (or `OLLAMA_HOST` was changed). Start it; `start.sh` does so automatically for a local install. |
| Avatar never renders; `backend/static/model.fbx` is a ~130-byte text file starting with `version https://git-lfs` | Cloned without Git LFS. `git lfs install && git lfs pull`, then restart the frontend. |
| Presets greyed out in Event Configuration | The module is not in `HURI_MODULES` for this machine (see `huri-modules/.huri-local/plan.env`). Expected on CPU-only / small-VRAM installs; `--force-tts`/`--force-gesture` to override. |
| Second `Start` collides on ports | Only one HuRI per machine: Ray Serve binds 8000/8265. Stop the first one. |

Logs to look at, in order: the Control Panel log tail, the Ray dashboard
(<http://127.0.0.1:8265>, per-deployment logs), `huri-modules/.huri-local/install.log`, and
`huri-modules/.huri-local/{qdrant,ollama}.log` for local services.
