"""
Parse HuRI gesture (motion) output into avatar animation frames.

HuRI runs all inference (RAG, TTS, EMAGE gesture) remotely; the backend only
receives the resulting ``huri.motion`` events over its HuRI session, turned
into raw arrays by huri_bridge.motion_message and reshaped here via main.py's
``_transform_outbound``. This module turns one chunk's raw arrays into the
frame format the frontend consumes — no models, no torch, just numpy + scipy.
"""

import numpy as np
from scipy.spatial.transform import Rotation

# SMPL-X joint names (55 joints; indices match poses[:, i*3:(i+1)*3]).
# 0-21: body. 22: jaw. 23-24: eyes. 25-39: left hand (MANO order). 40-54: right hand.
SMPLX_JOINT_NAMES = [
    "pelvis", "left_hip", "right_hip", "spine1",
    "left_knee", "right_knee", "spine2",
    "left_ankle", "right_ankle", "spine3",
    "left_foot", "right_foot", "neck",
    "left_collar", "right_collar", "head",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "jaw", "left_eye", "right_eye",
    "left_index1", "left_index2", "left_index3",
    "left_middle1", "left_middle2", "left_middle3",
    "left_pinky1", "left_pinky2", "left_pinky3",
    "left_ring1", "left_ring2", "left_ring3",
    "left_thumb1", "left_thumb2", "left_thumb3",
    "right_index1", "right_index2", "right_index3",
    "right_middle1", "right_middle2", "right_middle3",
    "right_pinky1", "right_pinky2", "right_pinky3",
    "right_ring1", "right_ring2", "right_ring3",
    "right_thumb1", "right_thumb2", "right_thumb3",
]

EMAGE_FPS = 30

# FLAME expression coefficient index -> avatar blendshape name. The first 10
# expression channels map onto the morph targets the frontend rig exposes.
EXPR_BLENDSHAPE_NAMES = [
    "eyeBlinkLeft", "eyeBlinkRight",
    "jawOpen", "mouthSmileLeft", "mouthSmileRight",
    "browDownLeft", "browDownRight",
    "cheekPuff", "noseSneerLeft", "noseSneerRight",
]


def _rotvec_to_quat(rotvec: np.ndarray) -> list:
    """Convert a 3-element axis-angle rotation vector to [qx, qy, qz, qw]."""
    return Rotation.from_rotvec(rotvec).as_quat().tolist()  # scipy returns [x, y, z, w]


def _expr_to_blendshapes(expr_row) -> dict:
    """Map one row of FLAME expression coefficients to avatar blendshapes."""
    blendshapes = {}
    for k, bs_name in enumerate(EXPR_BLENDSHAPE_NAMES):
        if k < len(expr_row):
            # FLAME coeffs are roughly in [-3, 3]; normalise to [0, 1].
            blendshapes[bs_name] = float(np.clip((expr_row[k] + 3) / 6, 0, 1))
    return blendshapes


def motion_arrays_to_frames(poses, expressions, trans, pts: float, fps: int) -> list:
    """Convert one streamed gesture chunk into avatar animation frames.

    Args:
        poses:       (n, 165) SMPL-X axis-angle, 55 joints x 3.
        expressions: (n, 100) FLAME expression coefficients.
        trans:       (n, 3)   global root translation (metres).
        pts:         presentation timestamp of the chunk start, in seconds.
        fps:         frames per second of the motion chunk.

    Returns a list of ``{t, rotations, blendshapes, positions}`` frames whose
    ``t`` is absolute (``pts + i/fps``) so they line up with the audio clock.
    """
    poses = np.asarray(poses)
    n = poses.shape[0]
    expressions = np.asarray(expressions) if expressions is not None else None
    trans = np.asarray(trans) if trans is not None else None
    fps = fps or EMAGE_FPS

    frames = []
    for i in range(n):
        rotations = {
            name: _rotvec_to_quat(poses[i, j * 3: j * 3 + 3])
            for j, name in enumerate(SMPLX_JOINT_NAMES)
            if j * 3 + 3 <= poses.shape[1]
        }

        blendshapes = {}
        if expressions is not None and i < expressions.shape[0]:
            blendshapes = _expr_to_blendshapes(expressions[i])

        positions = {}
        if trans is not None and i < trans.shape[0]:
            # The frontend only tracks the pelvis for root translation.
            positions["pelvis"] = [float(v) for v in trans[i]]

        frames.append({
            "t": pts + i / fps,
            "rotations": rotations,
            "blendshapes": blendshapes,
            "positions": positions,
        })
    return frames
