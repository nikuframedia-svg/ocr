"""Phase 0.5 image preprocessing: deskew, CLAHE.

Applied just before the image is encoded for the inference engine.
Designed to be cheap (a few OpenCV calls) and conservative — VLMs are
trained on natural images, so aggressive thresholding/binarisation
typically *hurts* accuracy. We only correct lighting (CLAHE) and small
rotations (deskew).
"""

from app.pipeline.preprocessing.pipeline import preprocess

__all__ = ["preprocess"]
