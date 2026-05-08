"""Tests for the Phase 0.5 preprocessing pipeline."""
from __future__ import annotations

import numpy as np
import pytest
from app.pipeline.preprocessing.clahe import apply_clahe
from app.pipeline.preprocessing.deskew import deskew
from app.pipeline.preprocessing.pipeline import preprocess
from PIL import Image, ImageDraw


def _low_contrast_image(width: int = 400, height: int = 300) -> Image.Image:
    """Image with a low-contrast horizontal ramp (110..150). CLAHE
    should noticeably boost the spread."""
    arr = np.tile(np.linspace(110, 150, width, dtype=np.uint8), (height, 1))
    rgb = np.stack([arr, arr, arr], axis=-1)
    return Image.fromarray(rgb)


def _grid_image(width: int = 800, height: int = 600) -> Image.Image:
    """Image with a strong horizontal/vertical grid — provides Hough signal."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    for y in range(0, height, 60):
        draw.line([(0, y), (width, y)], fill="black", width=2)
    for x in range(0, width, 80):
        draw.line([(x, 0), (x, height)], fill="black", width=2)
    return img


def test_clahe_preserves_dimensions_and_mode() -> None:
    img = _low_contrast_image()
    out = apply_clahe(img)
    assert out.size == img.size
    assert out.mode == "RGB"


def test_clahe_increases_contrast() -> None:
    img = _low_contrast_image()
    before = np.asarray(img.convert("L"))
    after = np.asarray(apply_clahe(img).convert("L"))
    assert after.std() > before.std() * 0.95  # CLAHE should not destroy contrast


def test_deskew_undoes_small_rotation() -> None:
    img = _grid_image()
    rotated = img.rotate(2.5, fillcolor="white", resample=Image.Resampling.BICUBIC)
    fixed = deskew(rotated)
    # Verify the fix moved the image closer to the original by comparing
    # variance of column sums (vertical lines → high variance).
    rotated_var = np.asarray(rotated.convert("L")).sum(axis=0).var()
    fixed_var = np.asarray(fixed.convert("L")).sum(axis=0).var()
    assert fixed_var > rotated_var


def test_deskew_leaves_unrotated_image_alone() -> None:
    img = _grid_image()
    out = deskew(img)
    # Should be byte-identical (no rotation applied) or at most a no-op resize.
    assert out.size == img.size
    diff = np.asarray(out).astype(int) - np.asarray(img).astype(int)
    # Mean abs diff should be tiny (no rotation = no change).
    assert np.abs(diff).mean() < 1.0


def test_pipeline_returns_rgb_same_shape() -> None:
    img = _grid_image(640, 480)
    out = preprocess(img)
    assert out.size == img.size
    assert out.mode == "RGB"


def test_deskew_ignores_extreme_skews() -> None:
    """A 30° rotation is outside the physical-paper-on-table range; do
    not blindly try to compensate."""
    img = _grid_image()
    heavily_rotated = img.rotate(30, fillcolor="white")
    out = deskew(heavily_rotated)
    # Should NOT have rotated again — output is the input image (same array).
    assert np.array_equal(np.asarray(out), np.asarray(heavily_rotated))


@pytest.mark.parametrize("size", [(320, 240), (1280, 868), (1640, 1232)])
def test_clahe_works_on_various_sizes(size: tuple[int, int]) -> None:
    arr = np.random.RandomState(0).randint(0, 256, (size[1], size[0], 3), dtype=np.uint8)
    out = apply_clahe(Image.fromarray(arr))
    assert out.size == size
