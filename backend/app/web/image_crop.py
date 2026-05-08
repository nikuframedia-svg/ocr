"""Auto-crop kanban photo: detect paper boundary, perspective-correct.

Round 46 — when a phone photo is uploaded, automatically detect the paper
rectangle within the photo, perspective-warp it to A4 landscape, and save
as a sibling ``_cropped.jpg`` next to the original. The /image/{id}
route serves the cropped version when present, falling back to original.

Pipeline mirrors scripts/scan_kanban.py (Round 14) but as a callable
library function instead of a CLI script. Detection-failure: silent
no-op (no cropped file) so /image route falls back to original.

Style: ``color`` (CLAHE on L channel) — keeps colour fidelity for the
review UI, doesn't binarize (which would destroy handwriting nuances).
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

# A4 landscape at 150 DPI
_A4_W = 1754
_A4_H = 1240


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Sort 4 (x,y) points: TL, TR, BR, BL."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).flatten()
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    rect[1] = pts[np.argmin(d)]
    rect[3] = pts[np.argmax(d)]
    return rect


def _detect_paper(image: np.ndarray) -> np.ndarray | None:
    h, w = image.shape[:2]
    scale = 1500 / max(h, w) if max(h, w) > 1500 else 1.0
    small = (cv2.resize(image, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_AREA)
             if scale < 1.0 else image.copy())
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(gray, 50, 150)
    edged = cv2.dilate(edged, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    img_area = small.shape[0] * small.shape[1]
    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * 0.20:
            continue
        peri = cv2.arcLength(c, closed=True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, closed=True)
        if len(approx) == 4:
            corners = approx.reshape(4, 2).astype(np.float32) / scale
            return _order_corners(corners)
    return None


def _warp_to_a4(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Warp paper region into A4 LANDSCAPE (1754×1240).

    Kanban sheets in this system are always landscape. If the photo was
    taken with phone held portrait, the detected paper rect will be
    portrait-shaped — we still warp to landscape, rotating the corner
    mapping so output content is correctly oriented.
    """
    tl, tr, br, bl = corners
    width_top = np.linalg.norm(tr - tl)
    width_bot = np.linalg.norm(br - bl)
    height_l = np.linalg.norm(bl - tl)
    height_r = np.linalg.norm(br - tr)
    detected_w = max(width_top, width_bot)
    detected_h = max(height_l, height_r)

    # Always output landscape A4. If detected paper is portrait-shaped
    # (phone held vertically), rotate the corner mapping by 90° so the
    # content lands oriented correctly.
    out_w, out_h = _A4_W, _A4_H
    if detected_w >= detected_h:
        # Detected landscape — direct mapping
        dst = np.array([[0, 0], [out_w - 1, 0],
                        [out_w - 1, out_h - 1], [0, out_h - 1]],
                       dtype=np.float32)
    else:
        # Detected portrait — rotate corners 90° CW so portrait paper
        # warps into landscape canvas. Equivalent to mapping
        # TL→TR, TR→BR, BR→BL, BL→TL of the destination.
        dst = np.array([[out_w - 1, 0], [out_w - 1, out_h - 1],
                        [0, out_h - 1], [0, 0]],
                       dtype=np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    return cv2.warpPerspective(image, M, (out_w, out_h))


def _enhance_color(warped: np.ndarray) -> np.ndarray:
    """CLAHE on L channel — even out lighting, preserve colour."""
    lab = cv2.cvtColor(warped, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def auto_crop(src_path: Path, out_path: Path | None = None) -> Path | None:
    """Detect kanban paper in src image, warp to A4 landscape, save next to src.

    Returns out_path on success, None if detection failed (caller falls
    back to original image). Does not raise — silent no-op for edge cases.
    """
    if out_path is None:
        out_path = src_path.with_name(src_path.stem + "_cropped.jpg")
    try:
        image = cv2.imread(str(src_path))
        if image is None:
            return None
        # Apply EXIF orientation if any (phone photos may need it)
        # OpenCV doesn't auto-apply EXIF; do it via PIL roundtrip for safety
        from PIL import Image, ImageOps
        with Image.open(src_path) as pil:
            pil = ImageOps.exif_transpose(pil)
            if pil.mode != "RGB":
                pil = pil.convert("RGB")
            arr = np.array(pil)
            image = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

        corners = _detect_paper(image)
        if corners is None:
            return None
        warped = _warp_to_a4(image, corners)
        enhanced = _enhance_color(warped)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out_path), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return out_path
    except Exception:  # noqa: BLE001
        # Defensive: any OpenCV/PIL/IO error → fall back to original
        return None


def cropped_path_for(src_path: Path) -> Path:
    """Convention for cropped sibling — used by /image/{id} route."""
    return src_path.with_name(src_path.stem + "_cropped.jpg")
