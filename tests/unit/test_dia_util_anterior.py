"""R265 — a data do kanban é o dia útil anterior ao dia do upload.

Cobre as duas metades da regra: o calendário (o que é dia útil) e o carimbo
(quem escreve `header.data`, e quando NÃO escreve).
"""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest
from app.web import calendario, db, main

# 2026-08-03 é uma segunda-feira. Toda a semana de referência dos testes:
#   seg 03 · ter 04 · qua 05 · qui 06 · sex 07 · sáb 08 · dom 09 · seg 10
SEG = date(2026, 8, 3)
TER = date(2026, 8, 4)
QUA = date(2026, 8, 5)
QUI = date(2026, 8, 6)
SEX = date(2026, 8, 7)
SAB = date(2026, 8, 8)
DOM = date(2026, 8, 9)
SEG2 = date(2026, 8, 10)


@pytest.fixture()
def cal_file(tmp_path, monkeypatch):
    """Aponta o calendário para um ficheiro temporário e limpa a cache."""
    path = tmp_path / "calendario_util.json"
    monkeypatch.setattr(calendario, "_CAL_PATH", path)
    calendario.invalidate_cache()
    yield path
    calendario.invalidate_cache()


def _escreve(cal_file, cal: dict, version: int = 1) -> None:
    cal_file.write_text(
        json.dumps({"version": version, "calendario": cal, "history": []}),
        encoding="utf-8")
    calendario.invalidate_cache()


# --- calendário: defaults (segunda a sábado) --------------------------------

@pytest.mark.parametrize(("ref", "esperado"), [
    (TER, SEG),
    (QUA, TER),
    (QUI, QUA),
    (SEX, QUI),
    (SAB, SEX),
    (DOM, SAB),
    (SEG2, SAB),   # a produção de sábado de manhã não se cola à sexta
])
def test_default_seg_a_sab(cal_file, ref, esperado):
    assert calendario.dia_util_anterior(ref) == esperado


def test_domingo_nunca_e_dia_util_por_defeito(cal_file):
    assert calendario.is_dia_util(SAB) is True
    assert calendario.is_dia_util(DOM) is False


def test_sem_ficheiro_usa_defaults(cal_file):
    assert not cal_file.exists()
    state = calendario.load_state()
    assert state["version"] == 0
    assert state["calendario"] == calendario.DEFAULT_CALENDARIO


def test_json_corrompido_usa_defaults(cal_file):
    cal_file.write_text("{isto não é json", encoding="utf-8")
    calendario.invalidate_cache()
    assert calendario.get_calendario() == calendario.DEFAULT_CALENDARIO
    assert calendario.dia_util_anterior(SEG2) == SAB


# --- calendário: configurações alternativas ---------------------------------

def test_sem_sabado_segunda_recua_para_sexta(cal_file):
    _escreve(cal_file, {"dias_semana": [0, 1, 2, 3, 4],
                        "nao_uteis": [], "uteis_extra": []})
    assert calendario.dia_util_anterior(SEG2) == SEX
    assert calendario.dia_util_anterior(DOM) == SEX


def test_feriado_e_saltado(cal_file):
    _escreve(cal_file, {"dias_semana": [0, 1, 2, 3, 4, 5],
                        "nao_uteis": [{"data": QUA.isoformat(), "nota": "feriado"}],
                        "uteis_extra": []})
    assert calendario.dia_util_anterior(QUI) == TER


def test_feriados_seguidos_sao_saltados(cal_file):
    _escreve(cal_file, {
        "dias_semana": [0, 1, 2, 3, 4, 5],
        "nao_uteis": [{"data": QUA.isoformat(), "nota": "ponte"},
                      {"data": TER.isoformat(), "nota": "feriado"}],
        "uteis_extra": []})
    assert calendario.dia_util_anterior(QUI) == SEG


def test_feriado_encadeia_com_fim_de_semana(cal_file):
    # Sábado não trabalhado (mesma lista dos feriados) → segunda cai na sexta.
    _escreve(cal_file, {"dias_semana": [0, 1, 2, 3, 4, 5],
                        "nao_uteis": [{"data": SAB.isoformat(), "nota": "sem turno"}],
                        "uteis_extra": []})
    assert calendario.dia_util_anterior(SEG2) == SEX


def test_util_extra_ganha_ao_dia_da_semana(cal_file):
    _escreve(cal_file, {"dias_semana": [0, 1, 2, 3, 4, 5],
                        "nao_uteis": [],
                        "uteis_extra": [{"data": DOM.isoformat(), "nota": "extra"}]})
    assert calendario.is_dia_util(DOM) is True
    assert calendario.dia_util_anterior(SEG2) == DOM


def test_config_degenerada_nao_faz_loop(cal_file):
    _escreve(cal_file, {"dias_semana": [], "nao_uteis": [], "uteis_extra": []})
    assert calendario.dia_util_anterior(QUI) == QUA  # ref-1, sem exceção


# --- gravação ---------------------------------------------------------------

def test_save_incrementa_versao_e_normaliza(cal_file):
    novo = calendario.save_calendario(
        {"dias_semana": [5, 0, 0, 1], "nao_uteis": ["2026-12-25"],
         "uteis_extra": []},
        expected_version=0)
    assert novo["version"] == 1
    assert novo["calendario"]["dias_semana"] == [0, 1, 5]
    assert novo["calendario"]["nao_uteis"] == [{"data": "2026-12-25", "nota": ""}]


def test_save_com_versao_velha_da_conflito(cal_file):
    calendario.save_calendario(
        {"dias_semana": [0, 1, 2, 3, 4], "nao_uteis": [], "uteis_extra": []},
        expected_version=0)
    with pytest.raises(calendario.CalendarioVersionConflict):
        calendario.save_calendario(
            {"dias_semana": [0], "nao_uteis": [], "uteis_extra": []},
            expected_version=0)


def test_save_rejeita_semana_vazia_e_data_invalida(cal_file):
    with pytest.raises(ValueError):
        calendario.save_calendario(
            {"dias_semana": [], "nao_uteis": [], "uteis_extra": []}, 0)
    with pytest.raises(ValueError):
        calendario.save_calendario(
            {"dias_semana": [0], "nao_uteis": ["25-12-2026"], "uteis_extra": []}, 0)


def test_save_rejeita_dia_nas_duas_listas(cal_file):
    with pytest.raises(ValueError, match="ambas as listas"):
        calendario.save_calendario(
            {"dias_semana": [0, 1, 2, 3, 4],
             "nao_uteis": ["2026-12-25"], "uteis_extra": ["2026-12-25"]}, 0)


def test_revert_repoe_defaults(cal_file):
    calendario.save_calendario(
        {"dias_semana": [0], "nao_uteis": [], "uteis_extra": []}, 0)
    state = calendario.revert_calendario("defaults")
    assert state["calendario"] == calendario.DEFAULT_CALENDARIO


# --- carimbo no pipeline ----------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "test_app.db")
    db.init_db()


def _sheet_data(data: str = "01-01-2020") -> dict:
    return {
        "template_name": "bobine_formato",
        "header": {
            "operador": "JULIO LIMA",
            "n_operador": "537",
            "data": data,
            "setor_maquina": "BOBINE-FORMATO",
            "cod_maquina": "M032",
        },
        "rows": [{"of": "262107", "modelo": "CFC5F45RIV", "qtd": "4",
                  "lote": "H26B0546", "larg_mm": "1200", "esp": "2.6"}],
        "footer": {"horas_trabalhadas": "8"},
    }


def _sheet_capturada_em(quando: str) -> int:
    """Cria uma folha e força o `captured_at` (UTC, como o CURRENT_TIMESTAMP)."""
    sid = db.insert_sheet("images/dia-util.jpg")
    with db.conn() as c:
        c.execute("UPDATE sheets SET captured_at = ? WHERE id = ?", (quando, sid))
    return sid


def test_carimbo_ignora_a_data_lida_pelo_ocr(cal_file, tmp_db):
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    current = _sheet_data("31-12-1999")
    assert main._stamp_dia_util_anterior(sid, current) == SAB.strftime("%d-%m-%Y")
    assert current["header"]["data"] == "08-08-2026"


def test_carimbo_regista_o_valor_do_ocr_no_audit(cal_file, tmp_db):
    sid = _sheet_capturada_em(f"{QUI.isoformat()} 09:00:00")
    main._stamp_dia_util_anterior(sid, _sheet_data("31-12-1999"))
    with db.conn() as c:
        rows = c.execute(
            "SELECT field_path, old_value, new_value, source FROM edits "
            "WHERE sheet_id = ?", (sid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["field_path"] == "header.data"
    assert rows[0]["old_value"] == "31-12-1999"
    assert rows[0]["new_value"] == "05-08-2026"
    assert rows[0]["source"] == main.DIA_UTIL_EDIT_SOURCE
    # A linha documenta uma escrita do sistema: NÃO pode proteger a célula.
    assert rows[0]["source"] not in db.AUTHORITATIVE_EDIT_SOURCES
    assert "header.data" not in main._human_edited_paths(sid)


def test_carimbo_nao_reverte_correcao_humana(cal_file, tmp_db):
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    db.update_extraction(sid, _sheet_data(), {}, _sheet_data())
    db.apply_edit(sid, "header.data", "01-07-2026", source="human")
    current = copy.deepcopy(db.get_sheet(sid)["sheet_data"])
    assert current["header"]["data"] == "01-07-2026"
    assert main._stamp_dia_util_anterior(sid, current) is None
    assert current["header"]["data"] == "01-07-2026"


def test_rebuild_from_raw_nao_ressuscita_a_data_manuscrita(cal_file, tmp_db):
    """R212 reconstrói o sheet_data a partir do OCR cru; a data é DERIVADA e
    tem de ser recarimbada, senão um bump de ENGINE_VERSION reporia o que o
    modelo leu na folha."""
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    raw = _sheet_data("31-12-1999")
    current = _sheet_data("31-12-1999")
    main._stamp_dia_util_anterior(sid, current)
    db.update_extraction(sid, raw, {}, current)
    # Suja o sheet_data como um snap antigo faria, para o rebuild ter efeito.
    db.apply_edit(sid, "header.operador", "OUTRO NOME", source="system")

    assert main._rebuild_sheet_data_from_raw(sid, db.get_sheet(sid)) is True
    reconstruido = db.get_sheet(sid)["sheet_data"]
    assert reconstruido["header"]["operador"] == "JULIO LIMA"  # veio do raw
    assert reconstruido["header"]["data"] == "08-08-2026"      # recarimbada
    # Sem linha de audit duplicada: o carimbo original já ficou registado.
    with db.conn() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM edits WHERE sheet_id = ? AND source = ?",
            (sid, main.DIA_UTIL_EDIT_SOURCE)).fetchone()["n"]
    assert n == 1


def test_ancora_e_o_upload_nao_o_dia_do_reprocesso(cal_file, tmp_db):
    """Reprocessar dias depois tem de devolver exatamente a mesma data."""
    sid = _sheet_capturada_em(f"{QUA.isoformat()} 07:30:00")
    primeira = main._stamp_dia_util_anterior(sid, _sheet_data())
    segunda = main._stamp_dia_util_anterior(sid, _sheet_data())
    assert primeira == segunda == TER.strftime("%d-%m-%Y")
    assert primeira != date.today().strftime("%d-%m-%Y")


def test_captured_at_e_convertido_para_hora_local(cal_file, tmp_db):
    """captured_at é UTC; a âncora tem de ser o dia LOCAL."""
    sid = _sheet_capturada_em(f"{QUA.isoformat()} 23:30:00")
    local = db.captured_local_date(sid)
    with db.conn() as c:
        esperado = c.execute(
            "SELECT DATE(captured_at,'localtime') AS d FROM sheets WHERE id = ?",
            (sid,)).fetchone()["d"]
    assert local == date.fromisoformat(esperado)


def test_modo_ocr_preserva_o_valor_lido(cal_file, tmp_db, monkeypatch):
    from app import config
    monkeypatch.setattr(config.get_settings(), "kanban_date_mode", "ocr")
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    current = _sheet_data("31-12-1999")
    assert main._stamp_dia_util_anterior(sid, current) is None
    assert current["header"]["data"] == "31-12-1999"


def test_sem_cabecalho_nao_rebenta(cal_file, tmp_db):
    sid = _sheet_capturada_em(f"{QUI.isoformat()} 09:00:00")
    assert main._stamp_dia_util_anterior(sid, {"template_name": "paragens"}) is None


def test_worker_carimba_a_folha_processada(cal_file, tmp_db, tmp_path, monkeypatch):
    """Wiring: o carimbo tem de estar ligado ao worker, não só disponível."""
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    img = tmp_path / "images" / "dia-util.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"fake")
    monkeypatch.setattr(main, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(main.ocr_runner, "run_pipeline", lambda *a, **k: {
        "raw": _sheet_data("31-12-1999"),
        "dq": {},
        "current": _sheet_data("31-12-1999"),
    })
    monkeypatch.setattr(main, "_run_and_store_cross_check", lambda *a, **k: None)
    monkeypatch.setattr(main, "_deposit_csv_to_factory", lambda *a, **k: None)

    main._process_sheet_ocr(sid)

    sheet = db.get_sheet(sid)
    assert sheet["status"] == "extracted"
    assert sheet["sheet_data"]["header"]["data"] == "08-08-2026"
    # O raw é o instantâneo imutável do OCR: mantém o que o modelo leu.
    assert sheet["raw_extraction"]["header"]["data"] == "31-12-1999"


def test_admin_calendario_abre_e_grava(cal_file, tmp_db):
    from fastapi.testclient import TestClient
    client = TestClient(main.app)

    page = client.get("/admin/calendario")
    assert page.status_code == 200
    assert "Dias da semana trabalhados" in page.text

    r = client.post("/admin/calendario/dias",
                    data={"expected_version": 0, "dia": ["0", "1", "2", "3", "4"]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calendario.get_calendario()["dias_semana"] == [0, 1, 2, 3, 4]
    assert calendario.dia_util_anterior(SEG2) == SEX

    r = client.post("/admin/calendario/excecao",
                    data={"expected_version": 1, "tipo": "nao_uteis",
                          "data": SEX.isoformat(), "nota": "ponte"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert calendario.dia_util_anterior(SEG2) == QUI

    # Versão desatualizada não escreve por cima do outro separador.
    r = client.post("/admin/calendario/dias",
                    data={"expected_version": 0, "dia": ["0"]},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert calendario.get_calendario()["dias_semana"] == [0, 1, 2, 3, 4]


def test_production_rows_herdam_a_data_carimbada(cal_file, tmp_db):
    sid = _sheet_capturada_em(f"{SEG2.isoformat()} 08:12:00")
    current = _sheet_data("31-12-1999")
    main._stamp_dia_util_anterior(sid, current)
    db.update_extraction(sid, _sheet_data("31-12-1999"), {}, current)
    with db.conn() as c:
        rows = c.execute(
            "SELECT sheet_date, sheet_iso_date FROM production_rows "
            "WHERE sheet_id = ?", (sid,)).fetchall()
    assert rows
    assert rows[0]["sheet_date"] == "08-08-2026"
    assert rows[0]["sheet_iso_date"] == "2026-08-08"
