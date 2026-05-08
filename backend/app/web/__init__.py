"""MVP web app — capture (mobile) → review/edit → dashboard.

Single-process FastAPI service that wraps the v9 OCR pipeline
(``ocr6.py``) + DQ Module (``app.dq.pipeline``). Runs synchronously,
SQLite for state, Jinja2 templates with HTMX for inline editing.
"""
