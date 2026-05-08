"""Mine learned data quality artefacts from gold + extractions.

Run once after each ground-truth update to refresh:
- confusion_matrix: per-character substitution frequencies (gold->ocr) per
  field family. Drives L2 confusion-weighted Levenshtein.
- operator_stats: per-operator (qty, comp_mm, larg_mm, esp) percentile
  bands. Drives L3 soft outlier rule.
- lote_consistency: lotes that appear in >=2 rows must share LARG_MM,
  ESP, CONI, LBASE, LTOPO. Drives L3 hard rule 2.
- sum_qtd_tolerance: empirical tolerance for |sum(qtd) - colunas_produzidas|
  measured on gold. Drives L3 hard rule 1.
- ov_to_cliente: persistent {ov: cliente} map seeded from gold. Drives
  L3 hard rule 3 (cross-sheet consistency).

Output: lexicons/learned.json (single artefact, version-tracked).

Usage:
    python -m backend.app.dq.mining
"""
from __future__ import annotations

import json
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


def _normalise_name(s: str) -> str:
    """Strip diacritics + uppercase + collapse whitespace.

    JÚLIO LIMA == JULIO LIMA after this pass.
    """
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    no_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(no_marks.upper().split())

# Fields where char-level confusion mining is meaningful (alphanumeric
# ground-truth). Skip purely numeric (digit transposition is a different
# error mode handled by per-operator KDE).
_LEXICAL_FIELDS = ("cliente", "modelo", "lote")
_NUMERIC_FIELDS = ("qty", "qtd", "comp_mm", "larg_mm", "esp", "lbase", "ltopo")


def _align_chars(gold: str, ocr: str) -> list[tuple[str, str]]:
    """Return (gold_char, ocr_char) pairs at aligned positions.

    Uses simple SequenceMatcher to align; for our short cell strings
    (<=20 chars typically), greedy alignment is sufficient. Pairs where
    chars differ are the substitution events; pairs where they match
    are skipped (we only want noise events).
    """
    from difflib import SequenceMatcher

    pairs: list[tuple[str, str]] = []
    if not gold or not ocr:
        return pairs
    sm = SequenceMatcher(a=gold, b=ocr, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            # Pair char-by-char where lengths overlap; skip ragged tails.
            n = min(i2 - i1, j2 - j1)
            for k in range(n):
                gc, oc = gold[i1 + k], ocr[j1 + k]
                if gc != oc:
                    pairs.append((gc, oc))
    return pairs


def mine_confusion_matrix(
    gold_dir: Path,
    extractions_dir: Path,
    confirmed_stems: set[str],
) -> dict[str, dict[str, float]]:
    """Build {gold_char -> {ocr_char -> p}} from cell mismatches.

    Restricted to confirmed-gold sheets to avoid noise from draft-as-gold.
    Only counts substitutions in lexical fields where char-level confusion
    is the dominant error mode.
    """
    pair_counts: Counter[tuple[str, str]] = Counter()
    for gold_path in sorted(gold_dir.glob("*.json")):
        if gold_path.stem not in confirmed_stems:
            continue
        ocr_path = extractions_dir / gold_path.name
        if not ocr_path.exists():
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        ocr = json.loads(ocr_path.read_text(encoding="utf-8"))
        n = min(len(gold.get("rows", [])), len(ocr.get("rows", [])))
        for i in range(n):
            gr = gold["rows"][i]
            or_ = ocr["rows"][i]
            for f in _LEXICAL_FIELDS:
                gv = (gr.get(f, "") or "").upper().strip()
                ov = (or_.get(f, "") or "").upper().strip()
                if gv and ov and gv != ov:
                    for pair in _align_chars(gv, ov):
                        pair_counts[pair] += 1

    # Normalize: p(ocr_char | gold_char) = count(gold,ocr) / count(gold)
    by_gold: dict[str, Counter[str]] = defaultdict(Counter)
    for (gc, oc), c in pair_counts.items():
        by_gold[gc][oc] += c

    matrix: dict[str, dict[str, float]] = {}
    for gc, ocs in by_gold.items():
        total = sum(ocs.values())
        if total < 2:  # noise floor; need at least 2 events to be a pattern
            continue
        matrix[gc] = {oc: round(c / total, 3) for oc, c in ocs.most_common(5)}
    return matrix


def mine_operator_stats(
    gold_dir: Path,
    confirmed_stems: set[str],
) -> dict[str, dict[str, dict[str, float]]]:
    """{operador: {field: {p5, p50, p95, min, max, n}}}.

    Per-operator percentile bands for numeric fields. Drives soft
    outlier rule. With only 1-7 sheets per operator, scipy.gaussian_kde
    is unstable — use percentile_cont via numpy directly.
    """
    by_op: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for gold_path in sorted(gold_dir.glob("*.json")):
        if gold_path.stem not in confirmed_stems:
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        op = _normalise_name(gold.get("header", {}).get("operador", ""))
        if not op:
            continue
        for row in gold.get("rows", []):
            for f in ("qtd", "comp_mm", "larg_mm", "esp", "lbase", "ltopo"):
                v = (row.get(f, "") or "").strip()
                # ESP can be "2,6" — convert.
                v_norm = v.replace(",", ".")
                try:
                    by_op[op][f].append(float(v_norm))
                except ValueError:
                    continue

    # Compute percentiles. Pure stdlib to avoid scipy dependency for
    # such a simple stat.
    def pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        k = (len(s) - 1) * q
        lo = int(k)
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (k - lo)

    out: dict[str, dict[str, dict[str, float]]] = {}
    for op, fields in by_op.items():
        out[op] = {}
        for f, vals in fields.items():
            if not vals:
                continue
            out[op][f] = {
                "p5": round(pct(vals, 0.05), 2),
                "p50": round(pct(vals, 0.50), 2),
                "p95": round(pct(vals, 0.95), 2),
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "n": len(vals),
            }
    return out


def mine_lote_consistency(
    gold_dir: Path,
    confirmed_stems: set[str],
) -> dict[str, dict[str, list[str]]]:
    """{lote: {field: [observed_values]}} where field in (larg_mm, esp, coni, lbase, ltopo).

    For each lote that appears in >=2 rows across gold, collect distinct
    values. If len(values) > 1 for any field -> the lote is inconsistent
    even in gold (rare; flag for sanity). Otherwise len(values) == 1
    is the canonical expected value.
    """
    by_lote: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for gold_path in sorted(gold_dir.glob("*.json")):
        if gold_path.stem not in confirmed_stems:
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        for row in gold.get("rows", []):
            lote = (row.get("lote", "") or "").upper().strip()
            if not lote:
                continue
            for f in ("larg_mm", "esp", "coni", "lbase", "ltopo"):
                v = (row.get(f, "") or "").strip()
                if v:
                    by_lote[lote][f].add(v)

    # Convert sets -> sorted lists; drop lotes seen only once
    out: dict[str, dict[str, list[str]]] = {}
    for lote, fields in by_lote.items():
        # Count rows: a lote is "shared" if it has >1 distinct value-set
        # OR appears in >1 row — but our `by_lote` only aggregates
        # values, not row count. Approximate: include all lotes for now;
        # caller can filter.
        out[lote] = {f: sorted(vs) for f, vs in fields.items()}
    return out


def mine_sum_qtd_tolerance(
    gold_dir: Path,
    confirmed_stems: set[str],
) -> dict[str, float]:
    """Empirical tolerance for |sum(qtd) - colunas_produzidas| on gold."""
    deltas: list[float] = []
    for gold_path in sorted(gold_dir.glob("*.json")):
        if gold_path.stem not in confirmed_stems:
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        try:
            col = float((gold.get("footer", {}).get("colunas_produzidas", "") or "0").replace(",", "."))
        except ValueError:
            continue
        if col == 0:
            continue
        sum_qtd = 0.0
        for row in gold.get("rows", []):
            try:
                sum_qtd += float((row.get("qtd", "") or "0").replace(",", "."))
            except ValueError:
                pass
        rel = abs(sum_qtd - col) / col
        deltas.append(rel)

    if not deltas:
        return {"max_observed": 0.05, "median": 0.0, "p95": 0.05, "n": 0}
    s = sorted(deltas)
    n = len(s)
    return {
        "max_observed": round(max(s), 4),
        "median": round(s[n // 2], 4),
        "p95": round(s[int(0.95 * (n - 1))], 4),
        "n": n,
    }


def mine_ov_to_cliente(
    gold_dir: Path,
    confirmed_stems: set[str],
) -> dict[str, str]:
    """{ov: cliente} — first-occurrence wins. Used as seed for cross-sheet index."""
    out: dict[str, str] = {}
    for gold_path in sorted(gold_dir.glob("*.json")):
        if gold_path.stem not in confirmed_stems:
            continue
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        for row in gold.get("rows", []):
            ov = (row.get("ov", "") or "").strip()
            cli = (row.get("cliente", "") or "").upper().strip()
            if ov and cli and ov not in out:
                out[ov] = cli
    return out


def main() -> int:
    repo = Path(__file__).resolve().parents[3]
    gold_dir = repo / "ground_truth"
    ext_dir = repo / "reports" / "extractions_v9"

    if not gold_dir.exists() or not ext_dir.exists():
        print(f"missing input dirs ({gold_dir}, {ext_dir})", file=sys.stderr)
        return 1

    # Confirmed-gold subset (14 sheets — AI/processados + walkthrough sheet).
    # The 5 draft-as-gold are excluded from learning to avoid noise.
    confirmed = {
        "AugustoMonteiro_2026.04.09",  # walkthrough
        "AugustoMonteiro_2026.04.16",
        "JulioLima_2026.04.10",
        "JulioLima_2026.04.14",
        "JulioLima_2026.04.15-1",
        "JulioLima_2026.04.15",
        "JulioLima_2026.04.17",
        "JulioLima_2026.04.18",
        "VitorCarvalho_2026.04.09",
        "VitorCarvalho_2026.04.13",
        "VitorCarvalho_2026.04.14",
        "VitorCarvalho_2026.04.15",
        "VitorCarvalho_2026.04.16",
        "VitorCarvalho_2026.04.17",
    }

    confusion = mine_confusion_matrix(gold_dir, ext_dir, confirmed)
    operator_stats = mine_operator_stats(gold_dir, confirmed)
    lote_consistency = mine_lote_consistency(gold_dir, confirmed)
    sum_qtd = mine_sum_qtd_tolerance(gold_dir, confirmed)
    ov_to_cliente = mine_ov_to_cliente(gold_dir, confirmed)

    out_path = repo / "lexicons" / "learned.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "version": "0.1.0",
                "source": {
                    "gold_dir": str(gold_dir.relative_to(repo)),
                    "extractions_dir": str(ext_dir.relative_to(repo)),
                    "confirmed_sheets": sorted(confirmed),
                    "n_sheets": len(confirmed),
                },
                "confusion_matrix": confusion,
                "operator_stats": operator_stats,
                "lote_consistency": lote_consistency,
                "sum_qtd_tolerance": sum_qtd,
                "ov_to_cliente": ov_to_cliente,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Wrote {out_path.relative_to(repo)}")
    print(f"  confusion_matrix:  {len(confusion)} gold-chars with substitutions")
    print(f"  operator_stats:    {len(operator_stats)} operadores")
    print(f"  lote_consistency:  {len(lote_consistency)} unique lotes")
    print(f"  sum_qtd tolerance: max={sum_qtd['max_observed']:.3f} (n={sum_qtd['n']})")
    print(f"  ov_to_cliente:     {len(ov_to_cliente)} OV mappings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
