"""R257 — lint do ground truth: o ano do campo data == ano do filename.

Apanhou um erro real: ground_truth/JulioLima_2026.04.15.json tinha
"15-04-2020" onde a fotografia mostra 15-04-2026 (o draft da IA estava
certo; o erro entrou na revisão humana). Como o GT alimenta as métricas de
accuracy, um label errado penaliza leituras corretas silenciosamente.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_GT_DIR = Path(__file__).resolve().parents[2] / "ground_truth"
_FNAME_RE = re.compile(r"_(\d{4})\.(\d{2})\.(\d{2})")
# Dia/mês com 1-2 dígitos: o GT transcreve o manuscrito fielmente
# ("10-4-2026" é o que o operador escreveu — não é erro).
_DATA_RE = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")


def _gt_files() -> list[Path]:
    return sorted(_GT_DIR.glob("*.json"))


@pytest.mark.parametrize("gt_path", _gt_files(), ids=lambda p: p.name)
def test_gt_data_year_matches_filename(gt_path: Path):
    m_name = _FNAME_RE.search(gt_path.stem)
    if m_name is None:
        pytest.skip(f"{gt_path.name}: filename sem data AAAA.MM.DD")
    gt = json.loads(gt_path.read_text(encoding="utf-8-sig"))
    data = str((gt.get("header") or {}).get("data") or "").strip()
    if not data:
        pytest.skip(f"{gt_path.name}: header.data vazio")
    m_data = _DATA_RE.match(data)
    assert m_data, f"{gt_path.name}: header.data {data!r} não é DD-MM-AAAA"
    assert m_data.group(3) == m_name.group(1), (
        f"{gt_path.name}: ano do header.data ({data}) difere do filename "
        f"({m_name.group(1)}) — provável typo de revisão (caso JulioLima "
        f"2026→2020, R257)"
    )
