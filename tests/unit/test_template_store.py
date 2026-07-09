"""Task C E4 — registry runtime (set_runtime_templates/alias_conflicts) +
template_store (spec_json ↔ TemplateSpec, reload, validação do wizard)."""
from __future__ import annotations

import json

import pytest

from app import templates_registry as reg
from app.web import db, template_store


@pytest.fixture(autouse=True)
def restore_registry():
    """Qualquer teste que instale templates runtime tem de deixar o
    registry byte-idêntico aos builtins no fim."""
    yield
    reg.set_runtime_templates([])


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", tmp_path / "app.db")
    db.init_db()
    return tmp_path / "app.db"


def _mk_spec(name="u2_corte_esposende", aliases=("CORTE ESPOSENDE",),
             unidade_id=2, **over):
    d = {
        "name": name,
        "tpl_code": "TPL103",
        "phase": "Corte",
        "setor_aliases": list(aliases),
        "row_fields": ["of", "modelo", "qtd"],
        "cross_check_fields": ["of", "modelo"],
        "description": "template de teste",
    }
    d.update(over)
    return template_store.spec_from_dict(d, unidade_id=unidade_id, db_id=9)


class TestBuiltinRegression:
    def test_18_builtins_intact(self):
        assert len(reg.TEMPLATES) == 18
        assert all(t.source == "builtin" for t in reg.TEMPLATES.values())
        assert all(t.unidade_id is None for t in reg.TEMPLATES.values())

    def test_set_empty_is_identity(self):
        before = dict(reg.TEMPLATES)
        reg.set_runtime_templates([])
        assert reg.TEMPLATES == before

    def test_detection_unchanged(self):
        reg.set_runtime_templates([])
        t, reason = reg.detect_template_with_reason("BOBINE-FORMATO")
        assert t.name == "bobine_formato" and reason == "exact_alias"
        t, _ = reg.detect_template_with_reason("QUINADORA PAV.4")
        assert t.name == "quinadora_pav4"
        t, reason = reg.detect_template_with_reason("XPTO DESCONHECIDO")
        assert t.name == "bobine_formato" and reason == "default"


class TestSetRuntimeTemplates:
    def test_install_and_detect(self):
        spec = _mk_spec()
        skipped = reg.set_runtime_templates([spec])
        assert skipped == []
        assert reg.TEMPLATES["u2_corte_esposende"].unidade_id == 2
        t, reason = reg.detect_template_with_reason("CORTE ESPOSENDE")
        assert t.name == "u2_corte_esposende"
        assert reason == "exact_alias"
        assert reg.get_template("u2_corte_esposende").db_id == 9

    def test_idempotent_replace(self):
        reg.set_runtime_templates([_mk_spec()])
        reg.set_runtime_templates([_mk_spec(name="u2_outro",
                                            aliases=("OUTRO SETOR",))])
        assert "u2_corte_esposende" not in reg.TEMPLATES  # substituído
        assert "u2_outro" in reg.TEMPLATES
        assert len(reg.TEMPLATES) == 19

    def test_builtin_name_collision_skipped(self):
        spec = _mk_spec(name="bobine_formato")
        skipped = reg.set_runtime_templates([spec])
        assert skipped == ["bobine_formato"]
        assert reg.TEMPLATES["bobine_formato"].source == "builtin"

    def test_builtin_alias_never_stolen(self):
        spec = _mk_spec(aliases=("GUILHOTINA",))  # alias de builtin
        reg.set_runtime_templates([spec])
        t, _ = reg.detect_template_with_reason("GUILHOTINA")
        assert t.name == "guilhotina"  # builtin ganha

    def test_two_sided_includes_runtime(self):
        from app.web import ocr_runner
        reg.set_runtime_templates([_mk_spec()])
        m = ocr_runner.TWO_SIDED_TEMPLATES
        assert m.get("u2_corte_esposende") == "paragens"
        reg.set_runtime_templates([])
        assert "u2_corte_esposende" not in ocr_runner.TWO_SIDED_TEMPLATES


class TestAliasConflicts:
    def test_exact_builtin_conflict(self):
        out = reg.alias_conflicts(["GUILHOTINA"])
        assert any(c["kind"] == "exact" and c["builtin"] for c in out)

    def test_containment_warning(self):
        out = reg.alias_conflicts(["GUILHOTINA 6M ESPOSENDE"])
        kinds = {c["kind"] for c in out}
        assert "containment" in kinds and "exact" not in kinds

    def test_clean_alias_no_conflicts(self):
        assert reg.alias_conflicts(["PRENSA ESPOSENDE P1"]) == []

    def test_normalization_applied(self):
        # acentos/pontuação não escondem o conflito
        out = reg.alias_conflicts(["expedição"])
        assert any(c["kind"] == "exact" for c in out)


class TestSpecRoundtrip:
    def test_roundtrip(self):
        spec = _mk_spec()
        d = template_store.spec_to_dict(spec)
        again = template_store.spec_from_dict(d, unidade_id=2, db_id=9)
        assert again == spec

    def test_missing_essentials_raise(self):
        with pytest.raises(ValueError):
            template_store.spec_from_dict({"name": "", "row_fields": []})

    def test_defaults_filled(self):
        spec = template_store.spec_from_dict(
            {"name": "u1_x", "row_fields": ["of"]})
        assert spec.header_fields[0] == "operador"
        assert spec.has_production_rows is True
        assert spec.source == "db"


class TestValidateSpecPayload:
    def test_ok(self):
        errors, warnings = template_store.validate_spec_payload({
            "name": "u2_corte", "setor_aliases": ["CORTE X"],
            "row_fields": ["of", "modelo", "qtd"],
            "cross_check_fields": ["of"],
        })
        assert errors == [] and warnings == []

    def test_bad_name(self):
        errors, _ = template_store.validate_spec_payload({
            "name": "Corte Esposende", "setor_aliases": ["X"],
            "row_fields": ["of"]})
        assert any("snake_case" in e for e in errors)

    def test_missing_alias_blocks(self):
        errors, _ = template_store.validate_spec_payload({
            "name": "u2_c", "setor_aliases": [], "row_fields": ["of"]})
        assert any("alias" in e for e in errors)

    def test_custom_field_warns(self):
        errors, warnings = template_store.validate_spec_payload({
            "name": "u2_c", "setor_aliases": ["X"],
            "row_fields": ["of", "campo_novo"]})
        assert errors == []
        assert any("campo_novo" in w for w in warnings)

    def test_cross_check_must_be_crossable(self):
        errors, _ = template_store.validate_spec_payload({
            "name": "u2_c", "setor_aliases": ["X"],
            "row_fields": ["of", "qtd"], "cross_check_fields": ["qtd"]})
        assert any("qtd" in e for e in errors)

    def test_cross_check_must_be_in_rows(self):
        errors, _ = template_store.validate_spec_payload({
            "name": "u2_c", "setor_aliases": ["X"],
            "row_fields": ["of"], "cross_check_fields": ["modelo"]})
        assert any("modelo" in e for e in errors)

    def test_duplicate_field(self):
        errors, _ = template_store.validate_spec_payload({
            "name": "u2_c", "setor_aliases": ["X"],
            "row_fields": ["of", "of"]})
        assert any("duplicado" in e for e in errors)


class TestReloadRegistry:
    def test_empty_db_identity(self, tmp_db):
        before = dict(reg.TEMPLATES)
        result = template_store.reload_registry()
        assert result["loaded"] == []
        assert reg.TEMPLATES == before

    def test_loads_active_only(self, tmp_db):
        uid = db.create_unidade("Esposende")
        spec = template_store.spec_to_dict(_mk_spec(unidade_id=uid))
        tid = db.insert_kanban_template(
            "u2_corte_esposende", uid, json.dumps(spec), status="ativo")
        db.insert_kanban_template(
            "u2_draft", uid, json.dumps(dict(spec, name="u2_draft")),
            status="draft")
        result = template_store.reload_registry()
        assert result["loaded"] == ["u2_corte_esposende"]
        assert "u2_draft" not in reg.TEMPLATES
        assert reg.TEMPLATES["u2_corte_esposende"].db_id == tid
        assert reg.TEMPLATES["u2_corte_esposende"].unidade_id == uid

    def test_broken_spec_ignored(self, tmp_db):
        uid = db.create_unidade("Esposende")
        db.insert_kanban_template("u2_bad", uid, "{ nao json", status="ativo")
        result = template_store.reload_registry()
        assert result["invalid"]
        assert "u2_bad" not in reg.TEMPLATES

    def test_unidade_for_template(self, tmp_db):
        uid = db.create_unidade("Esposende")
        spec = template_store.spec_to_dict(_mk_spec(unidade_id=uid))
        db.insert_kanban_template(
            "u2_corte_esposende", uid, json.dumps(spec), status="ativo")
        template_store.reload_registry()
        assert template_store.unidade_for_template("u2_corte_esposende") == uid
        assert template_store.unidade_for_template("bobine_formato") is None
        assert template_store.unidade_for_template(None) is None
        assert template_store.unidade_for_template("inexistente") is None
