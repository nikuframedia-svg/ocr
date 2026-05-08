"""Data Quality module — L1 (validation) → L2 (snap) → L3 (constraints).

Orchestrates post-extraction validation, lexicon-aware auto-fix, and
constraint-based anomaly flagging. Produces an audit trail per cell so
every automatic fix is traceable and every human-review flag has a
justified confidence score.
"""

__version__ = "0.1.0"
