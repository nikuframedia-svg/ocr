"""Predict factory validator outcome (OK/AVISO/DIVERGÊNCIA/SEM MATCH) for our R24 outputs.

Mirrors the logic of `kanban_csv2excel_novo_layout.py` (the factory's actual
validator) but reads our `*_fixed.json` files instead of OCR CSVs, and prints
a per-row + aggregate summary without writing to Excel.

Key validator semantics (verbatim from the factory):
  - StockSAP (Regra 1): LOTE in stock + ESP exact + LARG ±10mm vs SAP
  - Plano (Regra 2):    OF in plan + best-entry pick (modelo substring or
                        closest comp), then OV match, modelo substring of
                        designacao, ESP exact, LARG ±10mm vs plan.lbase
                        (NOTE: this LARG check compares bobine width to
                        lbase — different physical dimensions on the kanban,
                        so always-AVISO unless OCR.larg coincides with
                        column-base width), COMP ±50mm vs plan.comp.
  - Status priority: DIVERGÊNCIA > SEM MATCH > AVISO > OK
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

REPO = Path(__file__).resolve().parent.parent
EXTRACT_DIR = REPO / "reports" / "extractions_v_qwen35_round24"
MINED_PATH = REPO / "lexicons" / "sap_plan_mined.json"
TOL_LARG_MM = 10
TOL_COMP_MM = 50


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _best_entry(entries, modelo_ocr, comp_ocr):
    if not entries:
        return None
    if modelo_ocr:
        for e in entries:
            if modelo_ocr.upper() in e["designacao"].upper():
                return e
    if comp_ocr is not None:
        candidatos = [e for e in entries if _num(e.get("comp")) is not None]
        if candidatos:
            return min(candidatos, key=lambda e: abs((_num(e["comp"]) or 0) - comp_ocr))
    return entries[0]


def validar_stocksap(row, stock):
    lote = str(row.get("lote", "") or "").strip().upper()
    if not lote or lote in ("?",):
        return "SEM LOTE", ["LOTE: vazio"]
    if lote not in stock:
        return "LOTE NÃO ENCONTRADO", [f"LOTE {lote}: não em StockSAP"]
    entrada = stock[lote]
    notas, status = [], "OK"
    larg_ocr = _num(row.get("larg_mm"))
    larg_sap = _num(entrada.get("larg"))
    if larg_ocr is not None and larg_sap is not None:
        diff = abs(larg_ocr - larg_sap)
        if diff > TOL_LARG_MM:
            status = "DIVERGÊNCIA"
            notas.append(f"LARG: OCR={larg_ocr:.0f} SAP={larg_sap:.0f} (dif={diff:.0f}mm)")
    esp_ocr = _num(row.get("esp"))
    esp_sap = _num(entrada.get("esp"))
    if esp_ocr is not None and esp_sap is not None:
        if abs(esp_ocr - esp_sap) > 0.01:
            status = "DIVERGÊNCIA"
            notas.append(f"ESP: OCR={esp_ocr} SAP={esp_sap}")
    return status, notas


def validar_plano(row, plano_idx):
    of_raw = str(row.get("of", "") or "").strip()
    try:
        of_int = int(float(of_raw))
    except (ValueError, TypeError):
        return "OF INVÁLIDO", [f"OF '{of_raw}': não numérico"], None
    entradas = plano_idx.get(of_int, [])
    if not entradas:
        return "OF SEM MATCH", [f"OF {of_int}: não no plano"], None
    modelo_ocr = str(row.get("modelo", "") or "").strip().upper()
    comp_ocr = _num(row.get("comp_mm"))
    entrada = _best_entry(entradas, modelo_ocr, comp_ocr)
    notas, status = [], "OK"
    ov_ocr = str(row.get("ov", "") or "").strip()
    ov_plan = str(entrada.get("ov") or "").strip()
    if ov_ocr and ov_plan and ov_ocr != ov_plan:
        if status == "OK":
            status = "AVISO"
        notas.append(f"OV: OCR={ov_ocr} → Plano={ov_plan}")
    if modelo_ocr and modelo_ocr not in entrada["designacao"].upper():
        if status == "OK":
            status = "AVISO"
        notas.append(f"MODELO: OCR={modelo_ocr} ⊄ {entrada['designacao'][:30]}")
    esp_ocr = _num(row.get("esp"))
    esp_plan = _num(entrada.get("esp"))
    if esp_ocr is not None and esp_plan is not None:
        if abs(esp_ocr - esp_plan) > 0.01:
            status = "DIVERGÊNCIA"
            notas.append(f"ESP: OCR={esp_ocr} Plano={esp_plan}")
    # PATCHED VERSION: compares OCR.lbase (kanban's column-base width) to
    # plan.lbase, instead of OCR.larg_mm (bobine width).
    lbase_ocr = _num(row.get("lbase"))
    lbase_pl = _num(entrada.get("lbase"))
    if lbase_ocr is not None and lbase_pl is not None:
        diff = abs(lbase_ocr - lbase_pl)
        if diff > TOL_LARG_MM:
            if status == "OK":
                status = "AVISO"
            notas.append(f"LBASE: OCR={lbase_ocr:.0f} Plano.lbase={lbase_pl:.0f} (dif={diff:.0f}mm)")
    comp_plan = _num(entrada.get("comp"))
    if comp_ocr is not None and comp_plan is not None:
        diff = abs(comp_ocr - comp_plan)
        if diff > TOL_COMP_MM:
            status = "DIVERGÊNCIA"
            notas.append(f"COMP: OCR={comp_ocr:.0f} Plano={comp_plan:.0f} (dif={diff:.0f}mm)")
    return status, notas, entrada


def consolidate(st_sap, st_plan):
    todos = (st_sap, st_plan)
    if "DIVERGÊNCIA" in todos or "LOTE NÃO ENCONTRADO" in todos or "OF INVÁLIDO" in todos:
        return "DIVERGÊNCIA"
    if any(s in todos for s in ("SEM MATCH", "OF SEM MATCH", "SEM LOTE")):
        return "SEM MATCH"
    if "AVISO" in todos:
        return "AVISO"
    return "OK"


def consolidate_no_larg(st_sap, st_plan, notas_sap, notas_plan):
    """Predict outcome IF the LARG-vs-lbase check were dropped (it compares
    different physical dimensions). Re-derive status from the remaining notes.
    """
    # If only divergence reason was LARG: drop it
    if st_plan == "DIVERGÊNCIA" and len(notas_plan) == 1 and notas_plan[0].startswith("LARG"):
        st_plan = "AVISO"  # demote to aviso
    # Drop LARG aviso notes from plan
    real_plan_notas = [n for n in notas_plan if not n.startswith("LARG")]
    if not real_plan_notas and st_plan in ("AVISO", "OK"):
        st_plan = "OK"
    return consolidate(st_sap, st_plan)


def main():
    mined = json.loads(MINED_PATH.read_text(encoding="utf-8"))
    stock = mined["lotes_sap_full"]
    # Re-build plan index from mined OFs by re-reading Excel:
    # but we only saved OFs/clients, not full plan rows. Need to re-mine plan
    # rows; do it here for the validator.
    import openpyxl
    base = Path(r"C:\Users\User\AppData\Local\Temp\ai_check\AI")
    plano_idx: dict[int, list] = {}
    wb = openpyxl.load_workbook(base / "nifruka/04_Documentacao/plan_colunas_cpis.xlsx", read_only=True, data_only=True)
    ws = wb["plan_colunas_cpis"]
    hdrs = {}
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            for j, h in enumerate(r):
                if h:
                    hdrs[str(h).strip().lower()] = j
            continue
        if r[0] is None:
            break
        try:
            of_int = int(float(str(r[hdrs["of"]])))
        except (ValueError, TypeError):
            continue
        plano_idx.setdefault(of_int, []).append({
            "cliente": str(r[hdrs["cliente"]] or "").strip().upper(),
            "ov": str(r[hdrs["ov"]] or "").strip(),
            "designacao": str(r[hdrs["designacao"]] or "").strip(),
            "esp": r[hdrs["esp"]],
            "lbase": r[hdrs["lbase"]],
            "ltopo": r[hdrs["ltopo"]],
            "ltotal": r[hdrs["ltotal"]],
            "comp": r[hdrs["comp"]],
        })
    wb.close()
    print(f"Loaded: {len(stock)} SAP lotes, {len(plano_idx)} plan OFs")

    fixeds = sorted(EXTRACT_DIR.glob("*_fixed.json"))
    print(f"Sheets to validate: {len(fixeds)}")
    print()

    sheet_results = []
    for fp in fixeds:
        data = json.loads(fp.read_text(encoding="utf-8"))
        sheet_name = fp.stem.replace("_fixed", "")
        rows = data.get("rows", [])
        per_sheet = {"sheet": sheet_name, "rows": []}
        counts = Counter()
        counts_no_larg = Counter()
        for i, row in enumerate(rows):
            st_sap, notas_sap = validar_stocksap(row, stock)
            st_plan, notas_plan, _entry = validar_plano(row, plano_idx)
            final = consolidate(st_sap, st_plan)
            final_no_larg = consolidate_no_larg(st_sap, st_plan, notas_sap, notas_plan)
            counts[final] += 1
            counts_no_larg[final_no_larg] += 1
            per_sheet["rows"].append({
                "i": i, "lote": row.get("lote",""), "of": row.get("of",""),
                "modelo": row.get("modelo",""), "esp": row.get("esp",""),
                "larg_mm": row.get("larg_mm",""), "comp_mm": row.get("comp_mm",""),
                "lbase": row.get("lbase",""),
                "st_sap": st_sap, "st_plan": st_plan,
                "final": final, "final_no_larg": final_no_larg,
                "notes": notas_sap + notas_plan,
            })
        per_sheet["counts"] = dict(counts)
        per_sheet["counts_no_larg"] = dict(counts_no_larg)
        sheet_results.append(per_sheet)

    # Aggregate
    total = Counter()
    total_no_larg = Counter()
    total_rows = 0
    for s in sheet_results:
        for k, v in s["counts"].items():
            total[k] += v
            total_rows += v
        for k, v in s["counts_no_larg"].items():
            total_no_larg[k] += v
    total_rows = total_rows // 1  # already counted

    print("=" * 70)
    print("AGGREGATE — As-is (validator runs unchanged)")
    print("=" * 70)
    for status in ("OK", "AVISO", "SEM MATCH", "DIVERGÊNCIA"):
        n = total.get(status, 0)
        pct = 100.0 * n / sum(total.values()) if sum(total.values()) else 0
        print(f"  {status:18s} {n:4d}  ({pct:5.1f} %)")
    print(f"  {'TOTAL ROWS':18s} {sum(total.values()):4d}")
    print()
    print("=" * 70)
    print("AGGREGATE — IF validator's LARG-vs-lbase check were dropped")
    print("(they compare different physical dimensions on the kanban)")
    print("=" * 70)
    for status in ("OK", "AVISO", "SEM MATCH", "DIVERGÊNCIA"):
        n = total_no_larg.get(status, 0)
        pct = 100.0 * n / sum(total_no_larg.values()) if sum(total_no_larg.values()) else 0
        print(f"  {status:18s} {n:4d}  ({pct:5.1f} %)")
    print()

    print("=" * 70)
    print("PER-SHEET BREAKDOWN")
    print("=" * 70)
    for s in sheet_results:
        c = s["counts"]
        c2 = s["counts_no_larg"]
        ok, av, sm, dv = c.get("OK",0), c.get("AVISO",0), c.get("SEM MATCH",0), c.get("DIVERGÊNCIA",0)
        ok2 = c2.get("OK",0)
        n = ok+av+sm+dv
        print(f"  {s['sheet']:42s}  {n:2d} rows  | as-is: {ok:2d} OK / {av:2d} AVISO / {sm} SM / {dv} DIV  | no-LARG: {ok2:2d} OK")

    # Failure mode analysis
    print()
    print("=" * 70)
    print("FAILURE MODE — top reasons")
    print("=" * 70)
    reasons = Counter()
    of_not_found = []
    lote_not_found = []
    esp_div = []
    comp_div = []
    larg_div_sap = []
    for s in sheet_results:
        for r in s["rows"]:
            for n in r["notes"]:
                tag = n.split(":", 1)[0]
                reasons[tag] += 1
            if r["st_plan"] == "OF SEM MATCH":
                of_not_found.append((s["sheet"], r["i"], r["of"]))
            if r["st_sap"] == "LOTE NÃO ENCONTRADO":
                lote_not_found.append((s["sheet"], r["i"], r["lote"]))
            for n in r["notes"]:
                if n.startswith("ESP:"):
                    esp_div.append((s["sheet"], r["i"], n))
                if n.startswith("COMP:"):
                    comp_div.append((s["sheet"], r["i"], n))
                if n.startswith("LARG:") and "SAP" in n:
                    larg_div_sap.append((s["sheet"], r["i"], n))

    for tag, n in reasons.most_common(10):
        print(f"  {tag:20s} {n}")

    print()
    print(f"  OF não encontrado no plano: {len(of_not_found)}")
    for s, i, of in of_not_found[:8]:
        print(f"    {s} row[{i}] of={of}")
    print(f"  LOTE não encontrado em SAP: {len(lote_not_found)}")
    for s, i, lt in lote_not_found[:8]:
        print(f"    {s} row[{i}] lote={lt}")
    print(f"  ESP divergence: {len(esp_div)}")
    for s, i, n in esp_div[:5]: print(f"    {s} row[{i}] {n}")
    print(f"  COMP divergence (>50mm): {len(comp_div)}")
    for s, i, n in comp_div[:5]: print(f"    {s} row[{i}] {n}")
    print(f"  LARG vs SAP divergence: {len(larg_div_sap)}")
    for s, i, n in larg_div_sap[:5]: print(f"    {s} row[{i}] {n}")

    # Save
    out_dir = REPO / "reports" / "sap_validation"
    out_dir.mkdir(exist_ok=True)
    full = out_dir / "predicted_round24.json"
    full.write_text(json.dumps({
        "aggregate_as_is": dict(total),
        "aggregate_no_larg_check": dict(total_no_larg),
        "per_sheet": sheet_results,
        "failure_modes": {
            "reasons_count": dict(reasons),
            "of_not_in_plan": of_not_found,
            "lote_not_in_sap": lote_not_found,
            "esp_divergence": esp_div,
            "comp_divergence": comp_div,
            "larg_div_sap": larg_div_sap,
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ saved {full}")


if __name__ == "__main__":
    main()
