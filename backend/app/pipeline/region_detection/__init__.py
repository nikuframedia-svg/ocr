"""Region detection: split a Kanban photo into header / table / footer crops.

Phase 3 of the briefing. Today this is a deterministic percentage-based
splitter — works because the form layout is consistent across the 19
sheets. When the dataset grows or sheets show heavier rotation /
perspective, swap the implementation for template matching or YOLOv8
without changing the public API in :mod:`crops`.
"""

from app.pipeline.region_detection.crops import Crops, split_kanban

__all__ = ["Crops", "split_kanban"]
