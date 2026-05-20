# scripts/finetune/

Scripts do pipeline de fine-tuning (R105). Movidos de `data/_logs/` no R107.

| Ficheiro | Função | Onde corre |
|---|---|---|
| `build_dataset.py` | Lê `data/app.db`, junta foto + leitura corrigida das folhas validadas, separa exame final | Portátil ou PC Metalogalva |
| `finetune_setup.ps1` | Instala `.venv-train` com Unsloth + PyTorch | PC Metalogalva (5090) |
| `train.py` | LoRA training do Qwen3.5-9B | PC Metalogalva (5090) |
| `deploy_model.ps1` | Converte e instala o modelo treinado no Ollama (`kanban-9b`) | PC Metalogalva |

Ver `docs/MIGRATION.md` Parte E para o fluxo completo (F1→F5).
