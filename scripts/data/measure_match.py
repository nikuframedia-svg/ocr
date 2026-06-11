"""Measure MATCH% per sector after re-OCR + reload-refs."""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.cross_check import storage  # noqa: E402
from app.pipeline.scoring_engine import ENGINE_VERSION  # noqa: E402
from app.web import db  # noqa: E402


def _iter_sheet_cells(sheet: dict):
    for row in sheet.get("rows", []) or []:
        for info in (row.get("fields") or {}).values():
            yield info or {}
    for section in ("header", "footer"):
        for info in (sheet.get(section) or {}).values():
            yield info or {}


c = sqlite3.connect(str(db.db_path()))
c.row_factory = sqlite3.Row
setor_by_sid = {
    r["id"]: ((r["s"] or "").strip().upper() or "(NONE)")
    for r in c.execute(
        "SELECT id, json_extract(sheet_data,'$.header.setor_maquina') AS s FROM sheets WHERE sheet_data IS NOT NULL"
    )
}

by_sector = defaultdict(lambda: {"match": 0, "no_match": 0, "na": 0, "total": 0})
for cc in storage.iter_sheet_cross_checks():
    sid = cc.get("sheet_id")
    sec = setor_by_sid.get(int(sid), "(NONE)") if sid is not None else "(NONE)"
    for info in _iter_sheet_cells(cc):
        st = info.get("status", "NA").lower()
        if st in by_sector[sec]:
            by_sector[sec][st] += 1
        by_sector[sec]["total"] += 1

summary = storage.load_summary()
print(f"ENGINE: {ENGINE_VERSION}  stale skipped: {summary.get('stale_sheets', 0)}")
print(f'{"SECTOR":<18} {"M":>5} {"NM":>4} {"NA":>4} {"TOT":>5}  MATCH%')
tM = tNM = tNA = tT = 0
for sec in sorted(by_sector, key=lambda s: -by_sector[s]["total"]):
    d = by_sector[sec]
    t = d["total"]
    comparable = d["match"] + d["no_match"]
    m_pct = 100 * d["match"] / comparable if comparable else 0
    print(f'{sec:<18} {d["match"]:>5} {d["no_match"]:>4} {d["na"]:>4} {t:>5}  {m_pct:>5.1f}%')
    tM += d["match"]; tNM += d["no_match"]; tNA += d["na"]; tT += t
print("-" * 50)
comparable = tM + tNM
m_pct = 100 * tM / comparable if comparable else 0
print(f'{"GLOBAL":<18} {tM:>5} {tNM:>4} {tNA:>4} {tT:>5}  {m_pct:>5.1f}%')
