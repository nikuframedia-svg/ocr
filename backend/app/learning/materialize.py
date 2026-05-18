"""Project approved learnings into ``lexicons/learned_overlay.json``.

The overlay is a small JSON file the OCR pipeline loads *on top of* the
static lexicons. The static files (``learned.json``, ``sap_plan_mined``,
``observed.json``) are never modified — only this overlay grows.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from app.web import db

_REPO = Path(__file__).resolve().parents[3]
OVERLAY_PATH = _REPO / "lexicons" / "learned_overlay.json"


def build_overlay() -> dict:
    """Build the overlay dict from all ``status='approved'`` learnings."""
    overlay: dict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "cliente_aliases": {},
        "operador_aliases": {},
        "modelo_aliases": {},
        "confusion_overlay": {},
        "clientes_observed": [],
        "modelos_observed": [],
        "fewshot_by_template": {},
    }
    with db.conn() as c:
        rows = c.execute(
            "SELECT kind, field, template_name, payload FROM learnings "
            "WHERE status = 'approved'"
        ).fetchall()

    for r in rows:
        try:
            p = json.loads(r["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        kind = r["kind"]
        if kind in ("cliente_alias", "operador_alias", "modelo_alias"):
            frm, to = p.get("from"), p.get("to")
            if frm and to:
                overlay[kind + "es"][frm] = to
        elif kind == "confusion_pair":
            gc, oc = p.get("gold_char"), p.get("ocr_char")
            if gc and oc:
                overlay["confusion_overlay"].setdefault(gc, {})[oc] = (
                    p.get("weight") or 0.5
                )
        elif kind == "observed_value":
            value = p.get("value")
            if value and p.get("field") == "cliente":
                overlay["clientes_observed"].append(value)
            elif value and p.get("field") == "modelo":
                overlay["modelos_observed"].append(value)
        elif kind == "fewshot_example":
            tname = p.get("template_name") or r["template_name"]
            sid = p.get("sheet_id")
            if tname and sid is not None:
                overlay["fewshot_by_template"].setdefault(tname, []).append(sid)

    return overlay


def materialize_overlay() -> dict:
    """Build the overlay and write it atomically to :data:`OVERLAY_PATH`."""
    overlay = build_overlay()
    OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERLAY_PATH.with_name(OVERLAY_PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(overlay, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, OVERLAY_PATH)
    return overlay
