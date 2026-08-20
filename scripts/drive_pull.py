"""Poller do Google Drive: scans e referências entram sozinhos.

Fecha o ciclo scanner→Drive→OCR sem uploads manuais:
1. descarrega a pasta partilhada do Drive (a mesma que alimenta o resto do
   ecossistema MTG) para ``data/_drive_staging``;
2. ficheiros de REFERÊNCIA (StockSAP, plan_colunas_cpis, ListaColaboradores,
   maquinas) são pousados em ``KANBAN_REFS_IMPORT_DIR`` — o ref_importer
   nativo da app faz a classificação por conteúdo e o dedupe por sha256;
3. PDFs de KANBAN (nome começa pela data: «18-08-2026.PDF») são submetidos a
   ``POST /upload?return=json``. Como o servidor NÃO deduplica folhas (o
   mesmo PDF submetido duas vezes gera folhas duplicadas e re-OCR na GPU), a
   idempotência vive AQUI: ``data/drive_pull_state.json`` regista o sha256 de
   cada PDF já submetido e nunca o reenvia.

Desenhado para correr como tarefa agendada do Windows (2x/dia, ver
``scripts/ops/register_drive_pull.ps1``); cada corrida apanha o atraso da
anterior, por isso falhas de rede não perdem nada.

    python scripts/drive_pull.py --dry-run    # mostra o que faria
    python scripts/drive_pull.py

Requer o extra ``drive`` (``pip install -e .[drive]`` → gdown).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

_REPO = Path(__file__).resolve().parents[1]
_STAGING = _REPO / "data" / "_drive_staging"
_STATE_PATH = _REPO / "data" / "drive_pull_state.json"
_LOG_PATH = _REPO / "data" / "_logs" / "drive_pull.log"

# A pasta pública «MTG | Kanban Digital» — a mesma de onde o planeamento e o
# scanner já são consumidos pelo resto do ecossistema.
DEFAULT_FOLDER_ID = "1ZYUt85vo7ETRX8Q8orhtFfG6f1Z3Nj-6"

# PDFs de kanban começam pela data («06-08-2026 - Rapid20T 1.pdf»,
# «18-08-2026.PDF»); tudo o resto na pasta são planos/relatórios.
_KANBAN_PDF = re.compile(r"^\d{2}-\d{2}-\d{4}.*\.pdf$", re.IGNORECASE)

# A pasta do Drive tem subpastas por setor («Kanban's MTG2», «Kanban's MTG3»)
# e os NOMES de PDF repetem-se entre elas (dois «18-08-2026.PDF» diferentes).
# Este sistema só ingere os do seu setor; ajustável por env sem mexer no
# código (lista separada por ponto-e-vírgula).
_DEFAULT_SCAN_SUBDIRS = "Kanban's MTG2"

# Referências que a app consome via ref_importer (classificação por conteúdo;
# os nomes aqui são só um filtro para não arrastar Excels gigantes de planos
# que não são refs — o Met3_Plan_Cantoneiras tem ~50 MB).
_REF_NAMES = re.compile(
    r"^(stocksap.*|plan_colunas_cpis|lista ?colaboradores|maquinas)\.xls[xm]$",
    re.IGNORECASE,
)

# Espaço para o /upload rasterizar um PDF de dezenas de páginas.
_UPLOAD_TIMEOUT_S = 300.0


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line)
    _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"submitted": {}}


def save_state(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _STATE_PATH)


def refs_import_dir() -> Path:
    """A MESMA resolução que o ref_importer da app usa."""
    for var in ("KANBAN_REFS_IMPORT_DIR", "OCR_REFS_IMPORT_DIR"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value)
    return Path(r"F:\ocr\files")


def download_folder(folder_id: str, dest: Path) -> list[Path]:
    import gdown

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    gdown.download_folder(id=folder_id, output=str(dest), quiet=True,
                          use_cookies=False)
    return sorted(p for p in dest.rglob("*") if p.is_file())


def place_refs(files: list[Path], import_dir: Path, dry_run: bool) -> int:
    placed = 0
    for src in files:
        if not _REF_NAMES.match(src.name):
            continue
        target = import_dir / src.name
        if target.exists() and sha256_file(target) == sha256_file(src):
            continue                      # igual ao que lá está: nada a fazer
        if dry_run:
            log(f"[dry-run] ref {src.name} -> {target}")
        else:
            import_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            log(f"ref pousada para o importador: {src.name}")
        placed += 1
    return placed


def scan_subdirs() -> tuple[str, ...]:
    raw = os.environ.get("DRIVE_PULL_SUBDIRS", _DEFAULT_SCAN_SUBDIRS)
    return tuple(s.strip().lower() for s in raw.split(";") if s.strip())


def submit_scans(files: list[Path], app_url: str, state: dict,
                 dry_run: bool) -> tuple[int, int]:
    novos = repetidos = 0
    subdirs = scan_subdirs()
    submitted: dict = state.setdefault("submitted", {})
    for pdf in files:
        if not _KANBAN_PDF.match(pdf.name):
            continue
        if pdf.parent.name.strip().lower() not in subdirs:
            continue                      # PDF de outro setor: não é nosso
        sha = sha256_file(pdf)
        if sha in submitted:
            repetidos += 1
            continue
        if dry_run:
            log(f"[dry-run] submeteria {pdf.name} ({pdf.stat().st_size} bytes)")
            novos += 1
            continue
        with pdf.open("rb") as fh:
            resp = httpx.post(
                f"{app_url}/upload",
                params={"return": "json"},
                files={"image": (pdf.name, fh, "application/pdf")},
                timeout=_UPLOAD_TIMEOUT_S,
            )
        if resp.status_code != 200:
            # fica de fora do estado: a próxima corrida tenta outra vez
            log(f"ERRO: upload de {pdf.name} devolveu {resp.status_code}: "
                f"{resp.text[:200]}")
            continue
        payload = resp.json()
        submitted[sha] = {
            "name": pdf.name,
            "sheets": payload.get("count"),
            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_state(state)                 # gravar já: um crash não re-submete
        log(f"submetido {pdf.name}: {payload.get('count')} folha(s) na fila")
        novos += 1
    return novos, repetidos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra o que faria, sem copiar nem submeter")
    ap.add_argument("--folder-id",
                    default=os.environ.get("DRIVE_FOLDER_ID", DEFAULT_FOLDER_ID))
    ap.add_argument("--app-url",
                    default=os.environ.get("DRIVE_PULL_APP_URL",
                                           "http://127.0.0.1:8080"))
    args = ap.parse_args()

    log(f"drive_pull início (dry_run={args.dry_run})")
    try:
        files = download_folder(args.folder_id, _STAGING)
    except Exception as exc:  # noqa: BLE001 — rede/quota: a próxima corrida apanha
        log(f"ERRO: download da pasta do Drive falhou: {exc}")
        return 1
    log(f"{len(files)} ficheiro(s) na pasta do Drive")

    state = load_state()
    refs = place_refs(files, refs_import_dir(), args.dry_run)
    novos, repetidos = submit_scans(files, args.app_url.rstrip("/"), state,
                                    args.dry_run)
    log(f"drive_pull fim: {refs} ref(s), {novos} PDF(s) novo(s), "
        f"{repetidos} já submetido(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
