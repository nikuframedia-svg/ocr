"""Drive transport for the two Kanban MES applications only.

The original OCR reads references from F:\\ocr\\files and receives no
references or scans from Drive. Its production export and SQLite backup are
excluded from uploads, including old files left in the shared output folder.
The historical Windows task name is retained so the MES workflows keep running.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

_REPO = Path(__file__).resolve().parents[1]
_STAGING = _REPO / "data" / "_drive_staging"
_LOG_PATH = _REPO / "data" / "_logs" / "drive_pull.log"

# A pasta pública «MTG | Kanban Digital» — a mesma de onde o planeamento e o
# scanner já são consumidos pelo resto do ecossistema.
DEFAULT_FOLDER_ID = "1ZYUt85vo7ETRX8Q8orhtFfG6f1Z3Nj-6"

# Only these applications may participate in this shared Drive transport.
_MES_EXPORT_PORTS = {
    "BaseDados_Cantoneiras_MTG3": 8100,
    "BaseDados_Perfis_MTG2": 8101,
}
_MES_BACKUP_APPS = ("kanban-mes", "kanban-mes-mtg2")
_MES_OUTPUT_PATTERNS = (
    *(f"/{name}.xlsx" for name in _MES_EXPORT_PORTS),
    *(f"/backups/{app}/app-*.db" for app in _MES_BACKUP_APPS),
)


def _mes_url(url: str, *, port: int | None = None, path: str) -> bool:
    try:
        parsed = urlsplit(url.strip())
        return (
            parsed.scheme == "http"
            and parsed.hostname in ("127.0.0.1", "localhost")
            and parsed.port in ((port,) if port else (8100, 8101))
            and parsed.path.rstrip("/") == path
            and not parsed.username and not parsed.password
        )
    except ValueError:
        return False


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


def fetch_exports(specs: list[str], saida: Path, dry_run: bool) -> list[Path]:
    """GET aos exports das apps kanban (specs «nome=url») → SAIDA local.
    As apps geram o BaseDados na hora; se uma estiver em baixo, log e segue."""
    fetched: list[Path] = []
    for spec in specs:
        if "=" not in spec:
            continue
        name, url = spec.split("=", 1)
        name, url = name.strip(), url.strip()
        if name not in _MES_EXPORT_PORTS or not _mes_url(
            url, port=_MES_EXPORT_PORTS.get(name), path="/export/basedados",
        ):
            log(f"export ignorado: {name} nao pertence aos dois Kanbans MES")
            continue
        dest = saida / f"{name}.xlsx"
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
            # A shared SAIDA may still contain the original OCR's app.db.
            # An allowlist also excludes renamed/unrecognised OCR artifacts.
            for pattern in _MES_OUTPUT_PATTERNS:
                cmd.extend(["--include", pattern])
        else:
            if path.name not in {f"{name}.xlsx" for name in _MES_EXPORT_PORTS}:
                log(f"saida ignorada: {path.name} nao pertence aos Kanbans MES")
                continue
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
    destination = mirror.resolve()
    for protected in (_REPO.resolve(), refs_import_dir().resolve()):
        if destination.is_relative_to(protected) or protected.is_relative_to(destination):
            raise ValueError("O espelho Drive nao pode sobrepor pastas do OCR original")
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
        if not _mes_url(url, path="/ingest/drive"):
            log("notificacao ignorada: destino fora dos dois Kanbans MES")
            continue
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
    # Accepted for compatibility with existing scheduled command lines;
    # this value no longer authorizes any calls to the original OCR.
    ap.add_argument("--app-url", help=argparse.SUPPRESS)
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

    log(f"drive_pull início: apenas Kanbans MES; OCR original local (dry_run={args.dry_run})")

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
            for app in _MES_BACKUP_APPS:
                subprocess.run([args.rclone, "delete",
                                f"{args.push_remote.strip()}/backups/{app}",
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

    espelhados = 0
    if args.mirror_to:
        espelhados = mirror_tree(_STAGING, Path(args.mirror_to), args.dry_run)
        notify(notify_urls, args.dry_run)

    log(f"drive_pull fim (Kanbans MES): {espelhados} espelhado(s), "
        f"{subidos} subido(s) à SAIDA; OCR original sem operacoes Drive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
