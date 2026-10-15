"""Named module-combination presets for the Event Configuration modal.

Each preset is a ``{tag: {"name": <module>, "args": {...}}}`` block: the
``modules`` block of a HuRI session handshake (the ``modules:`` section of a
huri-cli client config), with huri-modules' module names. See
``presets/README.md`` for the layout and huri-modules'
``config/client_template.yaml`` for the shape.

Presets live as ``<name>.json`` files under the repo-root ``presets/`` folder,
keyed by their path relative to it. ``load_presets()`` re-scans the tree on
every call, so a file added, edited or removed shows up on the next
``GET /presets`` with no restart. ``HURI_PRESETS_DIR`` overrides the folder
(e.g. a mounted volume in a container, see backend/Dockerfile).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

Preset = Dict[str, Dict[str, Any]]

_DEFAULT_PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
PRESETS_DIR = Path(os.environ.get("HURI_PRESETS_DIR", str(_DEFAULT_PRESETS_DIR)))


def load_presets(presets_dir: Path = PRESETS_DIR) -> Dict[str, Preset]:
    """Every ``*.json`` under ``presets_dir`` (any depth) as
    ``{relative_path_without_suffix: preset}``, sorted by key:
    ``presets/F4/voice_with_speech.json`` is ``F4/voice_with_speech``.

    A file that is not valid JSON, or not a JSON object, is skipped with a
    logged warning instead of failing the whole endpoint.
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

        presets[path.relative_to(presets_dir).with_suffix("").as_posix()] = preset

    return presets
