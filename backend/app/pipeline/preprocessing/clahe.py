"""CLAHE: Contrast Limited Adaptive Histogram Equalization.

Equalises lighting across the sheet without altering text shape. The
operation runs only on the L channel of the LAB colour space so colour
balance is preserved (handwritten ink vs. paper).
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

_CLIP_LIMIT = 2.0
_TILE_SIZE = 8


def apply_clahe(
    image: Image.Image,
    *,
    clip_limit: float = _CLIP_LIMIT,
    tile_size: int = _TILE_SIZE,
) -> Image.Image:
    """Return a CLAHE-equalised RGB copy of ``image``.

    ``clip_limit`` higher → more aggressive contrast boost (and noise).
    ``tile_size`` is the side of each adaptive tile; smaller → more
    locality but more block artefacts.
    """
    arr = np.asarray(image.convert("RGB"))
    lab = cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(tile_size, tile_size))
    lightness_eq = clahe.apply(lightness)
    merged = cv2.merge([lightness_eq, a_channel, b_channel])
    rgb_eq = cv2.cvtColor(merged, cv2.COLOR_LAB2RGB)
    return Image.fromarray(rgb_eq)
