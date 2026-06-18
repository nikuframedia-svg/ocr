"""Motor unificado de scoring / cross-check.

Fluxo atual:

  Top-K por campo + score global contra todo o Plan → winner → proposta por
  célula. O motor classifica a distância entre OCR e proposta; a camada web
  aplica automaticamente `snapped` e `very_different` com origem concreta.

    Guardas principais:
    - Todos os campos lidos têm o mesmo peso na escolha da linha do plan.
    - OF/OV/cliente/modelo não ancoram nem vetam sozinhos a linha.
    - Winner global aceite pode corrigir OF/OV/cliente/modelo/campos técnicos.
    - Campos preenchidos sem plan/SAP validam por regra local ou vão a revisão.

  Estados de célula (com legendas para a UI):
    - confirmed:      "Confirmado"          — motor escolheu valor igual ao OCR
    - snapped:        "Substituído"          — motor mudou (ou preencheu) sem ser radical
    - very_different: "Muito diferente"     — motor propõe valor longe do OCR; vermelho
    - NA:             "Sem valor"           — célula vazia sem dado para validar
"""
from __future__ import annotations

import time
import unicodedata
from datetime import datetime, timezone
import re
from itertools import product
from typing import Any

from app.dq.ferramenta import ALLOWED_FERRAMENTA_TEXT, normalize_ferramenta
from app.dq.machines import machine_phase_from_setor, resolve_machine_from_setor


# Inline deps (R109 — motor self-contained) ----------------------------------

_MIN_GLOBAL_WINNER_SCORE = 1.0

# R218 — margem de "líder claro": rivais cujo `combined` está a <= esta margem
# do melhor são considerados quase-empatados (entram na guarda de ambiguidade).
_WINNER_MARGIN = 0.5

_CLIENTE_STOPWORDS = frozenset({
    "GMBH", "SAS", "SARL", "SA", "S.A", "LDA", "LTD", "SL", "BV", "NV",
    "LIMITED", "UNIPESSOAL",
})


def _norm_ascii_upper(value: object) -> str:
    d = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(c for c in d if not unicodedata.combining(c)).strip().upper()


def _cliente_tokens(value: object) -> tuple[str, ...]:
    norm = _norm_ascii_upper(value)
    cleaned = re.sub(r"[^A-Z0-9]+", " ", norm)
    return tuple(
        tok for tok in cleaned.split()
        if tok and tok not in _CLIENTE_STOPWORDS
    )


def _cliente_compact(value: object) -> str:
    return "".join(_cliente_tokens(value))


def _cliente_values_match(
    ocr_value: object,
    plan_value: object,
    refs: dict | None = None,
) -> bool:
    ocr_raw = _norm_ascii_upper(ocr_value)
    plan_raw = _norm_ascii_upper(plan_value)
    if not ocr_raw or not plan_raw:
        return False

    ocr_compact = _cliente_compact(ocr_raw)
    plan_compact = _cliente_compact(plan_raw)
    return bool(ocr_compact and ocr_compact == plan_compact)


def normalize_of(value: object) -> str:
    """6 dígitos canónico. Pure-digit OFs < 6 chars são zero-padded; o
    resto fica intocado para coabitar com snap_of."""
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    if s.isdigit() and len(s) < 6:
        return s.zfill(6)
    return s


def _o_zero_variants(s: str) -> list[str]:
    """0/O swap variants (R93). Capped 8 variants, ≤3 swap positions."""
    if not s:
        return [s]
    positions = [i for i, ch in enumerate(s) if ch in ("0", "O", "o")]
    if not positions or len(positions) > 3:
        return [s]
    variants: set[str] = {s}
    chars = list(s)
    for combo in product(("0", "O"), repeat=len(positions)):
        for idx, ch in zip(positions, combo):
            chars[idx] = ch
        variants.add("".join(chars))
    return list(variants)[:8]


def _lote_variants(value: object) -> list[str]:
    raw = str(value or "").strip().upper()
    if not raw:
        return []
    variants = [raw]
    if raw.startswith("H") and len(raw) >= 2:
        variants.append("M" + raw[1:])
    elif not raw.startswith("M") and re.match(r"^\d{2}B", raw):
        variants.append("M" + raw)
    out: list[str] = []
    for v in variants:
        if v and v not in out:
            out.append(v)
    return out


def _sap_lote_entry(refs: dict, lote_value: object) -> tuple[str, dict | None]:
    sap_full = refs.get("lotes_sap_full", {}) or {}
    for variant in _lote_variants(lote_value):
        if variant in sap_full:
            return variant, sap_full[variant]
    return "", None


def score_entry(
    entry: dict,
    row: dict,
    refs: dict,
    cliente_aliases: dict[str, str] | None = None,
) -> int:
    """R123 — Holistic 0-10 match score. Uma feature por campo da folha,
    todas com peso 1 quando identificam uma entry do plan: of, cliente, ov,
    modelo, comp, larg, lbase, ltopo, esp. Nenhum campo vale mais que os
    outros — a OF conta como o cliente, como o modelo, como as medidas
    (filosofia do Luís).

    Notas:
      - `of`/`ov` comparados com tolerância 0/O (erro de OCR comum).
      - `larg` só pontua a entry se existir na própria entry de plan.
      - `lote` é SAP-only: valida a célula `lote`, mas não identifica uma
        entry do plan e por isso não pontua o winner.
      - `material` saiu (R123 B2): não é um campo da folha do operador.
    """
    op_of = "" if _is_missing_ocr(row.get("of")) else _identifier_compact(row.get("of"), pad_of=True)
    op_cli = "" if _is_missing_ocr(row.get("cliente")) else (row.get("cliente") or "").strip()
    op_ov = "" if _is_missing_ocr(row.get("ov")) else _identifier_compact(row.get("ov"))
    op_mod = "" if _is_missing_ocr(row.get("modelo")) else (row.get("modelo") or "").strip().upper()
    op_comp = _num(row.get("comp_mm"))
    op_lb = _num(row.get("lbase"))
    op_lt = _num(row.get("ltopo"))
    op_larg = _num(row.get("larg_mm"))
    # R128 — kanban LASER: dbase/dtopo distintos de lbase/ltopo
    op_db = _num(row.get("dbase"))
    op_dt = _num(row.get("dtopo"))

    s = 0
    # of — OF do operador == OF da entry (tolerância 0/O)
    entry_of = _identifier_compact(entry.get("_of") or entry.get("of"), pad_of=True)
    if op_of and entry_of and entry_of in _o_zero_variants(op_of):
        s += 1
    # cliente
    if op_cli and _cliente_values_match(op_cli, entry.get("cliente"), refs):
        s += 1
    # ov (tolerância 0/O)
    entry_ov = _identifier_compact(entry.get("ov"))
    if op_ov and entry_ov and entry_ov in _o_zero_variants(op_ov):
        s += 1
    # modelo — o código do operador aparece na designação da entry, ou bate
    # no primeiro token com tolerância 0/O (erro comum em códigos curtos).
    if _model_matches_designacao(op_mod, entry.get("designacao")):
        s += 1
    # comp — só conta como evidência se também estiver dentro da tolerância
    # aceite pela validação da célula.
    plan_comp = _num(entry.get("comp"))
    if (
        op_comp is not None
        and plan_comp is not None
        and abs(op_comp - plan_comp) <= _VERY_DIFF_NUM_ABS["comp_mm"]
    ):
        s += 1
    # larg — só se a entry do plan trouxer largura própria. A largura via
    # StockSAP é global ao lote e não pode escolher a OF/modelo.
    plan_larg = _num(entry.get("larg"))
    if (
        op_larg is not None
        and plan_larg is not None
        and abs(op_larg - plan_larg) <= _VERY_DIFF_NUM_ABS["larg_mm"]
    ):
        s += 1
    # lbase
    plan_lb = _num(entry.get("lbase"))
    if (
        op_lb is not None
        and plan_lb is not None
        and abs(op_lb - plan_lb) <= _VERY_DIFF_NUM_ABS["lbase"]
    ):
        s += 1
    # ltopo
    plan_lt = _num(entry.get("ltopo"))
    if (
        op_lt is not None
        and plan_lt is not None
        and abs(op_lt - plan_lt) <= _VERY_DIFF_NUM_ABS["ltopo"]
    ):
        s += 1
    # R128 — dbase (LASER) — campo próprio, threshold ±30mm
    plan_db = _num(entry.get("dbase"))
    if op_db is not None and plan_db is not None and abs(op_db - plan_db) <= 30:
        s += 1
    # R128 — dtopo (LASER)
    plan_dt = _num(entry.get("dtopo"))
    if op_dt is not None and plan_dt is not None and abs(op_dt - plan_dt) <= 30:
        s += 1
    # esp
    plan_esp = _num(entry.get("esp"))
    if _num_matches("esp", row.get("esp"), plan_esp, 0.05):
        s += 1
    return s


# Configuração ---------------------------------------------------------------

_NO_REF_FIELDS = frozenset({
    "pri", "qtd",
    "horas_trabalhadas", "colunas_produzidas",
    "n_operador", "data", "setor_maquina", "cod_maquina", "operador",
    # R132 — turno (M/R/XM/T) é header próprio de acabamento e
    # maq_fustes; sem ref no plan/SAP.
    "turno",
    # R132 — paragens (TPL103 verso MÁQUINA DE FUSTES): nunca cruzam plan.
    "motivo", "inicio", "fim", "duracao", "resolvido",
    # R132 — qtd_metros (soldline, laser, maq_fustes) é informativo, NA cinza.
    "qtd_metros",
    "sobras", "cesta_n",
    # TPL102 Gemini: área informativa, sem ref, mas com sintaxe numérica.
    "m2",
})

_ROW_FIELDS = (
    "cliente", "ov", "of", "modelo", "lote",
    "comp_mm", "larg_mm", "lbase", "ltopo", "esp",
    # R128 — kanban LASER: dimensões próprias (dbase/dtopo no plan_colunas)
    "dbase", "dtopo",
)

# R123 — campos cujo top-K traz plan_entries para o pool do winner.
# Inclui agora `cliente` (via plan_by_cliente) e `larg_mm`, para que uma
# linha que só bate por esses campos ainda entre no pool de candidatas.
_PLAN_FIELDS = ("of", "ov", "modelo", "cliente", "comp_mm", "larg_mm",
                "lbase", "ltopo", "esp", "dbase", "dtopo")

_TOP_K = 10

# Thresholds de "muito diferente" — abaixo destes níveis, vermelho.
_VERY_DIFF_STR_SIM = 50.0          # se sim < 50, é muito diferente
_VERY_DIFF_NUM_ABS = {             # diferença absoluta > X = revisão humana
    "comp_mm": 50.0,
    "larg_mm": 10.0,
    "lbase": 10.0,
    "ltopo": 10.0,
    "esp": 0.05,
    # R128 — LASER
    "dbase": 30.0,
    "dtopo": 30.0,
}

# Legendas para a UI (status → label PT-PT)
_STATUS_LABELS = {
    "confirmed":      "Confirmado",
    "snapped":        "Substituído",
    "very_different": "Muito diferente — rever",
    "NA":             "Sem valor",
}

# R123 — versão do motor. Gravada em cada cross-check JSON; o viewer
# regenera on-demand qualquer folha cujo JSON seja de uma versão anterior.
# R130 — cross-check rigoroso: of/ov/cliente nunca silenciosamente
# substituídos; dim só auto-corrige com winner score>=4; UI marca cells
# auto-substituídas com cor amarela (cc-warn) via `proposed` no tooltip.
# R140 — legado: identity anchoring removido em R208; OF/OV contam como
# campos normais no score global.
# R142 — corrige regressões: Acabamento preserva OF/REFERÊNCIA não-vazias;
# obra_concluida respeita a mesma âncora; dbase/dtopo usam regras numéricas.
# R143 — legado: campos de linha sem referência real ficavam NA, não MATCH
# sintáctico. Em R216, campos preenchidos passam por regra local/sintaxe.
# R144 — header.data valida datas impossíveis; fila to_analisar cobre
# header/footer e `ocr_raw` deixa de fingir ref.
# R145 — header.n_operador valida contra ListaColaboradores quando existe.
# R146 — header.setor_maquina/cod_maquina validam contra maquinas.xlsx e
# contra a combinação esperada setor→codmaq.
# R147 — separa origem do valor (`source`) da origem da referência
# (`ref_source`) em células com referência explícita.
# R148 — contador top-level `snapped` deixa de incluir `very_different`.
# R149 — lote parecido em SAP vira revisão com ref concreta; pode ser
# auto-substituído no ciclo de 30/05.
# R150 — identidade escrita (of/ov/cliente) volta a carregar o canónico quando
# existe winner/ref concreta.
# R151 — `very_different` com ref concreta segue substitute-everything; OCR só
# é preservado quando não há canónico seguro.
# R152 — legado: autosnap já não depende de âncora OF/OV; o winner global
# propõe refs e cada célula decide o estado pela diferença local.
# R153 — legado: sem pool de referência (plan/SAP vazio) ficava NA para não
# gerar falsos NO_MATCH. Em R216, campo preenchido cruzável fica em revisão.
# R154 — candidatos fuzzy sem winner elegível não viram NA quando há plan:
# valor escrito em campo validável continua a ir para revisão.
# R155 — legado: fallback diagnóstico de identidade removido em R208.
# R156 — lote/SAP e largura via StockSAP deixaram de pontuar o winner:
# validam células próprias, mas não identificam uma entry do plan.
# R157 — índices cliente→entries são derivados dentro do motor como fallback:
# refs parciais não podem fazer um cliente existente parecer sem ligação ao plan.
# R158 — OF e OV escritas mas apontando para entries diferentes são conflito
# de identidade; não se ancora cegamente na OF nem se auto-preenche a linha.
# R159 — disponibilidade de referência passa a ser por campo: plan carregado
# não significa que dbase/dtopo/comp/etc tenham pool válido.
# R160 — legado: `clientes_lexicon` sozinho não era pool de plan; em R214
# deixou de entrar nos candidatos de cliente das rows.
# R161 — o fallback diagnóstico de conflito de identidade usa a mesma regra
# de modelo que o score; O/0 em modelo já não impede winner diagnóstico.
# R162 — espessura recupera vírgula perdida no OCR (26 → 2,6) apenas quando
# a variante /10 bate a referência; erros grandes continuam em revisão.
# R163 — obra_concluida deixa de pintar a linha inteira como NO_MATCH. A fase
# cheia no plan é aviso/metadata; não transforma células certas em erro.
# R164 — legado: header/footer sem referência real deixavam de contar como
# MATCH só por estarem preenchidos. Em R216, usam regra/sintaxe local.
# R165 — header.pernr validado contra ListaColaboradores e contra
# operador/n_operador; PERNR errado já não passa cinzento para o export CPIS.
# R166 — espessura também cruza StockSAP quando há lote com esp; divergências
# SAP deixam de cair em NA quando o plan não fornece referência útil.
# R167 — OCR não-numérico em campo numérico já não é auto-substituído pela
# referência; largura SAP acima de ±10mm vai para revisão.
# R168 — tolerâncias numéricas do cross alinhadas com o validador: COMP
# >50mm, LBASE/LTOPO/LARG >10mm e ESP >0,05mm entram em revisão.
# R169 — ferramenta/CONI inválido expõe a regra real (`ref_source`
# ferramenta) e a lista aceite, em vez de sair como ref ocr_raw.
# R170 — legado removido em R216: modelo já não bloqueia por grupos numéricos
# internos; a coerência global do winner decide a designação final.
# R171 — score do winner usa as mesmas tolerâncias numéricas do validador;
# medidas que viram NO_MATCH deixam de inflacionar confiança/autofill.
# R172 — `larg_mm` usa StockSAP quando há lote com largura; se não houver
# SAP mas a entry vencedora trouxer `larg`, valida contra o plan em vez de NA.
# R173 — footer.horas_trabalhadas em HH:MM já não aceita valores acima de
# 24h disfarçados como 24:01..24:59.
# R174 — footer.colunas_produzidas volta a alinhar com a schema/prompt:
# TOTAL QTD é inteiro não-negativo, não decimal.
# R175 — footer.horas_trabalhadas aceita os formatos permitidos pela schema
# (8h, 8:30h, 8 30h, 830), mantendo o limite máximo de 24:00.
# R176 — legado: campos de linha sem referência continuavam NA quando tinham
# formato plausível. Em R216, valores preenchidos validam por regra local.
# R177 — SOBRAS e CESTA Nº deixam de esconder texto impossível como NA.
# R178 — QTD de linha segue a schema estrita: 1-4 dígitos, sem decimais.
# R179 — PRI deixa de esconder texto impossível como NA; aceita apenas
# códigos curtos/números plausíveis, nunca OF de 6 dígitos.
# R180 — header.n_operador sem ListaColaboradores continua NA se tiver
# formato 1-5 dígitos, mas lixo textual entra em revisão por syntax.
# R181 — header.cod_maquina sem maquinas.xlsx continua NA para Mxxx
# plausível, mas formato impossível deixa de ficar cinzento.
# R182 — legado: fast-path por identidade removido em R208.
# R183 — refs sintéticas sem loaded_at não usam cache por id(), evitando
# reutilização acidental de índices quando o CPython recicla object ids.
# R184 — header.operador respeita operador_aliases no cross-check; nomes de
# uso comum deixam de falhar contra o sname oficial quando o código/PERNR bate.
# R185 — legado: abreviação de cliente removida em R208; só compacto igual.
# R186 — lote com H inicial é aceite como variante OCR de M quando o lote M...
# existe no StockSAP, e essa normalização também alimenta largura/espessura SAP.
# R187 — modelo compara código compacto (sem pontuação/espaços, com O/0)
# contra a designação do plan antes de declarar "muito diferente".
# R188 — modelo compacto normaliza marcas Nº/N° como N para apanhar códigos
# escritos como "N1" no OCR e "Nº1" no plan.
# R189 — OF/OV finais respeitam a mesma equivalência de identificadores usada
# no scoring (O/0 e zero extra único em OV) antes de irem para revisão.
# R190 — aliases de cliente já não entram no cross de rows em R208.
# R191 — legado: variantes I/1 removidas em R208.
# R192 — modelo em winner diagnóstico confirma quando o OCR é exatamente o
# primeiro token compacto da designação, sem auto-substituir a célula.
# R193 — winner diagnóstico também confirma modelo quando o código compacto
# OCR aparece literalmente na designação; fuzzy/O0 continua em revisão.
# R194 — legado: aliases de cliente compostos removidos em R208.
# R195 — legado: recuperação de I omitido antes de V removida em R208.
# R196 — cliente conhecido no plan confirma mesmo sem winner de linha; OF/OV
# e modelo continuam a suportar a revisão da identidade.
# R197 — cliente compacto deixa de aceitar substring no meio de outro nome
# (RODEL não pode bater FERRO DE LISBOA).
# R198 — removido também prefixo compacto genérico (METAL/COMP não batem
# METALOGALVA/COMPANHIA).
# R206..R213 — historial de remoção de âncoras/regras casuísticas.
# R214 — winner global calcula score contra todas as linhas do Plan, não só
# contra a união Top-K por campo. Cliente usa apenas nomes do Plan no Top-K
# local; aliases/lexicon não entram na escolha de rows.
# R215 — restaura a filosofia R134/R135 de substitute-everything: quando há
# winner concreto, divergências fortes também carregam o valor da referência.
# R216 — campos preenchidos deixam de cair em NA neutro quando podem ser
# validados por regra/sintaxe; modelo deixa de bloquear por conflito numérico
# interno (ex.: 1200 vs 1500).
# R217 (restore 30/05) — substitute-everything também em campos numéricos cujo
# OCR seja texto/lixo: removido o guarda de sintaxe numérica do R216 e a flag
# `auto_apply=False` (review-only). O valor canónico do plan/SAP volta a
# substituir sempre; a divergência só afeta a cor.
# R218 — winner por MISTURA (contagem de acertos + soma graduada: campo certo
# vale o dobro de um "parecido") + guarda de ambiguidade: quando rivais
# quase-empatados discordam num campo, esse campo não é substituído
# (auto_apply=False, review-only estreito por AMBIGUIDADE de winner — distinto
# do guarda numérico do R216, que continua removido).
# BUMP obrigatório: força regeneração dos cross-check JSON antigos.
ENGINE_VERSION = "v19_R218"

_FERRAMENTA_REF_LABEL = f"{'/'.join(sorted(ALLOWED_FERRAMENTA_TEXT))} ou número"
_PRI_RE = re.compile(r"^(?:[A-Z]?\d{1,3}|P\.?\d|REP\.?\s?C?\d+)$")


# Utilidades de distância ----------------------------------------------------

def _lev_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 5:
        return 999
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _str_sim(target: str, candidate: str) -> float:
    if not target or not candidate:
        return 0.0
    t = target.upper()
    c = candidate.upper()
    if t == c:
        return 100.0
    if t in c or c in t:
        return 80.0
    d = _lev_distance(t, c)
    m = max(len(t), len(c))
    if d >= m:
        return 0.0
    return 100.0 * (1 - d / m)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _num_variants(field: str, value: Any) -> list[float]:
    """Numeric interpretations for a field.

    ``esp`` often loses the decimal separator in OCR: 2,6 -> 26. Keep this
    recovery local to thickness so lengths/widths are not silently rescaled.
    """
    n = _num(value)
    if n is None:
        return []

    variants = [n]
    raw = str(value or "").strip().replace(" ", "")
    if field == "esp" and raw.isdigit() and len(raw) == 2:
        recovered = n / 10
        if 0.5 <= recovered <= 15:
            variants.append(recovered)

    out: list[float] = []
    for v in variants:
        if not any(abs(v - seen) <= 1e-9 for seen in out):
            out.append(v)
    return out


def _num_matches(field: str, value: Any, candidate: Any, max_delta: float) -> bool:
    cand = _num(candidate)
    if cand is None:
        return False
    return any(abs(v - cand) <= max_delta for v in _num_variants(field, value))


def _best_num_sim(field: str, target: Any, candidate: Any, max_delta: float) -> float:
    cand = _num(candidate)
    if cand is None:
        return 0.0
    variants = _num_variants(field, target)
    if not variants:
        return 0.0
    return max(_num_sim(v, cand, max_delta) for v in variants)


def _num_sim(target: float | None, candidate: float | None, max_delta: float) -> float:
    if target is None or candidate is None:
        return 0.0
    d = abs(target - candidate)
    if d >= max_delta:
        return 0.0
    if d <= max_delta / 10:
        return 100.0
    return 100.0 * (1 - d / max_delta)


def _model_first_token(value: object) -> str:
    """Canonical model key used by plan indexes: text before " - "."""
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text.split(" - ", 1)[0].strip()


def _model_compact(value: object) -> str:
    raw = str(value or "")
    raw = re.sub(r"(?i)\bN[º°]\s*(?=\d)", "N", raw)
    norm = _norm_ascii_upper(raw)
    norm = re.sub(r"\bNO\s*(?=\d)", "N", norm)
    return re.sub(r"[^A-Z0-9]+", "", norm)


def _model_compact_variants(value: object) -> list[str]:
    base = _model_compact(value)
    if not base:
        return []
    return list(set(_o_zero_variants(base)))


def _model_compact_matches(model_value: object, designacao: object) -> bool:
    """Loose model-code containment after removing punctuation/spacing.

    This is deliberately containment-only, not general fuzzy matching: it
    catches OCR/layout variants like ``0641-S-515`` vs ``0641S515`` and
    ``CA06F18D N1`` vs ``CAO6F18D - Nº1 ...``. Remaining differences fall
    back to the normal model similarity/winner scoring.
    """
    model_variants = [v for v in _model_compact_variants(model_value) if len(v) >= 4]
    if not model_variants:
        return False
    des_compact = _model_compact(designacao)
    des_ft_compact = _model_compact(_model_first_token(designacao))
    haystacks = [h for h in (des_compact, des_ft_compact) if len(h) >= 4]
    if not haystacks:
        return False

    for variant in model_variants:
        for haystack in haystacks:
            if variant in haystack:
                return True
    return False


def _model_matches_designacao(model_value: object, designacao: object) -> bool:
    model = str(model_value or "").strip().upper()
    if not model or len(model) < 4:
        return False
    des = str(designacao or "").strip().upper()
    if not des:
        return False
    des_ft = _model_first_token(des)
    return bool(
        model in des
        or (des_ft and des_ft in _o_zero_variants(model))
        or _model_compact_matches(model, des)
    )


def _model_exact_compact_contained(model_value: object, designacao: object) -> bool:
    model = _model_compact(model_value)
    if len(model) < 4:
        return False
    des = _model_compact(designacao)
    return bool(des and model in des)


def _is_missing_ocr(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    compact = re.sub(r"[\s\-_.:/\\]+", "", text)
    return bool(compact) and set(compact) <= {"?"}


def _identifier_compact(value: object, *, pad_of: bool = False) -> str:
    raw = _norm_ascii_upper(value)
    compact = re.sub(r"[^A-Z0-9]+", "", raw)
    if pad_of and compact.isdigit() and len(compact) < 6:
        compact = compact.zfill(6)
    return compact


def _identifier_similarity(value: object, candidate: object, *, pad_of: bool = False) -> float:
    left = _identifier_compact(value, pad_of=pad_of)
    right = _identifier_compact(candidate, pad_of=pad_of)
    if not left or not right:
        return 0.0
    left_variants = _o_zero_variants(left)
    right_variants = _o_zero_variants(right)
    if set(left_variants) & set(right_variants):
        return 1.0
    return max(
        _str_sim(lv, rv) / 100.0
        for lv in left_variants
        for rv in right_variants
    )


def _numeric_similarity(field: str, value: object, candidate: object, tolerance: float) -> float:
    cand = _num(candidate)
    if cand is None:
        return 0.0
    variants = _num_variants(field, value)
    if not variants:
        return 0.0
    best_delta = min(abs(v - cand) for v in variants)
    if best_delta <= tolerance:
        return 1.0
    # Depois da tolerância, ainda há evidência fraca; mas diferenças grandes
    # em dimensões não podem parecer quase perfeitas.
    decay_window = max(tolerance, 1.0)
    return max(0.0, 1.0 - ((best_delta - tolerance) / decay_window))


def _entry_field_similarity(field: str, entry: dict, row: dict, refs: dict) -> float | None:
    value = row.get(field)
    if _is_missing_ocr(value):
        return None

    if field == "of":
        entry_value = entry.get("_of") or entry.get("of")
        return _identifier_similarity(value, entry_value, pad_of=True)

    if field == "ov":
        return _identifier_similarity(value, entry.get("ov"))

    if field == "cliente":
        if _cliente_values_match(value, entry.get("cliente"), refs):
            return 1.0
        left = _cliente_compact(value)
        right = _cliente_compact(entry.get("cliente"))
        if not left or not right:
            return None
        return _str_sim(left, right) / 100.0

    if field == "modelo":
        designacao = entry.get("designacao")
        if _model_matches_designacao(value, designacao):
            return 1.0
        model = _model_compact(value)
        if len(model) < 4:
            return None
        candidates = [
            _model_compact(_model_first_token(designacao)),
            _model_compact(designacao),
        ]
        candidates = [c for c in candidates if c]
        if not candidates:
            return None
        return max(_str_sim(model, c) / 100.0 for c in candidates)

    plan_attr = {
        "comp_mm": "comp",
        "larg_mm": "larg",
        "lbase": "lbase",
        "ltopo": "ltopo",
        "esp": "esp",
        "dbase": "dbase",
        "dtopo": "dtopo",
    }.get(field)
    if plan_attr:
        candidate = entry.get(plan_attr)
        if candidate in (None, ""):
            return None
        tolerance = _VERY_DIFF_NUM_ABS[field]
        return _numeric_similarity(field, value, candidate, tolerance)

    return None


def _entry_global_score(
    entry: dict,
    row: dict,
    refs: dict,
    score_fields: set[str] | frozenset[str] | None = None,
) -> tuple[float, int]:
    total = 0.0
    exact = 0
    fields = score_fields if score_fields is not None else _PLAN_FIELDS
    for field in fields:
        if field not in _PLAN_FIELDS:
            continue
        sim = _entry_field_similarity(field, entry, row, refs)
        if sim is None:
            continue
        total += max(0.0, min(1.0, sim))
        if sim >= 1.0:
            exact += 1
    return total, exact


# Normalização cosmética (única guarda mantida) -----------------------------

def _format_value(field: str, value: Any) -> str:
    """2,6 = 2.6 → '2,6'. 1227.0 = 1227 → '1227'. Strings: strip."""
    if value is None or value == "":
        return ""
    if field == "esp":
        n = _num(value)
        if n is None:
            return str(value).strip()
        return f"{n:g}".replace(".", ",")
    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "dbase", "dtopo", "qtd"):
        n = _num(value)
        if n is None:
            return str(value).strip()
        return str(int(round(n)))
    return str(value).strip()


# Pre-indexação das refs (cache por refs id) --------------------------------

_INDEX_CACHE: dict[int, dict] = {}


def invalidate_index_cache() -> None:
    """R134 — limpar o cache de índices das refs.

    Chamado no reload das refs (`RefWatcher.force_reload`/`get_refs`), à
    semelhança de `obras_status.invalidate_cache()`. `_get_indices` é keyed por
    `id(refs)` + `loaded_at` (resolução de 1s); um re-upload no mesmo segundo
    com reutilização de `id()` pelo CPython poderia servir índices do plano
    antigo. Limpar no reload fecha essa janela e evita o crescimento ilimitado
    do cache (uma entrada por objeto de refs).
    """
    _INDEX_CACHE.clear()


def _get_indices(refs: dict) -> dict:
    key = id(refs)
    loaded_at = refs.get("loaded_at")
    if loaded_at:
        cached = _INDEX_CACHE.get(key)
        if cached and cached.get("loaded_at") == loaded_at:
            return cached

    of_to_entries = refs.get("of_to_entries", {}) or {}
    ov_to_entries: dict[str, list[dict]] = {}
    des_to_entries: dict[str, list[dict]] = {}
    model_ft_to_entries: dict[str, list[dict]] = {}
    plan_by_cliente: dict[str, list[dict]] = {}
    clientes_plan: set[str] = set()
    dim_indices: dict[str, dict[float, list[dict]]] = {
        "comp": {}, "larg": {}, "lbase": {}, "ltopo": {}, "esp": {},
        # R128 — kanban LASER (dbase/dtopo)
        "dbase": {}, "dtopo": {},
    }

    for of_key, entries in of_to_entries.items():
        for e in entries:
            stamped = dict(e)
            stamped["_of"] = of_key
            cli_val = str(e.get("cliente") or "").strip().upper()
            if cli_val:
                clientes_plan.add(cli_val)
                plan_by_cliente.setdefault(cli_val, []).append(stamped)
            ov_val = str(e.get("ov") or "").strip()
            if ov_val:
                ov_to_entries.setdefault(ov_val, []).append(stamped)
            des = str(e.get("designacao") or "").strip()
            if des:
                des_to_entries.setdefault(des, []).append(stamped)
                ft = _model_first_token(des)
                if ft:
                    model_ft_to_entries.setdefault(ft, []).append(stamped)
            for attr in ("comp", "larg", "lbase", "ltopo", "esp", "dbase", "dtopo"):
                v = _num(e.get(attr))
                if v is not None:
                    dim_indices[attr].setdefault(v, []).append(stamped)

    indices = {
        "loaded_at": loaded_at,
        "of_to_entries": of_to_entries,
        "ov_to_entries": ov_to_entries,
        "des_to_entries": des_to_entries,
        "model_ft_to_entries": model_ft_to_entries,
        "plan_by_cliente": plan_by_cliente,
        "clientes_plan": clientes_plan,
        "dim_indices": dim_indices,
        "of_keys": list(of_to_entries.keys()),
        "ov_keys": list(ov_to_entries.keys()),
        "des_keys": list(des_to_entries.keys()),
        "model_ft_keys": list(model_ft_to_entries.keys()),
    }
    if loaded_at:
        _INDEX_CACHE[key] = indices
    return indices


# Geração de candidatos por campo --------------------------------------------

def _topk_keys_by_sim(target: str, pool: list[str], k: int) -> list[tuple[str, float]]:
    if not target or not pool:
        return []
    scored = [(_str_sim(target, key), key) for key in pool]
    scored.sort(reverse=True)
    return [(key, s) for s, key in scored[:k] if s > 0]


def _candidates_for_field(field: str, row: dict, refs: dict, idx: dict) -> list[dict]:
    """Top-K candidatos por campo (puro top-K)."""
    ocr_value = str(row.get(field) or "").strip()
    if _is_missing_ocr(ocr_value) or field in _NO_REF_FIELDS:
        return []

    of_to_entries = idx["of_to_entries"]
    ov_to_entries = idx["ov_to_entries"]
    des_to_entries = idx["des_to_entries"]
    dim_indices = idx["dim_indices"]
    lotes_sap = refs.get("lotes_sap_full", {}) or {}
    clientes_plan = (
        set(refs.get("clientes_plan", frozenset()) or frozenset())
        | set(idx.get("clientes_plan", set()))
    )

    out: list[dict] = []

    if field == "of":
        normalized = _identifier_compact(ocr_value, pad_of=True)
        seen: set[str] = set()
        for v in _o_zero_variants(normalized):
            if v in of_to_entries and v not in seen:
                seen.add(v)
                out.append({
                    "value": v, "sim": _str_sim(normalized, v),
                    "plan_entries": [dict(e, _of=e.get("_of") or v) for e in of_to_entries[v]],
                })
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(normalized, idx["of_keys"], _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({
                        "value": k, "sim": s,
                        "plan_entries": [dict(e, _of=e.get("_of") or k) for e in of_to_entries[k]],
                    })
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field == "ov":
        seen: set[str] = set()
        normalized = _identifier_compact(ocr_value)
        for v in _o_zero_variants(normalized):
            if v in ov_to_entries and v not in seen:
                seen.add(v)
                out.append({"value": v, "sim": _str_sim(normalized, v), "plan_entries": ov_to_entries[v]})
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(normalized, idx["ov_keys"], _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({"value": k, "sim": s, "plan_entries": ov_to_entries[k]})
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field == "modelo":
        model_ft_to_entries = {
            str(k).strip().upper(): list(v or [])
            for k, v in (idx.get("model_ft_to_entries", {}) or {}).items()
        }
        for k, entries in (refs.get("plan_by_modelo_ft", {}) or {}).items():
            key = str(k).strip().upper()
            if not key:
                continue
            model_ft_to_entries.setdefault(key, [])
            model_ft_to_entries[key].extend(entries or [])

        seen: set[tuple] = set()

        def _append_model_candidate(value: str, sim: float, entries: list[dict]) -> None:
            plan_entries: list[dict] = []
            for e in entries or []:
                k = _entry_key(e)
                if k in seen:
                    continue
                seen.add(k)
                plan_entries.append(e)
            if plan_entries:
                out.append({"value": value, "sim": sim, "plan_entries": plan_entries})

        ocr_u = ocr_value.upper()
        for v in _o_zero_variants(ocr_u):
            if v in model_ft_to_entries:
                _append_model_candidate(v, _str_sim(ocr_u, v), model_ft_to_entries[v])

        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(ocr_u, list(model_ft_to_entries.keys()), _TOP_K):
                _append_model_candidate(k, s, model_ft_to_entries[k])
                if len(out) >= _TOP_K:
                    break

        for k, s in _topk_keys_by_sim(ocr_value, idx["des_keys"], _TOP_K):
            _append_model_candidate(k, s, des_to_entries[k])
            if len(out) >= _TOP_K:
                break
        return out[:_TOP_K]

    if field == "cliente":
        ocr_u = ocr_value.upper()
        plan_by_cliente = {
            str(k).strip().upper(): list(v or [])
            for k, v in (idx.get("plan_by_cliente", {}) or {}).items()
        }
        for k, entries in (refs.get("plan_by_cliente", {}) or {}).items():
            key = str(k).strip().upper()
            if not key:
                continue
            plan_by_cliente.setdefault(key, [])
            plan_by_cliente[key].extend(entries or [])
        pool = set(clientes_plan)

        seen_clientes: set[str] = set()

        def _append_cliente_candidate(key: str, sim: float) -> None:
            key = str(key or "").strip().upper()
            if not key or key in seen_clientes:
                return
            seen_clientes.add(key)
            out.append({
                "value": key, "sim": sim,
                "plan_entries": plan_by_cliente.get(key, []),
            })

        for k, s in _topk_keys_by_sim(ocr_u, list(pool), _TOP_K):
            _append_cliente_candidate(k, s)
            if len(out) >= _TOP_K:
                break
        return out[:_TOP_K]

    if field == "lote":
        if not lotes_sap:
            return []
        seen: set[str] = set()
        for v in _o_zero_variants(ocr_value):
            if v in lotes_sap and v not in seen:
                seen.add(v)
                out.append({"value": v, "sim": _str_sim(ocr_value, v), "plan_entries": [], "sap_entry": lotes_sap[v]})
        if len(out) < _TOP_K:
            for k, s in _topk_keys_by_sim(ocr_value, list(lotes_sap.keys()), _TOP_K):
                if k not in seen:
                    seen.add(k)
                    out.append({"value": k, "sim": s, "plan_entries": [], "sap_entry": lotes_sap[k]})
                if len(out) >= _TOP_K:
                    break
        return out[:_TOP_K]

    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo"):
        ocr_nums = _num_variants(field, ocr_value)
        if not ocr_nums:
            return []
        plan_attr = {
            "comp_mm": "comp", "larg_mm": "larg",
            "lbase": "lbase", "ltopo": "ltopo", "esp": "esp",
            # R128 — kanban LASER
            "dbase": "dbase", "dtopo": "dtopo",
        }[field]
        max_delta = {
            "comp_mm": _VERY_DIFF_NUM_ABS["comp_mm"],
            "larg_mm": _VERY_DIFF_NUM_ABS["larg_mm"],
            "lbase": _VERY_DIFF_NUM_ABS["lbase"],
            "ltopo": _VERY_DIFF_NUM_ABS["ltopo"],
            "esp": _VERY_DIFF_NUM_ABS["esp"],
            "dbase": _VERY_DIFF_NUM_ABS["dbase"],
            "dtopo": _VERY_DIFF_NUM_ABS["dtopo"],
        }[field]
        nearest = [
            (min(abs(val - ocr_num) for ocr_num in ocr_nums), val, entries)
            for val, entries in dim_indices[plan_attr].items()
        ]
        nearest.sort(key=lambda kv: kv[0])
        for _delta, v, entries in nearest[:_TOP_K]:
            out.append({
                "value": v,
                "sim": _numeric_similarity(field, ocr_value, v, max_delta) * 100.0,
                "plan_entries": entries,
            })
        return out[:_TOP_K]

    return []


# Passe 2: cruzar candidatos e escolher entry vencedora ----------------------

def _entry_key(entry: dict) -> tuple:
    return (
        str(entry.get("_of") or entry.get("of") or "").strip(),
        str(entry.get("ov") or "").strip(),
        str(entry.get("designacao") or "").strip().upper(),
    )


def _all_plan_entries(idx: dict) -> dict[tuple, dict]:
    entries_by_key: dict[tuple, dict] = {}
    for of_key, entries in (idx.get("of_to_entries") or {}).items():
        for entry in entries or []:
            stamped = dict(entry)
            stamped["_of"] = entry.get("_of") or of_key
            entries_by_key.setdefault(_entry_key(stamped), stamped)
    return entries_by_key


def _best_scored_entry(
    entries_by_key: dict[tuple, dict],
    row: dict,
    refs: dict,
    current_phase: str | None,
    score_fields: set[str] | frozenset[str] | None = None,
) -> dict | None:
    """Escolhe a melhor entry do plan.

    R218 — MISTURA contagem + soma: o critério principal é
    ``combined = exact_score + global_score`` (um campo que bate certo vale o
    dobro de um campo só "parecido"). A contagem domina; a soma graduada afina e
    desempata. R213 mantém-se: nenhum campo veta, cada campo vale 0..1.
    Os rivais quase-empatados (``combined`` a <= ``_WINNER_MARGIN`` do melhor)
    ficam em ``winner['_rivals']`` para a guarda de ambiguidade.
    """
    from app.pipeline.of_consumption import remaining as _remaining

    eligible: list[tuple] = []
    for order, (k, e) in enumerate(entries_by_key.items()):
        if "_of" not in e:
            e = dict(e)
            e["_of"] = k[0]
        global_score, exact_score = _entry_global_score(e, row, refs, score_fields)
        combined = exact_score + global_score
        phase_full = 1 if (current_phase and _phase_is_full(e, current_phase)) else 0
        # R138 — remaining consciente do setor (mesma medida do wizard).
        rem = _remaining(e, phase=current_phase)
        rem_sort = 9e9 if rem == float("inf") else rem
        eligible.append(
            (-combined, -global_score, -exact_score, phase_full, rem_sort, order, e)
        )

    if not eligible:
        return None
    eligible.sort()
    best = eligible[0]
    best_combined = -best[0]
    best_global = -best[1]
    best_exact = -best[2]
    best_rem_sort = best[4]
    if best_global < _MIN_GLOBAL_WINNER_SCORE:
        return None
    winner = dict(best[6])
    winner["_score"] = round(float(best_global), 3)
    winner["_exact_score"] = int(best_exact)
    winner["_combined"] = round(float(best_combined), 3)
    if best_rem_sort < 9e9:
        winner["_remaining"] = best_rem_sort
    # R218 — rivais quase-empatados (eligible já ordenado por combined desc).
    rivals: list[dict] = []
    for cand in eligible[1:]:
        if best_combined - (-cand[0]) <= _WINNER_MARGIN:
            rivals.append(cand[6])
        else:
            break
    if rivals:
        winner["_rivals"] = rivals
    return winner


# R125 — consciência do estado de produção (fases do plan) ------------------

def _current_phase(sheet_data: dict, refs: dict) -> str | None:
    """R125 — coluna do plan (bf/c/q/s/r/a/exp) que corresponde à máquina
    da folha. None se não houver mapeamento — toda a lógica de fases
    fica em no-op.
    """
    setor = ((sheet_data.get("header") or {}).get("setor_maquina") or "").strip()
    if not setor:
        return None
    return machine_phase_from_setor(setor, refs)


def _phase_is_full(entry: dict, phase: str) -> bool:
    """R125 — True se a fase `phase` já atingiu `quanttrp` (sector
    fechado para esta linha do plan).
    """
    q = _num(entry.get("quanttrp"))
    fases = entry.get("fases") or {}
    p = _num(fases.get(phase))
    if q is None or p is None or q <= 0:
        return False
    return p >= q


def _find_winner_entry(
    candidates_by_field: dict[str, list[dict]],
    row: dict,
    refs: dict,
    idx: dict | None = None,
    current_phase: str | None = None,
    score_fields: set[str] | frozenset[str] | None = None,
) -> dict | None:
    """Escolhe a entry do plan por melhor score global simples."""
    entries_by_key = _all_plan_entries(idx or {})
    if not entries_by_key:
        for field in _PLAN_FIELDS:
            for cand in candidates_by_field.get(field, []):
                for entry in cand.get("plan_entries", []):
                    key = _entry_key(entry)
                    entries_by_key.setdefault(key, entry)
    if not entries_by_key:
        return None
    return _best_scored_entry(entries_by_key, row, refs, current_phase, score_fields)


def _all_eligible_phase_full(
    candidates_by_field: dict[str, list[dict]],
    row: dict,
    refs: dict,
    current_phase: str | None,
    winner: dict | None = None,
) -> bool:
    """R125 — aviso de obra concluída só existe depois de winner válido."""
    _ = (candidates_by_field, row, refs)
    if not current_phase or not winner:
        return False
    return _phase_is_full(winner, current_phase)


# Detecção de "muito diferente" ---------------------------------------------

def _is_very_different(field: str, ocr_value: str, proposed: str) -> bool:
    """True se o proposto é muito diferente do OCR — sinaliza vermelho."""
    if not ocr_value or not proposed:
        return False
    if field in ("comp_mm", "larg_mm", "lbase", "ltopo", "dbase", "dtopo", "esp"):
        ocr_n = _num(ocr_value)
        prop_n = _num(proposed)
        if ocr_n is None or prop_n is None:
            return False
        if field == "esp" and _num_matches(field, ocr_value, proposed, 0.05):
            return False
        abs_max = _VERY_DIFF_NUM_ABS.get(field, 0)
        if abs(ocr_n - prop_n) > abs_max:
            return True
        return False
    if field == "modelo":
        proposed_u = str(proposed or "").strip().upper()
        ocr_u = str(ocr_value or "").strip().upper()
        ocr_variants = [v for v in _o_zero_variants(ocr_u) if len(v) >= 4]
        if proposed_u and any(v in proposed_u for v in ocr_variants):
            return False
        if _model_compact_matches(ocr_u, proposed_u):
            return False
        return _str_sim(_model_compact(ocr_u), _model_compact(proposed_u)) < _VERY_DIFF_STR_SIM
    sim = _str_sim(str(ocr_value), str(proposed))
    return sim < _VERY_DIFF_STR_SIM


# Aplicação da entry vencedora à linha ---------------------------------------

def _make_cell(value: str, status: str, source: str, **extra) -> dict:
    cell = {
        "value": value,
        "status": status,
        "label": _STATUS_LABELS.get(status, status),
        "source": source,
    }
    cell.update(extra)
    return cell


def _score_ferramenta_cell(ocr_value: str) -> dict:
    canonical = normalize_ferramenta(ocr_value)
    if canonical == "":
        return _make_cell("", "NA", "ocr_raw")
    if canonical is None:
        return _make_cell(
            ocr_value,
            "very_different",
            "ocr_raw",
            proposed=_FERRAMENTA_REF_LABEL,
            ref_source="ferramenta",
        )
    if canonical == str(ocr_value or "").strip():
        return _make_cell(canonical, "confirmed", "ocr_raw")
    return _make_cell(canonical, "snapped", "lexicon")


def _score_no_ref_row_cell(field: str, ocr_value: str) -> dict:
    """Validate filled no-plan fields by local rule/syntax instead of NA."""
    if not ocr_value:
        return _make_cell("", "NA", "ocr_raw")

    valid: bool | None = None
    if field == "pri":
        valid = _looks_like_pri(ocr_value)
    elif field == "qtd":
        valid = _looks_like_short_digits(ocr_value)
    elif field in ("qtd_metros", "m2", "sobras"):
        valid = _looks_like_non_negative_decimal(ocr_value)
    elif field == "cesta_n":
        valid = any(ch.isdigit() for ch in ocr_value)
    elif field in ("inicio", "fim"):
        valid = _looks_like_time_of_day(ocr_value)
    elif field == "duracao":
        valid = _looks_like_hours(ocr_value)
    elif field == "resolvido":
        valid = _looks_like_yes_no_marker(ocr_value)

    if valid is False:
        return _make_cell(ocr_value, "very_different", "syntax")
    return _make_cell(ocr_value, "confirmed", "syntax")


def _finish_cell(
    field: str,
    ocr_value: str,
    proposed: str,
    source: str,
    score: float | int | None,
) -> dict:
    """Formata o valor proposto, decide o estado vs o OCR, devolve a célula.

    R134/R135 (30/05): quando há proposta concreta, o `value` devolvido é o
    canónico. `very_different` é um estado intermédio para revisão/auto-apply,
    não um motivo para preservar OCR dentro da célula validada.
    """
    proposed_fmt = _format_value(field, proposed)
    ocr_fmt = _format_value(field, ocr_value)

    # R217 (30/05) — substitute-everything: havendo proposta concreta do
    # plan/SAP, o valor canónico substitui sempre o OCR, mesmo em campos
    # numéricos cujo OCR seja texto/lixo. Removido o guarda de sintaxe
    # numérica do R216 (que preservava o OCR e bloqueava auto-apply); a única
    # coisa que a divergência afeta é a cor (very_different/vermelho).
    if proposed_fmt and ocr_fmt and proposed_fmt.upper() == ocr_fmt.upper():
        status = "confirmed"
    elif not ocr_value:
        status = "snapped"  # autofill
    elif _is_very_different(field, ocr_value, proposed):
        status = "very_different"
    else:
        status = "snapped"
    return _make_cell(proposed_fmt, status, source=source, score=score)


def _identifier_values_match(field: str, ocr_value: object, proposed: object) -> bool:
    ocr = str(ocr_value or "").strip().upper()
    ref = str(proposed or "").strip().upper()
    if not ocr or not ref:
        return False

    if field == "of":
        ocr_variants = {normalize_of(v) for v in _o_zero_variants(ocr)}
        return normalize_of(ref) in ocr_variants

    if field != "ov":
        return False

    ocr_variants = set(_o_zero_variants(ocr))
    if ref in ocr_variants:
        return True

    numeric_ocr = [v for v in ocr_variants if v.isdigit()]
    numeric_ref = [v for v in _o_zero_variants(ref) if v.isdigit()]
    for left in numeric_ocr:
        for right in numeric_ref:
            if abs(len(left) - len(right)) != 1:
                continue
            longer, shorter = (left, right) if len(left) > len(right) else (right, left)
            if any(
                ch == "0" and longer[:i] + longer[i + 1:] == shorter
                for i, ch in enumerate(longer)
            ):
                return True
    return False


def _local_candidate_proposal(
    field: str,
    ocr_value: str,
    candidates: list[dict],
    row: dict,
    refs: dict,
) -> str | None:
    """Validação local de uma célula quando ainda não há winner global."""
    if not ocr_value:
        return None
    _ = row
    if not candidates:
        return None
    best_sim = max(float(cand.get("sim") or 0.0) for cand in candidates)
    best_candidates = [
        cand for cand in candidates
        if abs(float(cand.get("sim") or 0.0) - best_sim) <= 1e-9
    ]
    if not best_candidates:
        return None

    proposals: list[str] = []
    for cand in best_candidates:
        value = cand.get("value")
        if value is None or value == "":
            continue
        proposed = str(value).strip()
        if proposed:
            proposals.append(proposed)

    if not proposals:
        return None

    first_fmt = _format_value(field, proposals[0])
    if any(_format_value(field, value) != first_fmt for value in proposals[1:]):
        return None
    proposed = proposals[0]
    if field in ("of", "ov"):
        return proposed if _identifier_values_match(field, ocr_value, proposed) else None
    if field == "cliente":
        return proposed if _cliente_values_match(ocr_value, proposed, refs) else None
    if field == "modelo":
        return proposed if _model_matches_designacao(ocr_value, proposed) else None
    return proposed


def _has_plan_reference_pool(refs: dict) -> bool:
    return bool(refs.get("of_to_entries"))


def _has_field_reference_pool(field: str, refs: dict, idx: dict, row: dict) -> bool:
    if field == "lote":
        return bool(refs.get("lotes_sap_full"))
    if field == "larg_mm":
        _sap_lote, sap_e = _sap_lote_entry(refs, row.get("lote"))
        if sap_e and sap_e.get("larg") not in (None, ""):
            return True
        return bool((idx.get("dim_indices") or {}).get("larg"))
    if field == "esp":
        _sap_lote, sap_e = _sap_lote_entry(refs, row.get("lote"))
        if sap_e and sap_e.get("esp") not in (None, ""):
            return True
        return bool((idx.get("dim_indices") or {}).get("esp"))
    if field == "of":
        return bool(idx.get("of_keys"))
    if field == "ov":
        return bool(idx.get("ov_keys"))
    if field == "modelo":
        return bool(
            idx.get("des_keys")
            or idx.get("model_ft_keys")
            or refs.get("plan_by_modelo_ft")
        )
    if field == "cliente":
        return bool(
            set(refs.get("clientes_plan", frozenset()) or frozenset())
            | set(idx.get("clientes_plan", set()) or set())
        )
    plan_attr = {
        "comp_mm": "comp",
        "lbase": "lbase",
        "ltopo": "ltopo",
        "dbase": "dbase",
        "dtopo": "dtopo",
    }.get(field)
    if plan_attr:
        return bool((idx.get("dim_indices") or {}).get(plan_attr))
    return _has_plan_reference_pool(refs)


def _field_ref_source(field: str, refs: dict | None = None, row: dict | None = None) -> str:
    if field == "lote":
        return "sap"
    if field == "larg_mm":
        _sap_lote, sap_e = _sap_lote_entry(refs or {}, (row or {}).get("lote"))
        if sap_e and sap_e.get("larg") not in (None, ""):
            return "sap"
        return "plan"
    if field == "esp":
        _sap_lote, sap_e = _sap_lote_entry(refs or {}, (row or {}).get("lote"))
        if sap_e and sap_e.get("esp") not in (None, ""):
            return "sap"
    return "plan"


def _entry_field_canonical(field: str, entry: dict, template_name: str | None = None) -> str:
    """Valor canónico que uma entry do plan proporia para `field` (espelha a
    extração do winner em _apply_winner_to_field). "" se a entry não tem dado."""
    if field == "of":
        return str(entry.get("_of") or entry.get("of") or "").strip()
    if field == "ov":
        return str(entry.get("ov") or "").strip()
    if field == "modelo":
        des = " ".join(str(entry.get("designacao") or "").split())
        if not des:
            return ""
        if template_name == "acabamento":
            return _model_first_token(des) or des
        return des
    if field == "cliente":
        return str(entry.get("cliente") or "").strip()
    plan_attr = {
        "comp_mm": "comp", "larg_mm": "larg", "lbase": "lbase",
        "ltopo": "ltopo", "esp": "esp", "dbase": "dbase", "dtopo": "dtopo",
    }.get(field)
    if plan_attr:
        v = entry.get(plan_attr)
        return str(v) if v not in (None, "") else ""
    return ""


def _canonical_values_disagree(field: str, a: str, b: str) -> bool:
    """True se dois valores canónicos divergem significativamente. Números:
    diferença acima da tolerância do campo. Texto: diferente após normalização."""
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) > _VERY_DIFF_NUM_ABS.get(field, 0.0)
    return _format_value(field, a).strip().upper() != _format_value(field, b).strip().upper()


def _winner_ambiguous_for_field(
    field: str, proposed: str, winner: dict, template_name: str | None = None
) -> bool:
    """R218 — guarda de ambiguidade: True se algum rival quase-empatado propõe,
    neste campo, um valor canónico diferente do do winner. Nesse caso o motor
    não deve substituir o OCR — fica para revisão humana."""
    rivals = winner.get("_rivals") or []
    if not rivals or not proposed:
        return False
    for rival in rivals:
        rv = _entry_field_canonical(field, rival, template_name)
        if rv and _canonical_values_disagree(field, proposed, rv):
            return True
    return False


def _apply_winner_to_field(
    field: str,
    ocr_value: str,
    winner: dict | None,
    candidates: list[dict],
    refs: dict,
    row: dict,
    has_field_reference: bool,
    template_name: str | None = None,
) -> dict:
    if field in _NO_REF_FIELDS:
        return _score_no_ref_row_cell(field, ocr_value)

    # --- Campos validados directamente contra o StockSAP (R123) ---------
    # O lote e a largura vivem no StockSAP, não na entry do plan_colunas.
    if field == "lote":
        if not ocr_value:
            return _make_cell("", "NA", "ocr_raw")
        sap_full = refs.get("lotes_sap_full", {}) or {}
        if not sap_full:
            return _make_cell(ocr_value, "very_different", "ocr_raw", ref_source="sap")
        sap_lote, sap_entry = _sap_lote_entry(refs, ocr_value)
        if sap_entry:
            extra = {"ref_source": "sap"}
            if sap_lote and sap_lote != ocr_value.strip().upper():
                extra["proposed"] = sap_lote
            return _make_cell(ocr_value, "confirmed", "sap", **extra)
        if candidates and candidates[0].get("sim", 0) >= 80:
            return _make_cell(
                ocr_value, "very_different", "ocr_raw",
                proposed=str(candidates[0].get("value") or ""),
                ref_source="sap",
            )
        # lote escrito mas desconhecido — amarelo via cc-warn no _cell.html
        return _make_cell(
            ocr_value, "very_different", "ocr_raw", ref_source="sap",
        )

    if field == "larg_mm":
        # StockSAP tem prioridade porque a largura da bobine é indexada pelo
        # lote. Sem lote/SAP, deixamos a lógica do plan usar `winner.larg`
        # quando essa referência existir.
        _sap_lote, sap_e = _sap_lote_entry(refs, row.get("lote"))
        sap_larg = sap_e.get("larg") if sap_e else None
        if sap_larg not in (None, ""):
            ocr_larg_n = _num(ocr_value)
            sap_larg_n = _num(sap_larg)
            if (
                ocr_value
                and ocr_larg_n is not None
                and sap_larg_n is not None
                and abs(ocr_larg_n - sap_larg_n) > _VERY_DIFF_NUM_ABS["larg_mm"]
            ):
                return _finish_cell(
                    field, ocr_value, str(sap_larg), "sap", None
                )
            return _finish_cell(
                field, ocr_value, str(sap_larg), "sap", None
            )

    if field == "esp":
        # StockSAP também traz espessura do lote. Se o operador escreveu esp,
        # validamos primeiro contra SAP; se estiver em branco, deixamos a
        # lógica do plan decidir se há confiança suficiente para autofill.
        _sap_lote, sap_e = _sap_lote_entry(refs, row.get("lote"))
        sap_esp = sap_e.get("esp") if sap_e else None
        if ocr_value and sap_esp not in (None, ""):
            return _finish_cell(
                field, ocr_value, str(sap_esp), "sap", None
            )

    # --- Campos resolvidos pela entry vencedora do plan -----------------
    if winner is None and not has_field_reference:
        if ocr_value:
            return _make_cell(
                ocr_value, "very_different", "ocr_raw",
                ref_source=_field_ref_source(field, refs, row),
            )
        return _make_cell("", "NA", "ocr_raw")

    if winner is None and not candidates:
        # R120 — operador escreveu algo num campo validável e o motor não
        # achou candidato nem winner: vermelho (very_different) em vez de
        # cinza. OCR vazio continua NA (sem dado para validar).
        if ocr_value:
            return _make_cell(
                ocr_value, "very_different", "ocr_raw",
                ref_source=_field_ref_source(field, refs, row),
            )
        return _make_cell(ocr_value, "NA", "ocr_raw")

    proposed: str | None = None
    if winner is not None:
        if field == "of":
            proposed = str(winner.get("_of") or winner.get("of") or "").strip()
        elif field == "ov":
            proposed = str(winner.get("ov") or "").strip()
        elif field == "modelo":
            des = " ".join(str(winner.get("designacao") or "").split())
            if not des:
                proposed = ocr_value or None
            elif template_name == "acabamento":
                proposed = _model_first_token(des) or des
            else:
                proposed = des
        elif field == "cliente":
            proposed = str(winner.get("cliente") or "").strip()
        elif field in ("comp_mm", "larg_mm", "lbase", "ltopo", "esp", "dbase", "dtopo"):
            plan_attr = {
                "comp_mm": "comp", "larg_mm": "larg", "lbase": "lbase",
                "ltopo": "ltopo", "esp": "esp",
                # R128 — kanban LASER
                "dbase": "dbase", "dtopo": "dtopo",
            }[field]
            v = winner.get(plan_attr)
            if v is not None and v != "":
                proposed = str(v)
    elif ocr_value:
        proposed = _local_candidate_proposal(field, ocr_value, candidates, row, refs)

    if not proposed:
        if field == "cliente" and ocr_value:
            for cand in candidates or []:
                proposed_cliente = str(cand.get("value") or "").strip()
                if not cand.get("plan_entries") or not proposed_cliente:
                    continue
                if _cliente_values_match(ocr_value, proposed_cliente, refs):
                    return _make_cell(
                        _format_value(field, ocr_value),
                        "confirmed",
                        "ocr_raw",
                        proposed=_format_value(field, proposed_cliente),
                        ref_source="plan",
                        score=None,
                    )
        if winner is None and ocr_value and has_field_reference:
            return _make_cell(
                ocr_value, "very_different", "ocr_raw",
                ref_source=_field_ref_source(field, refs, row),
            )
        return _make_cell(ocr_value, "NA", "ocr_raw")

    score = winner.get("_score") if winner else None

    # R218 — guarda de ambiguidade: se o winner não é líder claro e os rivais
    # quase-empatados DISCORDAM neste campo, só substituímos se o OCR confirmar
    # o winner. Se o OCR não confirma (vazio ou diferente), não inventar a
    # partir de um winner arbitrário — fica para revisão.
    if (
        winner is not None
        and proposed
        and _winner_ambiguous_for_field(field, proposed, winner, template_name)
        and (_entry_field_similarity(field, winner, row, refs) or 0.0) < 1.0
    ):
        proposed_fmt = _format_value(field, proposed)
        ocr_fmt = _format_value(field, ocr_value)
        if not ocr_fmt:
            # Campo vazio + ambíguo: não inventar — NA com a ref no tooltip.
            return _make_cell(
                "", "NA", "ocr_raw", proposed=proposed_fmt, ref_source="plan",
            )
        # OCR presente mas não confirma o winner: mantém OCR, mostra ref,
        # não auto-aplica (revisão humana).
        return _make_cell(
            ocr_fmt, "very_different", "ocr_raw",
            proposed=proposed_fmt, ref_source="plan",
            auto_apply=False, score=score,
        )

    if field == "cliente" and ocr_value:
        if winner is not None:
            return _finish_cell(
                field, ocr_value, proposed, "plan", score
            )
        if _cliente_values_match(ocr_value, proposed, refs):
            return _make_cell(
                _format_value(field, ocr_value),
                "confirmed",
                "ocr_raw",
                proposed=_format_value(field, proposed),
                ref_source="plan",
                score=score,
            )
        return _finish_cell(
            field, ocr_value, proposed, "plan", score
        )

    if field in ("of", "ov") and ocr_value:
        if winner is not None:
            return _finish_cell(
                field, ocr_value, proposed, "plan", score
            )
        if _identifier_values_match(field, ocr_value, proposed):
            return _make_cell(
                _format_value(field, ocr_value),
                "confirmed",
                "ocr_raw",
                proposed=_format_value(field, proposed),
                ref_source="plan",
                score=score,
            )
        return _finish_cell(
            field, ocr_value, proposed, "plan", score
        )

    # Acabamento TPL086: com winner global também mostra/aplica a referência
    # da melhor linha; se estiver distante, fica vermelho para revisão.
    if template_name == "acabamento" and field in ("of", "modelo") and ocr_value:
        return _finish_cell(
            field, ocr_value, proposed, "plan", score
        )

    proposed_fmt = _format_value(field, proposed)
    ocr_fmt = _format_value(field, ocr_value)
    if proposed_fmt and ocr_fmt and proposed_fmt.upper() == ocr_fmt.upper():
        return _make_cell(proposed_fmt, "confirmed", source="plan", score=score)

    return _finish_cell(
        field, ocr_value, proposed,
        source="plan",
        score=score,
    )


# Scoring de uma linha completa ----------------------------------------------

def _score_row(
    row_idx: int,
    row: dict,
    refs: dict,
    idx: dict,
    row_fields: tuple[str, ...],
    cross_check_fields: tuple[str, ...],
    current_phase: str | None = None,
    template_name: str | None = None,
) -> tuple[dict, int, int, int, int, int]:
    """R123 / R125 — itera os `row_fields` do template (não os 10 fixos
    do bobine).

    Cada campo cai num de três tratamentos:
      - campo com referência no plan/SAP (_ROW_FIELDS) → winner/candidatos;
      - campo sem referência (_NO_REF_FIELDS: pri, qtd, ...) → regra local;
      - campo próprio do template sem referência (cesta_n, m2, sobras, ...) →
        regra local, para não deixar valores preenchidos neutros.

    R125: quando `current_phase` é passado, desempata o winner entre
    linhas ainda com espaço nessa fase e, se TODAS as linhas candidatas
    estiverem fechadas nessa fase, marca a linha com `obra_concluida=True`.
    R163: isto é apenas aviso/metadata; não altera os estados das células.
    """
    cc_fields = set(cross_check_fields)

    # Candidatos para todos os campos declarados no template como validáveis.
    # OF/OV são apenas campos normais no score global; não filtram o winner.
    candidates_by_field: dict[str, list[dict]] = {}
    for field in _ROW_FIELDS:
        if field in cc_fields:
            candidates_by_field[field] = _candidates_for_field(field, row, refs, idx)

    score_fields = cc_fields & set(_PLAN_FIELDS)
    winner = _find_winner_entry(
        candidates_by_field,
        row,
        refs,
        idx,
        current_phase,
        score_fields,
    )

    obra_concluida = _all_eligible_phase_full(
        candidates_by_field, row, refs, current_phase, winner
    )

    fields_out: dict[str, dict] = {}
    snapped = confirmed = na = very_diff = 0

    def _tally(st: str) -> None:
        nonlocal snapped, confirmed, na, very_diff
        if st == "snapped":
            snapped += 1
        elif st == "confirmed":
            confirmed += 1
        elif st == "very_different":
            very_diff += 1
        else:
            na += 1

    for field in row_fields:
        ocr_value = str(row.get(field) or "").strip()
        if field == "coni":
            result = _score_ferramenta_cell(ocr_value)
        elif field in _ROW_FIELDS and field in cc_fields:
            result = _apply_winner_to_field(
                field, ocr_value, winner,
                candidates_by_field.get(field, []), refs, row,
                _has_field_reference_pool(field, refs, idx, row),
                template_name=template_name,
            )
        elif field in _NO_REF_FIELDS:
            result = _score_no_ref_row_cell(field, ocr_value)
        else:
            # Campo próprio do template, sem referência no plan/SAP: aplica
            # regra local para evitar NA neutro em valor preenchido.
            result = _score_no_ref_row_cell(field, ocr_value)
        fields_out[field] = result
        _tally(result["status"])

    # Campos extra que o OCR leu fora do schema do template — aplicar regra
    # local se houver valor, senão NA.
    for k, v in row.items():
        if k in fields_out:
            continue
        extra_value = str(v) if v is not None else ""
        if k in _NO_REF_FIELDS:
            extra_cell = _score_no_ref_row_cell(k, extra_value.strip())
        else:
            extra_cell = _score_no_ref_row_cell(k, extra_value.strip())
        fields_out[k] = extra_cell
        _tally(extra_cell["status"])

    total = snapped + confirmed + na + very_diff
    row_out = {
        "row_index": row_idx,
        "fields": fields_out,
        "winner_of": (winner or {}).get("_of") if winner else None,
        "winner_score": (winner or {}).get("_score") if winner else None,
        "identity_conflict": False,
        "obra_concluida": obra_concluida,
    }
    return row_out, snapped, confirmed, na, very_diff, total


# Cabeçalho / rodapé ---------------------------------------------------------

def _norm_name(s: object) -> str:
    """Upper + sem acentos — para comparar nomes de operador com a
    ListaColaboradores (cujos snames são UPPERCASE ASCII sem acentos)."""
    d = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in d if not unicodedata.combining(c)).strip().upper()


def _looks_like_date(s: str) -> bool:
    """Accept common PT/ISO date shapes while rejecting impossible dates."""
    value = (s or "").strip()
    if not value:
        return False
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            datetime.strptime(value, fmt)
            return True
        except ValueError:
            continue
    return False


def _operator_code_variants(value: object) -> set[int]:
    """Return comparable short/pernr operator codes from a numeric value."""
    s = str(value or "").strip()
    if not s or not s.isdigit():
        return set()
    stripped = s.lstrip("0") or "0"
    try:
        out = {int(stripped)}
        if len(stripped) >= 8 and stripped.startswith("1000"):
            out.add(int(stripped[-4:].lstrip("0") or "0"))
        return out
    except ValueError:
        return set()


def _operator_display_code(raw_cod: object, entry: dict | None) -> str:
    """Return the short operator code users expect to see."""
    variants = _operator_code_variants(raw_cod)
    if isinstance(entry, dict):
        variants |= _operator_code_variants(entry.get("pernr"))
    short = sorted(v for v in variants if 0 < v < 10000)
    if short:
        return str(short[0])
    raw = str(raw_cod or "").strip()
    if raw:
        return raw
    if isinstance(entry, dict):
        return str(entry.get("pernr") or "").strip()
    return ""


def _operator_entry_pernr(raw_cod: object, entry: dict | None) -> str:
    """Return the canonical full PERNR stored in ListaColaboradores."""
    if isinstance(entry, dict):
        pernr = str(entry.get("pernr") or "").strip()
        if pernr:
            return pernr
    raw = str(raw_cod or "").strip()
    if raw.isdigit() and len(raw.lstrip("0") or "0") >= 8:
        return raw
    return ""


def _looks_like_non_negative_integer(s: str) -> bool:
    n = _num(s)
    return n is not None and n >= 0 and abs(n - round(n)) <= 1e-9


def _looks_like_short_digits(s: str) -> bool:
    value = str(s or "").strip()
    return value.isdigit() and 1 <= len(value) <= 4


def _looks_like_operator_short_code(s: str) -> bool:
    value = str(s or "").strip()
    return value.isdigit() and 1 <= len(value) <= 5


def _looks_like_machine_code(s: str) -> bool:
    value = str(s or "").strip().upper()
    return len(value) == 4 and value.startswith("M") and value[1:].isdigit()


def _looks_like_pri(s: str) -> bool:
    value = str(s or "").strip().upper()
    if not value:
        return False
    if value.isdigit() and len(value) == 6:
        return False
    if len(value) == 1 and value.isalpha():
        return True
    return bool(_PRI_RE.fullmatch(value))


def _looks_like_non_negative_decimal(s: str) -> bool:
    n = _num(s)
    return n is not None and n >= 0


def _looks_like_time_of_day(s: str) -> bool:
    value = str(s or "").strip()
    if not value:
        return False
    if ":" not in value and " " not in value:
        return False
    sep = ":" if ":" in value else " "
    hh, found_sep, mm = value.partition(sep)
    if not (found_sep and hh.isdigit() and mm.isdigit()):
        return False
    hours = int(hh)
    minutes = int(mm)
    return 0 <= hours < 24 and 0 <= minutes < 60


def _looks_like_yes_no_marker(s: str) -> bool:
    value = str(s or "").strip()
    if not value:
        return False
    norm = _norm_name(value)
    return norm in {"SIM", "S", "NAO", "N", "YES", "Y", "NO"} or value in {
        "✓", "✔", "✗",
    }


def _looks_like_hours(s: str) -> bool:
    value = str(s or "").strip()
    if not value:
        return False

    value = value.rstrip("hH").strip()
    if not value:
        return False

    def _within_24h(hours: int, minutes: int = 0) -> bool:
        return (
            0 <= hours < 24 and 0 <= minutes < 60
        ) or (hours == 24 and minutes == 0)

    if ":" in value or " " in value:
        sep = ":" if ":" in value else " "
        hh, found_sep, mm = value.partition(sep)
        if found_sep and hh.isdigit() and mm.isdigit():
            return _within_24h(int(hh), int(mm))
        return False

    # Compact HHMM/HMM accepted by the extraction schema, e.g. 830 or 0830.
    if value.isdigit() and len(value) in (3, 4):
        hours = int(value[:-2])
        minutes = int(value[-2:])
        if _within_24h(hours, minutes):
            return True

    n = _num(value)
    if n is not None:
        return 0 <= n <= 24
    return False


def _ordered_union(*groups: tuple[str, ...] | list[str] | Any) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or ():
            key = str(item)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return tuple(out)


def _score_header_footer(
    header: dict,
    footer: dict,
    refs: dict,
    header_fields: tuple[str, ...] = (),
    footer_fields: tuple[str, ...] = (),
) -> tuple[dict, dict]:
    """R123 (B9) — valida o cabeçalho e o rodapé em vez de os forçar a NA.

    `operador`/`n_operador` cruzam-se com a ListaColaboradores;
    `setor_maquina`/`cod_maquina` com o catálogo de máquinas; `data`, `turno`
    e rodapé recebem validação sintática. Campos preenchidos sem catálogo
    externo passam por regra/sintaxe local para não ficarem neutros.
    """
    colaboradores = refs.get("colaboradores", {}) or {}
    colaborador_entries: list[dict] = []
    colaborador_codes: set[int] = set()
    for raw_cod, entry in colaboradores.items():
        variants = _operator_code_variants(raw_cod)
        if isinstance(entry, dict):
            variants.update(_operator_code_variants(entry.get("pernr")))
            name = str(entry.get("sname") or "").strip()
            pernr = _operator_entry_pernr(raw_cod, entry)
            if name or variants or pernr:
                colaborador_entries.append({
                    "name": name,
                    "name_norm": _norm_name(name),
                    "aliases_norm": {_norm_name(name)} if name else set(),
                    "codes": variants,
                    "pernr": pernr,
                    "display_code": _operator_display_code(raw_cod, entry),
                })
        colaborador_codes.update(variants)
    for alias_raw, alias_info in (refs.get("operador_aliases") or {}).items():
        alias_norm = _norm_name(alias_raw)
        if not alias_norm:
            continue
        info = alias_info if isinstance(alias_info, dict) else {}
        alias_codes = _operator_code_variants(info.get("cod"))
        alias_codes.update(_operator_code_variants(info.get("pernr")))
        alias_pernr = str(info.get("pernr") or "").strip()
        alias_name = str(info.get("sname") or "").strip()
        alias_name_norm = _norm_name(alias_name)
        target = next(
            (
                e for e in colaborador_entries
                if (
                    (alias_pernr and e.get("pernr") == alias_pernr)
                    or (alias_codes and e.get("codes") and alias_codes & e["codes"])
                    or (alias_name_norm and e.get("name_norm") == alias_name_norm)
                )
            ),
            None,
        )
        if target is None:
            target = {
                "name": alias_name,
                "name_norm": alias_name_norm,
                "aliases_norm": {alias_name_norm} if alias_name_norm else set(),
                "codes": alias_codes,
                "pernr": alias_pernr,
                "display_code": _operator_display_code(info.get("cod"), info),
            }
            colaborador_entries.append(target)
            colaborador_codes.update(alias_codes)
        target.setdefault("aliases_norm", set()).add(alias_norm)

    snames = {
        alias
        for e in colaborador_entries
        for alias in (e.get("aliases_norm") or set())
        if alias
    }
    header_name_norm = _norm_name(header.get("operador"))
    header_code_variants = _operator_code_variants(header.get("n_operador"))
    entry_by_header_code = next(
        (
            e for e in colaborador_entries
            if e["codes"] and header_code_variants & e["codes"]
        ),
        None,
    )
    header_pernr = str(header.get("pernr") or "").strip()
    entry_by_header_pernr = next(
        (
            e for e in colaborador_entries
            if header_pernr and e.get("pernr") == header_pernr
        ),
        None,
    )
    entries_by_header_name = [
        e for e in colaborador_entries
        if header_name_norm and header_name_norm in (e.get("aliases_norm") or set())
    ]
    entry_by_header_name = (
        entries_by_header_name[0]
        if len(entries_by_header_name) == 1 else None
    )
    maquinas_by_cod = refs.get("maquinas_by_codmaq", {}) or {}
    maquinas = {str(m).upper() for m in maquinas_by_cod}
    machine_catalog_available = bool(refs.get("maquinas_by_kanban") or maquinas_by_cod)
    expected_machine = (
        resolve_machine_from_setor(header.get("setor_maquina"), refs)
        if machine_catalog_available else None
    )
    expected_codmaq = (
        str(expected_machine.get("codmaq") or "").strip().upper()
        if isinstance(expected_machine, dict) else ""
    )

    def _cell(field: str, value: object) -> dict:
        v = str(value).strip() if value is not None else ""
        if not v:
            return _make_cell("", "NA", "ocr_raw")
        if field == "operador" and snames:
            expected_entry = entry_by_header_code or entry_by_header_pernr
            if (
                expected_entry
                and expected_entry.get("name")
                and _norm_name(v) not in (expected_entry.get("aliases_norm") or set())
            ):
                return _make_cell(
                    v, "very_different", "ocr_raw",
                    proposed=expected_entry["name"],
                    ref_source="colaboradores",
                )
            st = "confirmed" if _norm_name(v) in snames else "very_different"
            return _make_cell(v, st, "ocr_raw", ref_source="colaboradores")
        elif field == "operador":
            return _make_cell(v, "confirmed", "syntax")
        elif field == "n_operador" and colaborador_codes:
            variants = _operator_code_variants(v)
            expected_entry = entry_by_header_name or entry_by_header_pernr
            if (
                expected_entry
                and expected_entry.get("codes")
                and not (variants & expected_entry["codes"])
            ):
                return _make_cell(
                    v, "very_different", "ocr_raw",
                    proposed=expected_entry.get("display_code") or "",
                    ref_source="colaboradores",
                )
            st = "confirmed" if variants & colaborador_codes else "very_different"
            return _make_cell(v, st, "ocr_raw", ref_source="colaboradores")
        elif field == "n_operador":
            if _looks_like_operator_short_code(v):
                return _make_cell(v, "confirmed", "syntax")
            return _make_cell(v, "very_different", "syntax")
        elif field == "pernr" and colaborador_entries:
            expected_entry = entry_by_header_code or entry_by_header_name
            if (
                expected_entry
                and expected_entry.get("pernr")
                and v != expected_entry["pernr"]
            ):
                return _make_cell(
                    v, "very_different", "ocr_raw",
                    proposed=expected_entry["pernr"],
                    ref_source="colaboradores",
                )
            st = (
                "confirmed"
                if any(v == e.get("pernr") for e in colaborador_entries)
                else "very_different"
            )
            return _make_cell(v, st, "ocr_raw", ref_source="colaboradores")
        elif field == "pernr":
            st = "confirmed" if v.isdigit() else "very_different"
            return _make_cell(v, st, "syntax")
        elif field == "setor_maquina" and machine_catalog_available:
            st = "confirmed" if resolve_machine_from_setor(v, refs) else "very_different"
            return _make_cell(v, st, "ocr_raw", ref_source="maquinas")
        elif field == "setor_maquina":
            return _make_cell(v, "confirmed", "syntax")
        elif field == "cod_maquina" and expected_codmaq:
            if v.upper() == expected_codmaq:
                return _make_cell(v, "confirmed", "ocr_raw", ref_source="maquinas")
            return _make_cell(
                v, "very_different", "ocr_raw",
                proposed=expected_codmaq, ref_source="maquinas",
            )
        elif field == "cod_maquina" and maquinas:
            st = "confirmed" if v.upper() in maquinas else "very_different"
            return _make_cell(v, st, "ocr_raw", ref_source="maquinas")
        elif field == "cod_maquina":
            if _looks_like_machine_code(v):
                return _make_cell(v, "confirmed", "syntax")
            return _make_cell(v, "very_different", "syntax")
        elif field == "data":
            st = "confirmed" if _looks_like_date(v) else "very_different"
            return _make_cell(v, st, "syntax")
        elif field == "turno":
            st = "confirmed" if v.upper() in {"M", "R", "XM", "T"} else "very_different"
            return _make_cell(v, st, "syntax")
        elif field == "colunas_produzidas":
            st = "confirmed" if _looks_like_non_negative_integer(v) else "very_different"
            return _make_cell(v, st, "syntax")
        elif field == "horas_trabalhadas":
            st = "confirmed" if _looks_like_hours(v) else "very_different"
            return _make_cell(v, st, "syntax")
        else:
            return _make_cell(v, "confirmed", "syntax")

    header_keys = _ordered_union(header_fields, header.keys())
    footer_keys = _ordered_union(footer_fields, footer.keys())
    header_out = {k: _cell(k, header.get(k, "")) for k in header_keys}
    footer_out = {k: _cell(k, footer.get(k, "")) for k in footer_keys}
    return header_out, footer_out


# Entry point ----------------------------------------------------------------

def shadow_score(
    sheet_data: dict,
    dq_audit: dict | None,
    refs: dict,
) -> tuple[dict, int, int, int, int, int]:
    """Retorna (scoring, total, snapped, confirmed, na, duration_ms).

    `very_different` fica contabilizado em ``scoring["summary"]``. O
    contador top-level `snapped` é apenas correção suave/autofill.
    """
    started = time.perf_counter()
    idx = _get_indices(refs)

    rows = sheet_data.get("rows") or []
    header = sheet_data.get("header") or {}
    footer = sheet_data.get("footer") or {}
    template_name = sheet_data.get("template_name", "bobine_formato")
    # R123 — o motor itera os row_fields do template da folha, não os 10
    # campos fixos do bobine. Folhas não-bobine deixam de ter os campos
    # próprios (cesta_n, qtd_metros, m2, ...) visíveis mas NA no cross-check.
    from app.templates_registry import get_template
    template = get_template(template_name)
    row_fields = template.row_fields
    cross_check_fields = template.cross_check_fields
    canonical_template_name = template.name
    # R125 — fase em curso (bf/c/q/...) derivada do setor da máquina;
    # usada para desempatar o winner e detectar obra concluída.
    current_phase = _current_phase(sheet_data, refs)

    out_rows = []
    snapped = confirmed = na = very_diff = 0
    for i, row in enumerate(rows):
        row_out, s, c, n, vd, _t = _score_row(
            i, row, refs, idx, row_fields, cross_check_fields,
            current_phase, canonical_template_name
        )
        out_rows.append(row_out)
        snapped += s
        confirmed += c
        na += n
        very_diff += vd

    # R123 (B9) — header/footer validados (operador vs ListaColaboradores,
    # máquina, etc.) em vez de forçados a NA; os estados contam como
    # qualquer outra célula.
    header_out, footer_out = _score_header_footer(
        header, footer, refs,
        header_fields=getattr(template, "header_fields", ()),
        footer_fields=getattr(template, "footer_fields", ()),
    )
    for cell in (*header_out.values(), *footer_out.values()):
        st = cell["status"]
        if st == "confirmed":
            confirmed += 1
        elif st == "snapped":
            snapped += 1
        elif st == "very_different":
            very_diff += 1
        else:
            na += 1

    total = snapped + confirmed + na + very_diff
    duration_ms = int((time.perf_counter() - started) * 1000)

    scoring = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": ENGINE_VERSION,
        "template_name": canonical_template_name,
        "summary": {
            "confirmed": confirmed,
            "snapped": snapped,
            "very_different": very_diff,
            "na": na,
            "total": total,
        },
        "status_labels": _STATUS_LABELS,
        "rows": out_rows,
        "header": header_out,
        "footer": footer_out,
        "duration_ms": duration_ms,
    }
    return scoring, total, snapped, confirmed, na, duration_ms


# R109 — Wrapper compat com UI legacy (MATCH/NO_MATCH/NA) -------------------

CROSS_CHECK_STATUSES = ("MATCH", "NO_MATCH", "NA")

# Mapping interno v5 → legacy
_V5_TO_LEGACY = {
    "confirmed": "MATCH",
    "snapped": "MATCH",            # snap suave + autofill = verde
    "very_different": "NO_MATCH",  # vermelho — operador revê
    "NA": "NA",
}


def _to_legacy_cell(v5_cell: dict, ref_value: str | None = None) -> dict:
    """Converte célula do shadow output para shape esperado pela UI.

    R124: expõe também `source` ("plan", "sap", "lexicon", "ocr_raw") para
    a UI/audit trail distinguir propostas vindas de refs do fallback OCR.

    Propaga `score`, `auto_apply` (R218 — guarda de ambiguidade) e usa
    `proposed` como `ref` para tooltip de referência.
    """
    v5_status = v5_cell.get("status", "NA")
    legacy_status = _V5_TO_LEGACY.get(v5_status, "NA")
    out = {
        "value": v5_cell.get("value", ""),
        "status": legacy_status,
        "label": v5_cell.get("label", ""),
        "snapped": v5_status == "snapped",
        "engine_status": v5_status,
        "source": v5_cell.get("source"),
        "ref_source": v5_cell.get("ref_source") or v5_cell.get("source"),
        "score": v5_cell.get("score"),
    }
    # R218 — célula ambígua (rivais discordam): mostra ref mas não auto-aplica.
    if v5_cell.get("auto_apply") is False:
        out["auto_apply"] = False
    # Ref para tooltip de referência: prioriza `proposed`,
    # depois `ref_value` legado, depois `value`.
    if "proposed" in v5_cell:
        out["ref"] = v5_cell["proposed"]
    elif ref_value is not None:
        out["ref"] = ref_value
    elif (
        v5_status in ("snapped", "very_different")
        and v5_cell.get("source") not in (None, "ocr_raw", "obra_concluida", "syntax")
    ):
        out["ref"] = v5_cell.get("value", "")
    return out


def cross_check_sheet(
    sheet_data: dict,
    dq_audit: dict | None,
    refs: dict,
) -> dict:
    """R109 — Entry point oficial. Wraps shadow_score, devolve output no
    formato legacy esperado pela UI (status MATCH/NO_MATCH/NA, summary,
    rows, header, footer, to_analisar).
    """
    scoring, _total, _snapped, _confirmed, _na, duration_ms = shadow_score(
        sheet_data, dq_audit, refs,
    )

    # Reconstruir rows no shape legacy
    legacy_rows = []
    summary = {"match": 0, "no_match": 0, "na": 0, "total": 0}
    to_analisar: list[dict] = []

    def _review_item(
        section: str,
        field: str,
        legacy_cell: dict,
        row_index: int | None = None,
        raw_value: str | None = None,
    ) -> dict:
        field_path = (
            f"rows[{row_index}].{field}" if section == "rows"
            else f"{section}.{field}"
        )
        ref_source = legacy_cell.get("ref_source") or legacy_cell.get("source") or ""
        has_ref = bool(legacy_cell.get("ref"))
        if ref_source == "ferramenta":
            reason = "Valor não permitido no vocabulário de ferramenta/CONI"
        elif has_ref:
            reason = "Motor propõe valor muito diferente do OCR"
        elif ref_source == "syntax":
            reason = "Valor inválido para o formato esperado"
        elif ref_source == "colaboradores":
            reason = "Valor não encontrado na ListaColaboradores"
        elif ref_source == "maquinas":
            reason = "Valor não encontrado no catálogo de máquinas"
        elif ref_source == "sap":
            reason = "Valor não encontrado no SAP"
        elif ref_source == "plan":
            reason = "Valor não encontrado no plan"
        else:
            reason = "Valor não reconhecido pelo validador do campo"
        return {
            "section": section,
            "row_index": row_index,
            "field": field,
            "field_path": field_path,
            "value": legacy_cell.get("value", "") if raw_value is None else raw_value,
            "ref": legacy_cell.get("ref", ""),
            "ref_source": ref_source,
            "reason": reason,
        }

    for r in scoring.get("rows", []):
        legacy_fields: dict[str, dict] = {}
        row_summary = {"match": 0, "no_match": 0, "na": 0}
        for field, cell in r.get("fields", {}).items():
            # Ref é tratado dentro de _to_legacy_cell (prioriza `proposed`
            # do motor, depois value para snapped/very_different).
            legacy_cell = _to_legacy_cell(cell)
            legacy_fields[field] = legacy_cell
            st = legacy_cell["status"]
            if st == "MATCH":
                row_summary["match"] += 1
                summary["match"] += 1
            elif st == "NO_MATCH":
                row_summary["no_match"] += 1
                summary["no_match"] += 1
                # Adicionar ao to_analisar
                to_analisar.append(
                    _review_item(
                        "rows",
                        field,
                        legacy_cell,
                        row_index=r.get("row_index", 0),
                        raw_value=(
                            sheet_data.get("rows", [{}])[r.get("row_index", 0)].get(field, "")
                            if r.get("row_index", 0) < len(sheet_data.get("rows", [])) else ""
                        ),
                    )
                )
            else:
                row_summary["na"] += 1
                summary["na"] += 1
            summary["total"] += 1

        legacy_rows.append({
            "row_index": r.get("row_index", 0),
            "fields": legacy_fields,
            "summary": row_summary,
            "winner_of": r.get("winner_of"),
            "winner_score": r.get("winner_score"),
            "identity_conflict": r.get("identity_conflict", False),
            # R125 — bandeira propagada para UI / auto-overwrites
            "obra_concluida": r.get("obra_concluida", False),
        })

    legacy_header = {k: _to_legacy_cell(v) for k, v in scoring.get("header", {}).items()}
    legacy_footer = {k: _to_legacy_cell(v) for k, v in scoring.get("footer", {}).items()}
    for field, v in legacy_header.items():
        st = v["status"].lower() if v["status"] != "NO_MATCH" else "no_match"
        summary[st] += 1
        summary["total"] += 1
        if v["status"] == "NO_MATCH":
            to_analisar.append(_review_item("header", field, v))
    for field, v in legacy_footer.items():
        st = v["status"].lower() if v["status"] != "NO_MATCH" else "no_match"
        summary[st] += 1
        summary["total"] += 1
        if v["status"] == "NO_MATCH":
            to_analisar.append(_review_item("footer", field, v))

    return {
        "checked_at": scoring.get("checked_at"),
        "engine_version": scoring.get("engine_version"),
        "template_name": scoring.get("template_name"),
        "summary": summary,
        "rows": legacy_rows,
        "header": legacy_header,
        "footer": legacy_footer,
        "to_analisar": to_analisar,
        "refs_loaded_at": refs.get("loaded_at"),
        "duration_ms": duration_ms,
    }


__all__ = ["shadow_score", "cross_check_sheet", "CROSS_CHECK_STATUSES",
           "score_entry", "normalize_of", "ENGINE_VERSION"]
