"""Phone-photo → scan-look A4 converter for Kanban sheets.

Detects the paper boundary, corrects perspective, normalises lighting,
and exports a printable A4 landscape JPEG + PDF.

Usage:
    python scripts/scan_kanban.py inputs/originais/VitorCarvalho_2026.04.14.jpeg
    python scripts/scan_kanban.py <image> --style bw|color|enhanced --out data/scans

Outputs:
    <out>/<stem>_scan.jpg   — 1754x1240 px @ 150 DPI A4 landscape
    <out>/<stem>_scan.pdf   — single-page A4 landscape

Pipeline (OpenCV):
    1. Load + downscale to 1500 px max for fast detection
    2. Grayscale + Gaussian blur to suppress floor texture
    3. Canny edge → findContours → largest 4-corner polygon = paper
    4. Order corners (TL, TR, BR, BL)
    5. Perspective transform to A4 landscape rectangle
    6. Style enhancement (bw/color/enhanced)
    7. Save JPEG (q=95) + PDF (PIL Image.save format='PDF')

Fallback if 4-corner detection fails: emit warning + crop centre 95 %.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# A4 landscape at 150 DPI
A4_W = 1754  # 297mm × 150 / 25.4
A4_H = 1240  # 210mm × 150 / 25.4


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Sort 4 (x,y) points: TL, TR, BR, BL by sum/diff of coords."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).flatten()
    rect[0] = pts[np.argmin(s)]   # TL: smallest sum
    rect[2] = pts[np.argmax(s)]   # BR: largest sum
    rect[1] = pts[np.argmin(d)]   # TR: smallest diff (x-y)
    rect[3] = pts[np.argmax(d)]   # BL: largest diff
    return rect


def _detect_paper(image: np.ndarray) -> np.ndarray | None:
    """Return 4 ordered corners of the paper, or None if not found."""
    h, w = image.shape[:2]
    scale = 1500 / max(h, w) if max(h, w) > 1500 else 1.0
    small = cv2.resize(image, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA) if scale < 1.0 else image.copy()

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(gray, 50, 150)
    # Dilate so paper edges are continuous
    edged = cv2.dilate(edged, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
    img_area = small.shape[0] * small.shape[1]
    for c in contours:
        area = cv2.contourArea(c)
        if area < img_area * 0.20:  # paper should fill ≥20% of frame
            continue
        peri = cv2.arcLength(c, closed=True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, closed=True)
        if len(approx) == 4:
            corners = approx.reshape(4, 2).astype(np.float32) / scale
            return _order_corners(corners)
    return None


def _warp_to_a4(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Perspective-correct the paper region into A4 landscape."""
    # Decide orientation from corner shape (landscape if width > height)
    tl, tr, br, bl = corners
    width_top = np.linalg.norm(tr - tl)
    width_bot = np.linalg.norm(br - bl)
    height_l = np.linalg.norm(bl - tl)
    height_r = np.linalg.norm(br - tr)
    detected_w = max(width_top, width_bot)
    detected_h = max(height_l, height_r)

    if detected_w >= detected_h:
        out_w, out_h = A4_W, A4_H  # landscape
    else:
        out_w, out_h = A4_H, A4_W  # portrait — kanban paper is landscape so usually first branch

    dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]],
                   dtype=np.float32)
    M = cv2.getPerspectiveTransform(corners, dst)
    warped = cv2.warpPerspective(image, M, (out_w, out_h))
    return warped


def _style_bw(warped: np.ndarray) -> np.ndarray:
    """Black-and-white scan look via adaptive threshold."""
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    # Slight blur to suppress noise before thresholding
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    thr = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, blockSize=21, C=10,
    )
    return cv2.cvtColor(thr, cv2.COLOR_GRAY2BGR)


def _style_color(warped: np.ndarray) -> np.ndarray:
    """CLAHE on L channel — preserves colour, evens out lighting."""
    lab = cv2.cvtColor(warped, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)


def _style_enhanced(warped: np.ndarray) -> np.ndarray:
    """CLAHE grayscale + sharpening — best for printing handwriting."""
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    g = clahe.apply(gray)
    # Unsharp mask: blur + weighted subtract
    blurred = cv2.GaussianBlur(g, (0, 0), sigmaX=2.0)
    sharp = cv2.addWeighted(g, 1.5, blurred, -0.5, 0)
    return cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)


_STYLES = {"bw": _style_bw, "color": _style_color, "enhanced": _style_enhanced}


def _fallback_crop(image: np.ndarray) -> np.ndarray:
    """If detection fails: crop 5 % from each border + warp to A4."""
    h, w = image.shape[:2]
    pad_x, pad_y = int(w * 0.05), int(h * 0.05)
    cropped = image[pad_y:h - pad_y, pad_x:w - pad_x]
    return cv2.resize(cropped, (A4_W, A4_H), interpolation=cv2.INTER_AREA)


def scan(image_path: Path, out_dir: Path, style: str = "enhanced") -> tuple[Path, Path]:
    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError(f"cannot read image: {image_path}")

    corners = _detect_paper(image)
    if corners is None:
        print(f"  [WARN] paper boundary not detected; using fallback crop", file=sys.stderr)
        warped = _fallback_crop(image)
    else:
        warped = _warp_to_a4(image, corners)

    enhanced = _STYLES[style](warped)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem
    jpg_path = out_dir / f"{stem}_scan.jpg"
    pdf_path = out_dir / f"{stem}_scan.pdf"

    # JPEG via OpenCV (quality 95)
    cv2.imwrite(str(jpg_path), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 95])

    # PDF via Pillow (single-page A4 landscape, 150 DPI)
    rgb = cv2.cvtColor(enhanced, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    pil.save(pdf_path, format="PDF", resolution=150.0)

    return jpg_path, pdf_path


_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp"}


def _merge_pdfs(jpg_paths: list[Path], merged_path: Path) -> None:
    """Combine multiple processed JPEGs into one multi-page A4-landscape PDF."""
    if not jpg_paths:
        return
    pages = [Image.open(p).convert("RGB") for p in jpg_paths]
    pages[0].save(
        merged_path, format="PDF", resolution=150.0,
        save_all=True, append_images=pages[1:],
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Phone photo → scan-look A4 PDF")
    p.add_argument("path", type=Path, help="image file OR folder of images")
    p.add_argument("--out", type=Path, default=Path("data/scans"), help="output directory")
    p.add_argument("--style", choices=list(_STYLES), default="enhanced",
                   help="bw=high-contrast scan, color=preserve colour, enhanced=sharpened grayscale (default)")
    p.add_argument("--merged", type=Path, default=None,
                   help="when processing a folder, also save a combined multi-page PDF here")
    args = p.parse_args()

    if not args.path.exists():
        print(f"error: {args.path} not found", file=sys.stderr)
        return 1

    # Single image
    if args.path.is_file():
        print(f"Scanning {args.path.name} (style={args.style})...")
        jpg, pdf = scan(args.path, args.out, style=args.style)
        print(f"  → {jpg}")
        print(f"  → {pdf}")
        return 0

    # Folder
    images = sorted(p for p in args.path.iterdir() if p.suffix.lower() in _IMG_EXTS)
    if not images:
        print(f"no images found in {args.path}", file=sys.stderr)
        return 1

    print(f"Scanning {len(images)} images from {args.path} (style={args.style})...")
    jpg_paths: list[Path] = []
    for i, img in enumerate(images, 1):
        try:
            jpg, _ = scan(img, args.out, style=args.style)
            jpg_paths.append(jpg)
            print(f"  [{i}/{len(images)}] {img.name} → {jpg.name}")
        except Exception as e:
            print(f"  [{i}/{len(images)}] {img.name} FAILED: {e}", file=sys.stderr)

    print()
    print(f"Wrote {len(jpg_paths)} individual JPEGs + PDFs to {args.out}")

    if args.merged:
        args.merged.parent.mkdir(parents=True, exist_ok=True)
        _merge_pdfs(jpg_paths, args.merged)
        print(f"Merged PDF: {args.merged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
