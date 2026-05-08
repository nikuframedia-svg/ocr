"""Deskew: detect a small rotation via Hough lines on the table grid
and rotate the image to put the long edges of the table back to
horizontal.

The Kanban form has a strong rectangular grid that gives a very clean
Hough signal. We trim the search to angles within ``MAX_ANGLE`` of
horizontal so we do not accidentally "deskew" a 30° tilt into a 60° one.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

_MAX_ANGLE_DEG = 5.0
_MIN_ANGLE_DEG = 0.2  # don't rotate noise


def deskew(image: Image.Image, *, max_angle: float = _MAX_ANGLE_DEG) -> Image.Image:
    """Rotate ``image`` to undo a small skew detected via Hough lines.

    Returns the original image unchanged if no near-horizontal lines are
    found or the detected rotation is below ``_MIN_ANGLE_DEG``.
    """
    rgb_arr = np.asarray(image.convert("RGB"))
    grey = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(grey, 50, 150, apertureSize=3)
    height, width = grey.shape
    # Threshold proportional to image width so the call works across
    # 1280-px and 600-px inputs.
    hough_threshold = max(120, width // 8)
    lines = cv2.HoughLines(edges, 1, np.pi / 720, hough_threshold)
    if lines is None or len(lines) == 0:
        return image

    angles_from_horizontal: list[float] = []
    for line in lines[:200]:
        _rho, theta = line[0]
        # theta is the angle of the normal to the line, measured from x-axis.
        # A perfectly horizontal line has theta == pi/2; we want the deviation.
        deviation_deg = np.degrees(theta) - 90.0
        if abs(deviation_deg) <= max_angle:
            angles_from_horizontal.append(deviation_deg)

    if not angles_from_horizontal:
        return image

    rotation_deg = float(np.median(angles_from_horizontal))
    if abs(rotation_deg) < _MIN_ANGLE_DEG:
        return image

    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), rotation_deg, 1.0)
    rotated = cv2.warpAffine(
        rgb_arr, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE
    )
    return Image.fromarray(rotated)
