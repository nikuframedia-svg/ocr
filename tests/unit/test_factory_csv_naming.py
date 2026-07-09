"""R257 — CSVs da fábrica: colisão Operador_data, re-depósito e delete exato.

Bugs corrigidos (auditoria externa, confirmados):
- ``Operador_AAAA.MM.DD.csv`` sem uniquificador + overwrite silencioso: duas
  folhas do mesmo operador no mesmo dia clobber-avam-se (colisão real no
  dataset: JulioLima_2026.04.15 e ...-1).
- ``db.delete_sheet`` apagava CSVs por ``rglob(f"*{stem}*.csv")`` — por
  substring, apagar uma folha levava o CSV da irmã sobrevivente; e para CSVs
  com nome de operador (não stem) nunca encontrava nada (órfãos).
"""
from __future__ import annotations

import pytest
from app.web import db, main


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app.db"
    monkeypatch.setattr(db, "_DB_PATH", test_db)
    db.init_db()
    return test_db


@pytest.fixture()
def factory_dir(tmp_path, monkeypatch):
    d = tmp_path / "factory_csv"
    d.mkdir()
    (d / "imported").mkdir()
    monkeypatch.setattr(main, "_FACTORY_CSV_DIR", d)
    # db.delete_sheet resolve o dir via env (resolve_kanban_path).
    monkeypatch.setenv("FACTORY_CSV_DIR", str(d))
    # O conteúdo do CSV não interessa a estes testes — só o naming.
    monkeypatch.setattr(main, "_to_3block_csv", lambda *_a, **_k: "CSV\n")
    return d


def _mk_sheet(operador: str = "Julio Lima", data: str = "15-04-2026") -> int:
    sid = db.insert_sheet(f"img_{operador.replace(' ', '')}_{data}.jpg")
    sheet_data = {"header": {"operador": operador, "data": data}, "rows": []}
    db.update_extraction(sheet_id=sid, raw_extraction=sheet_data,
                         dq_audit={}, sheet_data=sheet_data)
    return sid


class TestFactoryCsvCollision:
    def test_two_sheets_same_operador_date_get_distinct_files(
            self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        s2 = _mk_sheet()
        p1 = main._deposit_csv_to_factory(s1)
        p2 = main._deposit_csv_to_factory(s2)
        assert p1.name == "JulioLima_2026.04.15.csv"
        assert p2.name == "JulioLima_2026.04.15-1.csv"
        assert p1.exists() and p2.exists()

    def test_redeposit_overwrites_own_file_not_a_new_one(
            self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        s2 = _mk_sheet()
        main._deposit_csv_to_factory(s1)
        p2a = main._deposit_csv_to_factory(s2)
        p2b = main._deposit_csv_to_factory(s2)  # validate re-deposita
        assert p2a == p2b
        assert len(list(factory_dir.glob("*.csv"))) == 2

    def test_header_rename_moves_file_instead_of_leaving_stale(
            self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        main._deposit_csv_to_factory(s1)
        sheet_data = {"header": {"operador": "Vitor Carvalho",
                                 "data": "15-04-2026"}, "rows": []}
        db.update_extraction(sheet_id=s1, raw_extraction=sheet_data,
                             dq_audit={}, sheet_data=sheet_data)
        p = main._deposit_csv_to_factory(s1)
        assert p.name == "VitorCarvalho_2026.04.15.csv"
        assert not (factory_dir / "JulioLima_2026.04.15.csv").exists()


class TestFactoryCsvDelete:
    def test_delete_removes_exact_file_and_spares_sibling(
            self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        s2 = _mk_sheet()
        main._deposit_csv_to_factory(s1)
        main._deposit_csv_to_factory(s2)
        db.delete_sheet(s1)
        assert not (factory_dir / "JulioLima_2026.04.15.csv").exists()
        # O glob antigo *stem* teria levado também o "-1" da irmã.
        assert (factory_dir / "JulioLima_2026.04.15-1.csv").exists()

    def test_delete_finds_file_moved_to_imported(self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        p1 = main._deposit_csv_to_factory(s1)
        # O importador da fábrica move CSVs consumidos para imported/.
        moved = factory_dir / "imported" / p1.name
        p1.rename(moved)
        db.delete_sheet(s1)
        assert not moved.exists()

    def test_delete_legacy_sheet_without_record_uses_computed_name(
            self, tmp_db, factory_dir):
        s1 = _mk_sheet()
        # Simula folha pré-R257: CSV existe mas sem registo na DB.
        (factory_dir / "JulioLima_2026.04.15.csv").write_text("CSV\n")
        db.set_factory_csv_name(s1, "")
        with db.conn() as c:
            c.execute("UPDATE sheets SET factory_csv_name = NULL WHERE id = ?",
                      (s1,))
        db.delete_sheet(s1)
        assert not (factory_dir / "JulioLima_2026.04.15.csv").exists()
