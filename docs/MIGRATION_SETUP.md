# Migração PC → Portátil

Setup para mover o desenvolvimento + produção do PC desktop para um
portátil, mantendo o PC apenas como Ollama (GPU) server na LAN.

## Arquitectura pós-migração

```
PC desktop (192.168.1.224)              Portátil (qualquer IP)
+----------------------------+          +----------------------------+
| Ollama serve               |  ←LAN←   | uvicorn (FastAPI)          |
|   qwen3.5:9b GPU inference |          | SQLite DB (data/app.db)    |
|   listens on 0.0.0.0:11434 |          | cloudflared tunnel         |
+----------------------------+          | kanban_refs/ (refs local)  |
                                        +----------------------------+
                                                     ↓
                                              factory team via
                                              https://*.trycloudflare.com
```

## Pré-requisitos no portátil

1. **Python 3.11** — mesma versão que o PC. Verificar:
   ```powershell
   python --version
   ```
2. **Git** — para clone do repo:
   ```powershell
   git --version
   ```
3. **cloudflared** — download direto do GitHub
   <https://github.com/cloudflare/cloudflared/releases>. Coloca em PATH.

## Passos no portátil

### 1. Clone do repo

```powershell
cd $HOME
git clone https://github.com/nikuframedia-svg/ocr.git
cd ocr
```

### 2. Cria virtualenv + instala dependências

O repo não tem `requirements.txt` — instala do `pyproject.toml` (o
`uv.lock` tracked desde o R257 crava as versões testadas se usares uv):

```powershell
python -m venv .venv
.venv\Scripts\pip install --upgrade pip
.venv\Scripts\pip install -e .
```

### 3. Cria ficheiro `.env` na raiz do repo

```ini
# Apontar para Ollama no PC (LAN IP)
OLLAMA_URL=http://192.168.1.224:11434
OCR_MODEL=qwen3.5:9b
OCR_NO_THINK=1

# Refs locais (dentro do repo)
KANBAN_DOC_DIR=kanban_refs\04_Documentacao
CROSS_CHECK_DIR=kanban_refs\03_Cross_Check
FACTORY_CSV_DIR=kanban_refs\02_Dados_Extraidos\csv
```

> **Nota**: se quiseres que o portátil escreva CSVs e cross-check
> JSONs **fora do repo** (para não ficarem em git), aponta as 2
> últimas vars para caminhos absolutos noutro lado, e.g.
> `C:\portatil\kanban\02_Dados_Extraidos\csv`.

### 4. Confirma conectividade ao Ollama do PC

Mesma wifi que o PC:
```powershell
curl http://192.168.1.224:11434/api/tags
```
Esperado: JSON com `qwen3.5:9b` na lista de modelos.

Se falhar:
- Confirma PC está ligado e Ollama a correr (ver "Setup no PC" abaixo)
- Confirma portátil na mesma rede WiFi
- Tenta `ping 192.168.1.224`
- Confirma firewall do PC permite TCP 11434

### 5. Inicia uvicorn + tunnel

```powershell
powershell -ExecutionPolicy Bypass -File data\_logs\start.ps1
```

Ler `data\_logs\tunnel_url.txt` para o URL público (muda a cada
restart).

Visitar `http://127.0.0.1:8080/queue` localmente — deve mostrar as
folhas existentes do DB.

## Setup no PC (one-off, antes de migrar)

### Ollama escuta na LAN

```powershell
# Persistir env var
[System.Environment]::SetEnvironmentVariable('OLLAMA_HOST', '0.0.0.0:11434', 'User')

# Matar processos Ollama antigos
Get-Process | Where-Object { $_.ProcessName -like 'ollama*' } | Stop-Process -Force

# Relançar com env var
$env:OLLAMA_HOST = '0.0.0.0:11434'
Start-Process 'C:\Users\User\AppData\Local\Programs\Ollama\ollama.exe' -ArgumentList 'serve' -WindowStyle Hidden
```

### Firewall rule (PRECISA UAC)

Run PowerShell **como administrador**:
```powershell
New-NetFirewallRule -DisplayName "Ollama LAN" -Direction Inbound `
    -Protocol TCP -LocalPort 11434 -Action Allow -Profile Private
```

### Auto-start no boot (recomendado)

Cria atalho em:
`C:\Users\User\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\`

Target:
```
powershell.exe -WindowStyle Hidden -Command "$env:OLLAMA_HOST='0.0.0.0:11434'; Start-Process 'C:\Users\User\AppData\Local\Programs\Ollama\ollama.exe' -ArgumentList 'serve' -WindowStyle Hidden"
```

## Verificar migração completa

No portátil:
```powershell
# 1. Pode falar com PC?
curl http://192.168.1.224:11434/api/tags

# 2. Refs carregam?
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'backend'); from app.cross_check.ref_watcher import RefWatcher; r=RefWatcher().get_refs(); print('OK' if r['available'] else 'NO REFS'); print(r.get('stats'))"

# 3. Servidor arranca?
powershell -ExecutionPolicy Bypass -File data\_logs\start.ps1
# Esperar 15s
curl http://127.0.0.1:8080/queue
```

Sucesso = `/queue` mostra as 67 sheets pre-existentes.

## Limitações conhecidas

- **Tunnel URL muda a cada restart** (cloudflared free). Factory team
  precisa actualizar bookmark.
- **Portátil tem de estar ligado para factory aceder** — quando
  fechado/desligado, tunnel cai.
- **Mesma WiFi** entre PC e portátil. Se moveres, perdes Ollama.
  Solução longo-prazo: Tailscale (rede mesh segura).
- **Repo pesado** (~120 MB com fotos + DB). Primeiro clone demora.
  Subsequentes pulls são rápidos.

## Resolução de problemas

### "Connection refused" ao Ollama do portátil
1. PC está ligado e ollama running? `Get-Process ollama` no PC.
2. Ollama em `0.0.0.0:11434`? `Get-NetTCPConnection -LocalPort 11434` —
   deve mostrar `LocalAddress :: ` ou `0.0.0.0`, não `127.0.0.1`.
3. Firewall rule existe? `Get-NetFirewallRule -DisplayName "Ollama LAN"`.

### Cross-check vê 0 refs
- `kanban_refs/04_Documentacao/StockSAP.xlsx` existe no portátil?
- Verificar `.env`: `KANBAN_DOC_DIR=kanban_refs\04_Documentacao` (relativo
  à raiz do repo).

### Tunnel URL não responde
- Cloudflared correu? `Get-Process cloudflared`
- URL no `data\_logs\tunnel_url.txt` é o mais recente.
- DNS pode demorar 30-60s a propagar; tenta de novo.
