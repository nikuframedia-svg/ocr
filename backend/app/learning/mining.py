"""Mine learning proposals from human corrections + validated sheets.

The ``edits`` table records every human correction as an ``old`` (OCR,
wrong) → ``new`` (human, right) pair. Validated sheets are confirmed
ground truth. This module turns both into :class:`Proposal` objects:

- :func:`derive_aliases`         — repeated old→new pairs become aliases
- :func:`derive_case_rules`      — repeated case-only edits retain canonical casing
- :func:`derive_confusions`      — char-level diffs become confusion pairs
- :func:`derive_observed_values` — new clientes/modelos seen in gold
- :func:`derive_fewshot_candidates` — best validated sheets per template
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from app.dq.mining import _align_chars
from app.learning.models import EditDiff, Proposal
from app.web import db

# ---- thresholds (deliberately conservative; the audit panel is the net) ----
MIN_ALIAS_SUPPORT = 2          # repeated old→new pairs to propose an alias
MIN_CONFUSION_SUPPORT = 3      # char-substitution events to propose a confusion
MIN_OBSERVED_SUPPORT = 2       # distinct validated sheets a new value must hit
FEWSHOT_PER_TEMPLATE = 3

_ALIAS_FIELDS: dict[str, str] = {
    "cliente": "cliente_alias",
    "operador": "operador_alias",
    "modelo": "modelo_alias",
}
_CONFUSION_FIELDS = ("cliente", "modelo", "lote")


def field_of(field_path: str) -> str:
    """``rows[3].modelo`` → ``modelo``; ``header.operador`` → ``operador``."""
    return field_path.rsplit(".", 1)[-1].strip()


def mine_edits(since_edit_id: int = 0) -> list[EditDiff]:
    """Read human corrections from the ``edits`` table.

    Drops noise: empty new value and exact no-op edits. Case-only changes are
    deliberately retained because casing is part of the factory identifier.
    System auto-snaps
    (``source='system'``) are excluded — only operator corrections teach.
    """
    diffs: list[EditDiff] = []
    with db.conn() as c:
        rows = c.execute(
            "SELECT e.id, e.sheet_id, e.field_path, e.old_value, e.new_value, "
            "s.sheet_data FROM edits e JOIN sheets s ON s.id = e.sheet_id "
            "WHERE e.id > ? AND e.source = 'human' ORDER BY e.id",
            (since_edit_id,),
        ).fetchall()
    for r in rows:
        old = (r["old_value"] or "").strip()
        new = (r["new_value"] or "").strip()
        if not new or old == new:
            continue
        try:
            sheet_data = json.loads(r["sheet_data"]) if r["sheet_data"] else {}
        except (json.JSONDecodeError, TypeError):
            sheet_data = {}
        template_name = db.get_sheet_template_name({"sheet_data": sheet_data})
        diffs.append(
            EditDiff(
                edit_id=r["id"],
                sheet_id=r["sheet_id"],
                field_path=r["field_path"],
                field=field_of(r["field_path"]),
                old=old,
                new=new,
                template_name=template_name,
            )
        )
    return diffs


def derive_case_rules(edits: list[EditDiff]) -> list[Proposal]:
    """Repeated case-only corrections become auditable canonical spellings."""
    groups: dict[tuple[str, str, str, str], list[EditDiff]] = defaultdict(list)
    targets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for d in edits:
        if d.field not in _ALIAS_FIELDS or not d.old:
            continue
        if d.old != d.new and d.old.casefold() == d.new.casefold():
            template_name = d.template_name or "bobine_formato"
            groups[(template_name, d.field, d.old.casefold(), d.new)].append(d)
            targets[(template_name, d.field, d.old.casefold())].add(d.new)
    out: list[Proposal] = []
    for (template_name, field, folded, canonical), ds in groups.items():
        if len(ds) < MIN_ALIAS_SUPPORT:
            continue
        conflict = len(targets[(template_name, field, folded)]) > 1
        out.append(Proposal(
            kind="case_rule",
            field=field,
            template_name=template_name,
            payload={
                "field": field,
                "from_casefold": folded,
                "to": canonical,
                "conflict": conflict,
            },
            dedup_key=(
                f"case_rule|{template_name}|{field}|{folded}=>{canonical}"
            ),
            evidence_count=len(ds),
            evidence_refs=sorted(d.edit_id for d in ds),
        ))
    return out


def derive_aliases(edits: list[EditDiff]) -> list[Proposal]:
    """Repeated ``old → new`` corrections on cliente/operador/modelo
    become alias proposals (operator shorthand / OCR-corrupt → canonical)."""
    groups: dict[tuple[str, str, str], list[EditDiff]] = defaultdict(list)
    for d in edits:
        kind = _ALIAS_FIELDS.get(d.field)
        if (
            not kind
            or not d.old
            or d.old.casefold() == d.new.casefold()
        ):
            continue
        groups[(kind, d.old.upper().strip(), d.new.strip())].append(d)

    out: list[Proposal] = []
    for (kind, frm, to), ds in groups.items():
        if len(ds) < MIN_ALIAS_SUPPORT:
            continue
        out.append(
            Proposal(
                kind=kind,
                field=kind.split("_")[0],
                payload={"from": frm, "to": to},
                dedup_key=f"{kind}|{frm}=>{to}",
                evidence_count=len(ds),
                evidence_refs=sorted(d.edit_id for d in ds),
            )
        )
    return out


def derive_confusions(edits: list[EditDiff]) -> list[Proposal]:
    """Char-level alignment of ``new`` (gold) vs ``old`` (OCR) on lexical
    fields. Recurring substitutions become confusion-pair proposals that
    feed L2's confusion-weighted Levenshtein distance."""
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_refs: dict[tuple[str, str], set[int]] = defaultdict(set)
    for d in edits:
        if d.field not in _CONFUSION_FIELDS or not d.old:
            continue
        gold = d.new.upper().strip()
        ocr = d.old.upper().strip()
        for pair in _align_chars(gold, ocr):
            pair_counts[pair] += 1
            pair_refs[pair].add(d.edit_id)

    by_gold: dict[str, int] = defaultdict(int)
    for (gc, _oc), n in pair_counts.items():
        by_gold[gc] += n

    out: list[Proposal] = []
    for (gc, oc), n in pair_counts.items():
        if n < MIN_CONFUSION_SUPPORT:
            continue
        weight = round(n / by_gold[gc], 3) if by_gold[gc] else 0.5
        out.append(
            Proposal(
                kind="confusion_pair",
                field=None,
                payload={"gold_char": gc, "ocr_char": oc,
                         "weight": weight, "count": n},
                dedup_key=f"confusion_pair|{gc}>{oc}",
                evidence_count=n,
                evidence_refs=sorted(pair_refs[(gc, oc)]),
            )
        )
    return out


def load_validated_sheets() -> list[dict]:
    """Validated sheets with parsed ``sheet_data``, oldest first."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT id, sheet_data, validated_at, operador "
            "FROM sheets WHERE status = 'validated' AND sheet_data IS NOT NULL "
            "ORDER BY validated_at"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            sd = json.loads(r["sheet_data"])
        except (json.JSONDecodeError, TypeError):
            continue
        out.append(
            {
                "id": r["id"],
                "sheet_data": sd,
                "validated_at": r["validated_at"],
                "operador": r["operador"],
            }
        )
    return out


def derive_observed_values(sheets: list[dict]) -> list[Proposal]:
    """Cliente/modelo values confirmed on >=2 validated sheets that are not
    yet in the static lexicons. Purely additive — extends the snap
    candidate set without changing any decision rule."""
    from app.dq import snap

    known: dict[str, set[str]] = {
        "cliente": {c.upper().strip() for c in snap._CLIENTES}
        | {c.upper().strip() for c in snap._CLIENTES_PLAN},
        "modelo": {m.upper().strip() for m in snap._MODELOS_FULL},
    }
    seen: dict[str, dict[str, set[int]]] = {
        "cliente": defaultdict(set),
        "modelo": defaultdict(set),
    }
    for s in sheets:
        for row in s["sheet_data"].get("rows", []) or []:
            if not isinstance(row, dict):
                continue
            for fld in ("cliente", "modelo"):
                v = (row.get(fld) or "").strip()
                if v:
                    seen[fld][v.upper()].add(s["id"])

    out: list[Proposal] = []
    for fld in ("cliente", "modelo"):
        for value, sheet_ids in seen[fld].items():
            if value in known[fld] or len(sheet_ids) < MIN_OBSERVED_SUPPORT:
                continue
            out.append(
                Proposal(
                    kind="observed_value",
                    field=fld,
                    payload={"field": fld, "value": value},
                    dedup_key=f"observed_value|{fld}|{value}",
                    origin="validated_sheets",
                    evidence_count=len(sheet_ids),
                    evidence_refs=sorted(sheet_ids),
                )
            )
    return out


def derive_fewshot_candidates(sheets: list[dict]) -> list[Proposal]:
    """Pick the best validated sheets per template as few-shot examples.

    Score favours sheets the OCR already got mostly right (few human
    edits) with good field coverage; recency breaks ties. Diversity:
    distinct operadores are preferred before doubling up."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT sheet_id, COUNT(*) n FROM edits "
            "WHERE source = 'human' GROUP BY sheet_id"
        ).fetchall()
    edits_by_sheet = {r["sheet_id"]: r["n"] for r in rows}

    by_tpl: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for s in sheets:
        sd = s["sheet_data"]
        tname = sd.get("template_name") or db.get_sheet_template_name(
            {"sheet_data": sd}
        )
        rows_data = [r for r in sd.get("rows", []) or [] if isinstance(r, dict)]
        n_rows = sum(
            1 for r in rows_data if any((str(v) or "").strip() for v in r.values())
        )
        n_edits = edits_by_sheet.get(s["id"], 0)
        coverage = min(n_rows, 12) / 12.0
        cleanliness = 1.0 / (1.0 + n_edits)
        score = round(0.6 * cleanliness + 0.4 * coverage, 4)
        by_tpl[tname].append((score, s))

    out: list[Proposal] = []
    for tname, scored in by_tpl.items():
        scored.sort(
            key=lambda x: (x[0], x[1]["validated_at"] or ""), reverse=True
        )
        chosen_ids: list[int] = []
        used_ops: set[str] = set()
        # Pass 1 — one example per distinct operador.
        for _score, s in scored:
            if len(chosen_ids) >= FEWSHOT_PER_TEMPLATE:
                break
            op = (s["operador"] or "").upper()
            if op and op in used_ops:
                continue
            chosen_ids.append(s["id"])
            used_ops.add(op)
        # Pass 2 — fill any remaining slots regardless of operador.
        for _score, s in scored:
            if len(chosen_ids) >= FEWSHOT_PER_TEMPLATE:
                break
            if s["id"] not in chosen_ids:
                chosen_ids.append(s["id"])

        score_by_id = {s["id"]: sc for sc, s in scored}
        for sid in chosen_ids:
            out.append(
                Proposal(
                    kind="fewshot_example",
                    field=None,
                    template_name=tname,
                    payload={"sheet_id": sid, "template_name": tname,
                             "score": score_by_id.get(sid, 0.0)},
                    dedup_key=f"fewshot_example|{tname}|{sid}",
                    origin="validated_sheets",
                    evidence_count=1,
                    evidence_refs=[sid],
                )
            )
    return out
