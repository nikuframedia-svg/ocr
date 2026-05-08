"""Persistent cross-sheet index for L3 consistency checks.

Stores observed mappings:
- ov -> {cliente, of, modelo}: same OV should map to same client/order
- lote -> {larg_mm, esp}: same lote should share physical properties
  (CONI/LBASE/LTOPO vary per row even with same lote, so excluded)

Index grows as new sheets are processed. Seeded from gold via mining
(``learned.json::ov_to_cliente``).

Storage: JSON on disk, append-mostly. Concurrent writes not supported
(Metalogalva runs one extraction at a time on this single GPU box).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CrossSheetIndex:
    ov_to_cliente: dict[str, str] = field(default_factory=dict)
    ov_to_of: dict[str, set[str]] = field(default_factory=dict)
    lote_to_larg_esp: dict[str, dict[str, str]] = field(default_factory=dict)
    # Provenance: which sheet first taught us this entry
    provenance: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "CrossSheetIndex":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            ov_to_cliente=data.get("ov_to_cliente", {}),
            ov_to_of={k: set(v) for k, v in data.get("ov_to_of", {}).items()},
            lote_to_larg_esp=data.get("lote_to_larg_esp", {}),
            provenance=data.get("provenance", {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "ov_to_cliente": self.ov_to_cliente,
                    "ov_to_of": {k: sorted(v) for k, v in self.ov_to_of.items()},
                    "lote_to_larg_esp": self.lote_to_larg_esp,
                    "provenance": self.provenance,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def seed_from_learned(self, learned_path: Path) -> None:
        """Initialise from ``learned.json`` (mining output).

        Idempotent: re-running does not overwrite already-known entries
        from later sheets. Provenance marked as ``gold:<sheet>``.
        """
        if not learned_path.exists():
            return
        data = json.loads(learned_path.read_text(encoding="utf-8"))
        for ov, cli in data.get("ov_to_cliente", {}).items():
            self.ov_to_cliente.setdefault(ov, cli)
            self.provenance.setdefault(f"ov:{ov}", "gold")
        # lote -> {larg_mm, esp} from lote_consistency where each has 1 value
        for lote, fields in data.get("lote_consistency", {}).items():
            larg_set = fields.get("larg_mm", [])
            esp_set = fields.get("esp", [])
            entry = {}
            if len(larg_set) == 1:
                entry["larg_mm"] = larg_set[0]
            if len(esp_set) == 1:
                entry["esp"] = esp_set[0]
            if entry:
                self.lote_to_larg_esp.setdefault(lote, entry)
                self.provenance.setdefault(f"lote:{lote}", "gold")

    def observe(self, sheet: str, extraction: dict) -> None:
        """Update index from a new extraction. Only adds; doesn't overwrite.

        Conflicts (same OV, different cliente) are NOT resolved here —
        :func:`check_cross_sheet_consistency` reports them as
        violations against the *existing* index.
        """
        for row in extraction.get("rows", []):
            ov = (row.get("ov", "") or "").strip()
            cli = (row.get("cliente", "") or "").upper().strip()
            of = (row.get("of", "") or "").strip()
            lote = (row.get("lote", "") or "").upper().strip()
            larg = (row.get("larg_mm", "") or "").strip()
            esp = (row.get("esp", "") or "").strip()

            if ov and cli:
                self.ov_to_cliente.setdefault(ov, cli)
                self.provenance.setdefault(f"ov:{ov}", f"sheet:{sheet}")
            if ov and of:
                self.ov_to_of.setdefault(ov, set()).add(of)
            if lote and larg and esp:
                entry = self.lote_to_larg_esp.setdefault(lote, {})
                entry.setdefault("larg_mm", larg)
                entry.setdefault("esp", esp)
                self.provenance.setdefault(f"lote:{lote}", f"sheet:{sheet}")
