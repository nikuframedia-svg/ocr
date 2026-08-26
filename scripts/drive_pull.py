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


def download_folder(folder_id: str, dest: Path, rclone_base: str = "",
                    rclone: str = "rclone") -> list[Path]:
    """Descarrega a pasta do Drive para o staging.

    Caminho preferido: rclone (API oficial, mesma autorização da subida) —
    o gdown raspa a página pública e o Google começou a recusá-lo com «may
    have had many accesses» (avaria real de 24-26/08: matava o poller ANTES
    de subir os backups). A SAIDA/ exclui-se: é a nossa própria produção,
    não faz sentido voltar a descarregá-la. gdown fica como fallback para
    instalações sem rclone configurado.
    """
    import subprocess

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if rclone_base:
        result = subprocess.run(
            [rclone, "copy", rclone_base, str(dest),
             "--exclude", "SAIDA/**", "--transfers", "4"],
            capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return sorted(p for p in dest.rglob("*") if p.is_file())
        log(f"AVISO: rclone copy falhou ({result.stderr.strip()[:200]}); "
            "a tentar gdown")
    import gdown

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


def fetch_exports(specs: list[str], saida: Path, dry_run: bool) -> list[Path]:
    """GET aos exports das apps kanban (specs «nome=url») → SAIDA local.
    As apps geram o BaseDados na hora; se uma estiver em baixo, log e segue."""
    fetched: list[Path] = []
    for spec in specs:
        if "=" not in spec:
            continue
        name, url = spec.split("=", 1)
        dest = saida / f"{name.strip()}.xlsx"
        if dry_run:
            log(f"[dry-run] GET {url.strip()} -> {dest.name}")
            continue
        try:
            resp = httpx.get(url.strip(), timeout=120.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log(f"AVISO: export {name.strip()} falhou (app em baixo?): {exc}")
            continue
        saida.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        fetched.append(dest)
        log(f"export gerado: {dest.name} ({len(resp.content)} bytes)")
    return fetched


def push_outputs(remote: str, paths: list[Path], rclone: str,
                 dry_run: bool) -> int:
    """Sobe ficheiros/pastas para a pasta SAIDA/ do Drive via rclone
    (config feita uma vez no PC com `rclone config` — login Google).
    Ficheiro → copyto (nome preservado); diretório → copy (recursivo).
    Falhas: log e continua — a corrida seguinte volta a tentar."""
    import subprocess

    subidos = 0
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            cmd = [rclone, "copy", str(path), remote, "--transfers", "2"]
        else:
            cmd = [rclone, "copyto", str(path), f"{remote}/{path.name}",
                   "--transfers", "2"]
        if dry_run:
            log(f"[dry-run] {' '.join(cmd)}")
            subidos += 1
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"AVISO: rclone falhou para {path.name}: {exc}")
            continue
        if result.returncode != 0:
            log(f"AVISO: rclone devolveu {result.returncode} para "
                f"{path.name}: {result.stderr.strip()[:200]}")
            continue
        log(f"subido ao Drive: {path.name}")
        subidos += 1
    return subidos


def mirror_tree(staging: Path, mirror: Path, dry_run: bool) -> int:
    """Espelha a pasta do Drive (com subpastas) para um diretório estável —
    é daí que as apps kanban do PC ingerem (MES_DRIVE_DIR nas subpastas do
    setor). Só copia o que mudou (sha256); nunca apaga do espelho."""
    copiados = 0
    for src in sorted(p for p in staging.rglob("*") if p.is_file()):
        rel = src.relative_to(staging)
        dst = mirror / rel
        if dst.exists() and sha256_file(dst) == sha256_file(src):
            continue
        if dry_run:
            log(f"[dry-run] espelho {rel}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            log(f"espelhado: {rel}")
        copiados += 1
    return copiados


def notify(urls: list[str], dry_run: bool) -> None:
    """Avisa as apps kanban para ingerirem do espelho. Elas deduplicam por
    sha (PDF e página), por isso avisar a mais nunca duplica folhas."""
    for url in urls:
        if dry_run:
            log(f"[dry-run] POST {url}")
            continue
        try:
            resp = httpx.post(url, timeout=120.0)
            log(f"notificado {url}: {resp.status_code} {resp.text[:120]}")
        except httpx.HTTPError as exc:
            log(f"AVISO: notificação {url} falhou (app em baixo?): {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra o que faria, sem copiar nem submeter")
    ap.add_argument("--folder-id",
                    default=os.environ.get("DRIVE_FOLDER_ID", DEFAULT_FOLDER_ID))
    ap.add_argument("--app-url",
                    default=os.environ.get("DRIVE_PULL_APP_URL",
                                           "http://127.0.0.1:8080"))
    ap.add_argument("--mirror-to",
                    default=os.environ.get("DRIVE_PULL_MIRROR_TO", ""),
                    help="espelhar a pasta do Drive para este diretório "
                         "(para as apps kanban do PC ingerirem)")
    ap.add_argument("--notify", action="append", metavar="URL", default=None,
                    help="POST depois do espelho (repetível) — ex. "
                         "http://127.0.0.1:8100/ingest/drive")
    ap.add_argument("--push-remote",
                    default=os.environ.get("DRIVE_PULL_PUSH_REMOTE", ""),
                    help="destino rclone da pasta SAIDA no Drive (ex. "
                         "«gdrive:MTG | Kanban Digital/SAIDA»); vazio = sem subida")
    ap.add_argument("--rclone",
                    default=os.environ.get("DRIVE_PULL_RCLONE", "rclone"))
    args = ap.parse_args()
    notify_urls = args.notify if args.notify is not None else [
        u.strip() for u in os.environ.get("DRIVE_PULL_NOTIFY", "").split(";")
        if u.strip()
    ]
    push_files = [Path(p.strip()) for p in
                  os.environ.get("DRIVE_PULL_PUSH_FILES", "").split(";")
                  if p.strip()]
    export_specs = [s.strip() for s in
                    os.environ.get("DRIVE_PULL_EXPORT_FETCH", "").split(";")
                    if s.strip()]

    log(f"drive_pull início (dry_run={args.dry_run})")

    # PERNA DE SUBIDA PRIMEIRO. Avaria real de 24-26/08: o download (gdown)
    # falhava com a quota do Google e o poller desistia ANTES de subir os
    # backups — 3 dias sem cópias no Drive. Os exports e os backups locais
    # não dependem de nada do download; sobem SEMPRE.
    subidos = 0
    if args.push_remote:
        saida_local = _REPO / "data" / "_saida_staging"
        fetched = fetch_exports(export_specs, saida_local, args.dry_run)
        subidos = push_outputs(args.push_remote.strip(),
                               fetched + push_files, args.rclone, args.dry_run)
        # Retenção no Drive: sem isto os backups acumulavam-se para sempre
        # na SAIDA/ (a retenção local de 14 já existia; a remota não).
        if not args.dry_run:
            import subprocess
            subprocess.run([args.rclone, "delete",
                            f"{args.push_remote.strip()}/backups",
                            "--min-age", "14d"],
                           capture_output=True, text=True, timeout=300)

    # O remote base do rclone («gdrive:») deriva do push_remote; sem push
    # configurado pode vir de DRIVE_PULL_REMOTE, senão cai no gdown.
    rclone_base = (args.push_remote.split(":", 1)[0] + ":"
                   if args.push_remote
                   else os.environ.get("DRIVE_PULL_REMOTE", ""))
    try:
        files = download_folder(args.folder_id, _STAGING,
                                rclone_base=rclone_base, rclone=args.rclone)
    except Exception as exc:  # noqa: BLE001 — rede/quota: a próxima corrida apanha
        log(f"ERRO: download da pasta do Drive falhou: {exc}")
        log(f"drive_pull fim (parcial): {subidos} subido(s) à SAIDA")
        return 1
    log(f"{len(files)} ficheiro(s) na pasta do Drive")

    state = load_state()
    refs = place_refs(files, refs_import_dir(), args.dry_run)
    novos, repetidos = submit_scans(files, args.app_url.rstrip("/"), state,
                                    args.dry_run)
    espelhados = 0
    if args.mirror_to:
        espelhados = mirror_tree(_STAGING, Path(args.mirror_to), args.dry_run)
        notify(notify_urls, args.dry_run)

    log(f"drive_pull fim: {refs} ref(s), {novos} PDF(s) novo(s), "
        f"{repetidos} já submetido(s), {espelhados} espelhado(s), "
        f"{subidos} subido(s) à SAIDA")
    return 0


if __name__ == "__main__":
    sys.exit(main())
