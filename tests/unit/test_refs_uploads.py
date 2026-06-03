"""Tests for Round 106 — refs upload log, OF normalisation, phase priority."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.cross_check import ref_watcher, refs_uploads, storage
from app.pipeline.scoring_engine import normalize_of
from app.web import main


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    monkeypatch.setattr(refs_uploads, "_LOG_PATH", tmp_path / "refs_uploads.json")
    return tmp_path / "refs_uploads.json"


def _write_plan(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "plan_colunas_cpis"
    ws.append([
        "cliente", "ov", "of", "designacao", "quanttrp",
        "bf", "c", "q", "s", "r", "a", "exp",
        "esp", "lbase", "ltopo", "comp",
    ])
    for row in rows:
        ws.append(row)
    wb.save(path)


def _write_stocksap(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Folha1"
    ws.append(["Lote", "Qtd", "Espessura", "Largura", "Desc"])
    for row in rows:
        ws.append(row)
    wb.save(path)


def _write_maquinas(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append([
        "codmaq", "desmaq", "desigkanban", "codsec", "ativo",
        "ordem", "operacao", "centrotrab", "dessec", "colunaexcel",
    ])
    for row in rows:
        ws.append(row)
    wb.save(path)


def _write_colaboradores(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append(["pernr", "sname", "cod"])
    for row in rows:
        ws.append(row)
    wb.save(path)


# --- upload log -----------------------------------------------------------

def test_upload_log_records_most_recent_first(log_path):
    assert refs_uploads.recent() == []
    refs_uploads.record(
        "plan", "plan_colunas_cpis.xlsx", 16617,
        sha256="abc123", n_ofs=5291, n_ovs=5000, size=123456,
    )
    refs_uploads.record("stocksap", "StockSAP.xlsx", 2770)
    rows = refs_uploads.recent()
    assert len(rows) == 2
    assert rows[0]["kind"] == "stocksap"        # most recent first
    assert rows[1]["kind"] == "plan"
    assert rows[1]["n_rows"] == 16617
    assert rows[1]["sha256"] == "abc123"
    assert rows[1]["n_ofs"] == 5291
    assert rows[1]["n_ovs"] == 5000
    assert rows[1]["size"] == 123456


# --- OF normalisation -----------------------------------------------------

def test_normalize_of():
    assert normalize_of("52306") == "052306"     # zero-pad short
    assert normalize_of("254877") == "254877"    # already 6
    assert normalize_of(254877) == "254877"      # int input
    assert normalize_of("05000A") == "05000A"    # has letters — untouched
    assert normalize_of("2502343") == "2502343"  # 7 digits — untouched
    assert normalize_of("") == ""
    assert normalize_of(None) == ""


# --- phase helpers --------------------------------------------------------

def test_phase_columns_detected_between_quanttrp_and_esp():
    hdrs = {"cliente": 0, "ov": 1, "of": 2, "designacao": 3, "quanttrp": 4,
            "bf": 5, "c": 6, "q": 7, "s": 8, "r": 9, "a": 10, "exp": 11,
            "esp": 12, "lbase": 13}
    assert ref_watcher._phase_columns(hdrs) == ["bf", "c", "q", "s", "r", "a", "exp"]


def test_phase_columns_empty_when_no_quanttrp():
    assert ref_watcher._phase_columns({"of": 0, "esp": 1}) == []


def test_fase_incompleta():
    # all phases == quanttrp → concluded
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": 20, "exp": 20}) is False
    # one phase below quanttrp → still in production
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": 20, "exp": 10}) is True
    # blank phase counts as 0 → incomplete
    assert ref_watcher._fase_incompleta(20, {"bf": 20, "c": None}) is True
    # quanttrp 0 / blank → cannot judge → not flagged
    assert ref_watcher._fase_incompleta(0, {"bf": 0}) is False
    assert ref_watcher._fase_incompleta(None, {"bf": 5}) is False


# --- plan upload audit -----------------------------------------------------

def test_ref_watcher_tracks_plan_hash_and_snapshot(tmp_path):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    plan = doc_dir / "plan_colunas_cpis.xlsx"
    _write_plan(plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
        ["B", "2603978", "263349", "CAC4E10C", 2, 0, 0, 0, 0, 0, 0, 0, 3, 600, 200, 9000],
    ])

    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    refs = watcher.force_reload()
    sha = ref_watcher.file_sha256(plan)

    assert refs["plan_sha256"] == sha
    assert refs["plan_size"] == plan.stat().st_size
    assert refs["stats"]["n_plan_rows"] == 2
    assert refs["stats"]["n_ofs"] == 2
    assert refs["stats"]["n_ovs"] == 2

    snap = ref_watcher.refs_snapshot(refs, watcher.plan_path)
    assert snap["plan_sha256"] == sha
    assert snap["plan_rows"] == 2
    assert snap["plan_ofs"] == 2
    assert snap["plan_ovs"] == 2
    assert snap["files"]["plan"]["sha256"] == sha
    assert snap["files"]["plan"]["rows"] == 2

    status = json.loads((doc_dir / "_refs_status.json").read_text())
    assert status["plan_colunas"]["sha256"] == sha
    assert status["plan_colunas"]["n_ovs"] == 2


def test_ref_watcher_tracks_all_ref_workbook_hashes_and_snapshot(tmp_path):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    plan = doc_dir / "plan_colunas_cpis.xlsx"
    sap = doc_dir / "StockSAP.xlsx"
    maquinas = doc_dir / "maquinas.xlsx"
    colabs = doc_dir / "ListaColaboradores.xlsx"
    _write_plan(plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
    ])
    _write_stocksap(sap, [["M26B001", 1, 2.6, 1250, "bobine"]])
    _write_maquinas(maquinas, [
        ["M061", "ACABAMENTO MTG2", "ACABAMENTO MTG2", "A", 1, 1, "", "", "ACABAMENTO", "a"],
        ["M062", "ACABAMENTO MTG4", "ACABAMENTO MTG4", "A", 1, 2, "", "", "ACABAMENTO", "a"],
    ])
    _write_colaboradores(colabs, [
        ["0000000537", "JULIO LIMA", 537],
        ["0000000123", "ANA SILVA", 123],
    ])

    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    refs = watcher.force_reload()
    snap = ref_watcher.refs_snapshot(refs, watcher.plan_path)

    assert snap["files"]["plan"]["sha256"] == ref_watcher.file_sha256(plan)
    assert snap["files"]["stocksap"]["sha256"] == ref_watcher.file_sha256(sap)
    assert snap["files"]["stocksap"]["lotes"] == 2  # lote + alias sem M
    assert snap["files"]["maquinas"]["sha256"] == ref_watcher.file_sha256(maquinas)
    assert snap["files"]["maquinas"]["maquinas"] == 2
    assert snap["files"]["colaboradores"]["sha256"] == ref_watcher.file_sha256(colabs)
    assert snap["files"]["colaboradores"]["colaboradores"] == 2

    status = watcher.status()
    assert status["sap"]["sha256"] == ref_watcher.file_sha256(sap)
    assert status["maquinas"]["n_maquinas"] == 2
    assert status["colaboradores"]["n_colaboradores"] == 2


def test_plan_inspection_rejects_missing_required_columns(tmp_path):
    bad = tmp_path / "bad_plan.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["of", "cliente"])
    ws.append(["263348", "COBELBA"])
    wb.save(bad)

    err, info = main._inspect_refs_xlsx(bad, "plan")

    assert err is not None
    assert "ov" in err
    assert "quanttrp" in err
    assert info["n_rows"] == 0


def test_refs_inspection_accepts_machines_and_collaborators(tmp_path):
    maquinas = tmp_path / "maquinas.xlsx"
    colabs = tmp_path / "ListaColaboradores.xlsx"
    _write_maquinas(maquinas, [
        ["M061", "ACABAMENTO MTG2", "ACABAMENTO MTG2", "A", 1, 1, "", "", "ACABAMENTO", "a"],
        ["M030", "LASER", "LASER", "C", 1, 2, "", "", "CORTE", "c"],
    ])
    _write_colaboradores(colabs, [
        ["0000000537", "JULIO LIMA", 537],
        ["0000000123", "ANA SILVA", 123],
    ])

    maq_err, maq_info = main._inspect_refs_xlsx(maquinas, "maquinas")
    colab_err, colab_info = main._inspect_refs_xlsx(colabs, "colaboradores")

    assert maq_err is None
    assert maq_info["n_maquinas"] == 2
    assert colab_err is None
    assert colab_info["n_colaboradores"] == 2


def test_refs_inspection_rejects_invalid_machines_and_collaborators(tmp_path):
    bad_maquinas = tmp_path / "bad_maquinas.xlsx"
    bad_colabs = tmp_path / "bad_colabs.xlsx"
    wb = Workbook()
    wb.active.append(["codmaq", "desmaq"])
    wb.save(bad_maquinas)
    wb = Workbook()
    wb.active.append(["pernr", "sname"])
    wb.save(bad_colabs)

    maq_err, _ = main._inspect_refs_xlsx(bad_maquinas, "maquinas")
    colab_err, _ = main._inspect_refs_xlsx(bad_colabs, "colaboradores")

    assert maq_err is not None
    assert "colunaexcel" in maq_err
    assert colab_err is not None
    assert "cod" in colab_err


def test_refs_upload_replaces_active_plan_and_reload_uses_same_hash(
    tmp_path, monkeypatch, log_path
):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    upload_plan = tmp_path / "upload_plan.xlsx"
    _write_plan(upload_plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
        ["B", "2603978", "263349", "CAC4E10C", 2, 0, 0, 0, 0, 0, 0, 0, 3, 600, 200, 9000],
        ["C", "2603979", "263350", "CAC4E10D", 3, 0, 0, 0, 0, 0, 0, 0, 3, 610, 210, 9100],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    monkeypatch.setattr(main, "get_watcher", lambda: watcher)

    client = TestClient(main.app)
    with upload_plan.open("rb") as f:
        resp = client.post(
            "/refs/upload",
            data={"kind": "plan"},
            files={"file": ("plan_colunas_cpis.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    active_sha = ref_watcher.file_sha256(watcher.plan_path)
    assert active_sha == ref_watcher.file_sha256(upload_plan)
    refs = watcher.get_refs()
    assert refs["plan_sha256"] == active_sha
    assert refs["stats"]["n_plan_rows"] == 3
    assert refs["stats"]["n_ofs"] == 3
    assert refs["stats"]["n_ovs"] == 3
    assert active_sha[:8] in resp.headers["location"]

    uploads = refs_uploads.recent()
    assert uploads[0]["sha256"] == active_sha
    assert uploads[0]["n_rows"] == 3
    assert uploads[0]["n_ofs"] == 3
    assert uploads[0]["n_ovs"] == 3


@pytest.mark.parametrize(
    ("kind", "filename", "writer", "rows", "stat_key", "count"),
    [
        (
            "stocksap", "StockSAP.xlsx", _write_stocksap,
            [["M26B001", 1, 2.6, 1250, "bobine"]],
            "n_lotes", 2,
        ),
        (
            "maquinas", "maquinas.xlsx", _write_maquinas,
            [
                ["M061", "ACABAMENTO MTG2", "ACABAMENTO MTG2", "A", 1, 1, "", "", "ACABAMENTO", "a"],
                ["M030", "LASER", "LASER", "C", 1, 2, "", "", "CORTE", "c"],
            ],
            "n_maquinas", 2,
        ),
        (
            "colaboradores", "ListaColaboradores.xlsx", _write_colaboradores,
            [["0000000537", "JULIO LIMA", 537], ["0000000123", "ANA SILVA", 123]],
            "n_colaboradores", 2,
        ),
    ],
)
def test_refs_upload_replaces_active_non_plan_refs_and_reload_uses_same_hash(
    tmp_path, monkeypatch, log_path, kind, filename, writer, rows, stat_key, count
):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    upload = tmp_path / filename
    writer(upload, rows)
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    monkeypatch.setattr(main, "get_watcher", lambda: watcher)

    client = TestClient(main.app)
    with upload.open("rb") as f:
        resp = client.post(
            "/refs/upload",
            data={"kind": kind},
            files={"file": (filename, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    target = getattr(watcher, main._REFS_WATCHER_ATTRS[kind])
    active_sha = ref_watcher.file_sha256(target)
    assert active_sha == ref_watcher.file_sha256(upload)
    refs = watcher.get_refs()
    assert refs[main._REFS_SHA_KEYS[kind]] == active_sha
    assert refs["stats"][stat_key] == count
    assert active_sha[:8] in resp.headers["location"]

    uploads = refs_uploads.recent()
    assert uploads[0]["kind"] == kind
    assert uploads[0]["sha256"] == active_sha
    assert uploads[0]["n_rows"] == count


def test_refs_page_shows_active_plan_hash_counts_and_path(
    tmp_path, monkeypatch, log_path
):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    plan = doc_dir / "plan_colunas_cpis.xlsx"
    _write_plan(plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
        ["B", "2603978", "263349", "CAC4E10C", 2, 0, 0, 0, 0, 0, 0, 0, 3, 600, 200, 9000],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    monkeypatch.setattr(main, "get_watcher", lambda: watcher)
    sha = ref_watcher.file_sha256(plan)

    resp = TestClient(main.app).get("/refs")

    assert resp.status_code == 200
    assert sha[:8] in resp.text
    assert "2 linhas" in resp.text
    assert "2 OVs" in resp.text
    assert str(plan) in resp.text


def test_refs_page_shows_all_ref_cards(tmp_path, monkeypatch, log_path):
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    _write_plan(doc_dir / "plan_colunas_cpis.xlsx", [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
    ])
    _write_stocksap(doc_dir / "StockSAP.xlsx", [["M26B001", 1, 2.6, 1250, "bobine"]])
    _write_maquinas(doc_dir / "maquinas.xlsx", [
        ["M061", "ACABAMENTO MTG2", "ACABAMENTO MTG2", "A", 1, 1, "", "", "ACABAMENTO", "a"],
    ])
    _write_colaboradores(doc_dir / "ListaColaboradores.xlsx", [
        ["0000000537", "JULIO LIMA", 537],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    monkeypatch.setattr(main, "get_watcher", lambda: watcher)

    resp = TestClient(main.app).get("/refs")

    assert resp.status_code == 200
    assert "plan_colunas_cpis.xlsx" in resp.text
    assert "StockSAP.xlsx" in resp.text
    assert "maquinas.xlsx" in resp.text
    assert "ListaColaboradores.xlsx" in resp.text
    assert 'value="maquinas"' in resp.text
    assert 'value="colaboradores"' in resp.text


def test_cross_check_storage_persists_refs_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_base_dir", lambda: tmp_path)
    snap = {
        "plan_path": "/tmp/plan_colunas_cpis.xlsx",
        "plan_sha256": "abc123",
        "plan_mtime": 123.0,
        "plan_rows": 3,
        "plan_ofs": 3,
        "plan_ovs": 3,
        "refs_loaded_at": "2026-06-03T10:00:00+00:00",
    }

    stored = storage.store_cross_check(
        sheet_id=1,
        image_path="images/a.jpg",
        operador="OPERADOR",
        date_iso="2026-06-03",
        sheet_status="extracted",
        cross_check_result={
            "checked_at": "2026-06-03T10:01:00+00:00",
            "refs_loaded_at": snap["refs_loaded_at"],
            "refs_snapshot": snap,
            "engine_version": "test",
            "summary": {"match": 0, "no_match": 0, "na": 0, "total": 0},
            "rows": [],
            "header": {},
            "footer": {},
            "to_analisar": [],
        },
    )

    payload = json.loads(Path(stored["file"]).read_text(encoding="utf-8"))
    assert payload["refs_snapshot"] == snap


def test_update_script_protects_all_uploaded_refs():
    script = Path("scripts/ops/update.ps1").read_text(encoding="utf-8")

    assert "kanban_refs/04_Documentacao/plan_colunas_cpis.xlsx" in script
    assert "kanban_refs/04_Documentacao/StockSAP.xlsx" in script
    assert "kanban_refs/04_Documentacao/maquinas.xlsx" in script
    assert "kanban_refs/04_Documentacao/ListaColaboradores.xlsx" in script


# --- R134 — robustez do upload do plano -----------------------------------

def test_miner_skips_blank_rows_without_truncating(tmp_path):
    """O minerador não deve terminar no primeiro `cliente` (col 0) vazio:
    antes (`if r[0] is None: break`) truncava OFs silenciosamente."""
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    plan = doc_dir / "plan_colunas_cpis.xlsx"
    _write_plan(plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
        # cliente (col 0) em branco mas OF presente — antes parava aqui (break)
        [None, "2603978", "263349", "CAC4E10C", 2, 0, 0, 0, 0, 0, 0, 0, 3, 600, 200, 9000],
        # linha totalmente vazia no meio — deve ser saltada, não terminar
        [None] * 16,
        ["C", "2603980", "263350", "CAC4E10D", 3, 0, 0, 0, 0, 0, 0, 0, 3, 610, 210, 9100],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    refs = watcher.force_reload()

    assert refs["stats"]["n_ofs"] == 3
    assert {"263348", "263349", "263350"} <= set(refs["of_to_entries"].keys())


def test_invalidate_index_cache_clears_all():
    from app.pipeline import scoring_engine
    scoring_engine._INDEX_CACHE[1] = {"loaded_at": "x"}
    scoring_engine._INDEX_CACHE[2] = {"loaded_at": "y"}
    scoring_engine.invalidate_index_cache()
    assert scoring_engine._INDEX_CACHE == {}


def test_force_reload_clears_scoring_index_cache(tmp_path):
    from app.pipeline import scoring_engine
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    _write_plan(doc_dir / "plan_colunas_cpis.xlsx", [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
    ])
    scoring_engine._INDEX_CACHE[424242] = {"loaded_at": "stale"}
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    watcher.force_reload()
    assert 424242 not in scoring_engine._INDEX_CACHE


def test_get_refs_degrades_on_corrupt_file_without_crashing(tmp_path):
    """Um ficheiro on-disk corrupto não deve rebentar scans: get_refs mantém
    as refs anteriores e marca o mtime falhado para não martelar o miner."""
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    plan = doc_dir / "plan_colunas_cpis.xlsx"
    _write_plan(plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    good_sha = watcher.force_reload()["plan_sha256"]
    assert good_sha

    plan.write_bytes(b"not a real xlsx")        # corromper
    watcher._refs["plan_mtime"] = 0.0            # forçar _needs_reload
    watcher._last_check_ts = 0.0                 # ultrapassar o debounce

    refs = watcher.get_refs()                    # não deve levantar
    assert refs["plan_sha256"] == good_sha       # manteve refs anteriores
    assert watcher._failed_mtimes is not None


def test_refs_upload_rolls_back_when_reload_fails(tmp_path, monkeypatch, log_path):
    """Se o reload falhar depois do os.replace, o ficheiro vivo é restaurado
    ao plano anterior e as refs ficam consistentes (não fica o ficheiro novo
    ativo com refs antigas)."""
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir()
    _write_plan(doc_dir / "plan_colunas_cpis.xlsx", [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
    ])
    watcher = ref_watcher.RefWatcher(doc_dir=doc_dir, repo_root=tmp_path)
    old_sha = watcher.force_reload()["plan_sha256"]
    assert old_sha

    monkeypatch.setattr(main, "get_watcher", lambda: watcher)

    def _boom(*_a, **_k):
        raise RuntimeError("mine boom")

    monkeypatch.setattr(ref_watcher, "_mine_from_excel", _boom)

    upload_plan = tmp_path / "upload_plan.xlsx"
    _write_plan(upload_plan, [
        ["A", "2603977", "263348", "CAC4E10B", 1, 0, 0, 0, 0, 0, 0, 0, 4, 659, 242, 11050],
        ["B", "2603978", "263349", "CAC4E10C", 2, 0, 0, 0, 0, 0, 0, 0, 3, 600, 200, 9000],
    ])
    client = TestClient(main.app)
    with upload_plan.open("rb") as f:
        resp = client.post(
            "/refs/upload",
            data={"kind": "plan"},
            files={"file": ("plan_colunas_cpis.xlsx", f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "err" in resp.headers["location"]
    # ficheiro vivo restaurado ao plano antigo
    assert ref_watcher.file_sha256(watcher.plan_path) == old_sha
    # backup transitório consumido pelo restore (os.replace)
    assert not (doc_dir / "plan_colunas_cpis.prevbak.xlsx").exists()
