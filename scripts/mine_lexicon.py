"""Extract observed values from draft (or gold) annotation JSONs.

Output: ``backend/app/pipeline/inference/lexicons/observed.json`` with
the unique values seen in each free-form field. Used as *hints* in
the prompt (not enforcement) so the model knows the working
vocabulary without being constrained against unseen variants.

Usage:
    python scripts/mine_lexicon.py
    python scripts/mine_lexicon.py --source ground_truth/
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "backend"))

from app.pipeline.inference.schemas import KanbanExtraction  # noqa: E402

_OUT = (
    _PROJECT_ROOT / "backend" / "app" / "pipeline" / "inference" / "lexicons" / "observed.json"
)


def _harvest(source_dir: Path) -> dict[str, list[str]]:
    """Return per-field sorted unique values seen across all JSONs."""
    counters: dict[str, Counter[str]] = {
        "operador": Counter(),
        "n_operador": Counter(),
        "setor_maquina": Counter(),
        "cliente": Counter(),
        "coni": Counter(),
        "esp": Counter(),
        "horas_trabalhadas": Counter(),
        "modelo_prefix": Counter(),  # first 3-5 chars of MODELO
        "lote": Counter(),
    }

    for path in sorted(source_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        ext = KanbanExtraction.model_validate(data)
        h = ext.header
        if h.operador:
            counters["operador"][h.operador] += 1
        if h.n_operador:
            counters["n_operador"][h.n_operador] += 1
        if h.setor_maquina:
            counters["setor_maquina"][h.setor_maquina] += 1
        for row in ext.rows:
            if row.cliente:
                counters["cliente"][row.cliente] += 1
            if row.coni:
                counters["coni"][row.coni] += 1
            if row.esp:
                counters["esp"][row.esp] += 1
            if row.lote:
                counters["lote"][row.lote] += 1
            if row.modelo:
                # Capture first 4 chars of modelo as a family prefix
                # (CGC, CLC, CD, CB, CA, OMEGA, PTJ, ... kept literal)
                head = row.modelo[:5]
                counters["modelo_prefix"][head] += 1
        if ext.footer.horas_trabalhadas:
            counters["horas_trabalhadas"][ext.footer.horas_trabalhadas] += 1

    return {field: sorted(c.keys()) for field, c in counters.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=_PROJECT_ROOT / "ground_truth_draft",
        help="folder with annotation JSONs (default: ground_truth_draft/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_OUT,
        help=f"output JSON (default: {_OUT.relative_to(_PROJECT_ROOT)})",
    )
    args = parser.parse_args()

    if not args.source.is_dir():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 2

    lexicon = _harvest(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(lexicon, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.output}")
    for field, values in lexicon.items():
        print(f"  {field}: {len(values)} unique")
    return 0


if __name__ == "__main__":
    sys.exit(main())
