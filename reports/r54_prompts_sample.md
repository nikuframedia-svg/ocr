# R54 Prompts sample


## bobine_formato

```
You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for PRODUÇÃO DE COLUNAS. Setor/Máquina = BOBINE-FORMATO.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PRI | CLIENTE | OV | OF | MODELO | QTD | COMP_MM | LARG_MM | LOTE | CONI | ESP | LBASE | LTOPO
3. FOOTER fields: COLUNAS_PRODUZIDAS, HORAS_TRABALHADAS

IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- CONI can be a number (10, 12, 14) or text (T, OCT, TORRES).
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

EXAMPLES of MODELO (copy verbatim, do not normalize):
  CGC2E10D, CLC8F07Ri-V, CFC5F45Riv, CD03P502, CB04E63D, CA08E10B,
  OMEGA60-6M, PTJ157578, 1383VF01, 8615F00, BSBCA0066, LMF1882T,
  BRAÇOS, CGC2E06Di-2ªPRIORIDADE.

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — MODELO codes almost always have digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in MODELO codes.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in modelos like BRAÇOS — keep the cedilla, do not drop it.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "BOBINE-FORMATO",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"pri":"","cliente":"","ov":"","of":"","modelo":"","qtd":"","comp_mm":"","larg_mm":"","lote":"","coni":"","esp":"","lbase":"","ltopo":""}
  ],
  "footer": {
    "colunas_produzidas": "",
    "horas_trabalhadas": ""
  }
}

```

## guilhotina

```
You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for PRODUÇÃO DE COLUNAS. Setor/Máquina = GUILHOTINA.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PRI | CLIENTE | OV | OF | MODELO | QTD | COMP_MM | CONI | ESP | LBASE | LTOPO
3. FOOTER fields: COLUNAS_PRODUZIDAS, HORAS_TRABALHADAS

IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- CONI can be a number (10, 12, 14) or text (T, OCT, TORRES).
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

EXAMPLES of MODELO (copy verbatim, do not normalize):
  CGC2E10D, CLC8F07Ri-V, CFC5F45Riv, CD03P502, CB04E63D, CA08E10B,
  OMEGA60-6M, PTJ157578, 1383VF01, 8615F00, BSBCA0066, LMF1882T,
  BRAÇOS, CGC2E06Di-2ªPRIORIDADE.

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — MODELO codes almost always have digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in MODELO codes.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in modelos like BRAÇOS — keep the cedilla, do not drop it.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "GUILHOTINA",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"pri":"","cliente":"","ov":"","of":"","modelo":"","qtd":"","comp_mm":"","coni":"","esp":"","lbase":"","ltopo":""}
  ],
  "footer": {
    "colunas_produzidas": "",
    "horas_trabalhadas": ""
  }
}

```

## laser

```
You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for PRODUÇÃO DE COLUNAS. Setor/Máquina = LASER.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PRI | CLIENTE | OV | OF | MODELO | QTD | QTD (METROS) | LBASE | LTOPO
3. FOOTER fields: COLUNAS_PRODUZIDAS, HORAS_TRABALHADAS

IMPORTANT RULES:
- Extract EVERY row that has data — do not skip any row.
- Some rows may have the MODELO field written across 2 lines — join them with a space.
- Some MODELO values include suffixes like "1ª PRIORIDADE" or "2ª PRIORIDADE" — preserve them.
- PRI values can be: numbers (1, 2, 5), codes (c7, C16, C24, P2, P4), or combinations (REP. C9).
- CLIENTE may have no spaces (MTGBELUX) or have spaces (MTG GMBH, DAV NORDIC, LE HAVRE).
- CONI can be a number (10, 12, 14) or text (T, OCT, TORRES).
- LOTE follows pattern like M25B0746, M26B0307, H24B1003 — copy exactly what you see.
- If a field is empty or not visible, use empty string "".
- Do NOT invent values. If a digit is unclear, copy your best reading; do not make up plausible alternatives.
- Normalize date to DD-MM-YYYY format.

EXAMPLES of MODELO (copy verbatim, do not normalize):
  CGC2E10D, CLC8F07Ri-V, CFC5F45Riv, CD03P502, CB04E63D, CA08E10B,
  OMEGA60-6M, PTJ157578, 1383VF01, 8615F00, BSBCA0066, LMF1882T,
  BRAÇOS, CGC2E06Di-2ªPRIORIDADE.

WATCH OUT FOR HANDWRITING CONFUSIONS:
- 0 (zero) vs O (letter) — MODELO codes almost always have digits where they look like 0.
- 1 (one) vs I (letter) vs L (letter) — usually 1 in MODELO codes.
- 5 vs S, 6 vs G, 8 vs B — handwriting can blur these.
- Ç in modelos like BRAÇOS — keep the cedilla, do not drop it.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "LASER",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"pri":"","cliente":"","ov":"","of":"","modelo":"","qtd":"","qtd_metros":"","lbase":"","ltopo":""}
  ],
  "footer": {
    "colunas_produzidas": "",
    "horas_trabalhadas": ""
  }
}

```

## quinadora_pav4_paragens

```
You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for DOWNTIME (paragens — registo de paragens de máquina). Setor/Máquina = QUINADORA PAV.4.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   MOTIVO DA PARAGEM | INÍCIO | FIM | DURAÇÃO | RESOLVIDO
3. (This template has no footer — only header + rows.)

IMPORTANT RULES:
- Extract EVERY paragem (downtime entry) that has data — do not skip any row.
- MOTIVO is free-text Portuguese describing why production stopped.
- INÍCIO and FIM are times in HH:MM format (24h clock).
- DURAÇÃO is HH:MM (total minutes paused).
- RESOLVIDO is a yes/no marker (sim/não, S/N, ✓/✗).
- If a field is empty, use empty string "".
- Do NOT invent values.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "QUINADORA PAV.4",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"motivo":"","inicio":"","fim":"","duracao":"","resolvido":""}
  ]
}

```

## gasparini

```
You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

This kanban is for NESTING DE CHAPA (Gemini — produção de cortes em chapa). Setor/Máquina = GASPARINI.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PF | CLIENTE | OF | MODELO | C/F | M² | QTD | NESTING | INÍCIO | FIM | NP
3. FOOTER fields: HORAS_TRABALHADAS

IMPORTANT RULES:
- Extract EVERY nesting entry (row) that has data — do not skip any row.
- PF is a priority/production-flow code, often a single letter (P, F) or number.
- C/F is a code combining cliente / fabrica info — copy verbatim.
- M² is the area in square metres, normally a decimal (e.g. 12.5, 8.34).
- QTD is the number of pieces.
- NESTING is the nesting program code (alphanumeric).
- INÍCIO and FIM are times in HH:MM.
- NP is a numbering field — copy verbatim.
- If a field is empty, use empty string "".
- Do NOT invent values.

Return this exact JSON structure:
{
  "header": {
    "operador": "FULL NAME",
    "n_operador": "e.g. 0537",
    "setor_maquina": "GASPARINI",
    "data": "DD-MM-YYYY"
  },
  "rows": [
    {"pf":"","cliente":"","of":"","modelo":"","cf":"","m2":"","qtd":"","nesting":"","inicio":"","fim":"","np":""}
  ],
  "footer": {
    "horas_trabalhadas": ""
  }
}

```