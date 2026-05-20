"""Measure MATCH% per sector after re-OCR + reload-refs."""
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

c = sqlite3.connect(r"C:\Users\User\ocr\data\app.db")
c.row_factory = sqlite3.Row
setor_by_sid = {
    r["id"]: ((r["s"] or "").strip().upper() or "(NONE)")
    for r in c.execute(
        "SELECT id, json_extract(sheet_data,'$.header.setor_maquina') AS s FROM sheets WHERE sheet_data IS NOT NULL"
    )
}

base = Path(r"C:\kanban\nifruka\03_Cross_Check")
idx = json.loads((base / "_index.json").read_text(encoding="utf-8"))
sheets_idx = idx.get("sheets", idx)

by_sector = defaultdict(lambda: {"match": 0, "no_match": 0, "na": 0, "total": 0})
for sid_s, e in sheets_idx.items():
    sec = setor_by_sid.get(int(sid_s), "(NONE)")
    cc = json.loads((base / e["file"]).read_text(encoding="utf-8"))
    for row in cc.get("rows", []) or []:
        for fn, info in (row.get("fields") or {}).items():
            st = info.get("status", "NA").lower()
            if st in by_sector[sec]:
                by_sector[sec][st] += 1
            by_sector[sec]["total"] += 1

print(f'{"SECTOR":<18} {"M":>5} {"NM":>4} {"NA":>4} {"TOT":>5}  MATCH%')
tM = tNM = tNA = tT = 0
for sec in sorted(by_sector, key=lambda s: -by_sector[s]["total"]):
    d = by_sector[sec]
    t = d["total"]
    m_pct = 100 * d["match"] / t if t else 0
    print(f'{sec:<18} {d["match"]:>5} {d["no_match"]:>4} {d["na"]:>4} {t:>5}  {m_pct:>5.1f}%')
    tM += d["match"]; tNM += d["no_match"]; tNA += d["na"]; tT += t
print("-" * 50)
m_pct = 100 * tM / tT if tT else 0
print(f'{"GLOBAL":<18} {tM:>5} {tNM:>4} {tNA:>4} {tT:>5}  {m_pct:>5.1f}%')
