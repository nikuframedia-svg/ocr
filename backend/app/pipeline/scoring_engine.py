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

import math
import time
import unicodedata
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from functools import lru_cache
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

_CLIENTE_OCR_TOKEN_ALIASES = {
    "STACK": "ESTOQUE",
    "STAEK": "ESTOQUE",
    "STACA": "ESTOQUE",
    "STOCK": "ESTOQUE",
    "STOQUE": "ESTOQUE",
}


@lru_cache(maxsize=200_000)
def _norm_ascii_upper_cached(s: str) -> str:
    d = unicodedata.normalize("NFKD", s)
    return "".join(c for c in d if not unicodedata.combining(c)).strip().upper()


def _norm_ascii_upper(value: object) -> str:
    # R225 — cache por valor distinto: as ~22k chaves do plano são normalizadas
    # em CADA linha; cachear (função pura) colapsa para 1x. Output idêntico.
    return _norm_ascii_upper_cached(str(value or ""))


@lru_cache(maxsize=200_000)
def _cliente_tokens_cached(s: str) -> tuple[str, ...]:
    norm = _norm_ascii_upper(s)
    cleaned = re.sub(r"[^A-Z0-9]+", " ", norm)
    return tuple(
        _CLIENTE_OCR_TOKEN_ALIASES.get(tok, tok)
        for tok in cleaned.split()
        if tok and tok not in _CLIENTE_STOPWORDS
    )


def _cliente_tokens(value: object) -> tuple[str, ...]:
    return _cliente_tokens_cached(str(value or ""))


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


def _value_has_letters(value: object) -> bool:
    """R231 — True se o valor tem letras. Uma OF do plano é SEMPRE numérica, por
    isso um valor com letras na coluna OF é, quase de certeza, um código de
    modelo que o OCR pôs na coluna errada."""
    return any(c.isalpha() for c in str(value or ""))


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
    # rev00 — SUCATA (nº peças sucatadas): informativo, sem ref no plano/SAP.
    "sucata",
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

_FORCED_WINNER_MIN_SIM = {
    "cliente": 50.0,
    "modelo": 50.0,
    "of": 70.0,
    "ov": 70.0,
    "comp_mm": 80.0,
    "larg_mm": 80.0,
    "lbase": 80.0,
    "ltopo": 80.0,
    "esp": 80.0,
    "dbase": 80.0,
    "dtopo": 80.0,
}
_MIN_FORCED_WINNER_SCORE = 0.01
_MIN_FORCED_TOP1_SCORE = -999.0
# R223 — votação holística: um campo "concorda" quando a similaridade >= isto.
# O winner passa a ser quem concorda em MAIS campos (todos com peso igual), e
# não quem soma mais peso — para nenhum campo (ex.: um modelo exato) mandar
# sozinho e arrastar para a encomenda errada.
_AGREE_THRESHOLD = 0.55
# R223 — para campos NUMÉRICOS/dimensão exige-se quase-exato para "concordar":
# um OF/cliente mal lido é um misread (conta em fuzzy), mas uma medida 0,4 ao
# lado é mesmo outra medida — não pode contar como concordância só pela cauda
# do decay numérico (senão peças com a mesma medida viram rivais falsas).
_AGREE_NUM_THRESHOLD = 0.9
# R223 — campos de IDENTIDADE (identificam a peça): of/ov/cliente/modelo.
_IDENTITY_FIELDS = frozenset({"of", "ov", "cliente", "modelo"})

_FIELD_SCORE_WEIGHTS = {
    "of": 1.00,
    "modelo": 0.90,
    "ov": 0.80,
    "cliente": 0.70,
    "comp_mm": 0.40,
    "larg_mm": 0.40,
    "lbase": 0.40,
    "ltopo": 0.40,
    "esp": 0.40,
    "dbase": 0.40,
    "dtopo": 0.40,
}
# NOTA R236: `_FIELD_SCORE_WEIGHTS` NÃO decide o winner (nunca decidiu — o
# `order` único da chave de ordenação resolvia o empate antes do weighted;
# provado pelo A/B equal_weights = 0.0pp). Fica para telemetria
# (`score_reasons`) e para a afinidade do fallback D4
# (`_winner_field_fallback_proposal`). O RANKING é o score FS em bits abaixo.

# R236 — ranking Fellegi-Sunter: score em BITS = Σ log2(m/u).
#   m = P(campo concorda | linha certa) — MEDIDO no app.db da fábrica
#       (1.147 folhas validadas, 3.2-3.4k linhas por campo; ver plano R236).
#   u = P(campo concorda | linha errada) ≈ freq(valor)/N no plano carregado —
#       calculado por VALOR em `_get_indices` (a blanket order OV vale pouco;
#       uma designação rara vale muito). Nada é hardcoded por cliente/modelo.
_FS_M = {"of": 0.693, "ov": 0.460, "cliente": 0.562, "modelo": 0.524}
# log2((1-m)/(1-u)) — discordar de identidade custa POUCO (misreads/shifts
# são ~30-50% dos campos escritos); é a assimetria medida, não intuída.
_FS_W_DISAGREE = {"of": -1.7, "ov": -0.9, "cliente": -1.2, "modelo": -1.1}
_FS_W_MIN, _FS_W_CAP = 1.0, 14.0
# Prior de RARIDADE: u = freq/max(N, isto) — para identidade E para o
# denominador do peso conjunto das dims. Com um plano pequeno (refs parciais
# no arranque, fixtures de teste) não há base para declarar um valor "comum"
# — sem o prior, u≈0.5, uma OF exata valeria ~1 bit e as margens colapsavam,
# deixando os vetos absolutos dominarem. No plano real (N≈21k) é inerte; a
# calibração que conta é a do backtest contra dados reais.
_FS_U_MIN_CORPUS = 1000
# Dims: m medido 0.87-0.96 → discordar é veto forte (log2((1-m)/(1-u)) ∈
# [-3.9,-2.6]); centro conservador, com cap para linhas multi-dim.
_FS_DIM_DISAGREE = -2.5
_FS_DIM_DISAGREE_CAP = -5.0
_FS_DIM_JOINT_CAP = 13.0
# Contradizer uma OF escrita QUE EXISTE no plano: m(of|válida)=0.890 medido
# → log2(1-0.890) = -3.3. É isto que impede dims comuns de atropelarem uma
# OF bem lida (backtest R236: GOOD 110/110 vs 102/110 do R231).
_FS_VETO_VALID_OF = -3.3
# Margem (bits) entre o winner e o melhor rival com OF DIFERENTE que separa
# "decisivo" de "marginal". Backtest: falhas p50≈0.3-1.5 bits; acertos
# p50≈5.5-17. Sub-linhas da mesma OF não contam como rivais.
_FS_MARGIN_DECISIVE = 4.0
# Rivais da guarda de ambiguidade R219 (por campo): entries a <= isto do topo.
_FS_RIVAL_MARGIN_BITS = 1.0
# Dims que pontuam o winner via plano (larg_mm NÃO tem coluna no plano —
# valida-se só por SAP-lote; incluí-la daria offset constante, nunca sinal).
_FS_DIM_FIELDS = ("comp_mm", "lbase", "ltopo", "esp", "dbase", "dtopo")
# P(colisão) por dim MEDIDA no plano real (20.839 linhas) — piso do u
# conjunto: u_D = max(n_conjunto/N, Π destes). Sem o piso, um plano pequeno
# (fixtures/refs parciais) daria ~10 bits a UMA dim; na realidade uma dim
# vale ~2.5-3.1 bits. Estatística de corpus, como _FS_M — não é regra à mão.
_FS_DIM_U_FLOOR = {
    "comp_mm": 0.116, "lbase": 0.114, "ltopo": 0.154,
    "esp": 0.171, "dbase": 0.164, "dtopo": 0.182,
}

# R247 — match do código-peça embebido após strip do sufixo A/B: quase-pleno
# (0.97·w ≫ fuzzy do token-família ≈0.67·w), mas <1.0 de propósito — se um
# irmão bater 1.0 PLENO (o sufixo era código, não decoração), ganha ele; e a
# guarda de irmãos R248 (gate sim<1.0) continua armada.
_MODEL_EMBEDDED_STRIPPED_SIM = 0.97
# R248 — margem (bits) abaixo da qual um IRMÃO da mesma OF com designação
# diferente torna a célula modelo ambígua. 2.0 cobre o "dígito de sorte" no
# token-família (Δsim≈0.11 × w_cap 14 ≈ 1.5 bits) e alinha com o cap dos
# bias de contexto R242 (contexto/sorte nunca vencem evidência real).
_FS_SIBLING_AMBIG_BITS = 2.0

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

# R222 — limiar SÓ para a COR vermelha (_is_very_different), restaurado aos
# valores de 30/05 (v9_R134). Separado de `_VERY_DIFF_NUM_ABS` de propósito:
# aquele rege a SELEÇÃO de winner (score_entry, _entry_field_similarity,
# candidatos, ambiguidade) e fica apertado; este só decide quando pintar
# vermelho, e volta a ser tolerante como em maio (menos falso-vermelho).
_COLOR_NUM_ABS = {                 # 30/05 — diferença absoluta > X = vermelho
    "comp_mm": 200.0,
    "larg_mm": 50.0,
    "lbase": 30.0,
    "ltopo": 30.0,
    "esp": 0.5,
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
# R163 — obra_concluida deixou de pintar a linha inteira como NO_MATCH (fase
# cheia = aviso/metadata). REVERTIDO em R222: volta a forçar very_different /
# source="obra_concluida" em toda a linha (ver _score_row + _all_eligible_phase_full).
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
# vale o dobro de um "parecido"). A deteção de ambiguidade (rivais
# quase-empatados que discordam num campo) mantém-se.
# R219 — substituir SEMPRE: em ambiguidade o sistema deixa de RETER (NA/keep-OCR
# do R218) e passa a SUBSTITUIR pelo valor da linha vencedora, marcando
# `very_different` (vermelho/rever) para o operador conferir. Removida a flag
# `auto_apply` (já não há retenção). A ambiguidade só afeta a cor, não o valor.
# BUMP obrigatório: força regeneração dos cross-check JSON antigos.
# R220 — winner forçado por candidatos reais: quando não há match forte mas
# há candidato concreto vindo de cliente/modelo/OF/OV/dimensões, escolhe-se a
# melhor linha candidata e deixa-se o cross preencher a linha toda.
# R221 — melhor linha plausível: campos errados/vazios deixam de bloquear.
# Linha não vazia com refs escolhe sempre strong/forced/forced_top1; valores
# locais vazios com winner viram MATCH_REGRA_VAZIO em vez de NA.
# R222 — alinhar com 30/05: (D7) cor vermelha numérica usa _COLOR_NUM_ABS
# tolerante (scoring fica apertado); (D6) obra_concluida volta a pintar a linha
# inteira de vermelho + bloquear auto-substituição (reverte R163); (D8) modelo
# no Acabamento volta à designação completa; (D4 novo) winner sem canónico para
# um campo busca valor coerente noutra entry (_winner_field_fallback_proposal).
# BUMP obrigatório: força regeneração dos cross-check JSON antigos.
# R223 — seleção de winner reescrita: votação HOLÍSTICA (ganha quem concorda em
# mais campos, todos com peso igual; nenhum campo manda sozinho) + realinhamento
# ref-validado da OF (quando o OCR a põe na coluna OV/PRI) + palpites fracos
# ficam vermelho/rever em vez de verde-confiante. BUMP obrigatório.
# R226 — o winner passa a ser a COMBINAÇÃO (agree) e nenhum campo a 100% manda
# sozinho (exact_id deixa de decidir). Muda decisões → BUMP obrigatório para
# as folhas antigas regenerarem com a correção.
# R231 — código de modelo na coluna OF é encaminhado para o campo modelo
# (realinhamento por conteúdo). Muda decisões nas linhas mal posicionadas → BUMP.
# R236 — winner por EVIDÊNCIA EM BITS (Fellegi-Sunter com parâmetros MEDIDOS:
# m no app.db da fábrica, u por valor no plano carregado) em vez do voto igual
# R223/R226; dims pesam pela COMBINAÇÃO (famílias correlacionadas deixam de
# outvotar identidade); contradizer OF escrita-e-válida custa -3.3 bits;
# margem em bits → decisivo/marginal (marginal substitui — R219 — mas fica
# vermelho); realinhamento de colunas por BUSCA DE HIPÓTESES (o mesmo scoring
# decide; -1.5 bits/campo movido); identificador escrito que existe no plano
# e diverge do winner fica very_different; ferramenta/CONI preserva decimais
# (13,7 já não vira 137); floor numérico nas propostas locais sem winner.
# Backtest (verdade humana): GOOD 110/110 (R231 destruía 8 OFs corretas),
# TOTAL 89.2% vs 86.1%. Muda decisões em massa → BUMP obrigatório.
# R240 — realinhamento por FORMA: OF válida em QUALQUER coluna de identidade
# (cheia/embebida) com custo medido por assinatura; modelo/lote exigem
# corroboração. SHIFT 80.9%→92.2% (cliente→OF 8/8; modelo→OF 1/1).
# R241 — canais de ruído FITTED (lexicons/cross_params.json, com proveniência):
# (C1) matriz de confusão de caracteres (NW sobre 1.556 pares reais; 1↔4,
# 7↔3, M↔H, 8↔9 medidos) refina o tier estrutural PARA CIMA e decide o waiver
# do veto; (C2) canal humano: veto relaxado p/ rivais da mesma família
# (p_same_family=0.096 medido) + decision_reason p/ a UI distinguir misread
# de erro de transcrição. ENG 72.9%→75.7%, TOTAL 90.2%. BUMP obrigatório.
# R242 — contexto como evidência: prior de PRODUÇÃO (P(ativa 14d|verdadeira)
# =71.2% vs 2.2% aleatória; +2.0/-1.77 bits, anti-circular) + COERÊNCIA de
# folha (lift medido: OF adjacente 21×, cliente 7.9×; cap +2; passe 2 nas
# linhas marginais). ENG 80.0%, SHIFT 96.5%, TOTAL 91.8%. BUMP obrigatório.
# R243 — posterior CALIBRADO (_p_top; T e s_ood fitted no harness com OOD
# explícito — quant7: 10.7% das linhas frescas têm a OF fora do plano) +
# decisão de gravação por PERDA ESPERADA (thresholds por classe de campo;
# absorve a flag CROSS_WRITE_GATE_MARGINAL) + fila to_analisar ordenada por
# incerteza×criticidade. Reliability: buckets >=0.6 com gap <=6pp; a banda
# 0.5-0.6 é o ponto cego OOD (conf 0.55, acerto 11%) — nunca grava com o
# gate ON (thresholds >=0.90), fica em revisão; melhoria futura = s_ood
# date-aware. R244: refit automático dos params no learning cycle (pisos +
# deriva limitada + backup). R245: canais de chars POR OPERADOR quando
# fitted. R246: descodificação ativa (re-read discriminativo de crops,
# flag OFF até calibrar com imagens na fábrica). BUMP obrigatório.
# R247 — modelo compara o CÓDIGO-PEÇA embebido na designação (tokens com
# dígito+letra len>=4, cacheados) e limpa as decorações do operador ANTES de
# compactar (prefixo N/Nº/No colado, '(-n)'/'-n' final, fração → match 1.0;
# sufixo A/B isolado → 0.97, pode ser código real). Canal de visão R241
# aplicado ao código-peça (misread comum a 1 char ≈ 0.95, MEDIDO, com
# pré-filtro barato). Motivo: irmãos da MESMA OF (dims idênticas, código a
# 1 dígito — 45,6% das OFs) deixavam o fuzzy do token-família escolher o
# irmão errado pelo ÚLTIMO dígito (742→'TME2'), verde com p_top 0.93-0.99;
# 17 trocas validadas em bloco no app.db. Marcadores de parte A/B↔Nº: a
# correlação medida é RUÍDO — decoração a limpar, nunca evidência.
# R248 — posterior consciente de IRMÃOS: `_sibling_margin_bits` (margem
# para o melhor irmão; a margem OF-level e a calibração R243 ficam
# intocadas) + guarda de célula: modelo escrito que não discrimina o winner
# dos irmãos (sim de um irmão >= winner−0.02 — apanha empates a 1.0:
# família-prefixo, código repetido) → very_different + decision_confidence
# = p_top × _sibling_p(margem irmãos) + decision_reason
# "ambiguous_sibling_designacao". Muda decisões em massa → BUMP obrigatório.
# R249 — colisões de atribuição (passe 3): núcleos escritos DISTINTOS
# (pós-strip A/B) na mesma folha a cair na MESMA designação do mesmo OF →
# o membro NÃO-exato desce para revisão (decision_reason
# "sibling_collision"); valores intocados (R219). Muda cores → BUMP.
ENGINE_VERSION = "v30_R249"

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


@lru_cache(maxsize=500_000)
def _str_sim_cached(t: str, c: str) -> float:
    if t == c:
        return 100.0
    if t in c or c in t:
        return 80.0
    if len(t) > 3 and len(c) > 3 and not (set(t) & set(c)):
        return 0.0
    d = _lev_distance(t, c)
    m = max(len(t), len(c))
    if d >= m:
        return 0.0
    return 100.0 * (1 - d / m)


def _str_sim(target: str, candidate: str) -> float:
    if not target or not candidate:
        return 0.0
    return _str_sim_cached(str(target).upper(), str(candidate).upper())


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


@lru_cache(maxsize=200_000)
def _model_compact_cached(raw: str) -> str:
    raw = re.sub(r"(?i)\bN[º°]\s*(?=\d)", "N", raw)
    norm = _norm_ascii_upper(raw)
    norm = re.sub(r"\bNO\s*(?=\d)", "N", norm)
    return re.sub(r"[^A-Z0-9]+", "", norm)


def _model_compact(value: object) -> str:
    # R225 — cache (as designações do plano repetem-se em todas as linhas).
    return _model_compact_cached(str(value or ""))


def _model_compact_variants(value: object) -> list[str]:
    base = _model_compact(value)
    if not base:
        return []
    return list(set(_o_zero_variants(base)))


@lru_cache(maxsize=200_000)
def _designacao_code_tokens_cached(des: str) -> tuple[str, ...]:
    """R247 — tokens-código embebidos na designação (ex.: '5100TME2 - CC4H1
    5100T743 1/2' → ('5100TME2', 'CC4H1', '5100T743')): alfanuméricos com
    dígito E letra, len>=4. É o código-peça embebido (não o 1º token) que
    discrimina entries IRMÃS da mesma OF; extração por tokens porque o
    formato das designações deriva ao longo do tempo ('TSA20 18M Nº1
    1234TJ02' → '1234TJ02 - … 1234T800 1/2')."""
    norm = re.sub(r"[^A-Z0-9]+", " ", _norm_ascii_upper(des))
    return tuple(dict.fromkeys(
        tok for tok in norm.split()
        if len(tok) >= 4
        and any(c.isdigit() for c in tok)
        and any(c.isalpha() for c in tok)
    ))


@lru_cache(maxsize=200_000)
def _model_code_cores_cached(raw: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """R247 — núcleos do código-peça ESCRITO, sem as decorações do operador,
    removidas ANTES de compactar (coladas no compacto partiam o containment:
    compact('No→1234.T.841(-1) 1/2') = 'NO1234T841112' não é substring de
    '…N21234T84112'). Decorações medidas no app.db (3.385 modelos escritos):
    sufixo A/B 9.8%, fração 5.4%, marcador Nº 3.2%, '(-n)' / '-n' final.

    Devolve (cores_puros, cores_sem_sufixo_AB). O sufixo A/B vai à parte
    porque PODE ser código real (CD03P10B existe no plano) — e a correlação
    A/B↔Nº1/Nº2 medida é ruído, portanto é decoração a limpar, nunca
    evidência de parte. Só variantes NOVAS (≠ compacto simples) são
    devolvidas: o caminho existente já cobre o resto."""
    t = _norm_ascii_upper(raw)
    t = re.sub(r"\(\s*-\s*\d\s*\)", " ", t)            # (-1), (-2)
    t = re.sub(r"(?<=\d)\s*-\s*\d\s*$", " ", t)        # '859-1' no fim
    t = re.sub(r"\b[12]\s*/\s*[1-4]\s*$", " ", t)      # fração final 1/2
    core = re.sub(r"[^A-Z0-9]+", "", t)
    core = re.sub(r"^NO?(?=\d)", "", core)             # prefixo N/Nº/No colado
    plain = _model_compact(raw)
    pure = tuple({core} - {"", plain})
    ab_base = core if core else plain
    ab = ab_base[:-1] if re.search(r"\d[AB]$", ab_base) else ""
    ab_cores = tuple({ab} - {"", plain, core})
    return pure, ab_cores


def _model_core_matches(
    model_value: object, designacao: object, *, strip_ab: bool
) -> bool:
    """R247 — containment dos núcleos limpos do OCR nos mesmos haystacks do
    `_model_compact_matches` (designação compacta + 1º token compacto)."""
    pure, ab = _model_code_cores_cached(str(model_value or ""))
    cores = pure + (ab if strip_ab else ())
    if not cores:
        return False
    des_compact = _model_compact(designacao)
    des_ft_compact = _model_compact(_model_first_token(designacao))
    haystacks = [h for h in (des_compact, des_ft_compact) if len(h) >= 4]
    if not haystacks:
        return False
    return any(
        v in h
        for c in cores
        for v in _o_zero_variants(c)
        if len(v) >= 4
        for h in haystacks
    )


def _model_channel_sim(value: object, designacao: object) -> float:
    """R247 — canal de visão FITTED aplicado ao MODELO: o melhor g do canal
    (matriz de confusão R241, custos medidos: 1↔4 barato, 8↔B caro) entre os
    núcleos escritos e os tokens-código da designação, mapeado para a escala
    de sim do modelo: sim = min(0.95, 0.55 + g). Um misread comum a 1 char
    (~6.2 bits) → ~0.95; sub rara/default (10 bits) → ~0.64; d=2 morre (0.0).
    Mesma semântica L0/cap do canal de of/ov; matriz GLOBAL (sem operador) —
    o resultado depende só de (valor, designação), preservando o memo R225.

    Pré-filtro barato antes do NW (|Δlen|<=2 e prefixo OU sufixo de 2 chars
    partilhado): evita ~12k DPs/linha no pool completo; os pares que passam
    ficam no lru_cache do alinhamento."""
    tokens = _designacao_code_tokens_cached(str(designacao or ""))
    if not tokens:
        return 0.0
    pure, ab = _model_code_cores_cached(str(value or ""))
    cores = set(pure) | set(ab)
    plain = _model_compact(value)
    if len(plain) >= 4:
        cores.add(plain)
    best = 0.0
    for core in cores:
        if len(core) < 4:
            continue
        for tok in tokens:
            if abs(len(tok) - len(core)) > 2:
                continue
            if tok[:2] != core[:2] and tok[-2:] != core[-2:]:
                continue
            cost = _channel_align_cost_bits(tok, core, "")
            g = max(0.0, min(_CHANNEL_G_CAP, 1.0 - cost / _CHANNEL_G_L0))
            if g > best:
                best = g
    if best <= 0.0:
        return 0.0
    return min(0.95, 0.55 + best)


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
        # R247 — núcleo sem decorações inequívocas (prefixo N/Nº colado,
        # '(-n)', fração) contido na designação: match pleno. O sufixo A/B
        # NÃO entra aqui (pode ser código real) — fica no tier 0.97.
        or _model_core_matches(model, des, strip_ab=False)
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


_PLAN_ATTR_BY_FIELD = {
    "comp_mm": "comp",
    "larg_mm": "larg",
    "lbase": "lbase",
    "ltopo": "ltopo",
    "esp": "esp",
    "dbase": "dbase",
    "dtopo": "dtopo",
}


def _entry_attr_for_field(field: str, entry: dict) -> object:
    """R225 — valor da ENTRY que determina a similaridade do campo (chave do
    memo por-linha; o lado do `row` é constante dentro de uma linha)."""
    if field == "of":
        return entry.get("_of") or entry.get("of")
    if field == "ov":
        return entry.get("ov")
    if field == "cliente":
        return entry.get("cliente")
    if field == "modelo":
        return entry.get("designacao")
    return entry.get(_PLAN_ATTR_BY_FIELD.get(field))


def _efs_compute(field: str, entry: dict, row: dict, refs: dict, value: object) -> float | None:
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
        # R247 — núcleo sem o sufixo A/B contido: quase-pleno (0.97·w ≫ fuzzy
        # do token-família ≈0.67·w), mas <1.0 de propósito — um irmão com
        # match PLENO ganha, e a guarda de irmãos (gate sim<1.0) fica armada.
        if _model_core_matches(value, designacao, strip_ab=True):
            return _MODEL_EMBEDDED_STRIPPED_SIM
        model = _model_compact(value)
        if len(model) < 4:
            return None
        candidates = [
            _model_compact(_model_first_token(designacao)),
            _model_compact(designacao),
        ]
        # R247 — tokens-código embebidos como alvos: dá discriminação entre
        # irmãos quando o código escrito tem um misread real (T792 →
        # T742=0.875 vs T743=0.75) em vez do shift +1 pelo token-família
        # (742 → 'TME2' por coincidência do último dígito).
        candidates += [
            _model_compact(t) for t in _designacao_code_tokens_cached(
                str(designacao or ""))
        ]
        candidates = [c for c in candidates if c]
        if not candidates:
            return None
        pure, ab = _model_code_cores_cached(str(value or ""))
        ocr_alts = [model, *pure, *ab]
        best = max(
            _str_sim(a, c) / 100.0 for a in ocr_alts for c in candidates
        )
        # R247 — canal de visão fitted como piso graduado: um misread comum
        # a 1 char do código-peça vale ~0.95 (evidência MEDIDA), não o
        # Levenshtein uniforme.
        return max(best, _model_channel_sim(value, designacao))

    plan_attr = _PLAN_ATTR_BY_FIELD.get(field)
    if plan_attr:
        candidate = entry.get(plan_attr)
        if candidate in (None, ""):
            return None
        tolerance = _VERY_DIFF_NUM_ABS[field]
        return _numeric_similarity(field, value, candidate, tolerance)

    return None


def _entry_field_similarity(
    field: str, entry: dict, row: dict, refs: dict, cache: dict | None = None,
) -> float | None:
    value = row.get(field)
    if _is_missing_ocr(value):
        return None
    if cache is None:
        return _efs_compute(field, entry, row, refs, value)
    # R225 — memo por-linha: a similaridade depende só do valor do campo da ENTRY
    # (o `row`/`refs` são constantes na linha) → calcula 1x por valor distinto.
    # As ~1300 entries do mesmo cliente passam a 1 cálculo, não 1300. Resultado
    # IDÊNTICO ao caminho sem cache (correção-preservante por construção).
    ck = (field, str(_entry_attr_for_field(field, entry)))
    if ck in cache:
        return cache[ck]
    r = _efs_compute(field, entry, row, refs, value)
    cache[ck] = r
    return r


def _entry_global_score(
    entry: dict,
    row: dict,
    refs: dict,
    score_fields: set[str] | frozenset[str] | None = None,
    cache: dict | None = None,
    collect_reasons: bool = True,
) -> tuple[float, int, list[dict], float, int, int, int]:
    total = 0.0
    raw_total = 0.0
    exact = 0
    agree = 0
    agree_id = 0  # R223 — campos de IDENTIDADE a concordar (of/ov/cliente/modelo)
    exact_id = 0  # R223 — of/ov batem EXATO (identificador único = decisivo)
    reasons: list[dict] = []
    fields = score_fields if score_fields is not None else _PLAN_FIELDS
    for field in fields:
        if field not in _PLAN_FIELDS:
            continue
        sim = _entry_field_similarity(field, entry, row, refs, cache=cache)
        if sim is None:
            continue
        clamped = max(0.0, min(1.0, sim))
        raw_total += clamped
        # R223 — contagem holística (peso igual entre campos). Identidade
        # concorda em fuzzy (misreads); dimensão só concorda perto do exato.
        agree_thr = _AGREE_THRESHOLD if field in _IDENTITY_FIELDS else _AGREE_NUM_THRESHOLD
        if sim >= agree_thr:
            agree += 1
            if field in _IDENTITY_FIELDS:
                agree_id += 1
        weight = _FIELD_SCORE_WEIGHTS.get(field, 0.30)
        contribution = clamped * weight
        if sim <= 0.10:
            contribution -= weight * 0.15
        total += contribution
        if sim >= 1.0:
            exact += 1
            # R226 — of/ov exato. JÁ NÃO decide o winner sozinho (isso passou a
            # ser a COMBINAÇÃO — `agree`); serve só de (a) desempate FINAL entre
            # entries com o mesmo agree E raw, e (b) discriminador de ambiguidade
            # (uma OF exata não é "rival" de uma OF fuzzy parecida).
            if field in ("of", "ov"):
                exact_id = 1
        if collect_reasons:
            reasons.append({
                "field": field,
                "sim": round(float(sim), 3),
                "weight": weight,
                "points": round(float(contribution), 3),
            })
    return total, exact, reasons, raw_total, agree, exact_id, agree_id


def _fs_value_weight(field: str, entry: dict, idx: dict) -> float:
    """R236 — peso FS (bits) do VALOR deste campo na entry: log2(m_f/u_f(v)),
    com u_f(v) = freq(valor)/N no plano carregado. Um valor raro pesa muito;
    um valor massificado (blanket order, medida de família) pesa pouco."""
    n = max(int(idx.get("fs_n") or 0), _FS_U_MIN_CORPUS)
    freq = (idx.get("fs_freq") or {}).get(field) or {}
    if field == "of":
        key = str(entry.get("_of") or entry.get("of") or "").strip()
    elif field == "ov":
        key = _identifier_compact(entry.get("ov"))
    elif field == "cliente":
        key = _cliente_compact(entry.get("cliente"))
    else:  # modelo
        key = _model_compact(entry.get("designacao"))
    u = max(freq.get(key, 1), 1) / n
    m = _FS_M[field]
    if u >= m:
        return _FS_W_MIN
    return max(_FS_W_MIN, min(_FS_W_CAP, math.log2(m / u)))


def _fs_row_context(row: dict, idx: dict, score_fields=None,
                    extra_bias: dict | None = None) -> dict:
    """R236 — contexto por-linha do scoring FS (calculado 1x por linha):
    valores de dims presentes, conjuntos de entry-ids dentro da tolerância
    (janelas ±tol via bisect), validade da OF escrita, e memo do peso
    conjunto por subconjunto de dims concordantes."""
    allowed = set(score_fields) if score_fields is not None else None
    dims: dict[str, float] = {}
    sets: dict[str, frozenset[int]] = {}
    dim_sorted = idx.get("fs_dim_sorted") or {}
    for field in _FS_DIM_FIELDS:
        if allowed is not None and field not in allowed:
            continue
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        vals = _num_variants(field, raw)
        if not vals:
            continue
        sorted_vals, ids = dim_sorted.get(field) or ([], [])
        if not sorted_vals:
            continue
        tol = _VERY_DIFF_NUM_ABS[field]
        members: set[int] = set()
        for v in vals:
            lo = bisect_left(sorted_vals, v - tol)
            hi = bisect_right(sorted_vals, v + tol)
            members.update(ids[lo:hi])
        dims[field] = vals[0]
        sets[field] = frozenset(members)
    of_written = str(row.get("of") or "").strip()
    of_written_key = normalize_of(_identifier_compact(of_written, pad_of=True))
    of_valid = bool(
        of_written and of_written_key in (idx.get("of_to_entries") or {})
    )
    # R241/C2 — famílias (cliente + 1º token da designação) das entries da OF
    # ESCRITA: se uma entry rival pertence à mesma família, o erro humano
    # (linha errada do cartão) é mais plausível e o veto relaxa (medido:
    # 9.6% das válidas-erradas são mesma-família → relaxamento pequeno).
    of_written_fams: frozenset[str] = frozenset()
    if of_valid:
        fams: set[str] = set()
        for e in (idx.get("of_to_entries") or {}).get(of_written_key, []) or []:
            cli = _cliente_compact(e.get("cliente"))
            if cli:
                fams.add("C:" + cli)
            ft = _model_compact(_model_first_token(e.get("designacao")))
            if ft:
                fams.add("F:" + ft)
        of_written_fams = frozenset(fams)
    return {
        "dims": dims,
        "sets": sets,
        "of_valid": of_valid,
        "of_written": of_written,
        "of_written_fams": of_written_fams,
        "joint_memo": {},
        # R242 — bias de CONTEXTO (bits) por entry: prior de produção (D1:
        # {"of": {of_key: +bits}, "of_default": bits_inativa}) e coerência de
        # folha (D2: {"coh_of": {of_key: +bits}, "coh_cliente": {compact:
        # +bits}}). None/{} = sem efeito (testes, arranque sem DB).
        "extra_bias": extra_bias or None,
        # Denominador do peso conjunto com o prior de corpus (ver
        # _FS_U_MIN_CORPUS): inerte no plano real, evita margens colapsadas
        # em planos minúsculos.
        "n": max(int(idx.get("fs_n") or 0), _FS_U_MIN_CORPUS),
    }


def _entry_bits_score(
    entry: dict,
    row: dict,
    refs: dict,
    idx: dict,
    ctx: dict,
    score_fields=None,
    cache: dict | None = None,
) -> float:
    """R236 — score Fellegi-Sunter em BITS de uma entry contra a linha.

    Identidade: g(sim)·w_valor com g graduado (OF a 1 dígito ≈ 3 bits — o
    backtest v1→v2 provou que cortar em 0.9 perde os misreads que o fuzzy
    do R231 recuperava). Dims: peso da COMBINAÇÃO (-log2(n_conjunto/N)) —
    as dims do plano andam em famílias, pesos por-campo somados mentem — e
    discordar é veto (m_dim 0.87-0.96). Contradizer uma OF escrita e VÁLIDA
    custa -3.3 bits (m(of|válida)=0.890 medido)."""
    allowed = set(score_fields) if score_fields is not None else None
    bits = 0.0
    for field in ("of", "ov", "cliente", "modelo"):
        if allowed is not None and field not in allowed:
            continue
        sim = _entry_field_similarity(field, entry, row, refs, cache=cache)
        if sim is None:
            continue
        w = _fs_value_weight(field, entry, idx)
        if field in ("of", "ov"):
            gch = 0.0
            if sim >= 1.0:
                bits += w
            elif sim >= 0.9:
                bits += 0.5 * w
            else:
                # R241 — canal de visão FITTED: refina o tier estrutural PARA
                # CIMA, nunca para baixo — o piso 0.3 (sim>=0.8) foi validado
                # pelo backtest R236/R240; a matriz acrescenta discriminação
                # onde tem contagens (1↔4 comum → até 0.45), sem degradar
                # onde não tem. O gch CRU (sem piso) decide o waiver do veto:
                # só um misread genuinamente plausível dispensa o veto.
                token = str(row.get(field) or "")
                value = entry.get("_of") or entry.get("of") if field == "of" else entry.get("ov")
                gch = _channel_g(value, token, pad_of=(field == "of"),
                                 op=(ctx.get("extra_bias") or {}).get("operator") or "")
                g_floor = 0.3 if sim >= 0.8 else 0.0
                g_eff = max(gch, g_floor)
                if g_eff > 0.0:
                    bits += g_eff * w
                elif sim <= 0.3:
                    bits += _FS_W_DISAGREE[field]
            if field == "of" and ctx["of_valid"] and sim < 0.9:
                # Veto por contradizer OF escrita-e-válida — com dois
                # amortecedores medidos (R241): (a) waiver se a entry é um
                # misread PLAUSÍVEL da escrita (o canal explica a diferença);
                # (b) relaxamento mesma-família (erro humano de cartão,
                # 9.6% medido → -2.98 em vez de -3.3).
                if gch < _CHANNEL_VETO_WAIVER_G:
                    veto = _FS_VETO_VALID_OF
                    fams = ctx.get("of_written_fams") or frozenset()
                    if fams:
                        cli = _cliente_compact(entry.get("cliente"))
                        ft = _model_compact(_model_first_token(entry.get("designacao")))
                        if ("C:" + cli in fams and cli) or ("F:" + ft in fams and ft):
                            veto = _fs_veto_relaxed_bits()
                    bits += veto
        else:
            if sim >= 1.0:
                bits += w
            elif sim >= _AGREE_THRESHOLD:
                bits += sim * w
            elif sim <= 0.3:
                bits += _FS_W_DISAGREE[field]

    dim_sets: dict[str, frozenset[int]] = ctx["sets"]
    if dim_sets:
        eid = (idx.get("fs_id_by_key") or {}).get(_entry_key(entry))
        agreeing = tuple(
            f for f in _FS_DIM_FIELDS
            if f in dim_sets and eid is not None and eid in dim_sets[f]
        )
        if agreeing:
            memo = ctx["joint_memo"]
            n_joint = memo.get(agreeing)
            if n_joint is None:
                inter: frozenset[int] | set[int] = dim_sets[agreeing[0]]
                for f in agreeing[1:]:
                    inter = inter & dim_sets[f]
                n_joint = max(len(inter), 1)
                memo[agreeing] = n_joint
            # u conjunto com piso realista: nunca mais raro do que o produto
            # das P(colisão) medidas por dim (ver _FS_DIM_U_FLOOR).
            u_floor = 1.0
            for f in agreeing:
                u_floor *= _FS_DIM_U_FLOOR[f]
            u_joint = max(n_joint / ctx["n"], u_floor)
            bits += max(0.0, min(_FS_DIM_JOINT_CAP, -math.log2(u_joint)))
        n_disagree = sum(
            1 for f in dim_sets
            if eid is None or eid not in dim_sets[f]
        )
        if n_disagree:
            bits += max(_FS_DIM_DISAGREE_CAP, n_disagree * _FS_DIM_DISAGREE)

    # R242 — bias de contexto: prior de produção (D1) + coerência de folha
    # (D2). Sinais MEDIDOS (quant5/6), com caps — quebram empates a favor do
    # contexto, nunca vencem evidência real (w_of exato ≈ 9.4 ≫ caps ±2).
    eb = ctx.get("extra_bias")
    if eb:
        entry_of = str(entry.get("_of") or entry.get("of") or "").strip()
        prod = eb.get("of")
        if prod is not None and entry_of:
            bits += prod.get(entry_of, float(eb.get("of_default") or 0.0))
        coh_of = eb.get("coh_of")
        if coh_of and entry_of:
            bits += coh_of.get(entry_of, 0.0)
        coh_cli = eb.get("coh_cliente")
        if coh_cli:
            cli_c = _cliente_compact(entry.get("cliente"))
            if cli_c:
                bits += coh_cli.get(cli_c, 0.0)
    return bits


def _row_has_any_value(row: dict) -> bool:
    return any(
        str(v or "").strip()
        for k, v in (row or {}).items()
        if not str(k).startswith("_")
    )


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

    # R236 — precompute Fellegi-Sunter (lado u): frequência por VALOR para os
    # campos de identidade, id físico por entry e arrays ordenados por dim
    # (para contagem conjunta via bisect). Tudo derivado do plano carregado.
    fs_freq: dict[str, dict[str, int]] = {
        "of": {}, "ov": {}, "cliente": {}, "modelo": {},
    }
    fs_id_by_key: dict[tuple, int] = {}
    fs_dim_values: dict[str, list[tuple[float, int]]] = {
        f: [] for f in _FS_DIM_FIELDS
    }
    fs_n = 0

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

            # --- R236: FS ---
            eid = fs_n
            fs_n += 1
            fs_id_by_key.setdefault(_entry_key(stamped), eid)
            fs_freq["of"][of_key] = fs_freq["of"].get(of_key, 0) + 1
            ov_c = _identifier_compact(ov_val)
            if ov_c:
                fs_freq["ov"][ov_c] = fs_freq["ov"].get(ov_c, 0) + 1
            cli_c = _cliente_compact(cli_val)
            if cli_c:
                fs_freq["cliente"][cli_c] = fs_freq["cliente"].get(cli_c, 0) + 1
            des_c = _model_compact(des)
            if des_c:
                fs_freq["modelo"][des_c] = fs_freq["modelo"].get(des_c, 0) + 1
            for field in _FS_DIM_FIELDS:
                v = _num(e.get(_PLAN_ATTR_BY_FIELD[field]))
                if v is not None:
                    fs_dim_values[field].append((v, eid))

    for field in _FS_DIM_FIELDS:
        fs_dim_values[field].sort()
    fs_dim_sorted = {
        f: ([v for v, _ in pairs], [i for _, i in pairs])
        for f, pairs in fs_dim_values.items()
    }

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
        # R236 — FS
        "fs_n": fs_n,
        "fs_freq": fs_freq,
        "fs_id_by_key": fs_id_by_key,
        "fs_dim_sorted": fs_dim_sorted,
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


def _topk_by_similarity(
    pool: list[str],
    similarity_fn,
    k: int,
) -> list[tuple[str, float]]:
    scored = [(similarity_fn(key), key) for key in pool]
    scored.sort(reverse=True)
    return [(key, s) for s, key in scored[:k] if s > 0]


def _cliente_candidate_similarity(ocr_value: object, candidate: object) -> float:
    return max(
        _str_sim(_norm_ascii_upper(ocr_value), _norm_ascii_upper(candidate)),
        _str_sim(_cliente_compact(ocr_value), _cliente_compact(candidate)),
    )


def _model_candidate_similarity(ocr_value: object, candidate: object) -> float:
    return max(
        _str_sim(str(ocr_value or "").strip().upper(), str(candidate or "").strip().upper()),
        _str_sim(_model_compact(ocr_value), _model_compact(candidate)),
    )


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
        ocr_compact_variants = set(_model_compact_variants(ocr_value))
        for v in _o_zero_variants(ocr_u):
            if v in model_ft_to_entries:
                _append_model_candidate(v, _str_sim(ocr_u, v), model_ft_to_entries[v])
        for k, entries in model_ft_to_entries.items():
            if _model_compact(k) in ocr_compact_variants:
                _append_model_candidate(k, 100.0, entries)

        if len(out) < _TOP_K:
            for k, s in _topk_by_similarity(
                list(model_ft_to_entries.keys()),
                lambda key: _model_candidate_similarity(ocr_value, key),
                _TOP_K,
            ):
                _append_model_candidate(k, s, model_ft_to_entries[k])
                if len(out) >= _TOP_K:
                    break

        # R225 — só varrer as ~12k designações (Levenshtein) se ainda faltarem
        # candidatos. Quando o modelo bate bem (exact/model_ft já encheram o
        # top-K), `out[:_TOP_K]` ignoraria estes na mesma → resultado idêntico,
        # mas poupa o scan caro no caso comum.
        if len(out) < _TOP_K:
            for k, s in _topk_by_similarity(
                idx["des_keys"],
                lambda key: _model_candidate_similarity(ocr_value, key),
                _TOP_K,
            ):
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

        for k, s in _topk_by_similarity(
            list(pool),
            lambda key: _cliente_candidate_similarity(ocr_value, key),
            _TOP_K,
        ):
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


# R224 — profiling: nº máximo de candidatos pontuados guardados por linha no
# traço de match (o pool pode ter ~12k, quase todos sim≈0/ruído).
_TRACE_TOP_K = 50


def _best_scored_entry(
    entries_by_key: dict[tuple, dict],
    row: dict,
    refs: dict,
    current_phase: str | None,
    score_fields: set[str] | frozenset[str] | None = None,
    min_agree: int = 1,
    trace: dict | None = None,
    idx: dict | None = None,
    extra_bias: dict | None = None,
) -> dict | None:
    """Escolhe a melhor entry do plan por EVIDÊNCIA EM BITS (R236 —
    Fellegi-Sunter com parâmetros medidos; ver `_entry_bits_score`).

    Critério principal: ``bits`` = Σ log2(m/u) sobre identidade (peso por
    valor, g graduado) + combinação de dims (-log2(n_conjunto/N)) − vetos
    (dims discordantes; OF escrita e válida contradita). ``agree``/``raw``
    (a votação holística R223/R226) ficam como desempate e telemetria.
    Devolve None se nada concorda em >= ``min_agree`` campos (linha sem
    NENHUMA evidência fica NA — inalterado). Rivais com OF diferente a
    <= _FS_RIVAL_MARGIN_BITS do topo ficam em ``winner['_rivals']`` para a
    guarda de ambiguidade R219; a margem para o melhor rival de OF diferente
    fica em ``winner['_margin_bits']`` e alimenta o modo decisivo/marginal.
    """
    from app.pipeline.of_consumption import remaining as _remaining

    # R225 — memo de similaridade por-linha: o pool tem ~12k entries mas poucos
    # valores distintos por campo; calcular a similaridade 1x por valor distinto
    # (não 1x por entry) corta o grosso do custo sem mudar nenhum resultado.
    _sim_cache: dict = {}
    # R225 — só construir os `reasons` (dict por campo por entry) quando são
    # precisos: o traço (opt-in) usa-os para todos; sem traço, recalculamos só
    # os do vencedor no fim. Poupa ~1,7M dicts/folha no caso de produção.
    _want_reasons = trace is not None
    idx = idx or _get_indices(refs)
    fs_ctx = _fs_row_context(row, idx, score_fields, extra_bias=extra_bias)
    eligible: list[tuple] = []
    for order, (k, e) in enumerate(entries_by_key.items()):
        if "_of" not in e:
            e = dict(e)
            e["_of"] = k[0]
        global_score, exact_score, reasons, raw_score, agree, exact_id, agree_id = (
            _entry_global_score(e, row, refs, score_fields, cache=_sim_cache,
                                collect_reasons=_want_reasons)
        )
        bits = _entry_bits_score(
            e, row, refs, idx, fs_ctx, score_fields, cache=_sim_cache,
        )
        phase_full = 1 if (current_phase and _phase_is_full(e, current_phase)) else 0
        # R138 — remaining consciente do setor (mesma medida do wizard).
        rem = _remaining(e, phase=current_phase)
        rem_sort = 9e9 if rem == float("inf") else rem
        # R236 — ordena por BITS primeiro (evidência medida); a votação
        # holística R226 (agree → raw → exact_id) fica como desempate, e o
        # setor-com-espaço/remaining como critérios finais.
        eligible.append((
            -bits, -agree, -raw_score, -exact_id, phase_full, rem_sort,
            order, e, reasons, raw_score, global_score, exact_score, agree,
            exact_id, agree_id, bits,
        ))

    if not eligible:
        if trace is not None:
            trace["pool_size"] = 0
            trace["candidates"] = []
        return None
    eligible.sort()
    if trace is not None:
        # R224 — traço de match: guarda os candidatos pontuados (ordenados) para
        # se ver porque o 2º/3º perderam. Cap em _TRACE_TOP_K; pool_size real à
        # parte (o pool pode ter milhares quase todos sim≈0).
        trace["pool_size"] = len(eligible)
        trace["candidates"] = [
            {
                "of": (cand[7] or {}).get("_of"),
                "bits": round(float(cand[15]), 2),
                "agree": int(cand[12]),
                "exact": int(cand[11]),
                "exact_id": int(cand[13]),
                "raw": round(float(cand[9]), 3),
                "weighted": round(float(cand[10]), 3),
                "combined": round(float(int(cand[12]) + float(cand[9])), 3),
                "field_sims": [
                    {
                        "field": r.get("field"),
                        "sim": round(float(r.get("sim") or 0.0), 3),
                        "weight": r.get("weight"),
                        "points": round(float(r.get("points") or 0.0), 3),
                    }
                    for r in (cand[8] or [])
                ],
            }
            for cand in eligible[:_TRACE_TOP_K]
        ]
    best = eligible[0]
    best_bits = float(best[15])
    best_agree = -best[1]
    best_raw = -best[2]
    best_rem_sort = best[5]
    if best_agree < min_agree:
        return None
    winner = dict(best[7])
    winner["_score"] = round(float(best[9]), 3)
    winner["_bits"] = round(best_bits, 2)
    winner["_agree"] = int(best_agree)
    winner["_weighted_score"] = round(float(best[10]), 3)
    winner["_exact_score"] = int(best[11])
    winner["_combined"] = round(float(best_agree + best_raw), 3)
    # R225 — se os reasons não foram colhidos (caso de produção), recalcula só
    # os do vencedor (idêntico ao que seria colhido; usa o cache quente).
    winner_reasons = best[8]
    if not _want_reasons:
        winner_reasons = _entry_global_score(
            best[7], row, refs, score_fields, cache=_sim_cache, collect_reasons=True,
        )[2]
    winner["_score_reasons"] = sorted(
        winner_reasons,
        key=lambda reason: abs(float(reason.get("points") or 0.0)),
        reverse=True,
    )[:6]
    if best_rem_sort < 9e9:
        winner["_remaining"] = best_rem_sort
    # R236 — margem em bits para o melhor rival com OF DIFERENTE (sub-linhas
    # da mesma OF são a mesma encomenda — não são rivais de identidade) +
    # rivais da guarda de ambiguidade R219 (entries a <= _FS_RIVAL_MARGIN_BITS
    # do topo, qualquer OF, para a cor por campo).
    winner_of_key = str(winner.get("_of") or winner.get("of") or "").strip()
    winner_des = str(winner.get("designacao") or "").strip().upper()
    margin_bits: float | None = None
    sibling_margin: float | None = None
    rivals: list[dict] = []
    for cand in eligible[1:]:
        gap = best_bits - float(cand[15])
        cand_of = str((cand[7] or {}).get("_of") or (cand[7] or {}).get("of") or "").strip()
        if margin_bits is None and cand_of and cand_of != winner_of_key:
            margin_bits = gap
        # R248 — melhor IRMÃO (mesma OF, designação diferente): margem em
        # bits própria, para telemetria e confiança da célula modelo. NÃO
        # altera `_margin_bits` (a calibração T/s_ood do R243 foi fitted na
        # margem OF-level) nem `_rivals`.
        if (sibling_margin is None and cand_of == winner_of_key
                and str((cand[7] or {}).get("designacao") or "").strip().upper()
                != winner_des):
            sibling_margin = gap
        if gap <= _FS_RIVAL_MARGIN_BITS:
            if len(rivals) < 10:
                rivals.append(cand[7])
        elif margin_bits is not None and (
                sibling_margin is not None or gap > _FS_SIBLING_AMBIG_BITS):
            break  # ordenado por bits: gaps só crescem — nada mais por achar
    winner["_margin_bits"] = round(margin_bits, 2) if margin_bits is not None else 99.0
    winner["_sibling_margin_bits"] = (
        round(sibling_margin, 2) if sibling_margin is not None else 99.0
    )
    if rivals:
        winner["_rivals"] = rivals
    return winner


def _candidate_is_real_evidence(field: str, cand: dict) -> bool:
    if not cand.get("plan_entries"):
        return False
    sim = float(cand.get("sim") or 0.0)
    return sim >= _FORCED_WINNER_MIN_SIM.get(field, 100.0)


def _candidate_entries_by_key(
    candidates_by_field: dict[str, list[dict]],
    score_fields: set[str] | frozenset[str] | None = None,
) -> dict[tuple, dict]:
    """R223 — pool LARGO para a votação holística: a união das entries de
    TODOS os candidatos top-K de cada campo (fuzzy incluído), para a votação
    ter as entries certas a considerar mesmo quando nenhum campo bate exato.
    (Antes só entravam candidatos com "evidência real"/exata, o que deixava
    a votação cega à encomenda certa quando estava mal lida.)"""
    allowed = set(score_fields or _PLAN_FIELDS)
    entries_by_key: dict[tuple, dict] = {}
    for field in _PLAN_FIELDS:
        if field not in allowed:
            continue
        for cand in candidates_by_field.get(field, []):
            for entry in cand.get("plan_entries", []):
                key = _entry_key(entry)
                entries_by_key.setdefault(key, entry)
    return entries_by_key


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


# R231 — similaridade mínima (0-100) contra o índice de modelos para aceitar que
# um código na coluna OF é mesmo um modelo e o encaminhar para o campo modelo.
# 72 inclui a família CD03P503->CD03P10B (~75) e exclui leituras garbled
# (CBRBE6D ~67, '(49566D)' ~38), que ficam como estão para revisão humana.
_REALIGN_MODEL_SIM = 72.0


def _realign_misplaced_of(
    row: dict, idx: dict | None, template_name: str | None = None
) -> dict:
    """R223 — repõe a OF quando o OCR a colocou na coluna errada (OV/PRI). SÓ
    realinha quando o número é MESMO uma OF do plano (ref-validado) — nunca às
    cegas (ex.: um nº de OV de 6 dígitos que não seja OF fica onde está).

    R231 — quando a coluna OF traz um CÓDIGO DE MODELO (tem letras → não é uma
    OF, que é sempre numérica) e a coluna modelo está vazia/lixo, encaminha-o
    para o campo `modelo` para o índice de modelos o reconhecer. É o erro mais
    comum: como a coluna OV vem em branco, o OCR desliza a linha — a OF real cai
    na OV e o modelo cai na OF. O `of` original NÃO é apagado (preserva o dado)."""
    if not idx:
        return row
    of_keys = idx.get("of_keys") or set()

    # Etapa 1 (R223) — OF numérica no sítio errado (OV/PRI) -> OF.
    if of_keys:
        if normalize_of(row.get("of")) in of_keys:
            return row  # a OF já é válida → nada a fazer
        for src in ("ov", "pri"):
            cand = normalize_of(row.get(src))
            if cand and cand in of_keys:
                row = dict(row)
                row["of"] = cand
                if src == "ov":
                    row["ov"] = ""  # era a OF, não a OV
                return row

    # Etapa 2 (R231) — CÓDIGO DE MODELO na coluna OF -> campo modelo. A
    # `acabamento` leva mesmo códigos de peça na coluna OF (tem branch próprio).
    if template_name == "acabamento":
        return row
    of_val = str(row.get("of") or "").strip()
    if not (_value_has_letters(of_val) and len(_model_compact(of_val)) >= 4):
        return row  # OF não tem letras (é numérica/vazia) ou é curta demais
    modelo_val = str(row.get("modelo") or "").strip()
    if not (_is_missing_ocr(modelo_val) or len(_model_compact(modelo_val)) < 4):
        return row  # já há um modelo legível no sítio → não sobrepor
    ft_keys = idx.get("model_ft_keys") or []
    top = _topk_by_similarity(
        ft_keys, lambda k: _model_candidate_similarity(of_val, k), 1
    )
    if (top[0][1] if top else 0.0) < _REALIGN_MODEL_SIM:
        return row  # sem evidência forte de modelo → fica como está (rever)
    row = dict(row)
    row["modelo"] = of_val  # encaminha para o índice de modelos; mantém o `of`
    return row


# R236 — realinhamento por BUSCA DE HIPÓTESES: as regras casuísticas
# (R223 Etapa 1, R231 Etapa 2, OF embebida, cliente na coluna modelo) passam a
# GERADORES de variantes da linha; o MESMO scoring FS decide qual variante tem
# mais evidência no plano. Prior: cada campo movido paga isto em bits (não se
# inventam realinhamentos) e a variante tal-qual (H0) ganha empates.
# Medido no app.db: ~1 linha em 12-20 tem shift de colunas (OF na coluna OV
# 5.2%, modelo na coluna OF 4.6%, assinatura completa 3.1%).
_ALIGN_MOVE_PENALTY_BITS = 1.5
_EMBEDDED_OF_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")

# R240 — geradores POR FORMA (shape-driven): qualquer coluna de identidade que
# contenha uma OF VÁLIDA do plano (cheia ou embebida) quando a coluna OF está
# inválida gera uma variante "move para OF". O custo é o prior MEDIDO e
# CONDICIONADO à assinatura — P(o movimento é certo | vejo este token nesta
# coluna com a OF inválida), das 3.413 linhas validadas do app.db, com
# suavização de Laplace:
#   ov       131/166 confirmadas → (131+1)/(166+2)=.786 → 0.35 bits
#   cliente    8/9               → (8+1)/(9+2)   =.818 → 0.29 bits
#   pri        2/3               → (2+1)/(3+2)   =.600 → 0.74 bits
#   modelo     0/1               → (0+1)/(1+2)   =.333 → 1.58 bits
#   lote       0/11              → (0+1)/(11+2)  =.077 → 3.70 bits
# (lote é caro DE PROPÓSITO: lotes sem letras parecem OFs e nunca se confirmou
# um único movimento — a evidência do plano tem de pagar 3.7 bits para mover.)
# Isto elimina a classe de buraco "permutação sem regra": QUALQUER coluna com
# uma OF plausível entra na busca, com o custo certo — nada fica por cobrir.
_SHIFT_TO_OF_COST_BITS = {
    "ov": 0.35, "cliente": 0.29, "pri": 0.74, "modelo": 1.58, "lote": 3.70,
}
# OF embebida em texto (vs coluna que É só a OF): sobretaxa fixa pequena.
_SHIFT_EMBEDDED_SURCHARGE_BITS = 0.5
# Fontes NUNCA confirmadas nos dados (modelo 0/1, lote 0/11): mover só com
# CORROBORAÇÃO (winner da variante com >=2 campos a concordar) — a validade
# da OF sozinha já está contada na assinatura; sem mais nada, seria dupla
# contagem e um lote numérico "moveria" só por parecer uma OF.
_SHIFT_REQUIRE_CORROBORATION = frozenset({"modelo", "lote"})

# R241 — CANAL DE VISÃO fitted: matriz de confusão de caracteres estimada por
# alinhamento NW sobre 1.556 pares (raw, verdade) do app.db da fábrica
# (scripts/diag/fit_char_confusion.py → lexicons/cross_params.json, tracked,
# com proveniência). Top confusões MEDIDAS: 1↔4, 7↔3, M↔H, 8↔9, 2↔7 — os
# dados batem a intuição (1↔4 nem constava dos priors de glifos). Substitui o
# tier fixo g=0.3 (sim>=0.8): um misread PLAUSÍVEL do canal vale mais; um
# improvável vale ~nada — hoje ambos eram "sim 0.83".
_CROSS_PARAMS_PATH = None  # resolvido em _load_cross_params()
_CHANNEL_G_CAP = 0.45      # nunca acima do tier estrutural 0.5 (sim>=0.9)
_CHANNEL_G_L0 = 11.0       # custo (bits) a que a evidência do canal morre
_CHANNEL_VETO_WAIVER_G = 0.30  # misread plausível da OF escrita → sem veto


@lru_cache(maxsize=1)
def _load_cross_params() -> dict:
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "lexicons" / "cross_params.json"
    try:
        import json as _json

        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Fallback sem ficheiro (testes/refs mínimas): custo moderado uniforme
        # — degrada para ~o tier antigo (1 sub qualquer ≈ g 0.36).
        return {}


# R243/E1 — POSTERIOR CALIBRADO + OOD explícito. A margem em bits vira uma
# probabilidade P(top é a linha certa) via logística com temperatura T,
# limitada pela alternativa "não está no plano" (s_ood): a evidência efetiva
# é min(margem_para_rival, bits_top − s_ood). T e s_ood são FITTED no
# harness (backtest_winner --calibrate, minimiza Brier sobre os conjuntos
# rotulados + OOD; quant7: 10.7% das linhas frescas têm a OF fora do plano
# de hoje — o OOD é 1 em 9, não um caso raro). Fallbacks razoáveis sem fit.
def _posterior_p_top(bits_top: float, margin_bits: float | None) -> float:
    cal = (_load_cross_params().get("calibration") or {})
    t = max(0.5, float(cal.get("temperature_bits") or 3.0))
    s_ood = float(cal.get("s_ood_bits") or 4.0)
    margin_eff = min(
        float(margin_bits if margin_bits is not None else 99.0),
        float(bits_top) - s_ood,
    )
    return 1.0 / (1.0 + 2.0 ** (-margin_eff / t))


def _sibling_p(margin_bits: float) -> float:
    """R248 — P(irmão certo) pela mesma logística/temperatura calibrada do
    R243, SEM o cap OOD (escolher entre irmãos da mesma OF não é OOD: a
    encomenda está no plano; a dúvida é qual sub-linha)."""
    cal = (_load_cross_params().get("calibration") or {})
    t = max(0.5, float(cal.get("temperature_bits") or 3.0))
    return 1.0 / (1.0 + 2.0 ** (-max(float(margin_bits), 0.0) / t))


def _model_sibling_ambiguous(
    winner: dict, row: dict, refs: dict, idx: dict | None
) -> bool:
    """R248 — o modelo ESCRITO não discrimina o winner dos IRMÃOS da mesma
    OF (designações diferentes): algum irmão atinge sim de modelo >= winner
    − 0.02. É a pergunta direta — a margem em bits OF-level ignora irmãos
    por design (R236) e pintava verde com p_top 0.93-0.99 a troca silenciosa
    de peça (17 casos históricos validados em bloco no app.db).

    Só com modelo escrito: com a célula vazia os irmãos empatam em bits e a
    guarda de rivais R219 (<=1.0 bit) já cobre. Compara por SIM e não por
    bits para apanhar também empates a 1.0 (família-prefixo '5100TME' contém
    em TODOS os irmãos; código repetido em 2 irmãos — sheet 814)."""
    if not idx:
        return False
    value = (row or {}).get("modelo")
    if _is_missing_ocr(value):
        return False
    w_of = str(winner.get("_of") or winner.get("of") or "").strip()
    entries = (idx.get("of_to_entries") or {}).get(w_of) or []
    if len(entries) < 2:
        return False
    w_sim = _efs_compute("modelo", winner, row, refs, value)
    if w_sim is None or w_sim <= 0.0:
        return False
    winner_des = str(winner.get("designacao") or "").strip().upper()
    for e in entries:
        des = str((e or {}).get("designacao") or "").strip().upper()
        if not des or des == winner_des:
            continue
        s = _efs_compute("modelo", e, row, refs, value)
        if s is not None and s >= w_sim - 0.02:
            return True
    return False


# R243/E2 — limiar de gravação por PERDA ESPERADA: grava sse
# (1−P)·C_erro < C_rev ⇔ P > 1 − C_rev/C_erro. Rácios C_erro/C_rev por
# classe (defaults defensáveis; a fixar com o Luís): dims críticas de
# fabrico 50× → 0.98; identidade 20× → 0.95; resto 10× → 0.90.
_WRITE_P_THRESHOLD = {
    "esp": 0.98, "comp_mm": 0.98,
    "of": 0.95, "ov": 0.95, "cliente": 0.95, "modelo": 0.95, "lote": 0.95,
}
_REVIEW_CRITICALITY = {
    "esp": 5.0, "comp_mm": 5.0,
    "of": 3.0, "ov": 3.0, "cliente": 3.0, "modelo": 3.0, "lote": 3.0,
}


def write_confidence_threshold(field: str) -> float:
    """P(top) mínimo para auto-gravar `field` (perda esperada, R243)."""
    return _WRITE_P_THRESHOLD.get(field, 0.90)


def _fs_veto_relaxed_bits() -> float:
    """R241/C2 — veto relaxado quando a entry rival é da MESMA família que a
    OF escrita (erro humano de transcrição plausível). Valor fitted:
    -3.3·(1-p_same_family) com p medido = 0.096 → -2.98. Fallback -3.0."""
    hc = (_load_cross_params().get("human_channel") or {})
    return float(hc.get("veto_relaxed_bits") or -3.0)


def _char_channel_costs(op: str = "") -> tuple[dict[str, float], float, float]:
    params = _load_cross_params()
    ch = (params.get("char_channel") or {})
    # R245 — canal POR OPERADOR quando fitted (>=300 pares dele; o refit
    # R244 povoa char_channel_by_operator sozinho à medida que as correções
    # crescem). Fallback: matriz global.
    if op:
        per_op = (params.get("char_channel_by_operator") or {}).get(op)
        if per_op:
            ch = per_op
    return (
        ch.get("sub_costs_bits") or {},
        float(ch.get("cost_default_bits") or 7.0),
        float(ch.get("cost_indel_bits") or 7.0),
    )


@lru_cache(maxsize=100_000)
def _channel_align_cost_bits(truth: str, written: str, op: str = "") -> float:
    """Custo (bits) do alinhamento ótimo escrito|verdadeiro sob a matriz
    fitted — DP de Needleman-Wunsch com custos por substituição. ``op``
    seleciona o canal por operador (R245) quando existe."""
    subs, default, indel = _char_channel_costs(op)
    n, m = len(truth), len(written)
    if not n or not m:
        return 99.0
    prev = [j * indel for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [i * indel]
        for j in range(1, m + 1):
            a, b = truth[i - 1], written[j - 1]
            sub_cost = 0.0 if a == b else subs.get(f"{a}>{b}", default)
            cur.append(min(prev[j] + indel, cur[j - 1] + indel,
                           prev[j - 1] + sub_cost))
        prev = cur
    return prev[-1]


def _channel_g(truth: object, written: object, *, pad_of: bool = False,
               op: str = "") -> float:
    """g ∈ [0, _CHANNEL_G_CAP] — evidência de que `written` é um misread de
    `truth` segundo o canal de visão fitted (por operador quando existe).
    0 quando o custo excede L0."""
    t = _identifier_compact(truth, pad_of=pad_of)
    w = _identifier_compact(written, pad_of=pad_of)
    if not t or not w:
        return 0.0
    cost = _channel_align_cost_bits(t, w, op)
    return max(0.0, min(_CHANNEL_G_CAP, 1.0 - cost / _CHANNEL_G_L0))


def _alignment_hypotheses(
    row: dict, idx: dict | None, template_name: str | None = None
) -> list[tuple[str, dict, float]]:
    """Variantes plausíveis de re-atribuição de colunas: (label, linha, CUSTO
    em bits). Só gera variantes ref-validadas (o valor movido tem de fazer
    sentido no campo destino); a maioria das linhas fica só com H0. R240: além
    dos geradores semânticos (R223/R231), qualquer coluna de identidade com
    uma OF válida do plano gera a variante "move para OF" com o custo medido
    (_SHIFT_TO_OF_COST_BITS) — nenhuma permutação fica sem cobertura."""
    hyps: list[tuple[str, dict, float]] = [("H0", row, 0.0)]
    if not idx:
        return hyps
    of_to_entries = idx.get("of_to_entries") or {}

    # As regras R223/R231 encadeadas, como UMA hipótese (mantêm-se testadas
    # e afinadas; deixam apenas de ser aplicadas às cegas).
    realigned = _realign_misplaced_of(row, idx, template_name)
    if realigned != row:
        moved = sum(
            1 for k in ("of", "ov", "pri", "modelo")
            if str(realigned.get(k) or "") != str(row.get(k) or "")
        )
        cost = max(moved, 1) * _ALIGN_MOVE_PENALTY_BITS
        hyps.append(("realign_of", realigned, cost))
        # Shift COMPLETO (assinatura em 3.1% das linhas): a OF real veio da
        # OV e a coluna OF trazia o CÓDIGO DE MODELO — a Etapa 1 do R231
        # sobrescreve a OF e perde esse texto. Variante extra que o preserva
        # no campo modelo; a evidência decide se compensa o movimento extra.
        orig_of = str(row.get("of") or "").strip()
        if (
            str(realigned.get("of") or "") != orig_of
            and orig_of
            and template_name != "acabamento"
            and _value_has_letters(orig_of)
            and len(_model_compact(orig_of)) >= 4
        ):
            modelo_after = str(realigned.get("modelo") or "").strip()
            if _is_missing_ocr(modelo_after) or len(_model_compact(modelo_after)) < 4:
                keep_model = dict(realigned)
                keep_model["modelo"] = orig_of
                hyps.append(("realign_of_keep_model", keep_model,
                             cost + _ALIGN_MOVE_PENALTY_BITS))

    # R240 — geradores POR FORMA: OF válida (cheia ou embebida) em QUALQUER
    # coluna de identidade quando a coluna OF é inválida. Cobre por construção
    # os casos sem regra (OF na coluna modelo/cliente/lote — cliente→OF tem 8
    # casos confirmados por humanos no app.db). Custo = prior medido por
    # coluna-fonte; embebida paga sobretaxa.
    of_text = str(row.get("of") or "").strip()
    of_key = normalize_of(_identifier_compact(of_text, pad_of=True))
    of_col_invalid = of_key not in of_to_entries
    if of_col_invalid:
        seen_variants = {str(realigned.get("of") or "") if realigned != row else ""}
        for src, base_cost in _SHIFT_TO_OF_COST_BITS.items():
            if src in ("ov", "pri"):
                continue  # cobertos pela Etapa 1 do realign_of (já testada)
            src_text = str(row.get(src) or "").strip()
            if not src_text:
                continue
            # (a) a coluna É a OF (token completo)
            full_key = normalize_of(_identifier_compact(src_text, pad_of=True))
            if full_key and full_key in of_to_entries and full_key not in seen_variants:
                variant = dict(row)
                variant["of"] = full_key
                variant[src] = ""  # o token era a OF, não um valor de `src`
                hyps.append((f"{src}_to_of", variant, base_cost))
                seen_variants.add(full_key)
                continue
            # (b) OF embebida em texto da coluna ("PETITJEAN 262107")
            for m in _EMBEDDED_OF_RE.finditer(src_text):
                token = normalize_of(m.group(1))
                if token in of_to_entries and token not in seen_variants:
                    variant = dict(row)
                    variant["of"] = token
                    variant[src] = re.sub(
                        rf"(?<!\d){re.escape(m.group(1))}(?!\d)", " ",
                        src_text, count=1,
                    ).strip()
                    hyps.append((f"{src}_to_of_embedded", variant,
                                 base_cost + _SHIFT_EMBEDDED_SURCHARGE_BITS))
                    seen_variants.add(token)
                    break

    # OF embebida em texto livre na coluna OF ("OF 262882 dobrar" → 262882);
    # o resto do texto pode ser o modelo se o campo modelo estiver vazio/lixo.
    if of_text and of_col_invalid:
        for m in _EMBEDDED_OF_RE.finditer(of_text):
            token = normalize_of(m.group(1))
            if token not in of_to_entries:
                continue
            variant = dict(row)
            variant["of"] = token
            moved = 1
            leftover = re.sub(
                rf"(?<!\d){re.escape(m.group(1))}(?!\d)", " ", of_text, count=1
            ).strip()
            modelo_val = str(row.get("modelo") or "").strip()
            if (
                leftover
                and template_name != "acabamento"
                and (_is_missing_ocr(modelo_val) or len(_model_compact(modelo_val)) < 4)
                and _value_has_letters(leftover)
            ):
                variant["modelo"] = leftover
                moved = 2
            hyps.append(("embedded_of", variant, moved * _ALIGN_MOVE_PENALTY_BITS))
            break

    # Cliente na coluna modelo (generaliza o _realign_misplaced_cliente do
    # R234 sem regra de resolução única: a evidência decide).
    if template_name != "acabamento":
        cliente_val = str(row.get("cliente") or "").strip()
        modelo_val = str(row.get("modelo") or "").strip()
        if not cliente_val and modelo_val and _value_has_letters(modelo_val):
            variant = dict(row)
            variant["cliente"] = modelo_val
            variant["modelo"] = ""
            hyps.append(("modelo_to_cliente", variant, _ALIGN_MOVE_PENALTY_BITS))

    return hyps


def _choose_row_alignment(
    row: dict,
    refs: dict,
    idx: dict | None,
    cc_fields: set[str],
    current_phase: str | None = None,
    score_fields: set[str] | frozenset[str] | None = None,
    template_name: str | None = None,
    trace: dict | None = None,
    extra_bias: dict | None = None,
) -> tuple[str, dict, dict[str, list[dict]]]:
    """R236/R240 — escolhe o alinhamento de colunas com MAIS evidência:
    pontua o winner (bits) de cada hipótese sobre o seu pool de candidatos e
    fica com a melhor após o CUSTO da hipótese (prior medido por tipo de
    movimento). Devolve (label, linha escolhida, candidatos dessa linha)."""
    hyps = _alignment_hypotheses(row, idx, template_name)
    best: tuple[tuple[float, float], str, dict, dict[str, list[dict]]] | None = None
    for label, variant, cost_bits in hyps:
        candidates_by_field = {
            f: _candidates_for_field(f, variant, refs, idx)
            for f in _ROW_FIELDS
            if f in cc_fields
        }
        if len(hyps) == 1:
            # Caso comum (~90% das linhas): sem variantes → sem scoring extra.
            return label, variant, candidates_by_field
        pool = _candidate_entries_by_key(candidates_by_field, score_fields)
        winner = (
            _best_scored_entry(pool, variant, refs, current_phase,
                               score_fields, idx=idx, extra_bias=extra_bias)
            if pool else None
        )
        # R240 — fontes de risco (modelo/lote → OF) exigem corroboração: o
        # winner da variante tem de concordar em >=2 campos (a OF movida + 1).
        src = label.split("_to_of")[0] if "_to_of" in label else ""
        if (
            src in _SHIFT_REQUIRE_CORROBORATION
            and int((winner or {}).get("_agree") or 0) < 2
        ):
            continue
        bits = float((winner or {}).get("_bits") or -99.0)
        adjusted = bits - cost_bits
        key = (adjusted, -cost_bits)  # empate → custo menor (H0 primeiro)
        if best is None or key > best[0]:
            best = (key, label, variant, candidates_by_field)
    assert best is not None
    _key, label, variant, candidates_by_field = best
    if trace is not None:
        trace["alignment"] = label
        trace["alignment_hypotheses"] = len(hyps)
    return label, variant, candidates_by_field


def _find_winner_entry(
    candidates_by_field: dict[str, list[dict]],
    row: dict,
    refs: dict,
    idx: dict | None = None,
    current_phase: str | None = None,
    score_fields: set[str] | frozenset[str] | None = None,
    force_top1: bool = True,
    trace: dict | None = None,
    extra_bias: dict | None = None,
) -> dict | None:
    """R223 — votação holística sobre um pool LARGO de candidatos (todos os
    top-K de cada campo). Full-scan do plano só como fallback se o pool não
    der vencedor. A confiança (modo) vem do nº de campos que concordam."""
    pool = _candidate_entries_by_key(candidates_by_field, score_fields)
    winner = (
        _best_scored_entry(pool, row, refs, current_phase, score_fields,
                           trace=trace, idx=idx, extra_bias=extra_bias)
        if pool else None
    )

    if winner is None and force_top1 and _row_has_any_value(row):
        # R223 — fallback: varre o plano todo caso o pool de candidatos tenha
        # falhado a entry certa. Exige >=1 campo a concordar (min_agree=1) — uma
        # linha sem NENHUM campo a bater fica sem winner (NA), em vez de forçar
        # uma peça aleatória. ("só se não encontrar mesmo nada é que não põe".)
        entries_by_key = _all_plan_entries(idx or {}) or pool
        if entries_by_key:
            winner = _best_scored_entry(
                entries_by_key, row, refs, current_phase, score_fields,
                trace=trace, idx=idx, extra_bias=extra_bias,
            )
            if trace is not None:
                trace["fallback_full_scan"] = True

    if winner is not None:
        # R236 — confiança pela MARGEM em bits para o melhor rival com OF
        # diferente (backtest: falhas p50≈0.3-1.5 bits, acertos p50≈5.5-17).
        # decisivo → "strong" (substitui, verde/amarelo); marginal →
        # "weak_guess" (substitui na mesma — R219 — mas vermelho/rever).
        margin = float(winner.get("_margin_bits") or 0.0)
        winner["_winner_mode"] = (
            "strong" if margin >= _FS_MARGIN_DECISIVE else "weak_guess"
        )
        # R243/E1 — probabilidade calibrada de o winner ser a linha certa
        # (logística sobre a evidência efetiva, limitada pelo OOD).
        winner["_p_top"] = round(
            _posterior_p_top(float(winner.get("_bits") or 0.0),
                             winner.get("_margin_bits")), 3)
        # R241/C2 — o winner contradiz uma OF escrita e VÁLIDA: marcar a
        # natureza provável do erro (visão vs transcrição humana) para a UI.
        of_written = str((row or {}).get("of") or "").strip()
        wk = normalize_of(_identifier_compact(of_written, pad_of=True)) if of_written else ""
        of_map = (idx or {}).get("of_to_entries") or {}
        w_of = str(winner.get("_of") or winner.get("of") or "").strip()
        if wk and wk in of_map and w_of and w_of != wk:
            winner["_contradicts_written_of"] = wk
            cli_w = _cliente_compact(winner.get("cliente"))
            ft_w = _model_compact(_model_first_token(winner.get("designacao")))
            same_family = any(
                (cli_w and _cliente_compact(e.get("cliente")) == cli_w)
                or (ft_w and _model_compact(_model_first_token(e.get("designacao"))) == ft_w)
                for e in of_map.get(wk) or []
            )
            winner["_written_of_same_family"] = bool(same_family)
    if trace is not None:
        trace["candidates_by_field"] = {
            f: len(candidates_by_field.get(f, []) or [])
            for f in ("of", "ov", "cliente", "modelo")
        }
    return winner


def select_winner(
    row: dict,
    refs: dict,
    template_name: str | None = None,
    current_phase: str | None = None,
    extra_bias: dict | None = None,
) -> dict | None:
    """R236 — caminho público de seleção de winner para UMA linha (o mesmo
    que `_score_row` usa: alinhamento por hipóteses → candidatos → winner).
    Serve o harness `scripts/diag/backtest_winner.py` e diagnósticos,
    garantindo que medem exatamente o caminho de produção."""
    idx = _get_indices(refs)
    _label, row2, candidates_by_field = _choose_row_alignment(
        dict(row), refs, idx, set(_ROW_FIELDS), current_phase, None,
        template_name, extra_bias=extra_bias,
    )
    return _find_winner_entry(
        candidates_by_field, row2, refs, idx, current_phase, None,
        force_top1=True, extra_bias=extra_bias,
    )


def _all_eligible_phase_full(
    candidates_by_field: dict[str, list[dict]],
    row: dict,
    refs: dict,
    current_phase: str | None,
    winner: dict | None = None,
) -> bool:
    """R125 (restaurado R222) — True se TODAS as entries elegíveis (score≥1)
    desta linha estão concluídas em `current_phase`. Sinaliza "obra concluída":
    o operador está a registar produção numa OF/peça já fechada nesse setor.
    False quando `current_phase` é None, não há candidatos elegíveis, ou pelo
    menos uma linha ainda tem espaço. (R163 revertido: volta a forçar vermelho
    no `_score_row`.) O param `winner` é aceite por compat. de chamada mas não
    é usado — a deteção é sobre TODAS as entries elegíveis, não só o winner."""
    _ = winner
    if not current_phase:
        return False
    entries_by_key: dict[tuple, dict] = {}
    for field in _PLAN_FIELDS:
        for cand in candidates_by_field.get(field, []):
            for e in cand.get("plan_entries", []):
                k = _entry_key(e)
                entries_by_key.setdefault(k, e)
    if not entries_by_key:
        return False
    found_eligible = False
    for k, e in entries_by_key.items():
        if "_of" not in e:
            e = dict(e)
            e["_of"] = k[0]
        if score_entry(e, row, refs) < 1:
            continue
        found_eligible = True
        if not _phase_is_full(e, current_phase):
            return False
    return found_eligible


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
        # R222 — cor usa o limiar tolerante de 30/05 (_COLOR_NUM_ABS), não o
        # limiar apertado do scoring (_VERY_DIFF_NUM_ABS).
        abs_max = _COLOR_NUM_ABS.get(field, 0)
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
        # R247 — cor honesta com OCR decorado: núcleo limpo contido (mesmo
        # sem sufixo A/B) ou misread a 1 char do canal fitted no código-peça
        # NÃO é "muito diferente" — sem isto o winner CERTO ('5100T742A' →
        # '5100TME1 - CC4H1 5100T742 1/2') ficava vermelho (o OCR decorado
        # não é substring e o compacto dá 0 pelo guard len>5 do Levenshtein).
        if _model_core_matches(ocr_u, proposed_u, strip_ab=True):
            return False
        if _model_channel_sim(ocr_u, proposed_u) >= 0.9:
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


def _winner_match_kind(winner: dict | None) -> str | None:
    mode = (winner or {}).get("_winner_mode")
    # R223 — palpite fraco (1 só campo fuzzy a concordar) é marcado como
    # "best guess" para a UI o mostrar como rever; um winner forte não precisa.
    if mode == "weak_guess":
        return "MATCH_BEST_GUESS"
    return None


def _mark_winner_cell(cell: dict, winner: dict | None) -> dict:
    if not winner:
        return cell
    out = dict(cell)
    mode = winner.get("_winner_mode")
    if mode:
        out["winner_mode"] = mode
    # R243 — confiança calibrada do winner na célula (decisão de gravação
    # por perda esperada + prioridade da fila de revisão).
    if winner.get("_p_top") is not None:
        out.setdefault("decision_confidence", winner.get("_p_top"))
    if winner.get("_score_reasons"):
        out["score_reasons"] = winner.get("_score_reasons")
    match_kind = _winner_match_kind(winner)
    if match_kind:
        out["match_kind"] = match_kind
        # R223 — NÃO forçar very_different→snapped. Um palpite (ou um campo que
        # diverge do canónico) mantém-se vermelho/rever; nunca verde-confiante
        # numa peça incerta. O valor canónico continua a ser aplicado, mas a
        # cor é honesta.
        # R236 — winner MARGINAL (margem em bits < _FS_MARGIN_DECISIVE):
        # substituições/autofills DERIVADAS DO WINNER ficam vermelhas para
        # revisão. O valor continua a ser aplicado (R219 — substitui sempre);
        # só a cor muda. Células validadas por referência própria (SAP-lote,
        # lexicon, sintaxe) não dependem do winner e mantêm a cor.
        if (
            out.get("status") == "snapped"
            and (out.get("ref_source") or out.get("source")) == "plan"
        ):
            out["status"] = "very_different"
    return out


def _empty_rule_cell(field: str, *, ref_source: str = "syntax") -> dict:
    _ = field
    return _make_cell(
        "",
        "confirmed",
        "syntax",
        ref_source=ref_source,
        match_kind="MATCH_REGRA_VAZIO",
        empty_ok=True,
    )


def _score_ferramenta_cell(ocr_value: str, *, row_has_winner: bool = False) -> dict:
    canonical = normalize_ferramenta(ocr_value)
    if canonical == "":
        if row_has_winner:
            return _empty_rule_cell("coni", ref_source="ferramenta")
        return _make_cell("", "NA", "ocr_raw")
    if canonical is None:
        if row_has_winner:
            return _make_cell(
                ocr_value,
                "confirmed",
                "syntax",
                proposed=_FERRAMENTA_REF_LABEL,
                ref_source="ferramenta",
                match_kind="MATCH_REGRA_FORCADO",
                warning="Valor CONI fora do vocabulário; aceite por winner da linha.",
            )
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


def _score_no_ref_row_cell(
    field: str,
    ocr_value: str,
    *,
    row_has_winner: bool = False,
) -> dict:
    """Validate filled no-plan fields by local rule/syntax instead of NA."""
    if not ocr_value:
        if row_has_winner:
            return _empty_rule_cell(field)
        return _make_cell("", "NA", "ocr_raw")

    valid: bool | None = None
    if field == "pri":
        valid = _looks_like_pri(ocr_value)
    elif field == "qtd":
        valid = _looks_like_short_digits(ocr_value)
    elif field in ("qtd_metros", "m2", "sobras", "sucata"):
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
        if row_has_winner:
            return _make_cell(
                ocr_value,
                "confirmed",
                "syntax",
                ref_source="syntax",
                match_kind="MATCH_REGRA_FORCADO",
                warning="Valor local inválido; aceite por winner da linha.",
            )
        return _make_cell(ocr_value, "very_different", "syntax")
    return _make_cell(
        ocr_value,
        "confirmed",
        "syntax",
        match_kind="MATCH_REGRA",
    )


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


def _identifier_points_to_other_reference(
    field: str, ocr_value: object, proposed: object, idx: dict | None
) -> bool:
    """R236 — o identificador ESCRITO existe no plano mas aponta para OUTRA
    referência que não a proposta do winner (ex.: OV da encomenda B numa linha
    cuja OF é da encomenda A). Conflito de identidade real: o valor do winner
    substitui na mesma (R219), mas a célula fica vermelha/rever. Substitui a
    guarda de rivais nestes casos — com o ranking em bits o rival perdedor já
    não é "quase-empate", mas o conflito continua a merecer olhos humanos."""
    if not idx or not ocr_value or not proposed:
        return False
    if _identifier_values_match(field, ocr_value, proposed):
        return False
    if field == "of":
        key = normalize_of(_identifier_compact(str(ocr_value), pad_of=True))
        return bool(key and key in (idx.get("of_to_entries") or {}))
    if field == "ov":
        pool = idx.get("ov_to_entries") or {}
        raw = str(ocr_value or "").strip()
        if raw in pool:
            return True
        key = _identifier_compact(raw)
        return bool(key) and any(
            _identifier_compact(k) == key for k in pool
        )
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
    # R236 — floor por tolerância NUMÉRICA: sem winner global, um candidato
    # de dimensão fora da tolerância do motor não é evidência de nada — não
    # substitui (a célula fica em revisão com o OCR à vista). A auditoria de
    # 2-3 Jul provou que os erros gravados são de magnitude, não de texto.
    if field in _VERY_DIFF_NUM_ABS and best_sim <= 0.0:
        return None
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
        # R222 (reverte D8) — modelo = designação COMPLETA do plan também no
        # Acabamento (antes usava _model_first_token / código curto).
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
    neste campo, um valor canónico diferente do do winner. Desde R219 isto só
    afeta revisão/cor; o valor canónico do winner continua a substituir."""
    rivals = winner.get("_rivals") or []
    if not rivals or not proposed:
        return False
    for rival in rivals:
        rv = _entry_field_canonical(field, rival, template_name)
        if rv and _canonical_values_disagree(field, proposed, rv):
            return True
    return False


def _model_rival_competes(winner: dict, row: dict, refs: dict) -> bool:
    """R248 — um rival quase-empatado só é ambiguidade PARA O MODELO se
    competir no próprio modelo escrito (sim >= winner − 0.02). Com o
    código-peça a suportar afirmativamente o winner (containment/tier 0.97),
    um rival a <1 bit no TOTAL mas sem suporte do modelo não pinta a célula
    — critério invariante à escala do plano (em fixtures pequenos w_modelo
    encolhe e o irmão certo caía dentro de _FS_RIVAL_MARGIN_BITS)."""
    value = (row or {}).get("modelo")
    if _is_missing_ocr(value):
        return True  # célula vazia: a ambiguidade R219 mantém-se como era
    w_sim = _efs_compute("modelo", winner, row, refs, value)
    if w_sim is None or w_sim <= 0.0:
        return True
    proposed_des = str(winner.get("designacao") or "").strip().upper()
    for rival in winner.get("_rivals") or []:
        des = str((rival or {}).get("designacao") or "").strip().upper()
        if not des or des == proposed_des:
            continue
        s = _efs_compute("modelo", rival, row, refs, value)
        if s is not None and s >= w_sim - 0.02:
            return True
    return False


def _winner_field_fallback_proposal(
    field: str,
    winner: dict,
    candidates: list[dict],
    row: dict,
    refs: dict,
    idx: dict | None,
    template_name: str | None = None,
) -> str | None:
    """R222/D4 — o winner não tem valor canónico para `field`. Em vez de cair
    logo em MATCH_FORCADO_SEM_CANONICO, procura noutra entry do plano um valor
    que faça sentido para esta linha. Reúne um pool — candidatos top-K do
    campo + rivais quase-empatados + entries com a mesma OF do winner — e
    escolhe a entry de maior afinidade pelos OUTROS campos da linha
    (`_entry_global_score` sem `field`) que tenha esse campo preenchido.
    Devolve esse valor canónico, ou None se nada fizer sentido."""
    pool: list[dict] = []
    for cand in candidates or []:
        pool.extend(cand.get("plan_entries") or [])
    pool.extend(winner.get("_rivals") or [])
    winner_of = winner.get("_of") or winner.get("of")
    if winner_of and idx:
        for entry in _all_plan_entries(idx).values():
            if (entry.get("_of") or entry.get("of")) == winner_of:
                pool.append(entry)
    if not pool:
        return None

    other_fields = frozenset(_PLAN_FIELDS) - {field}
    seen: set[tuple] = set()
    scored: list[tuple[float, int, str]] = []
    _sim_cache: dict = {}
    for entry in pool:
        key = _entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        value = _entry_field_canonical(field, entry, template_name)
        if not value:
            continue
        affinity, exact, _reasons, _raw, _agree, _exact_id, _agree_id = (
            _entry_global_score(entry, row, refs, other_fields, cache=_sim_cache)
        )
        scored.append((float(affinity), int(exact), value))
    if not scored:
        return None

    # Maior afinidade pelos OUTROS campos; desempata por nº de campos exatos.
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    best_affinity, _best_exact, best_value = scored[0]
    # Exige um mínimo de afinidade para não inventar a partir de uma entry
    # aleatória do plano (mesmo limiar do modo `forced`).
    if best_affinity < _MIN_FORCED_WINNER_SCORE:
        return None
    return best_value


def _apply_winner_to_field(
    field: str,
    ocr_value: str,
    winner: dict | None,
    candidates: list[dict],
    refs: dict,
    row: dict,
    has_field_reference: bool,
    template_name: str | None = None,
    idx: dict | None = None,
) -> dict:
    if field in _NO_REF_FIELDS:
        return _score_no_ref_row_cell(field, ocr_value)

    # --- Campos validados directamente contra o StockSAP (R123) ---------
    # O lote e a largura vivem no StockSAP, não na entry do plan_colunas.
    if field == "lote":
        if not ocr_value:
            if winner is not None:
                return _mark_winner_cell(_empty_rule_cell(field, ref_source="sap"), winner)
            return _make_cell("", "NA", "ocr_raw")
        sap_full = refs.get("lotes_sap_full", {}) or {}
        if not sap_full:
            return _make_cell(ocr_value, "very_different", "ocr_raw", ref_source="sap")
        sap_lote, sap_entry = _sap_lote_entry(refs, ocr_value)
        if sap_entry:
            extra = {"ref_source": "sap"}
            if sap_lote and sap_lote != ocr_value.strip().upper():
                extra["proposed"] = sap_lote
            return _mark_winner_cell(_make_cell(ocr_value, "confirmed", "sap", **extra), winner)
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
                return _mark_winner_cell(
                    _finish_cell(field, ocr_value, str(sap_larg), "sap", None),
                    winner,
                )
            return _mark_winner_cell(
                _finish_cell(field, ocr_value, str(sap_larg), "sap", None),
                winner,
            )

    if field == "esp":
        # StockSAP também traz espessura do lote. Se o operador escreveu esp,
        # validamos primeiro contra SAP; se estiver em branco, deixamos a
        # lógica do plan decidir se há confiança suficiente para autofill.
        _sap_lote, sap_e = _sap_lote_entry(refs, row.get("lote"))
        sap_esp = sap_e.get("esp") if sap_e else None
        if ocr_value and sap_esp not in (None, ""):
            return _mark_winner_cell(
                _finish_cell(field, ocr_value, str(sap_esp), "sap", None),
                winner,
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
            # R222 (reverte D8) — designação COMPLETA do plan também no
            # Acabamento (antes usava _model_first_token / código curto).
            des = " ".join(str(winner.get("designacao") or "").split())
            proposed = des if des else (ocr_value or None)
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
        if winner is not None:
            # R222/D4 — winner sem valor canónico para este campo: antes de
            # cair em MATCH_FORCADO_SEM_CANONICO, procura nas outras entries
            # plausíveis (mesma OF do winner, rivais, candidatos top-K) a que
            # melhor combina com os OUTROS campos da linha e usa o valor dela.
            # Se achar, cai no fluxo normal abaixo (guarda de ambiguidade R219
            # + _finish_cell).
            proposed = _winner_field_fallback_proposal(
                field, winner, candidates, row, refs, idx, template_name
            )
        if not proposed:
            if winner is not None:
                if ocr_value:
                    return _mark_winner_cell(
                        _make_cell(
                            _format_value(field, ocr_value),
                            "confirmed",
                            "syntax",
                            ref_source=_field_ref_source(field, refs, row),
                            match_kind="MATCH_FORCADO_SEM_CANONICO",
                            warning="Winner escolhido, mas sem valor canónico para este campo.",
                        ),
                        winner,
                    )
                return _mark_winner_cell(
                    _empty_rule_cell(field, ref_source=_field_ref_source(field, refs, row)),
                    winner,
                )
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

    # R248 — irmãos da mesma OF: o modelo escrito não discrimina qual
    # sub-linha é (família-prefixo, código repetido em 2 irmãos, ou código
    # que não existe em nenhum). Substitui na mesma (R219), mas vermelho +
    # confiança da CÉLULA reduzida pela margem de irmãos — o p_top/margin
    # OF-level (calibração R243) não é tocado. É o que fecha o buraco das
    # trocas silenciosas verdes com p_top 0.93-0.99.
    if (
        field == "modelo"
        and winner is not None
        and proposed
        and _model_sibling_ambiguous(winner, row, refs, idx)
    ):
        proposed_fmt = _format_value(field, proposed)
        p_top = float(winner.get("_p_top") or 0.0)
        conf = round(
            p_top * _sibling_p(float(winner.get("_sibling_margin_bits", 0.0))),
            3,
        )
        return _mark_winner_cell(
            _make_cell(
                proposed_fmt, "very_different", "plan",
                proposed=proposed_fmt, ref_source="plan", score=score,
                decision_confidence=conf,
                decision_reason="ambiguous_sibling_designacao",
            ),
            winner,
        )

    # R219 — guarda de ambiguidade: o winner não é líder claro (rivais
    # quase-empatados DISCORDAM neste campo) e o OCR não confirma o winner
    # (vazio ou diferente). O sistema SUBSTITUI sempre pelo valor da linha
    # vencedora, mas marca `very_different` (vermelho/rever) para o operador
    # conferir qual das linhas possíveis é a certa. Como `source="plan"`
    # (concreta), o valor é auto-aplicado e a célula entra na fila to_analisar.
    if (
        winner is not None
        and proposed
        and _winner_ambiguous_for_field(field, proposed, winner, template_name)
        and (_entry_field_similarity(field, winner, row, refs) or 0.0) < 1.0
        # R248 — para o modelo, um rival só é ambiguidade se COMPETIR no
        # próprio modelo escrito (código-peça a suportar o winner no tier
        # 0.97 não fica vermelho por um rival a <1 bit sem suporte).
        and (field != "modelo" or _model_rival_competes(winner, row, refs))
    ):
        proposed_fmt = _format_value(field, proposed)
        return _mark_winner_cell(
            _make_cell(
                proposed_fmt, "very_different", "plan",
                proposed=proposed_fmt, ref_source="plan", score=score,
            ),
            winner,
        )

    if field == "cliente" and ocr_value:
        if winner is not None:
            return _mark_winner_cell(
                _finish_cell(field, ocr_value, proposed, "plan", score),
                winner,
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
            # R236 — identificador escrito que EXISTE no plano e diverge do
            # winner: substitui (R219) mas fica vermelho para revisão.
            # R241/C2 — a UI distingue a natureza provável do erro: mesma
            # família → provável erro HUMANO de transcrição (cartão errado);
            # senão conflito de identidade genérico (provável misread).
            if _identifier_points_to_other_reference(field, ocr_value, proposed, idx):
                proposed_fmt = _format_value(field, proposed)
                reason = (
                    "possible_wrong_transcription"
                    if winner.get("_written_of_same_family")
                    else "identifier_conflict"
                )
                return _mark_winner_cell(
                    _make_cell(
                        proposed_fmt, "very_different", "plan",
                        proposed=proposed_fmt, ref_source="plan", score=score,
                        decision_reason=reason,
                    ),
                    winner,
                )
            return _mark_winner_cell(
                _finish_cell(field, ocr_value, proposed, "plan", score),
                winner,
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
        return _mark_winner_cell(
            _finish_cell(field, ocr_value, proposed, "plan", score),
            winner,
        )

    proposed_fmt = _format_value(field, proposed)
    ocr_fmt = _format_value(field, ocr_value)
    if proposed_fmt and ocr_fmt and proposed_fmt.upper() == ocr_fmt.upper():
        return _mark_winner_cell(
            _make_cell(proposed_fmt, "confirmed", source="plan", score=score),
            winner,
        )

    return _mark_winner_cell(
        _finish_cell(
            field, ocr_value, proposed,
            source="plan",
            score=score,
        ),
        winner,
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
    force_top1: bool = True,
    trace_sink: list | None = None,
    extra_bias: dict | None = None,
) -> tuple[dict, int, int, int, int, int]:
    """R123 / R125 — itera os `row_fields` do template (não os 10 fixos
    do bobine).

    Cada campo cai num de três tratamentos:
      - campo com referência no plan/SAP (_ROW_FIELDS) → winner/candidatos;
      - campo sem referência (_NO_REF_FIELDS: pri, qtd, ...) → regra local;
      - campo próprio do template sem referência (cesta_n, m2, sobras, ...) →
        regra local, para não deixar valores preenchidos neutros.

    R125 (restaurado R222): quando `current_phase` é passado, desempata o
    winner entre linhas ainda com espaço nessa fase e, se TODAS as linhas
    candidatas estiverem fechadas nessa fase, marca a linha inteira como
    "obra_concluída" — todos os campos passam a `very_different` com
    `source="obra_concluida"`, sinalizando vermelho na UI e bloqueando a
    auto-substituição. (R222 reverte o R163, que tornara isto só metadata.)
    """
    cc_fields = set(cross_check_fields)
    score_fields = cc_fields & set(_PLAN_FIELDS)
    wt: dict | None = {} if trace_sink is not None else None

    # R236 — realinhamento por hipóteses: o OCR pode ter lido nas colunas
    # erradas (OF na OV, modelo na OF, cliente no modelo, OF embebida em
    # texto). Cada variante plausível é pontuada pelo MESMO scoring FS e ganha
    # a de maior evidência (prior de -1.5 bits por campo movido; a linha
    # tal-qual ganha empates). Devolve também os candidatos da variante
    # escolhida — o resto do fluxo continua idêntico.
    _align_label, row, candidates_by_field = _choose_row_alignment(
        row, refs, idx, cc_fields, current_phase, score_fields,
        template_name, trace=wt, extra_bias=extra_bias,
    )
    winner = _find_winner_entry(
        candidates_by_field,
        row,
        refs,
        idx,
        current_phase,
        score_fields,
        force_top1=force_top1,
        trace=wt,
        extra_bias=extra_bias,
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
            result = _score_ferramenta_cell(ocr_value, row_has_winner=winner is not None)
        elif field in _ROW_FIELDS and field in cc_fields:
            result = _apply_winner_to_field(
                field, ocr_value, winner,
                candidates_by_field.get(field, []), refs, row,
                _has_field_reference_pool(field, refs, idx, row),
                template_name=template_name,
                idx=idx,
            )
        elif field in _NO_REF_FIELDS:
            result = _score_no_ref_row_cell(
                field, ocr_value, row_has_winner=winner is not None
            )
        else:
            # Campo próprio do template, sem referência no plan/SAP: aplica
            # regra local para evitar NA neutro em valor preenchido.
            result = _score_no_ref_row_cell(
                field, ocr_value, row_has_winner=winner is not None
            )
        fields_out[field] = result
        _tally(result["status"])

    # Campos extra que o OCR leu fora do schema do template — aplicar regra
    # local se houver valor, senão NA.
    for k, v in row.items():
        if k in fields_out:
            continue
        extra_value = str(v) if v is not None else ""
        if k in _NO_REF_FIELDS:
            extra_cell = _score_no_ref_row_cell(
                k, extra_value.strip(), row_has_winner=winner is not None
            )
        else:
            extra_cell = _score_no_ref_row_cell(
                k, extra_value.strip(), row_has_winner=winner is not None
            )
        fields_out[k] = extra_cell
        _tally(extra_cell["status"])

    # R125 (restaurado R222 — reverte R163): obra concluída na fase força a
    # linha inteira a `very_different` / `source="obra_concluida"`. Isso pinta
    # vermelho na UI e bloqueia a auto-substituição em `_maybe_apply_snap`
    # (que ignora `source=="obra_concluida"`).
    if obra_concluida:
        snapped = confirmed = na = very_diff = 0
        for fn, cell in fields_out.items():
            fields_out[fn] = _make_cell(
                cell.get("value", ""), "very_different", "obra_concluida",
            )
            very_diff += 1

    # R224 — profiling: traço da pontuação de match desta linha (candidatos,
    # vencedor, e o que cada campo decidiu/substituiu). Só quando pedido.
    if trace_sink is not None:
        decisions = []
        for fn in row_fields:
            if fn not in cc_fields:
                continue
            cell = fields_out.get(fn) or {}
            decisions.append({
                "field": fn,
                "ocr_value": str(row.get(fn) or "").strip(),
                "final_value": str(cell.get("value") or ""),
                "engine_status": cell.get("status"),
                "source": cell.get("source"),
                "match_kind": cell.get("match_kind"),
            })
        trace_sink.append({
            "row_index": row_idx,
            "pool_size": (wt or {}).get("pool_size"),
            "candidates_by_field": (wt or {}).get("candidates_by_field"),
            "fallback_full_scan": (wt or {}).get("fallback_full_scan", False),
            "winner_of": (winner or {}).get("_of") if winner else None,
            "winner_combined": (winner or {}).get("_combined") if winner else None,
            "winner_mode": (winner or {}).get("_winner_mode") if winner else None,
            "obra_concluida": obra_concluida,
            "candidates": (wt or {}).get("candidates", []),
            "decisions": decisions,
        })

    total = snapped + confirmed + na + very_diff
    row_out = {
        "row_index": row_idx,
        "fields": fields_out,
        "winner_of": (winner or {}).get("_of") if winner else None,
        "winner_score": (winner or {}).get("_score") if winner else None,
        "winner_weighted_score": (winner or {}).get("_weighted_score") if winner else None,
        "winner_combined": (winner or {}).get("_combined") if winner else None,
        # R236 — evidência FS (bits) e margem para o melhor rival de OF
        # diferente; é isto que decide winner_mode (decisivo/marginal).
        "winner_bits": (winner or {}).get("_bits") if winner else None,
        "winner_margin_bits": (winner or {}).get("_margin_bits") if winner else None,
        # R248 — margem para o melhor IRMÃO da mesma OF (telemetria/harness).
        "winner_sibling_margin_bits": (
            (winner or {}).get("_sibling_margin_bits") if winner else None
        ),
        "winner_p_top": (winner or {}).get("_p_top") if winner else None,
        "winner_score_reasons": (winner or {}).get("_score_reasons") if winner else None,
        "winner_mode": (winner or {}).get("_winner_mode") if winner else None,
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


def _derive_footer_values(rows: list[dict], footer: dict) -> dict[str, str]:
    derived: dict[str, str] = {}
    if not str((footer or {}).get("colunas_produzidas") or "").strip():
        total_qtd = 0
        found = False
        for row in rows or []:
            qtd = _num(row.get("qtd"))
            if qtd is None or qtd < 0 or abs(qtd - round(qtd)) > 1e-9:
                continue
            total_qtd += int(round(qtd))
            found = True
        if found:
            derived["colunas_produzidas"] = str(total_qtd)
    return derived


def _score_header_footer(
    header: dict,
    footer: dict,
    refs: dict,
    header_fields: tuple[str, ...] = (),
    footer_fields: tuple[str, ...] = (),
    derived_footer: dict | None = None,
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
            if derived_footer and field in derived_footer:
                return _make_cell(
                    str(derived_footer[field]),
                    "snapped",
                    "syntax",
                    ref_source="syntax",
                    match_kind="MATCH_REGRA_DERIVADO",
                )
            if field in _NO_REF_FIELDS:
                return _empty_rule_cell(field)
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
    trace_sink: list | None = None,
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

    # R242/D1 — prior de PRODUÇÃO: OFs com atividade validada recente (janela
    # 14d, estritamente antes de hoje) recebem o bias medido (+2.0/-1.77 bits;
    # P(ativa|verdadeira)=71.2% vs 2.2% aleatória — quant6). Sem DB/produção →
    # None (testes e arranque ficam byte-idênticos).
    prod_bias: dict | None = None
    try:
        from app.pipeline.of_consumption import recent_active_ofs

        active = recent_active_ofs()
        if active:
            q6 = (_load_cross_params().get("quant6_production_prior") or {})
            ab = float(q6.get("production_prior_bits") or 2.0)
            ib = float(q6.get("production_prior_inactive_bits") or -1.77)
            prod_bias = {"of": {k: ab for k in active}, "of_default": ib}
    except Exception:  # noqa: BLE001 — prior é opcional por construção
        prod_bias = None
    # R245 — operador da folha: seleciona o canal de chars por operador
    # quando fitted (fallback global; sem efeito até haver >=300 pares dele).
    _op = str((header or {}).get("operador") or "").strip().upper()
    if _op:
        prod_bias = dict(prod_bias or {})
        prod_bias["operator"] = _op

    out_rows = []
    row_tallies: list[tuple[int, int, int, int]] = []
    for i, row in enumerate(rows):
        row_out, s, c, n, vd, _t = _score_row(
            i, row, refs, idx, row_fields, cross_check_fields,
            current_phase, canonical_template_name,
            force_top1=getattr(template, "has_production_rows", True),
            trace_sink=trace_sink,
            extra_bias=prod_bias,
        )
        out_rows.append(row_out)
        row_tallies.append((s, c, n, vd))

    # R242/D2 — COERÊNCIA DE FOLHA (passe 2): linhas com winner marginal são
    # re-pontuadas com o bias dos vizinhos ADJACENTES confiantes (lift medido
    # quant5: mesma OF adjacente 21× → +4.42 bits, mesmo cliente 7.9× → +2.98;
    # ambos cap +2.0 — o contexto quebra empates, nunca vence evidência real).
    q5 = (_load_cross_params().get("quant5_sheet_coherence") or {})
    coh_of_bits = min(2.0, float(q5.get("coherence_of_bits") or 0.0))
    coh_cli_bits = min(2.0, float(q5.get("coherence_cliente_bits") or 0.0))
    if coh_of_bits > 0 and len(out_rows) > 1:
        def _confident(ro: dict) -> bool:
            return bool(
                ro.get("winner_of")
                and float(ro.get("winner_margin_bits") or 0.0) >= _FS_MARGIN_DECISIVE
            )

        for i, ro in enumerate(out_rows):
            if _confident(ro) or not _row_has_any_value(rows[i]):
                continue
            coh_of: dict[str, float] = {}
            coh_cli: dict[str, float] = {}
            for j in (i - 1, i + 1):
                if 0 <= j < len(out_rows) and _confident(out_rows[j]):
                    n_of = str(out_rows[j].get("winner_of") or "")
                    if n_of:
                        coh_of[n_of] = coh_of_bits
                    n_cli = _cliente_compact(
                        ((out_rows[j].get("fields") or {}).get("cliente") or {}).get("value")
                    )
                    if n_cli and coh_cli_bits > 0:
                        coh_cli[n_cli] = coh_cli_bits
            if not coh_of and not coh_cli:
                continue
            bias2 = dict(prod_bias or {})
            if coh_of:
                bias2["coh_of"] = coh_of
            if coh_cli:
                bias2["coh_cliente"] = coh_cli
            row_out, s, c, n, vd, _t = _score_row(
                i, rows[i], refs, idx, row_fields, cross_check_fields,
                current_phase, canonical_template_name,
                force_top1=getattr(template, "has_production_rows", True),
                trace_sink=trace_sink,
                extra_bias=bias2,
            )
            out_rows[i] = row_out
            row_tallies[i] = (s, c, n, vd)

    # R249 — COLISÕES DE ATRIBUIÇÃO (passe 3): >=2 linhas da MESMA folha com
    # núcleos de modelo escritos DISTINTOS (pós-strip A/B: '742A'+'742B' são
    # o MESMO núcleo — partes legítimas da mesma peça) a cair na MESMA
    # designação do mesmo OF (58 casos históricos em 41 folhas). Conservador:
    # só o membro NÃO-exato (o "boleia" fuzzy/canal) desce para revisão; um
    # membro com match pleno do código mantém-se — e se todos são exatos é
    # uso de aliases da mesma entry (designações têm 2 tokens-código), legit.
    # Só muda COR/confiança, nunca o valor (R219).
    if len(out_rows) > 1:
        _coll: dict[tuple[str, str], list[tuple[int, str, bool]]] = {}
        for i, ro in enumerate(out_rows):
            cell = (ro.get("fields") or {}).get("modelo") or {}
            w_of = str(ro.get("winner_of") or "")
            des = str(cell.get("value") or "").strip().upper()
            raw_mod = str((rows[i] or {}).get("modelo") or "")
            if not (w_of and des and raw_mod.strip()):
                continue
            pure, ab = _model_code_cores_cached(raw_mod)
            core = ab[0] if ab else (pure[0] if pure else _model_compact(raw_mod))
            if len(core) < 4:
                continue
            exact = _model_matches_designacao(raw_mod, des) or bool(
                ab and any(v in _model_compact(des)
                           for c in ab for v in _o_zero_variants(c))
            )
            _coll.setdefault((w_of, des), []).append((i, core, exact))
        for (_w_of, _des), members in _coll.items():
            if len(members) < 2 or len({c for _, c, _e in members}) < 2:
                continue
            for i, _core, exact in members:
                if exact:
                    continue
                cell = (out_rows[i].get("fields") or {}).get("modelo") or {}
                if cell.get("status") not in ("snapped", "confirmed"):
                    continue
                st_old = cell.get("status")
                new_cell = dict(
                    cell,
                    status="very_different",
                    label=_STATUS_LABELS["very_different"],
                    decision_reason="sibling_collision",
                )
                conf = new_cell.get("decision_confidence")
                new_cell["decision_confidence"] = (
                    min(float(conf), 0.5) if conf is not None else 0.5
                )
                out_rows[i]["fields"]["modelo"] = new_cell
                s, c, n, vd = row_tallies[i]
                if st_old == "snapped":
                    row_tallies[i] = (s - 1, c, n, vd + 1)
                else:
                    row_tallies[i] = (s, c - 1, n, vd + 1)

    snapped = sum(t[0] for t in row_tallies)
    confirmed = sum(t[1] for t in row_tallies)
    na = sum(t[2] for t in row_tallies)
    very_diff = sum(t[3] for t in row_tallies)

    # R123 (B9) — header/footer validados (operador vs ListaColaboradores,
    # máquina, etc.) em vez de forçados a NA; os estados contam como
    # qualquer outra célula.
    header_out, footer_out = _score_header_footer(
        header, footer, refs,
        header_fields=getattr(template, "header_fields", ()),
        footer_fields=getattr(template, "footer_fields", ()),
        derived_footer=_derive_footer_values(rows, footer),
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

    Propaga `score` e usa `proposed` como `ref` para tooltip de referência.
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
    if v5_cell.get("match_kind"):
        out["match_kind"] = v5_cell.get("match_kind")
    for key in ("winner_mode", "score_reasons", "forced_from_status", "warning",
                "empty_ok", "decision_confidence", "decision_reason"):
        if key in v5_cell:
            out[key] = v5_cell[key]
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
    collect_trace: bool = False,
) -> dict:
    """R109 — Entry point oficial. Wraps shadow_score, devolve output no
    formato legacy esperado pela UI (status MATCH/NO_MATCH/NA, summary,
    rows, header, footer, to_analisar).

    R224 — `collect_trace=True` devolve também `result["trace"]` (traço de
    match por linha: pool, candidatos pontuados, vencedor, decisões). Não é
    guardado no JSON do cross (o `store_cross_check` ignora a chave); serve o
    profiling. Sem o flag, custo zero.
    """
    trace_sink: list | None = [] if collect_trace else None
    scoring, _total, _snapped, _confirmed, _na, duration_ms = shadow_score(
        sheet_data, dq_audit, refs, trace_sink=trace_sink,
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
        # R243/E2 — prioridade da fila: incerteza × criticidade do campo.
        # O minuto humano gasto onde rende mais (esp/comp > identidade > resto).
        conf = legacy_cell.get("decision_confidence")
        uncertainty = 1.0 - float(conf) if conf is not None else 0.5
        crit = _REVIEW_CRITICALITY.get(field, 1.0)
        return {
            "section": section,
            "row_index": row_index,
            "field": field,
            "field_path": field_path,
            "value": legacy_cell.get("value", "") if raw_value is None else raw_value,
            "ref": legacy_cell.get("ref", ""),
            "ref_source": ref_source,
            "reason": reason,
            "decision_confidence": conf,
            "review_priority": round(crit * uncertainty, 3),
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
            "winner_weighted_score": r.get("winner_weighted_score"),
            "winner_combined": r.get("winner_combined"),
            "winner_score_reasons": r.get("winner_score_reasons"),
            "winner_mode": r.get("winner_mode"),
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

    # R243/E2 — fila ordenada por prioridade (incerteza × criticidade).
    to_analisar.sort(key=lambda it: -float(it.get("review_priority") or 0.0))

    result = {
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
    if trace_sink is not None:
        result["trace"] = trace_sink
    return result


__all__ = ["shadow_score", "cross_check_sheet", "CROSS_CHECK_STATUSES",
           "score_entry", "select_winner", "normalize_of", "ENGINE_VERSION"]
