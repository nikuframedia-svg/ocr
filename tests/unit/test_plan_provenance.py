"""R253/F2 — idade do plano por CONTEÚDO (fix do G9): re-copiar o mesmo
xlsx renova o mtime e o π_H0 subestimava a idade → sobreconfiança com plano
velho. A idade tem de vir do primeiro avistamento do sha256."""
from __future__ import annotations

import json
import os

import app.cross_check.ref_watcher as rw


def test_same_content_different_mtime_keeps_first_seen(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "_PLAN_PROVENANCE_PATH",
                        tmp_path / "plan_provenance.json")
    plan = tmp_path / "plan.xlsx"
    plan.write_bytes(b"conteudo-do-plano-v1")
    os.utime(plan, (1_000_000, 1_000_000))  # mtime antigo

    refs1: dict = {}
    rw._set_file_meta(refs1, "plan", plan)
    assert refs1["plan_content_mtime"] == 1_000_000

    # "re-cópia" do MESMO conteúdo com mtime novo — a idade NÃO reinicia.
    os.utime(plan, (2_000_000, 2_000_000))
    refs2: dict = {}
    rw._set_file_meta(refs2, "plan", plan)
    assert refs2["plan_mtime"] == 2_000_000
    assert refs2["plan_content_mtime"] == 1_000_000


def test_new_content_resets_age(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "_PLAN_PROVENANCE_PATH",
                        tmp_path / "plan_provenance.json")
    plan = tmp_path / "plan.xlsx"
    plan.write_bytes(b"conteudo-v1")
    os.utime(plan, (1_000_000, 1_000_000))
    refs1: dict = {}
    rw._set_file_meta(refs1, "plan", plan)

    plan.write_bytes(b"conteudo-v2-DIFERENTE")
    os.utime(plan, (3_000_000, 3_000_000))
    refs2: dict = {}
    rw._set_file_meta(refs2, "plan", plan)
    assert refs2["plan_content_mtime"] == 3_000_000  # conteúdo novo = fresco


def test_provenance_file_corrupt_degrades_to_mtime(tmp_path, monkeypatch):
    prov = tmp_path / "plan_provenance.json"
    prov.write_text("{corrupto", encoding="utf-8")
    monkeypatch.setattr(rw, "_PLAN_PROVENANCE_PATH", prov)
    plan = tmp_path / "plan.xlsx"
    plan.write_bytes(b"x")
    os.utime(plan, (5_000_000, 5_000_000))
    refs: dict = {}
    rw._set_file_meta(refs, "plan", plan)
    assert refs["plan_content_mtime"] == 5_000_000
    # e o ficheiro foi regenerado válido
    assert isinstance(json.loads(prov.read_text("utf-8")), dict)
