"""
Pipeline Kanban OCR — passo único:
  qwen3.5:9b faz OCR + extração estruturada numa só chamada
  Output: CSV com 3 blocos (CABEÇALHO / TABELA / RODAPÉ)

PATCHED 2026-05-03 (Round 26):
  Adicionado "think": False no payload Ollama. Sem isto, qwen3.5:9b
  emite thinking blocks que poluem ou invalidam o JSON output e
  causam ~47% de failures (medido em 19 fotos: 9 falham completamente).
  Com este fix: 19/19 photos parseiam, latência cai de ~57s para ~22s.
"""
import base64, json, re, sys, csv, urllib.request
from datetime import datetime
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL      = "qwen3.5:9b"   # ajusta se o nome for diferente (qwen3.5:4b, qwen3.5:9b, qwen3:9b, qwen2.5:7b, etc.)

# ── Prompt ────────────────────────────────────────────────────────────────────
# Prompt v3 (canónico, R23+): mais detalhe que original = melhor accuracy
# em sheets longas e menos JSON failures (reduz de 16/19 para 19/19 OK
# nas 19 fotos de teste).
PROMPT = """You are an OCR and data extraction expert. Analyze this industrial Kanban production sheet image carefully.

Extract ALL data and return ONLY a valid JSON object. No markdown, no <think> blocks, no explanation.

The sheet has:
1. HEADER fields: Operador (full name), N° (operator number), Setor/Maquina, Data
2. A TABLE with exactly these columns in order:
   PRI | CLIENTE | OV | OF | MODELO | QTD | COMP_MM | LARG_MM | LOTE | CONI | ESP | LBASE | LTOPO
3. FOOTER fields: Colunas Produzidas, Horas Trabalhadas

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
}"""

# ── Utilitários ───────────────────────────────────────────────────────────────
def fmt_dur(s): return f"{s:.1f}s" if s < 60 else f"{int(s)//60}m {int(s)%60:02d}s"
def fmt_dt(dt): return dt.strftime("%d/%m/%Y %H:%M:%S")
def sep(c="─", w=64): print(c * w)

def ollama_request(image_b64, timeout=1000):
    payload = {
        "model": MODEL,
        "prompt": PROMPT,
        "images": [image_b64],
        "stream": False,
        "keep_alive": -1,
        "think": False,                                   # <<< PATCH: desliga reasoning blocks
        "options": {"temperature": 0, "num_predict": 8192}
    }
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(OLLAMA_URL, data=data,
           headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("response","").strip()
    except urllib.error.HTTPError as e:
        print(f"    [ERRO HTTP {e.code}] '{MODEL}'" +
              (" — corre --list-models" if e.code == 404 else ""))
        return None
    except urllib.error.URLError as e:
        print(f"    [ERRO] Ollama: {e}"); return None

def image_to_base64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def clean_json(raw):
    """Remove <think>...</think> e markdown fences, extrai JSON."""
    text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"): text = part; break
    s, e = text.find("{"), text.rfind("}") + 1
    return json.loads(text[s:e]) if s != -1 and e > s else json.loads(text)

# ── ESCREVER CSV COM 3 BLOCOS ─────────────────────────────────────────────────
def write_csv(output_csv, all_results):
    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")

        # BLOCO 1 — CABEÇALHO
        w.writerow(["### CABEÇALHO"])
        w.writerow(["FICHEIRO","DATA","OPERADOR","N_OPERADOR","SETOR_MAQUINA"])
        for r in all_results:
            h = r["header"]
            w.writerow([r["file"], h.get("data",""), h.get("operador",""),
                        h.get("n_operador",""), h.get("setor_maquina","")])
        w.writerow([])

        # BLOCO 2 — TABELA
        w.writerow(["### TABELA DE PRODUÇÃO"])
        w.writerow(["FICHEIRO","DATA","OPERADOR",
                    "PRI","CLIENTE","OV","OF","MODELO",
                    "QTD","COMP_MM","LARG_MM","LOTE","CONI","ESP","LBASE","LTOPO"])
        for r in all_results:
            h = r["header"]
            for row in r["rows"]:
                w.writerow([
                    r["file"], h.get("data",""), h.get("operador",""),
                    row.get("pri",""),    row.get("cliente",""),
                    row.get("ov",""),     row.get("of",""),
                    row.get("modelo",""), row.get("qtd",""),
                    row.get("comp_mm",""),row.get("larg_mm",""),
                    row.get("lote",""),   row.get("coni",""),
                    row.get("esp",""),    row.get("lbase",""),
                    row.get("ltopo",""),
                ])
        w.writerow([])

        # BLOCO 3 — RODAPÉ
        w.writerow(["### RODAPÉ"])
        w.writerow(["FICHEIRO","DATA","OPERADOR","COLUNAS_PRODUZIDAS","HORAS_TRABALHADAS"])
        for r in all_results:
            h, ft = r["header"], r["footer"]
            w.writerow([r["file"], h.get("data",""), h.get("operador",""),
                        ft.get("colunas_produzidas",""), ft.get("horas_trabalhadas","")])

# ── PIPELINE POR IMAGEM ───────────────────────────────────────────────────────
def process_image(image_path, idx, total):
    name = Path(image_path).name
    perf = {"file":name, "rows":0, "status":"ok"}

    sep()
    print(f"  [{idx}/{total}] {name}")
    sep()

    t0 = datetime.now()
    print(f"  ▶ {MODEL}")
    print(f"    Início : {fmt_dt(t0)}")

    raw = ollama_request(image_to_base64(image_path))

    t1 = datetime.now()
    dur = (t1-t0).total_seconds()
    perf["dur"] = fmt_dur(dur)
    print(f"    Fim    : {fmt_dt(t1)}")
    print(f"    Duração: {fmt_dur(dur)}")

    if not raw:
        perf["status"] = "erro_modelo"; return None, perf

    # Mostra resposta raw
    print()
    print("  ── RESPOSTA DO MODELO " + "─"*42)
    for line in raw.splitlines()[:40]:   # primeiras 40 linhas para não encher o ecrã
        print(f"  {line}")
    if len(raw.splitlines()) > 40:
        print(f"  ... (+{len(raw.splitlines())-40} linhas)")
    print("  " + "─"*63)
    print()

    try:
        extracted = clean_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"    [ERRO] JSON inválido: {e}")
        print(f"    Raw (primeiros 300 chars): {raw[:300]}")
        perf["status"] = "erro_json"; return None, perf

    rows = extracted.get("rows", [])
    perf["rows"]   = len(rows)
    perf["status"] = "ok" if rows else "sem_dados"

    print(f"  Linhas: {len(rows)}  |  Total: {fmt_dur(dur)}")
    return {"file":name, **extracted}, perf

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    if "--list-models" in sys.argv:
        try:
            req = urllib.request.Request("http://192.168.29.14:11434/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                models = json.loads(r.read().decode("utf-8")).get("models",[])
                sep("═"); [print(f"    {m.get('name')}") for m in models]; sep("═")
        except Exception as e: print(f"Erro: {e}")
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Uso:")
        print("  python kanban_ocr.py imagem.jpg")
        print("  python kanban_ocr.py img1.jpg img2.jpg img3.jpg")
        print(r"  python kanban_ocr.py D:\kanban\ ")
        print("  python kanban_ocr.py --list-models")
        sys.exit(1)

    image_paths = []
    if len(args) == 1 and Path(args[0]).is_dir():
        folder = Path(args[0])
        exts   = {".jpg",".jpeg",".png",".bmp",".tiff",".webp"}
        image_paths = sorted([str(p) for p in folder.iterdir() if p.suffix.lower() in exts])
    else:
        image_paths = [a for a in args if Path(a).exists()]
        for a in args:
            if not Path(a).exists(): print(f"[AVISO] Não encontrado: {a}")

    if not image_paths:
        print("[ERRO] Nenhuma imagem."); sys.exit(1)

    # Pasta processados
    base_dir        = Path(image_paths[0]).parent
    processados_dir = base_dir / "processados"
    processados_dir.mkdir(exist_ok=True)

    t_global = datetime.now()
    sep("═")
    print("  KANBAN OCR — INÍCIO")
    print(f"  Data/Hora  : {fmt_dt(t_global)}")
    print(f"  Imagens    : {len(image_paths)}")
    print(f"  Modelo     : {MODEL}")
    print(f"  Processados: {processados_dir}")
    sep("═")

    all_results, all_perfs = [], []
    for i, img in enumerate(image_paths, 1):
        result, perf = process_image(img, i, len(image_paths))
        if result:
            all_results.append(result)
            csv_path = str(Path(img).with_suffix(".csv"))
            write_csv(csv_path, [result])
            print(f"  → CSV: {Path(csv_path).name}")
            dest = processados_dir / Path(img).name
            Path(img).rename(dest)
            print(f"  → Movido: processados/{Path(img).name}")
        else:
            print(f"  → [AVISO] Sem dados — NÃO movido: {Path(img).name}")
        all_perfs.append(perf)

    t_end     = datetime.now()
    total_dur = (t_end-t_global).total_seconds()

    # Relatório
    sep("═"); print("  RELATÓRIO DE PERFORMANCE"); sep("═")
    print(f"  {'Ficheiro':<40} {'Linhas':>6}  {'Duração':>8}  Status")
    sep()
    for p in all_perfs:
        icon = "✓" if p["status"]=="ok" else "✗"
        print(f"  {p['file']:<40} {p.get('rows',0):>6}  "
              f"{p.get('dur','—'):>8}  {icon} {p['status']}")
    sep()
    ok = sum(1 for p in all_perfs if p["status"]=="ok")
    print(f"  Imagens: {len(all_perfs)}  ({ok} OK, {len(all_perfs)-ok} erro(s))")
    print(f"  Linhas : {sum(p.get('rows',0) for p in all_perfs)}")
    sep("═"); print("  RESUMO TEMPORAL"); sep("═")
    print(f"  Início  : {fmt_dt(t_global)}")
    print(f"  Fim     : {fmt_dt(t_end)}")
    print(f"  Duração : {fmt_dur(total_dur)}")
    if len(image_paths) > 1:
        print(f"  Média   : {fmt_dur(total_dur/len(image_paths))} por imagem")
    sep("═")
    if ok:
        print(f"  CSVs gerados : {ok}")
        print(f"  Total linhas : {sum(p.get('rows',0) for p in all_perfs)}")
        print(f"  Movidos para : processados/")
    else:
        print("  [AVISO] Nenhum dado extraído.")
        print(f"  Verifica o modelo com: python kanban_ocr.py --list-models")
    sep("═")

if __name__ == "__main__":
    main()
