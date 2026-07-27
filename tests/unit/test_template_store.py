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
        assert len(reg.TEMPLATES) == 19
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
        assert len(reg.TEMPLATES) == 20

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


class TestDeclaredCrossSpec:
    """Cross declarado (fase A) — spec_json ↔ TemplateSpec."""

    _DECLARED = {"pbase": {"ref": "plan", "column": "pbase",
                           "cmp": "num", "tol": 2.0}}

    def test_roundtrip_with_declared(self):
        spec = _mk_spec(row_fields=["of", "modelo", "pbase"],
                        declared_cross=self._DECLARED)
        assert spec.declared_cross == {
            "pbase": reg.DeclaredRef(column="pbase", cmp="num", tol=2.0)}
        d = template_store.spec_to_dict(spec)
        assert d["declared_cross"] == self._DECLARED
        again = template_store.spec_from_dict(d, unidade_id=2, db_id=9)
        assert again == spec

    def test_old_spec_without_key_defaults_empty(self):
        spec = template_store.spec_from_dict(
            {"name": "u1_x", "row_fields": ["of"]})
        assert spec.declared_cross == {}
        assert template_store.spec_to_dict(spec)["declared_cross"] == {}

    def test_malformed_declared_discarded_not_raised(self):
        # reload_registry nunca pode perder um template por uma chave
        # acessória: entradas partidas são descartadas silenciosamente.
        spec = template_store.spec_from_dict({
            "name": "u1_x", "row_fields": ["of", "pbase", "obs"],
            "declared_cross": {
                "pbase": {"column": "pbase", "cmp": "fuzzy"},   # cmp inválido
                "obs": {"cmp": "text"},                          # sem column
                "ok": {"column": "destino", "cmp": "text"},
            },
        })
        assert spec.declared_cross == {
            "ok": reg.DeclaredRef(column="destino", cmp="text", tol=None)}
        spec2 = template_store.spec_from_dict(
            {"name": "u1_x", "row_fields": ["of"],
             "declared_cross": ["nao", "dict"]})
        assert spec2.declared_cross == {}

    def test_text_never_carries_tol(self):
        spec = template_store.spec_from_dict({
            "name": "u1_x", "row_fields": ["obs"],
            "declared_cross": {"obs": {"column": "destino", "cmp": "text",
                                       "tol": 3.0}}})
        assert spec.declared_cross["obs"].tol is None

    def test_vote_roundtrip_and_strict_parse(self):
        # round-trip com vote=true
        spec = _mk_spec(row_fields=["of", "pbase"],
                        declared_cross={"pbase": {
                            "column": "pbase", "cmp": "num",
                            "tol": 2.0, "vote": True}})
        assert spec.declared_cross["pbase"].vote is True
        d = template_store.spec_to_dict(spec)
        assert d["declared_cross"]["pbase"]["vote"] is True
        again = template_store.spec_from_dict(d, unidade_id=2, db_id=9)
        assert again == spec

    def test_vote_omitted_when_false(self):
        # specs sem voto ficam byte-idênticos (chave omitida)
        spec = _mk_spec(row_fields=["of", "pbase"],
                        declared_cross=self._DECLARED)
        d = template_store.spec_to_dict(spec)
        assert "vote" not in d["declared_cross"]["pbase"]

    @pytest.mark.parametrize("raw", ["sim", 1, "true", [True]])
    def test_vote_truthy_strings_never_enable(self, raw):
        # parse ESTRITO-para-True: spec_json malformado nunca liga um voto
        spec = template_store.spec_from_dict({
            "name": "u1_x", "row_fields": ["pbase"],
            "declared_cross": {"pbase": {"column": "pbase", "cmp": "num",
                                         "vote": raw}}})
        assert spec.declared_cross["pbase"].vote is False

    def test_runtime_declared_columns_union(self):
        reg.set_runtime_templates([
            _mk_spec(row_fields=["of", "pbase"],
                     declared_cross=self._DECLARED),
            _mk_spec(name="u2_outro", aliases=("OUTRO",),
                     row_fields=["of", "obs"],
                     declared_cross={"obs": {"column": "destino",
                                             "cmp": "text"}}),
        ])
        assert reg.runtime_declared_columns() == frozenset({"pbase", "destino"})
        reg.set_runtime_templates([])
        assert reg.runtime_declared_columns() == frozenset()


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

    # ---- cross declarado (fase A) ----

    @staticmethod
    def _declared_payload(**over):
        d = {
            "name": "u2_c", "setor_aliases": ["X"],
            "row_fields": ["of", "pbase"],
            "declared_cross": {"pbase": {"ref": "plan", "column": "pbase",
                                         "cmp": "num", "tol": 2.0}},
        }
        d.update(over)
        return d

    def test_declared_ok(self):
        errors, warnings = template_store.validate_spec_payload(
            self._declared_payload())
        assert errors == []
        # campo custom continua a avisar (comportamento pré-existente)
        assert any("pbase" in w and "canónico" in w for w in warnings)

    def test_declared_must_be_in_rows(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(row_fields=["of"]))
        assert any("pbase" in e and "campos da tabela" in e for e in errors)

    def test_declared_rejects_crossable_field(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "of": {"column": "of", "cmp": "text"}}))
        assert any("'of'" in e and "cross-check normal" in e for e in errors)

    def test_declared_rejects_known_non_crossable_field(self):
        # qtd tem regra local própria — declarado é só para campos custom
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(
                row_fields=["of", "qtd"],
                declared_cross={"qtd": {"column": "quanttrp", "cmp": "num"}}))
        assert any("'qtd'" in e and "regra própria" in e for e in errors)

    def test_declared_rejects_non_plan_ref(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"ref": "sap", "column": "pbase", "cmp": "num"}}))
        assert any("'sap'" in e for e in errors)

    def test_declared_column_must_be_lowercase(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "PBase", "cmp": "num"}}))
        assert any("coluna do plano inválida" in e for e in errors)

    def test_declared_column_required(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "", "cmp": "num"}}))
        assert any("coluna do plano inválida" in e for e in errors)

    def test_declared_bad_cmp(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "fuzzy"}}))
        assert any("fuzzy" in e for e in errors)

    def test_declared_tol_with_text_rejected(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "text", "tol": 2.0}}))
        assert any("tolerância" in e for e in errors)

    @pytest.mark.parametrize("tol", [0, -1, "abc"])
    def test_declared_bad_tol(self, tol):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "num", "tol": tol}}))
        assert any("tolerância inválida" in e for e in errors)

    def test_declared_num_without_tol_is_exact_match(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "num"}}))
        assert errors == []

    def test_declared_warns_when_column_missing_from_plan(self):
        _, warnings = template_store.validate_spec_payload(
            self._declared_payload(),
            plan_headers=["of", "cliente", "destino"])
        assert any("não existe no último plano" in w for w in warnings)

    def test_declared_no_warning_without_plan(self):
        _, warnings = template_store.validate_spec_payload(
            self._declared_payload(), plan_headers=None)
        assert not any("não existe no último plano" in w for w in warnings)

    def test_declared_not_dict_is_error(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross=["pbase"]))
        assert any("declared_cross inválido" in e for e in errors)

    def test_vote_non_bool_is_error(self):
        errors, _ = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "num", "vote": "sim"}}))
        assert any("vote inválido" in e for e in errors)

    def test_vote_with_env_off_warns_but_accepts(self):
        errors, warnings = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "num", "vote": True}}),
            declared_vote_env="off")
        assert errors == []
        assert any("CROSS_DECLARED_VOTE" in w for w in warnings)

    @pytest.mark.parametrize("env", [None, "on", "shadow"])
    def test_vote_no_warning_when_env_not_off(self, env):
        _, warnings = template_store.validate_spec_payload(
            self._declared_payload(declared_cross={
                "pbase": {"column": "pbase", "cmp": "num", "vote": True}}),
            declared_vote_env=env)
        assert not any("CROSS_DECLARED_VOTE" in w for w in warnings)


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
