# Presets

Drop a `<name>.json` file here to add a module-combination preset to the
Event Configuration modal's dropdown — no server restart needed, the backend
recursively re-scans this folder (and every subfolder, any depth) on every
`GET /presets` request (see `backend/huri_presets.py`).

- The preset's key/label is its path relative to this folder, without the
  `.json` suffix: `F9/rag_only.json` -> `F9/rag_only`. Nesting is purely
  organizational.
- The file content is the `modules` block for that combination: a JSON object
  mapping each tag to its module, in exactly the shape of a HuRI session
  handshake (the `modules:` section of a huri-cli client config, see
  huri-modules' `config/client_template.yaml`) and with huri-modules' module
  names — `vad`, `stt`, `tag`, `emo`, `eag`, `qag`, `rag`, `tts` (Piper) /
  `tts_cosyvoice` (CosyVoice3), `mov` (gestures):

  ```json
  {
    "tag": {
      "name": "module_name",
      "args": { "...": "..." }
    }
  }
  ```

- A malformed file (invalid JSON, or not an object) is skipped and logged as
  a warning by the backend rather than breaking the whole `/presets`
  response.

Speech presets name `tts_cosyvoice`, the engine `huri.yaml` serves. Against a
server that runs Piper instead (`huri_cpu.yaml`, or a generated config on a
machine without the VRAM for CosyVoice), the frontend sends `tts`, the engine
that server lists in `GET /modules` (`frontend/src/modules.js`'s
`withServedTTS`). This is a deliberate exception to sending the presets'
module names as written: a speech preset is therefore never greyed out on a
Piper-only server, and runs there with Piper. F7 and F8 test CosyVoice: run
them with `huri.yaml` started from the Control Panel.

This folder holds client presets only. The HuRI server configs are
huri-modules' own `config/huri*.yaml`, listed by the HuRI Control Panel
straight from the `huri-modules/` checkout.

## `F*/` folders: one per ATP feature

`F2/` through `F11/` mirror the ATP sheet's feature rows: each holds the
preset that feature's Prerequisites column points at. F1 and F3 are pure
HuRI-launch tests with no client, so they have no folder.

Each feature runs on one of huri-modules' Serve configs, launched from the
Control Panel (`huri.yaml` and `huri_cpu.yaml` need huri-modules `0.1.0`'s
runtime fixes: with the submodule's current pin, use the installer's
`huri_local.generated.yaml` instead, see FULL_INSTALLATION.md §6.5):

| Feature | Client preset | Modules | huri-modules config |
|---|---|---|---|
| F1 | — | — | `config/huri.yaml` (GPU, CosyVoice) vs `config/huri_cpu.yaml` (CPU, Piper) |
| F2 | `F2/full_preset.json`, `F2/rag_only.json` | full pipeline / `rag` | `config/huri.yaml` |
| F3 | — | — | `config/huri.yaml` (launched in parallel, no client) |
| F4 | `F4/voice_with_speech.json` | full pipeline | `config/huri.yaml` (several clients on one server) |
| F5, F6 | `F5/voice_to_question.json`, `F6/voice_to_question.json` | `vad`, `stt`, `tag`, `qag` (no emotion) | `config/huri.yaml` |
| F7 | `F7/text_to_voice.json` | `tts_cosyvoice` | `config/huri.yaml` |
| F8 | `F8/text_to_avatar.json` | `tts_cosyvoice`, `mov` | `config/huri.yaml` |
| F9 | `F9/rag_only.json` | `rag` | `config/huri.yaml` |
| F10 | `F10/voice_emo_to_question.json` | `vad` ... `qag` with emotion | `config/huri.yaml` |
| F11 | `F11/voice_to_answer.json` | `vad` ... `rag` | `config/huri.yaml` |

"Full pipeline" is `vad`, `stt`, `tag`, `emo`, `eag`, `qag`, `rag`,
`tts_cosyvoice`, `mov`: mic through speech and gesture.

On a machine where `config/huri.yaml` does not fit (no NVIDIA GPU, not enough
VRAM, remote LLM/Qdrant endpoints), launch `config/huri_local.generated.yaml`
instead: the installer's config for that machine. Presets that need a module
it does not serve are greyed out.

Some features share a preset: `F2/full_preset.json` and
`F4/voice_with_speech.json`, `F2/rag_only.json` and `F9/rag_only.json`,
`F5/` and `F6/voice_to_question.json` are identical copies, so each folder is
self-contained for a tester following the sheet. Change them together:
`backend/tests/test_presets.py` fails when they differ.

Naming: each name says what the preset *tests*, not just which modules it
contains — `rag_only` (text in, text out), `voice_with_speech` (the full
voice pipeline: mic through TTS/gesture), `voice_to_question` (voice in,
question out), `text_to_voice` / `text_to_avatar` (direct token -> speech /
gesture, bypassing RAG).
