"""Named module-combination presets for the Event Configuration modal.

Each preset is a ``{tag: {"name": <module>, "args": {...}}}`` block — exactly
the ``modules`` block of a HuRI session handshake (the ``modules:`` section of
a huri-cli client config), with huri-modules' module names (``vad``, ``stt``,
``rag``, ``tts``, ``mov``...). ``GET /presets`` serves them renamed for the
frontend (``vad`` -> ``mic``, see huri_bridge.py), which the browser's
handshake then sends back and huri_bridge.py maps to HuRI again. The
frontend lets a tester pick one of these as a starting point (or build a
custom combination) when configuring a session, covering HuRI/ATP.xlsx F1/F2
(different module combinations) without needing a server restart.

Presets themselves live as ``<name>.json`` files under the repo-root
``presets/`` folder (see that folder's README), one preset per file, keyed by
their path relative to that folder (subfolders included — see below).
``load_presets()`` re-scans the folder tree on every call, so dropping a new
file in anywhere under it — or editing/removing one — takes effect on the
next ``GET /presets`` request with no server restart, and no re-import of
this module. Override the folder with the ``HURI_PRESETS_DIR`` env var (e.g.
to point at a mounted volume in a container where the repo root isn't
present — see backend/Dockerfile's build-context note).

See huri-modules' config/client_template.yaml for the shape.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from huri_bridge import to_browser_modules

logger = logging.getLogger(__name__)

Preset = Dict[str, Dict[str, Any]]

# Repo root is this file's grandparent (backend/huri_presets.py -> backend/ -> repo root).
_DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
PRESETS_DIR = Path(os.environ.get("HURI_PRESETS_DIR", str(_DEFAULT_PRESETS_DIR)))


def load_presets(
    presets_dir: Path = PRESETS_DIR, browser_names: bool = False
) -> Dict[str, Preset]:
    """Recursively scan ``presets_dir`` for ``*.json`` files (any depth of
    subfolders) and return ``{relative_path_without_suffix: preset}``, sorted
    by that key — e.g. ``presets/F5/voice_with_speech.json`` becomes
    ``F5/voice_with_speech``. Subfolders are just an organizational convenience
    for whoever maintains the presets; nesting carries no meaning to the
    backend or frontend beyond the resulting key.

    Called fresh on every ``/presets`` request (see main.py) rather than
    cached at import time, so new/edited/removed preset files anywhere under
    the tree are picked up immediately. A preset file that fails to parse
    (bad JSON, or JSON that isn't an object) is skipped with a logged warning
    instead of failing the whole endpoint.

    ``browser_names`` renames the modules for the frontend (see
    huri_bridge.to_browser_modules); the default returns the files as is.
    """
    presets: Dict[str, Preset] = {}

    if not presets_dir.is_dir():
        logger.warning("Presets directory %s does not exist; no presets loaded.", presets_dir)
        return presets

    paths = sorted(
        presets_dir.rglob("*.json"),
        key=lambda p: p.relative_to(presets_dir).as_posix(),
    )
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as f:
                preset = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping invalid preset file %s: %s", path, exc)
            continue

        if not isinstance(preset, dict):
            logger.warning(
                "Skipping preset file %s: expected a JSON object mapping tag -> module, got %s.",
                path,
                type(preset).__name__,
            )
            continue

        key = path.relative_to(presets_dir).with_suffix("").as_posix()
        presets[key] = to_browser_modules(preset) if browser_names else preset

    return presets
