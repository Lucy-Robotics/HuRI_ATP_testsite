# Presets

Drop a `<name>.json` file here to add a module-combination preset to the
Event Configuration modal's dropdown — no server restart needed, the backend
recursively re-scans this folder (and every subfolder, any depth) on every
`GET /presets` request (see `backend/huri_presets.py`).

- The preset's key/label is its path relative to this folder, without the
  `.json` suffix, e.g. `rag_only.json` -> `rag_only`. Presets can be
  organized into subfolders for convenience — a file at
  `demo/no_gesture.json` becomes the key `demo/no_gesture`. Nesting is purely
  organizational; it carries no meaning to the backend or frontend beyond the
  resulting key.
- The file content is the `modules` block for that combination: a JSON object
  mapping each event tag to its module, in exactly the shape of a HuRI session
  handshake (the `modules:` section of a huri-cli client config) and with
  huri-modules' module names — `vad`, `stt`, `tag`, `emo`, `eag`, `qag`, `rag`,
  `tts` (Piper) / `tts_cosyvoice` (CosyVoice3), `mov` (gestures):

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

The frontend still names modules as the HuRI monorepo did (`mic`, `gesture`):
`GET /presets` serves these files renamed for it, and the backend maps the
browser's handshake back (see `backend/huri_bridge.py`). Write `tts` for speech:
the backend sends whichever TTS engine the running server enabled (`tts`, or
`tts_cosyvoice` when only CosyVoice is there).

See the `F*/` files for real examples, and huri-modules'
`config/client_template.yaml` for the shape.

## HuRI server configs (`huri*.yaml`)

A folder can also hold a *copy* of one of huri-modules' `config/huri*.yaml`
(the `huri-modules/` submodule) — the server-side Ray Serve config `serve run`
launches HuRI with, as opposed to
the client `modules` presets above. `backend/huri_launcher.py` scans this
folder tree for `huri*.yaml` the same way `huri_presets.py` scans it for
`*.json`, and offers them in the HuRI Control Panel's launch dropdown keyed
by their path here (e.g. `F1/huri_cpu.yaml`), alongside the bare
`huri-modules/config/huri*.yaml` files (keyed by plain filename). Same rule as
the client presets: these are copies for the tester's convenience, not the
source of truth — change huri-modules' `config/huri.yaml` for a real change,
then re-copy it into whichever `Fx/` folders reference it (they are copies of
the pinned submodule's files).

They are huri-core 0.4 Serve configs: `import_path: huri_core.app:app`, the
enabled modules in the `HURI_MODULES` env var of `runtime_env` (default-deny:
a module not listed there is not served), one deployment per module with a
shared actor named after its `*Handle` class (`STTHandle`, `EMOHandle`,
`RAGHandle`, `TTSHandle` / `CosyVoiceTTSHandle`, `MOVHandle`) next to the
`HuRI` ingress, and `HURI_ACTUATION_MODULES` to opt in a module that can drive
hardware (no curated module does, so none of these sets it). `huri.yaml` runs
CosyVoice (`tts_cosyvoice`, NVIDIA GPU), `huri_cpu.yaml` Piper (`tts`, CPU).

## `F*/` folders

`F1/` through `F11/` mirror the ATP sheet's (ATP.xlsx) feature rows one-to-one — each
holds a *copy* of whichever preset and/or HuRI config that feature's
Prerequisites column points at (see the sheet's `presets/Fx/...` paths), so a
tester can follow the spreadsheet straight to a real file without
cross-referencing which top-level file it happens to reuse. F1 and F3 hold
only a `huri*.yaml` (no client `modules` preset — neither feature involves a
client, they're pure HuRI-launch tests).

These are deliberate duplicates, not the source of truth — if you change a
module combination that's shared across features (e.g. `voice_with_speech`)
or a HuRI config (`huri.yaml`, `huri_cpu.yaml`), update every copy listed
below, or the dropdown/ATP will drift out of sync:

| Folder | Client preset | Modules | HuRI config copy of | Used because |
|---|---|---|---|---|
| `F1/` | — | — | `huri.yaml`, `huri_cpu.yaml` | GPU vs CPU deployment settings to compare |
| `F2/` | `full_preset.json`, `rag_only.json` | full pipeline / `rag` | `huri.yaml` | two configs to compare (Configure modules) |
| `F3/` | — | — | `huri.yaml` | run-in-parallel check needs no client |
| `F4/` | `voice_with_speech.json` | full pipeline | `huri.yaml` | same config, launched twice (multi-client) |
| `F5/`, `F6/` | `voice_to_question.json` | `vad`, `stt`, `tag`, `qag` (no emotion) | `huri.yaml` | needs mic + stt |
| `F7/` | `text_to_voice.json` | `tts` | `huri.yaml` | typed tokens (rag.out) straight to speech |
| `F8/` | `text_to_avatar.json` | `tts`, `mov` | `huri.yaml` | typed tokens straight to speech + gesture |
| `F9/` | `rag_only.json` | `rag` | `huri.yaml` | rag only, no voice needed |
| `F10/` | `voice_emo_to_question.json` | `vad` ... `qag` with emotion | `huri.yaml` | transcript + linked emotion, no reply |
| `F11/` | `voice_to_answer.json` | `vad` ... `rag` | `huri.yaml` | emotion/transcription checks with a text answer, no tts/gesture output needed |

"Full pipeline" is `vad`, `stt`, `tag`, `emo`, `eag`, `qag`, `rag`, `tts`, `mov`:
mic through speech and gesture.

Naming: each name says what the preset *tests*, not just which modules it
contains — `rag_only` (text in, text out), `voice_with_speech` (the full
voice pipeline: mic through TTS/gesture), `voice_to_question` (voice in,
question out), `text_to_voice` / `text_to_avatar` (direct token -> speech /
gesture, bypassing RAG).
