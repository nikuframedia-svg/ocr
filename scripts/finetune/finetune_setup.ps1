# R105 — instala o ambiente de treino para o fine-tuning na RTX 5090.
# SCAFFOLDING: corre no PC da Metalogalva, NAO no portatil (precisa da GPU).
#
#   powershell -ExecutionPolicy Bypass -File data\_logs\finetune_setup.ps1
#
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$venv = "$root\.venv-train"
Write-Host "REPO_ROOT=$root"

# Ambiente SEPARADO do .venv do servidor — as libs de treino (torch, unsloth)
# sao pesadas e nao se devem misturar com o ambiente de producao.
if (-not (Test-Path $venv)) {
  Write-Host "A criar o ambiente de treino .venv-train ..."
  python -m venv $venv
}
$py = "$venv\Scripts\python.exe"
& $py -m pip install --upgrade pip

# PyTorch — a RTX 5090 e arquitetura Blackwell (sm_120) e precisa de um build
# CUDA recente (cu128+). Se falhar, ver o PLANO B no fim.
Write-Host "A instalar o PyTorch (CUDA 12.8, suporte Blackwell)..."
& $py -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128

# Unsloth — treino LoRA rapido e economico em memoria. Puxa transformers,
# peft, trl, accelerate, bitsandbytes, datasets.
Write-Host "A instalar o Unsloth + dependencias de treino..."
& $py -m pip install unsloth
& $py -m pip install pillow

# Verificacao — a 5090 e vista?
Write-Host ""
Write-Host "=== Verificacao ==="
& $py -c "import torch; ok=torch.cuda.is_available(); print('CUDA disponivel:', ok); print('GPU:', torch.cuda.get_device_name(0) if ok else 'NENHUMA — ver PLANO B')"

Write-Host ""
Write-Host "Se viu 'CUDA disponivel: True' e o nome da RTX 5090, esta pronto (passa ao train.py)."
Write-Host ""
Write-Host "PLANO B — se deu erro vermelho ou 'CUDA: False':"
Write-Host " - A 5090 (Blackwell) e placa nova; torch/unsloth podem exigir versao nightly."
Write-Host "   Tenta:  $py -m pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu128"
Write-Host " - Copia o erro completo e mostra ao Claude — ele adapta este script."
