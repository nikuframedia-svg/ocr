# R105 — funde o LoRA treinado e instala o modelo no Ollama. SCAFFOLDING.
# Corre no PC da Metalogalva, depois do train.py.
#
#   powershell -ExecutionPolicy Bypass -File data\_logs\deploy_model.ps1
#
# NOTA: a exportacao de um VLM para GGUF + suporte de visao no Ollama e
# delicada. Se der erro vermelho, copia-o e mostra ao Claude (passo F4).
#
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$py = "$root\.venv-train\Scripts\python.exe"
$ft = "$root\data\_finetune"

if (-not (Test-Path "$ft\lora_out")) {
  Write-Host "ERRO: nao encontrei $ft\lora_out — corre primeiro o train.py."
  exit 1
}

# 1. Fundir o adaptador LoRA no modelo base e exportar em GGUF (formato Ollama),
#    quantizado q4_k_m (cabe bem na 5090 e no Ollama).
Write-Host "A fundir o LoRA e a exportar para GGUF..."
& $py -c @"
from unsloth import FastVisionModel
model, tok = FastVisionModel.from_pretrained(r'$ft\lora_out')
model.save_pretrained_gguf(r'$ft\gguf', tok, quantization_method='q4_k_m')
"@

# 2. Escrever o Modelfile e criar o modelo no Ollama.
Write-Host "A criar o modelo 'kanban-9b' no Ollama..."
$modelfile = "$ft\Modelfile"
"FROM $ft\gguf" | Out-File -Encoding ascii $modelfile
ollama create kanban-9b -f $modelfile

Write-Host ""
Write-Host "Modelo 'kanban-9b' criado no Ollama."
Write-Host "Para o usar: no .env, muda  OCR_MODEL=qwen3-vl:8b  para  OCR_MODEL=kanban-9b"
Write-Host "e corre o update.ps1."
Write-Host "E REVERSIVEL: se o novo for pior, volta a por qwen3-vl:8b e reinicia."
