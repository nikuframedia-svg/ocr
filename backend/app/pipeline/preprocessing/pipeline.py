"""Phase 0.5 preprocessing orchestrator."""
from __future__ import annotations

from PIL import Image

from app.pipeline.preprocessing.clahe import apply_clahe
from app.pipeline.preprocessing.deskew import deskew


def preprocess(image: Image.Image) -> Image.Image:
    """Apply the Phase 0.5 pipeline: deskew first (cheap when there is
    no skew), then CLAHE.

    Order matters: CLAHE before deskew would boost contrast on the
    rotated edges that the rotation is about to discard.
    """
    image = deskew(image)
    image = apply_clahe(image)
    return image
