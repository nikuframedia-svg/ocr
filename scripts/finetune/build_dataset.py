"""R105 — constroi o dataset de fine-tuning a partir do data/app.db.

Junta cada foto de kanban com a leitura JA CORRIGIDA pelo humano e escreve
``data/_finetune/train.jsonl`` + ``eval.jsonl`` (+ imagens redimensionadas).
Corre na maquina que tem o ``data/app.db`` (portatil agora, PC da Metalogalva
depois do clone).

Decisoes (ver plano R105):
- Folhas usadas: ``status='extracted'`` com ``raw_extraction`` e >=1 edicao
  HUMANA e imagem existente.
- Alvo (a "resposta certa") = ``raw_extraction`` com APENAS as edicoes
  ``source='human'`` aplicadas. As edicoes ``source='system'`` (snap de
  modelo/cliente para o canonico do plano) NAO entram — o OCR deve aprender
  a ler o papel, nao o valor do plano.
- Imagem redimensionada com o MESMO resize do ocr6.py (lado max 1288 px,
  multiplo de 28, JPEG q90) — para treinar no que o modelo ve em producao.
- Prompt = prompts/ocr6_v3.txt (bobine_formato) ou prompt_builder por template.

Uso:
    .venv\\Scripts\\python data\\_logs\\build_dataset.py
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from pathlib import Path

from PIL import Image

# scripts/finetune/build_dataset.py  ->  <repo root>
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "backend"))

import ocr6  # noqa: E402  — fonte unica das constantes de resize

_DB_PATH = _REPO / "data" / "app.db"
_IMAGES_DIR = _REPO / "data" / "images"
_OUT_DIR = _REPO / "data" / "_finetune"
_OUT_IMAGES = _OUT_DIR / "images"
_PROMPT_BOBINE_PATH = _REPO / "prompts" / "ocr6_v3.txt"

_EVAL_COUNT = 30   # folhas postas de parte para o exame final
_SEED = 105        # split determinístico — sempre o mesmo train/eval

# Campos do header que o OCR NAO produz (vem de snap/refs) — fora do alvo.
_SYSTEM_HEADER_FIELDS = ("pernr", "cod_maquina")


# --- field-path: header.x / rows[i].x / footer.x --------------------------
# Mesma logica do backend/app/web/db.py (_set_by_path). Inline aqui para o
# script nao depender da cadeia de imports do backend.
def _set_by_path(data: dict, path: str, value: str) -> None:
    section, _, rest = path.partition(".")
    if section in ("header", "footer"):
        data.setdefault(section, {})[rest] = value
        return
    if section.startswith("rows[") and section.endswith("]"):
        idx = int(section[5:-1])
        if idx < 0 or idx > 100:
            raise ValueError(f"row index fora de alcance: {path}")
        rows = data.setdefault("rows", [])
        while len(rows) <= idx:
            rows.append({})
        rows[idx][rest] = value
        return
    raise ValueError(f"path nao reconhecido: {path}")


def _resize_for_model(src: Path, dst: Path) -> None:
    """Redimensiona como o ocr6.image_to_base64 e grava JPEG q90 no disco."""
    with Image.open(src) as raw:
        rgb = raw.convert("RGB")
    long_edge = max(rgb.size)
    scale = min(1.0, ocr6._MAX_IMAGE_LONG_EDGE / long_edge)
    target = (
        ocr6._round_to_stride(round(rgb.size[0] * scale)),
        ocr6._round_to_stride(round(rgb.size[1] * scale)),
    )
    if target != rgb.size:
        rgb = rgb.resize(target, Image.Resampling.LANCZOS)
    rgb.save(dst, format="JPEG", quality=90)


def _load_prompt_for(template_name: str) -> str:
    """Prompt de producao: ocr6_v3.txt para bobine_formato; senao tenta o
    prompt_builder por template, com fallback para o bobine."""
    bobine = _PROMPT_BOBINE_PATH.read_text(encoding="utf-8")
    if not template_name or template_name == "bobine_formato":
        return bobine
    try:
        from app.pipeline.prompt_builder import build_prompt
        from app.templates_registry import get_template
        return build_prompt(get_template(template_name))
    except Exception as e:  # noqa: BLE001 — fallback gracioso
        print(f"  aviso: prompt_builder falhou para '{template_name}' "
              f"({e}); uso o prompt bobine.")
        return bobine


def _build_target(raw_extraction: str, human_edits: list[tuple[str, str]]) -> dict:
    """raw_extraction + edicoes humanas, normalizado ao schema do prompt."""
    data = json.loads(raw_extraction)
    if not isinstance(data, dict):
        raise ValueError("raw_extraction nao e um objeto JSON")
    for field_path, new_value in human_edits:
        try:
            _set_by_path(data, field_path, new_value if new_value is not None else "")
        except (ValueError, TypeError) as e:
            print(f"  aviso: ignoro edicao '{field_path}' ({e})")
    # Normalizar ao que o prompt pede: sem campos de sistema, sem template_name.
    header = data.get("header")
    if isinstance(header, dict):
        for f in _SYSTEM_HEADER_FIELDS:
            header.pop(f, None)
    data.pop("template_name", None)
    return data


def main() -> int:
    if not _DB_PATH.exists():
        print(f"ERRO: nao encontrei a base de dados em {_DB_PATH}")
        return 1

    _OUT_IMAGES.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    sheets = conn.execute(
        "SELECT s.id, s.image_path, s.raw_extraction, "
        "       json_extract(s.sheet_data, '$.template_name') AS template_name "
        "FROM sheets s "
        "WHERE s.status = 'extracted' AND s.raw_extraction IS NOT NULL "
        "  AND s.id IN (SELECT DISTINCT sheet_id FROM edits WHERE source='human') "
        "ORDER BY s.id"
    ).fetchall()

    records: list[dict] = []
    skipped: dict[str, int] = {"sem_imagem": 0, "raw_invalido": 0}

    for s in sheets:
        sheet_id = s["id"]
        rel = (s["image_path"] or "").replace("\\", "/")
        img_src = _REPO / "data" / rel
        if not img_src.exists():
            # image_path pode ser so o nome — tentar data/images/
            img_src = _IMAGES_DIR / Path(rel).name
        if not img_src.exists():
            skipped["sem_imagem"] += 1
            continue

        edits = conn.execute(
            "SELECT field_path, new_value FROM edits "
            "WHERE sheet_id=? AND source='human' ORDER BY id",
            (sheet_id,),
        ).fetchall()
        try:
            target = _build_target(
                s["raw_extraction"], [(e["field_path"], e["new_value"]) for e in edits])
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  aviso: folha {sheet_id} — raw_extraction invalido ({e})")
            skipped["raw_invalido"] += 1
            continue

        out_name = f"ft_{sheet_id:05d}.jpg"
        _resize_for_model(img_src, _OUT_IMAGES / out_name)

        tpl = s["template_name"] or "bobine_formato"
        records.append({
            "sheet_id": sheet_id,
            "template": tpl,
            "image": f"images/{out_name}",
            "prompt": _load_prompt_for(tpl),
            "target": json.dumps(target, ensure_ascii=False),
        })

    conn.close()

    # Split determinístico: baralhar com seed fixa, primeiras N -> exame.
    rng = random.Random(_SEED)
    rng.shuffle(records)
    eval_n = min(_EVAL_COUNT, len(records) // 2)
    eval_set, train_set = records[:eval_n], records[eval_n:]

    def _write(name: str, rows: list[dict]) -> None:
        with (_OUT_DIR / name).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    _write("train.jsonl", train_set)
    _write("eval.jsonl", eval_set)

    print()
    print("=== Dataset de fine-tuning criado ===")
    print(f"  folhas candidatas : {len(sheets)}")
    print(f"  descartadas       : sem imagem={skipped['sem_imagem']} "
          f"raw invalido={skipped['raw_invalido']}")
    print(f"  treino  -> {len(train_set):3d}  ({_OUT_DIR / 'train.jsonl'})")
    print(f"  exame   -> {len(eval_set):3d}  ({_OUT_DIR / 'eval.jsonl'})")
    print(f"  imagens -> {_OUT_IMAGES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
