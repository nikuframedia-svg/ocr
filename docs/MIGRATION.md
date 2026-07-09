# Migração para o PC da Metalogalva + Fine-tuning — Runbook

Guia clique-a-clique. Tu não programas: o Claude escreve o código, tu corres
comandos por copiar-colar. Comandos correm-se no **PowerShell** (botão direito
→ Colar funciona).

> **Princípio:** os scripts ficam todos prontos já (Parte A, feita no portátil).
> O treino só acontece **depois** da migração — precisa dos dados e da RTX 5090
> na mesma máquina, e essa máquina é o PC da Metalogalva.

---

## Antes de começar — ter à mão
- IP da Ollama da Metalogalva.
- Acesso RDP ao PC da Metalogalva: IP/nome + utilizador + password.
- Conta GitHub `nikuframedia-svg` (e a password).
- Avisar o contacto da Metalogalva que vais fazer a migração, possivelmente
  fora de horas (entrar por RDP de noite gera um aviso de segurança — avisar
  antes evita que te bloqueiem a conta).

---

## PARTE A — no portátil (preparação) — FEITO PELO CLAUDE

Já está. O que foi feito (Round 105):
- **A1** — todo o trabalho está em `origin/main` (nada por commitar).
- **A2** — `.gitignore` ignora os artefactos de treino (`data/_finetune/`,
  `.venv-train/`). **O `data/app.db` continua tracked de propósito** — o
  `git clone` do Passo 6 precisa dele para o PC da Metalogalva receber as
  ~180 folhas corrigidas (a base do fine-tuning). O `update.ps1` protege a BD
  de produção (ver Parte D).
- **A3** — `scripts/ops/update.ps1` (o comando do dia-a-dia).
- **A4** — scripts de fine-tuning: `build_dataset.py` (testado no portátil:
  150 treino + 30 exame), `finetune_setup.ps1`, `train.py`, `deploy_model.ps1`.
- **A5** — este documento.
- **A6** — ⚠️ o repositório GitHub estava **PÚBLICO**; foi posto **privado**.
  (Tem a BD e fotos da Metalogalva — não pode ser público.)

---

## PARTE B — no PC da Metalogalva, por RDP (a migração)

**Passo 1 — Entrar por RDP.** "Ligação ao Ambiente de Trabalho Remoto" → IP do
PC da Metalogalva → utilizador + password.

**Passo 2 — Instalar o Python.** python.org/downloads → "Download Python 3.12"
→ abrir o ficheiro → ⚠️ marcar **"Add python.exe to PATH"** → "Install Now".
✅ `python --version` → `Python 3.12.x`

**Passo 3 — Instalar o Git.** git-scm.com/download/win → "64-bit Git for
Windows Setup" → "Next" em tudo → "Install".
✅ `git --version`

**Passo 4 — Instalar o cloudflared.** `winget install --id Cloudflare.cloudflared`
✅ `cloudflared --version` (se o winget falhar, avisa o Claude.)

**Passo 5 — Ollama + modelo.** `ollama --version` (se der erro, avisa o Claude).
Depois `ollama pull qwen3.5:9b` (≈5 GB) → esperar por "success".
✅ `ollama list` mostra `qwen3.5:9b`

**Passo 6 — Descarregar o programa.**
```
cd C:\
git clone https://github.com/nikuframedia-svg/ocr.git
```
Vai pedir login do GitHub no browser — entra com `nikuframedia-svg`.
✅ `cd C:\ocr` depois `dir` → vês `backend`, `data`, `lexicons`, `kanban_refs`.
O clone traz já o `app.db` com as ~180 folhas e as fotos.

**Passo 7 — Preparar o programa.**
```
cd C:\ocr
python -m venv .venv
.venv\Scripts\pip install -e .
```
✅ Termina sem "ERROR" vermelho.

**Passo 8 — Criar o .env.** `notepad .env` → "Sim" → cola exatamente:
```
OLLAMA_URL=http://localhost:11434
OCR_MODEL=qwen3.5:9b
OCR_NO_THINK=1
KANBAN_DOC_DIR=C:\ocr\kanban_refs\04_Documentacao
CROSS_CHECK_DIR=C:\ocr\data\_cross_check
```
Ctrl+S → fechar.

**Passo 9 — Arrancar.**
```
powershell -ExecutionPolicy Bypass -File data\_logs\start.ps1
```
No fim aparece `TUNNEL_URL=https://....trycloudflare.com` → guarda-o.

**Passo 10 — Confirmar.** Browser → `http://127.0.0.1:8080/queue` → vês a
lista de kanbans. Abre o TUNNEL_URL → o mesmo.
✅ Lista visível → o programa corre no PC da Metalogalva. 🎉

**Só agora desligas o tunnel antigo no teu PC.** Até aqui ninguém ficou sem
serviço.

---

## PARTE C — no portátil (desenvolvimento)

**Passo 11 — .env do portátil.** `cd C:\Users\User\ocr` → `notepad .env` →
cola (o `OLLAMA_URL` aponta para o IP da Metalogalva — a 5090):
```
OLLAMA_URL=http://<IP-DA-OLLAMA>:11434
OCR_MODEL=qwen3.5:9b
OCR_NO_THINK=1
KANBAN_DOC_DIR=C:\Users\User\ocr\kanban_refs\04_Documentacao
CROSS_CHECK_DIR=C:\Users\User\ocr\data\_cross_check
```

**Passo 12 — Testar.**
`powershell -ExecutionPolicy Bypass -File data\_logs\start.ps1` → browser →
`http://127.0.0.1:8080/queue`.

---

## PARTE D — o dia-a-dia (atualizar)

No portátil trabalha-se no código → o Claude faz `git push`. No PC da
Metalogalva (RDP), um único comando:
```
cd C:\ocr
powershell -ExecutionPolicy Bypass -File data\_logs\update.ps1
```
Busca o código novo e reinicia. O `update.ps1` protege a base de dados de
produção (`git update-index --skip-worktree`) — o `git pull` nunca sobrepõe a
BD nem as fotos da Metalogalva.

> **rev01 — nova dependência `pypdfium2` (ingestão de PDF).** O `update.ps1` só
> faz `git pull` + restart; **não reinstala dependências**. Na primeira vez que
> o PC receber esta versão, corre **uma vez** (com internet):
> ```
> cd C:\ocr
> .venv\Scripts\pip install -e .        # (ou: uv sync)
> ```
> O `pypdfium2` é um wheel self-contained (PDFium do Google — **sem poppler nem
> outro binário de sistema**), por isso instala num só passo, e o wheel `abi3`
> serve qualquer Python 3.x. Se o PC estiver sem internet, pré-descarrega o
> wheel no portátil (`uv pip download pypdfium2` / `pip download`) e copia-o.
> Depois disto, os `update.ps1` seguintes voltam a bastar.

---

## PARTE E — o fine-tuning (no PC da Metalogalva, depois da migração)

Os scripts já estão no PC (vieram no `git clone`). Modelo: a família VLM que o
projeto usa. Método: LoRA. Treino-teste com as ~180 folhas; mais tarde repete-se
com mais folhas.

**F1 — Juntar o material de estudo.**
```
cd C:\ocr
.venv\Scripts\python data\_logs\build_dataset.py
```
Cria `data\_finetune\` com as ~180 fichas (foto + resposta certa) e 30 postas
de parte para o exame. ~1 minuto.

**F2 — Instalar a ferramenta de treino.**
```
powershell -ExecutionPolicy Bypass -File data\_logs\finetune_setup.ps1
```
Cria o ambiente `.venv-train` e instala PyTorch + Unsloth. ⚠️ A 5090 é placa
nova (Blackwell) — se der erro vermelho, copia e mostra ao Claude (há plano B
no próprio script).

**F3 — Treinar.**
```
.venv-train\Scripts\python data\_logs\train.py
```
A 5090 trabalha sozinha ~1 hora. ⚠️ Antes, confirma o modelo base: corre
`ollama show qwen3.5:9b` e mostra ao Claude para ele acertar o `BASE_MODEL` no
`train.py`.

**F4 — Pôr o modelo treinado no Ollama.**
```
powershell -ExecutionPolicy Bypass -File data\_logs\deploy_model.ps1
```
Cria o modelo `kanban-9b`. Depois, no `.env`, muda `OCR_MODEL=qwen3.5:9b` para
`OCR_MODEL=kanban-9b` e corre o `update.ps1`. Reversível.

**F5 — Medir (FEITO PELO CLAUDE).** O Claude corre o cross-check nas 30 folhas
do exame com o modelo antigo e o novo, e compara o acerto.

---

## PARTE F — ⚠️ Segurança

- **Repositório GitHub:** já está **privado** (Round 105). Tem a BD e as fotos
  da Metalogalva. Esteve público durante algum tempo — o que tiver sido copiado
  nesse período já não se controla; daqui para a frente está fechado.
- **Ollama exposta:** se a Ollama da Metalogalva estiver acessível pela internet
  sem password, qualquer pessoa que descubra o IP pode usar a 5090. Avisa o IT
  deles (copia): *"A Ollama (porta 11434) está exposta à internet sem
  autenticação. Restrinjam o acesso por firewall ou ponham-na atrás de VPN."*

---

## Onde podes precisar do Claude
Login do GitHub (Passo 6) · winget falha (Passo 4) · Ollama não instalada
(Passo 5) · instalação do Unsloth na 5090 (F2) · `BASE_MODEL` do train.py (F3) ·
pôr o modelo no Ollama (F4) · qualquer "ERROR" vermelho → copia o texto e mostra.
