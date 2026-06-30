#!/usr/bin/env python3
"""Mede a EXATIDAO do cross-check contra a verdade-terreno (folhas validadas).

Porque as folhas validadas sao ground truth de borla:
  - `sheets.status='validated'`  -> `sheet_data` = valores confirmados pelo operador
  - `sheets.raw_extraction`      = OCR cru (o que o motor tem de re-correr)
  - `edits` com `source='human'` = exatamente onde o motor/OCR errou e o humano corrigiu

O script NAO escreve nada na BD. Re-corre o motor VIVO sobre o `raw_extraction`
(igual ao recheck.py / cross_diff.py) e, por celula checavel, emparelha:
  (ocr_value, truth_value, engine_value, engine_status, source, hypothesis_level,
   anchor_class, was_human_corrected) -> classifica e acumula metricas.

Metricas (globais, por nivel de confianca e por anchor_class):
  substitution_precision      = S_correct / S          (quando substitui, acerta?)
  wrong_auto_overwrite_per_1k = 1000 * A_wrong / Check  (o DANO: auto-aplicou errado)
  wrong_auto_overwrite_rate   = A_wrong / A             (condicional)
  good_correction_recall      = WO_fixed / WO           (o que NAO podemos perder)
  substitution_coverage       = S / Check
  na_rate                     = NA / (Check + NA)

Limitacao conhecida: se um auto-overwrite errado passou despercebido na validacao,
o `sheet_data` "verdade" tambem esta errado nessa celula. As edicoes `source='human'`
marcam as correcoes explicitas; e o melhor ground truth disponivel.

Uso (na fabrica/PC, com o python do .venv):
    .venv\\Scripts\\python.exe scripts\\diag\\accuracy_eval.py --all --json out.json
    .venv\\Scripts\\python.exe scripts\\diag\\accuracy_eval.py --last 200 --template laser
"""
from __future__ import annotations

import argparse
import csv as _csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "backend"))
sys.path.insert(0, str(_REPO))

# Reutiliza a normalizacao/relacao do ocr_vs_cross (mesma definicao de "igual").
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocr_vs_cross import _norm  # noqa: E402

_IDENTITY_FIELDS = {"of", "ov", "cliente", "modelo"}
_DIM_FIELDS = {"comp_mm", "larg_mm", "esp", "lbase", "ltopo", "dbase", "dtopo", "lote"}
# Espelha _maybe_apply_snap (backend/app/web/main.py:494-549). O teste-meta
# test_would_auto_apply_mirror garante que nao derivam.
_CONCRETE_SOURCES = {"plan", "sap", "ferramenta", "maquinas", "colaboradores", "lexicon"}
_CONCRETE_REF_SOURCES = {"plan", "sap", "maquinas", "colaboradores"}


def _would_auto_apply(cell: dict, protected: bool = False) -> bool:
    """Decisao identica a _maybe_apply_snap, sem escrever na BD (protected=False)."""
    if protected:
        return False
    if cell.get("source") == "obra_concluida":
        return False
    engine_status = cell.get("engine_status")
    source = cell.get("source")
    ref_source = cell.get("ref_source") or source
    canonical = ""
    if engine_status == "snapped":
        canonical = (cell.get("value") or "").strip()
    elif engine_status == "very_different":
        if source in _CONCRETE_SOURCES:
            canonical = (cell.get("value") or cell.get("ref") or cell.get("proposed") or "").strip()
        elif ref_source in _CONCRETE_REF_SOURCES:
            canonical = (cell.get("ref") or cell.get("proposed") or "").strip()
        else:
            return False
    else:
        return False
    return bool(canonical)


def _values_match(field: str, a: object, b: object, se) -> bool:
    """Igualdade tolerante: dimensoes por numero, identidade por _norm."""
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if field in _DIM_FIELDS:
        num = getattr(se, "_num", None)
        if num is not None:
            na, nb = num(sa), num(sb)
            if na is not None and nb is not None:
                return abs(na - nb) <= 1e-6
    return _norm(sa) == _norm(sb)


def _anchor_bucket(anchor_class: str) -> str:
    """Agrupa o anchor_class do motor nas classes do pattern_lab para o quadro."""
    ac = str(anchor_class or "").strip() or "no_winner"
    if ac.startswith(("of_only", "ov_only", "cliente_only", "modelo_only")):
        return ac.split("_")[0] + "_only"
    return ac


class _Acc:
    """Acumulador de contagens para uma fatia (global / nivel / anchor / template)."""

    __slots__ = (
        "A",
        "A_wrong",
        "S",
        "S_ok",
        "WO",
        "WO_fixed",
        "check",
        "id_S",
        "id_S_ok",
        "na",
    )

    def __init__(self) -> None:
        self.check = self.na = 0
        self.S = self.S_ok = 0
        self.A = self.A_wrong = 0
        self.WO = self.WO_fixed = 0
        self.id_S = self.id_S_ok = 0  # substituicoes so em campos de identidade

    def metrics(self) -> dict[str, Any]:
        def ratio(n, d):
            return round(n / d, 4) if d else None
        return {
            "checkable": self.check,
            "substitutions": self.S,
            "auto_applied": self.A,
            "ocr_wrong": self.WO,
            "substitution_precision": ratio(self.S_ok, self.S),
            "wrong_auto_overwrite_rate": ratio(self.A_wrong, self.A),
            "wrong_auto_overwrite_per_1k": (
                round(1000 * self.A_wrong / self.check, 3) if self.check else None
            ),
            "good_correction_recall": ratio(self.WO_fixed, self.WO),
            "substitution_coverage": ratio(self.S, self.check),
            "na_rate": ratio(self.na, self.check + self.na),
            "identity_subst": self.id_S,
            "identity_subst_precision": ratio(self.id_S_ok, self.id_S),
            "counts": {
                "S_ok": self.S_ok, "A_wrong": self.A_wrong,
                "WO_fixed": self.WO_fixed, "na": self.na,
            },
        }


def _sheet_ids(args, db) -> list[int]:
    if args.sheets_file:
        ids = [int(x) for x in Path(args.sheets_file).read_text().split() if x.strip()]
        return ids
    where = ["status = 'validated'",
             "raw_extraction IS NOT NULL AND raw_extraction <> ''",
             "sheet_data IS NOT NULL AND sheet_data <> ''"]
    params: list[Any] = []
    if args.since:
        where.append("validated_at >= ?")
        params.append(args.since)
    sql = "SELECT id FROM sheets WHERE " + " AND ".join(where) + " ORDER BY validated_at DESC"
    if not args.all:
        sql += " LIMIT ?"
        params.append(args.last)
    with db.conn() as c:
        rows = c.execute(sql, tuple(params)).fetchall()
    return [int(r["id"]) for r in rows]


def _human_paths(sheet_id: int, db) -> set[str]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT field_path FROM edits WHERE sheet_id = ? AND source = 'human'",
            (sheet_id,),
        ).fetchall()
    return {str(r["field_path"]) for r in rows}


def _iter_engine_cells(res: dict):
    """Gera (path, field, cell, row_index|None) das celulas checaveis do resultado."""
    for section in ("header", "footer"):
        for field, cell in (res.get(section) or {}).items():
            if isinstance(cell, dict):
                yield f"{section}.{field}", field, cell, None
    for pos, row in enumerate(res.get("rows") or []):
        if not isinstance(row, dict):
            continue
        ri = row.get("row_index", pos)
        ac = (row.get("proposal_strategy") or {}).get("anchor_class")
        for field, cell in (row.get("fields") or {}).items():
            if isinstance(cell, dict):
                yield f"rows[{ri}].{field}", field, cell, (ri, ac)


def _truth_ocr(raw: dict, sd: dict, path: str, field: str, row_key):
    """Devolve (ocr_value, truth_value) lendo raw/sheet_data pelo caminho."""
    if row_key is None:
        section = path.split(".", 1)[0]
        ocr = ((raw.get(section) or {}).get(field))
        truth = ((sd.get(section) or {}).get(field))
        return ocr, truth
    ri = row_key[0]
    rraw = (raw.get("rows") or [])
    rsd = (sd.get("rows") or [])
    ocr = rraw[ri].get(field) if ri < len(rraw) else None
    truth = rsd[ri].get(field) if ri < len(rsd) else None
    return ocr, truth


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Exatidao do cross vs folhas validadas.")
    ap.add_argument("--last", type=int, default=200)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--template", default=None, help="filtra por template_name")
    ap.add_argument("--since", default=None, help="validated_at >= ISO")
    ap.add_argument("--sheets-file", default=None, help="ficheiro com ids (fixa a amostra)")
    ap.add_argument("--csv", default=None, help="escreve linhas por celula")
    ap.add_argument("--json", default=None, help="escreve bloco de metricas agregadas")
    ap.add_argument("--engine-tag", default=None, help="rotulo para distinguir corridas")
    args = ap.parse_args()

    from app.cross_check.ref_watcher import get_watcher
    from app.pipeline import scoring_engine as se
    from app.web import db

    refs = get_watcher().get_refs()
    ids = _sheet_ids(args, db)
    if not ids:
        print("Sem folhas validadas (com raw + sheet_data) a avaliar.")
        return 1

    glob = _Acc()
    by_level: dict[str, _Acc] = defaultdict(_Acc)
    by_anchor: dict[str, _Acc] = defaultdict(_Acc)
    by_template: dict[str, _Acc] = defaultdict(_Acc)
    csv_rows: list[dict[str, Any]] = []
    n_sheets = 0

    for sid in ids:
        sheet = db.get_sheet(sid)
        if not sheet:
            continue
        raw = sheet.get("raw_extraction") or {}
        sd = sheet.get("sheet_data") or {}
        if not raw.get("rows") and not raw.get("header"):
            continue
        tpl = raw.get("template_name") or sheet.get("template_name") or ""
        if args.template and tpl != args.template:
            continue
        try:
            res = se.cross_check_sheet(raw, None, refs)
        except Exception as exc:
            print(f"  folha {sid}: erro motor: {exc!r}")
            continue
        human = _human_paths(sid, db)
        n_sheets += 1

        for path, field, cell, row_key in _iter_engine_cells(res):
            status = cell.get("status")
            estatus = cell.get("engine_status")
            if status == "NA" or estatus == "NA":
                for acc in (glob, by_template[tpl]):
                    acc.na += 1
                continue
            ocr, truth = _truth_ocr(raw, sd, path, field, row_key)
            level = str(cell.get("hypothesis_level") or "unidentified")
            anchor = _anchor_bucket(row_key[1] if row_key else "header_footer")
            val = cell.get("value")
            is_identity = field in _IDENTITY_FIELDS

            substituted = _norm(val) != _norm(ocr) and str(val or "").strip() != ""
            correct = _values_match(field, val, truth, se)
            ocr_wrong = not _values_match(field, ocr, truth, se)
            auto = _would_auto_apply(cell)

            slices = (glob, by_level[level], by_anchor[anchor], by_template[tpl])
            for acc in slices:
                acc.check += 1
                if substituted:
                    acc.S += 1
                    if correct:
                        acc.S_ok += 1
                    if is_identity:
                        acc.id_S += 1
                        if correct:
                            acc.id_S_ok += 1
                if auto:
                    acc.A += 1
                    if not correct:
                        acc.A_wrong += 1
                if ocr_wrong:
                    acc.WO += 1
                    if correct:
                        acc.WO_fixed += 1

            if args.csv:
                csv_rows.append({
                    "sheet_id": sid, "template": tpl, "path": path, "field": field,
                    "ocr": ocr, "truth": truth, "engine": val,
                    "engine_status": estatus, "source": cell.get("source"),
                    "level": level, "anchor": anchor,
                    "substituted": int(substituted), "correct": int(correct),
                    "ocr_wrong": int(ocr_wrong), "auto_applied": int(auto),
                    "human_corrected": int(path in human),
                })

    report = {
        "engine_tag": args.engine_tag or se.ENGINE_VERSION,
        "engine_version": se.ENGINE_VERSION,
        "n_sheets": n_sheets,
        "n_cells": glob.check,
        "global": glob.metrics(),
        "by_level": {k: v.metrics() for k, v in sorted(by_level.items())},
        "by_anchor": {k: v.metrics() for k, v in sorted(by_anchor.items())},
        "by_template": {k: v.metrics() for k, v in sorted(by_template.items())},
        "sheet_ids": ids if args.sheets_file else None,
    }

    # Tabela das classes perigosas (a prova da lei).
    print(
        f"\n=== ACCURACY EVAL ({report['engine_tag']}) | "
        f"folhas={n_sheets} | celulas={glob.check} ===\n"
    )
    g = glob.metrics()
    print(f"GLOBAL  subst_prec={g['substitution_precision']}  "
          f"wrong_AO/1k={g['wrong_auto_overwrite_per_1k']}  "
          f"good_recall={g['good_correction_recall']}  cov={g['substitution_coverage']}")
    print("\nPor anchor_class (id_subst / id_prec / wrong_AO_1k / recall):")
    hdr = f"  {'anchor':22} {'check':>6} {'idS':>5} {'idPrec':>7} {'AO/1k':>7} {'recall':>7}"
    print(hdr)
    for k, v in sorted(by_anchor.items()):
        m = v.metrics()
        print(f"  {k:22} {m['checkable']:>6} {m['identity_subst']:>5} "
              f"{m['identity_subst_precision']!s:>7} {m['wrong_auto_overwrite_per_1k']!s:>7} "
              f"{m['good_correction_recall']!s:>7}")

    if args.json:
        outp = Path(args.json)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nMetricas escritas em: {outp}")
    if args.csv and csv_rows:
        outp = Path(args.csv)
        outp.parent.mkdir(parents=True, exist_ok=True)
        with outp.open("w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)
        print(f"CSV (por celula) escrito em: {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
