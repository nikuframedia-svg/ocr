"""R131 — testes unitários explícitos para snap_operador.

Não existiam testes dedicados; o bug reportado pelo utilizador (cod
válido + nome OCR errado NÃO estava a ser substituído pelo canónico)
motivou criar esta bateria. Cobre cada Condition (A/B/D + E + F) e o
cenário-chave do bug (cod fiável determina nome).
"""
from __future__ import annotations

from app.dq.operador_snap import snap_operador

# Refs sintéticos espelhando o shape devolvido por
# `_mine_colaboradores` (sname UPPER ASCII, pernr 8 dígitos).
_COLABS = {
    95: {"sname": "AUGUSTO MONTEIRO", "pernr": "10000095"},
    1162: {"sname": "HELDER FERNANDES", "pernr": "10001162"},
    1391: {"sname": "VITOR CARVALHO", "pernr": "10001391"},
    537: {"sname": "JULIO LIMA", "pernr": "10000537"},
}


class TestSnapByCod:
    """Bug R131 do utilizador: cod válido deve sempre determinar o nome
    canónico, mesmo quando o OCR leu o nome errado/parcial/vazio."""

    def test_cod_valid_name_wrong_substitutes_canonical(self):
        """cod=1162 + OCR='HELENA FERNANDEZ' → snap para 'HELDER FERNANDES'."""
        sr = snap_operador("HELENA FERNANDEZ", "1162", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "HELDER FERNANDES"
        assert sr.snapped_cod == "1162"
        assert sr.pernr == "10001162"

    def test_cod_valid_name_empty_fills_canonical(self):
        """Nome em branco + cod fiável → preenche com canónico."""
        sr = snap_operador("", "0095", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "AUGUSTO MONTEIRO"
        assert sr.pernr == "10000095"

    def test_cod_valid_name_exact_no_substitution_needed(self):
        """cod + nome já canónicos → no-op (ou case_normalize)."""
        sr = snap_operador("AUGUSTO MONTEIRO", "95", _COLABS)
        # Pode ser Cond A com case_diff=False (no-op) ou case_normalize.
        # O importante: o snapped_name continua canónico.
        assert sr.snapped_name == "AUGUSTO MONTEIRO"
        assert sr.snapped_cod == "95"

    def test_cod_valid_name_case_only_diff_substitutes(self):
        """'Augusto Monteiro' + cod=95 → snap para UPPER canónico."""
        sr = snap_operador("Augusto Monteiro", "95", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "AUGUSTO MONTEIRO"

    def test_cod_valid_name_with_accent_substitutes_to_ascii(self):
        """'JÚLIO LIMA' (com acento) + cod=537 → 'JULIO LIMA' (canónico ASCII)."""
        sr = snap_operador("JÚLIO LIMA", "537", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "JULIO LIMA"

    def test_cod_valid_partial_token_overlap_substitutes(self):
        """OCR partial mas com ≥1 token comum → Cond B aplica."""
        sr = snap_operador("AUGUSTO M", "95", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "AUGUSTO MONTEIRO"
        # Cond B é "verde" (não suspended) porque há token overlap forte.
        assert not sr.suspended

    def test_cod_valid_zero_token_overlap_still_substitutes(self):
        """Cond D: cod exact + 0 tokens em comum → TRUST COD, suspended yellow."""
        sr = snap_operador("ALGUMNOMEERRADO", "1162", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "HELDER FERNANDES"
        assert sr.suspended  # amarelo — operador deve verificar

    def test_cod_with_leading_zeros_parsed_correctly(self):
        """'0095' deve resolver para cod=95 e snap canónico."""
        sr = snap_operador("AUUSTO MNTRO", "0095", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "AUGUSTO MONTEIRO"


class TestSnapFallbacks:
    """Cenários onde o cod não resolve directamente."""

    def test_cod_unknown_in_list_derives_pernr_preserves_name(self):
        """Cond E: cod válido mas inexistente em colabs → pernr derivado,
        nome OCR preservado (sem canónico disponível)."""
        sr = snap_operador("UNKNOWN PERSON", "9999", _COLABS)
        assert sr.snapped_name == "UNKNOWN PERSON"
        assert sr.pernr == "10009999"  # estrutural: "1000" + zfill(4)

    def test_cod_empty_name_resolves_via_name_unique_match(self):
        """Cond F + name_unique_match: cod vazio mas nome bate exact com
        UM único colaborador → snap pelo nome."""
        sr = snap_operador("HELDER FERNANDES", "", _COLABS)
        assert sr.applied
        assert sr.snapped_name == "HELDER FERNANDES"
        assert sr.snapped_cod == "1162"

    def test_empty_colabs_preserves_name_derives_pernr(self):
        """Production failure mode: ListaColaboradores não carregado.
        Cond E aplica-se (cod ausente da lista), preserva o nome OCR
        e deriva pernr estruturalmente. O caller em _apply_operador_snap
        tem um short-circuit `if not colabs: return 0` que skips este
        path em produção — este teste documenta o comportamento puro."""
        sr = snap_operador("AUGUSTO MONTEIRO", "95", {})
        assert sr.snapped_name == "AUGUSTO MONTEIRO"  # nome OCR preservado
        assert sr.pernr == "10000095"  # derivado: "1000" + zfill(4)

    def test_both_empty_no_substitution(self):
        """Nome E cod vazios → sem ref, sem snap."""
        sr = snap_operador("", "", _COLABS)
        assert not sr.applied
        assert sr.snapped_name == ""
        assert sr.snapped_cod == ""
