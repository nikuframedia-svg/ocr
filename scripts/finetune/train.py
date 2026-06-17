"""R105 — treino LoRA do modelo de OCR de kanbans. SCAFFOLDING.

NAO corre sem GPU. Corre no PC da Metalogalva (RTX 5090), no ambiente
.venv-train criado pelo finetune_setup.ps1:

    .venv-train\\Scripts\\python data\\_logs\\train.py

Le data/_finetune/train.jsonl (do build_dataset.py) e treina um adaptador
LoRA por cima do VLM base. O resultado fica em data/_finetune/lora_out/.

NOTA: este ficheiro e um ponto de partida. A API do Unsloth muda entre
versoes — se der erro, copia-o e mostra ao Claude (ele alinha com a versao
instalada). O id do modelo base TEM de ser confirmado na Metalogalva.
"""
from __future__ import annotations

import json
from pathlib import Path

# ---- Constantes a confirmar na Metalogalva -------------------------------
# O modelo de producao no Ollama chama-se
# 'yasserrmd/Nanonets-OCR2-3B:latest'. O fine-tuning precisa do modelo BASE
# equivalente no HuggingFace/Unsloth. Confirma com:
#     ollama show yasserrmd/Nanonets-OCR2-3B:latest
# e ajusta BASE_MODEL para o id correspondente antes de treinar.
BASE_MODEL = "unsloth/Qwen2.5-VL-7B-Instruct"   # <-- confirmar vs 'ollama show'
LORA_RANK = 16
EPOCHS = 2
LEARNING_RATE = 2e-4
BATCH_SIZE = 1
GRAD_ACCUM = 4

_REPO = Path(__file__).resolve().parents[2]
_DATA = _REPO / "data" / "_finetune"
_OUT = _DATA / "lora_out"


def _load(split: str) -> list[dict]:
    path = _DATA / f"{split}.jsonl"
    if not path.exists():
        raise SystemExit(
            f"Falta {path}. Corre primeiro:  python data\\_logs\\build_dataset.py")
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _to_messages(rec: dict) -> dict:
    """Uma linha do dataset -> formato de conversa do Unsloth: o utilizador
    manda imagem + prompt, o assistente responde com o JSON certo."""
    from PIL import Image
    img = Image.open(_DATA / rec["image"]).convert("RGB")
    return {"messages": [
        {"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text": rec["prompt"]},
        ]},
        {"role": "assistant", "content": [
            {"type": "text", "text": rec["target"]},
        ]},
    ]}


def main() -> int:
    from trl import SFTConfig, SFTTrainer
    from unsloth import FastVisionModel
    from unsloth.trainer import UnslothVisionDataCollator

    print(f"Modelo base: {BASE_MODEL}")
    model, tokenizer = FastVisionModel.from_pretrained(BASE_MODEL, load_in_4bit=True)
    model = FastVisionModel.get_peft_model(
        model,
        r=LORA_RANK,
        lora_alpha=LORA_RANK,
        finetune_vision_layers=True,
        finetune_language_layers=True,
    )

    train_data = [_to_messages(r) for r in _load("train")]
    print(f"Exemplos de treino: {len(train_data)}")
    FastVisionModel.for_training(model)

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        data_collator=UnslothVisionDataCollator(model, tokenizer),
        train_dataset=train_data,
        args=SFTConfig(
            per_device_train_batch_size=BATCH_SIZE,
            gradient_accumulation_steps=GRAD_ACCUM,
            num_train_epochs=EPOCHS,
            learning_rate=LEARNING_RATE,
            output_dir=str(_OUT / "_checkpoints"),
            logging_steps=5,
            save_strategy="epoch",
            bf16=True,
            report_to="none",
        ),
    )
    trainer.train()

    _OUT.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(_OUT))
    tokenizer.save_pretrained(str(_OUT))
    print(f"\nLoRA guardado em {_OUT}")
    print("Proximo passo:  powershell -File data\\_logs\\deploy_model.ps1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
