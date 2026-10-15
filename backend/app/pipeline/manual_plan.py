"""CPIS closed rows for the manual wizard, separate from automatic references.

Read-only projection rebuilt by publish_original_refs.py from the local CPIS
copy. No OCR sheets, corrections, active plan or stock are written here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

_lock = threading.Lock()
_cache_key: tuple | None = None
_cache: dict | None = None
_PHASES = ("bf", "c", "q", "s", "r", "a", "exp")


class ManualPlanUnavailable(ValueError):
    """History cannot be trusted for an explicit reference selection."""


def normalize_order(value: object) -> str:
    text = re.sub(r"^OF\s*", "", str(value or "").strip().upper())
    return text.zfill(6) if text.isdigit() else ""


def load_catalog() -> dict | None:
    """Reload atomic publications, retaining identity independent of row order."""
    global _cache_key, _cache
    configured = os.environ.get("KANBAN_MANUAL_PLAN_PATH", "").strip()
    if not configured:
        return None
    path = Path(configured)
    try:
        with _lock:
            stat = path.stat()
            key = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if _cache_key != key:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("version") != 1:
                    raise ValueError("unsupported manual catalog")
                content = {k: payload[k] for k in ("version", "closed_rows", "orders")}
                encoded = json.dumps(content, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"))
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                if digest != payload.get("content_sha256"):
                    raise ValueError("manual catalog checksum mismatch")
                by_of: dict[str, list[dict]] = {}
                by_id: dict[str, dict] = {}
                for row in payload["closed_rows"]:
                    of = normalize_order(row.get("of"))
                    identity = str(row["_source_id"])
                    if not of or identity in by_id or row.get("fechado") not in (1, "1", True):
                        raise ValueError("invalid closed-row identity or status")
                    entry = {
                        "of": of, "cliente": str(row.get("cliente") or "").strip(),
                        "ov": str(row.get("ov") or "").strip(),
                        "designacao": str(row.get("designacao") or "").strip(),
                        **{k: row.get(k) for k in (
                            "esp", "lbase", "ltopo", "ltotal", "comp", "dbase", "dtopo",
                            "pesounit", "npecas", "quanttrp",
                        )},
                        "material": str(row.get("material") or "").strip(),
                        "fechado": "1", "fechado_known": True,
                        "fases": {name: row.get(name) for name in _PHASES},
                        "_entry_source": "history", "_entry_id": identity,
                    }
                    by_of.setdefault(of, []).append(entry)
                    by_id[identity] = entry
                loaded = {
                    "sha256": digest, "by_of": by_of, "by_id": by_id,
                    "orders": frozenset(payload["orders"]),
                    "plan_source_read": payload["plan_source_read"],
                    "orders_source_read": payload["orders_source_read"],
                }
                _cache, _cache_key = loaded, key
            assert _cache is not None
            catalog = _cache
        now = datetime.now(UTC)
        for name in ("plan_source_read", "orders_source_read"):
            read_at = datetime.fromisoformat(catalog[name])
            if read_at.tzinfo is None or not (
                timedelta(minutes=-2) <= now - read_at < timedelta(hours=2)
            ):
                raise ValueError("stale manual catalog")
        return catalog
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ManualPlanUnavailable(
            "O histórico de peças fechadas está indisponível ou desatualizado; "
            "tenta novamente mais tarde."
        ) from exc
