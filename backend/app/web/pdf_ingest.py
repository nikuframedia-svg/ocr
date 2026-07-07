"""Rasteriza um PDF de kanbans → 1 JPEG por página em ``data/images/``.

rev01 — o utilizador passou a poder carregar folhas manuscritas **digitalizadas**
(scans de MFP do escritório) em PDF, além das fotos de telemóvel. Cada página do
PDF é tratada como uma folha independente e entra no mesmo pipeline de OCR
(``ocr_queue`` → ``run_pipeline`` → cross-check → CSV).

Espelha o estilo de ``image_crop.py``: função-biblioteca (não CLI), sem imports de
``main`` (evita ciclo), e nunca deixa uma exceção crua do PDFium propagar — embrulha
tudo em ``PdfIngestError`` com um ``reason`` tipado e uma mensagem PT-PT
apresentável ao operador.

NÃO faz warp/crop de perspetiva: scans de MFP já são "papel limpo" axis-aligned e o
OCR corre sobre esta imagem diretamente. Faz apenas normalização de orientação
(``portrait`` → ``landscape``), porque os kanbans são A4 landscape e o scanner pode
guardar a página deitada (ver ``PDF_ROTATE_DIR``).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]  # sem stubs publicados
from PIL import Image

# Defaults (env override lido em runtime → testes podem monkeypatch os.environ).
_DEFAULT_DPI = 200
_DEFAULT_MAX_PAGES = 50
_JPEG_QUALITY = 90

# PDFium não é thread-safe entre documentos; serializa uploads concorrentes.
_PDFIUM_LOCK = threading.Lock()

_REASONS = ("corrupt", "encrypted", "empty", "too_many_pages")


class PdfIngestError(Exception):
    """Erro de ingestão de PDF com mensagem apresentável ao operador.

    ``reason`` é um de :data:`_REASONS`; o ``/upload`` mapeia-o para o HTTP certo.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_pdf_bytes(header: bytes) -> bool:
    """True se os primeiros ~1KB contiverem a assinatura ``%PDF-``.

    O standard permite lixo antes da assinatura, por isso procuramos a
    subsequência em vez de exigir prefixo. Usado como sniff autoritativo no
    ``/upload`` (magic > extensão do filename)."""
    return b"%PDF-" in (header or b"")[:1024]


def _env_int(name: str, default: int) -> int:
    try:
        val = int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _normalize_orientation(pil: Image.Image, rotate_dir: str) -> Image.Image:
    """Kanbans são A4 landscape. Se a página sair portrait (mais alta que larga),
    roda 90° para landscape no sentido pedido. Páginas já landscape ficam intactas.

    ``rotate_dir``: 'cw' (default), 'ccw' ou 'none'. transpose de 90° é lossless.
    """
    if rotate_dir == "none":
        return pil
    if pil.height <= pil.width:  # já landscape (ou quadrado) → não mexer
        return pil
    if rotate_dir == "ccw":
        return pil.transpose(Image.Transpose.ROTATE_90)   # 90° anti-horário
    return pil.transpose(Image.Transpose.ROTATE_270)      # 90° horário (cw, default)


def rasterize_pdf(
    pdf_path: Path,
    out_dir: Path,
    *,
    dpi: int | None = None,
    max_pages: int | None = None,
    rotate_dir: str | None = None,
    stem: str | None = None,
) -> list[Path]:
    """Rasteriza cada página do PDF → JPEG em ``out_dir``; devolve os caminhos
    por ordem de página.

    Raises :class:`PdfIngestError` em: PDF ilegível/corrompido (``corrupt``),
    encriptado/protegido (``encrypted``), 0 páginas (``empty``), ou nº de páginas
    acima de ``max_pages`` (``too_many_pages``). Renderiza **página-a-página**
    (nunca segura N bitmaps em memória) e verifica o cap **antes** de renderizar
    (um PDF-bomba de milhares de páginas não consome memória).
    """
    dpi = dpi if dpi is not None else _env_int("PDF_RASTER_DPI", _DEFAULT_DPI)
    max_pages = (
        max_pages if max_pages is not None
        else _env_int("PDF_MAX_PAGES", _DEFAULT_MAX_PAGES)
    )
    rotate_dir = (
        rotate_dir if rotate_dir is not None
        else (os.environ.get("PDF_ROTATE_DIR", "cw").strip().lower() or "cw")
    )
    stem = stem or pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0

    with _PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
        except pdfium.PdfiumError as e:
            reason = "encrypted" if "password" in str(e).lower() else "corrupt"
            msg = (
                "PDF protegido por palavra-passe — remova a proteção e tente de novo."
                if reason == "encrypted"
                else f"Não consegui ler o PDF (ficheiro corrompido ou inválido): {e}"
            )
            raise PdfIngestError(reason, msg) from e
        try:
            n = len(doc)
            if n == 0:
                raise PdfIngestError("empty", "O PDF não tem páginas.")
            if n > max_pages:
                raise PdfIngestError(
                    "too_many_pages",
                    f"O PDF tem {n} páginas (máximo {max_pages}). "
                    "Divida o ficheiro em partes mais pequenas.",
                )
            paths: list[Path] = []
            try:
                for i in range(n):
                    page = doc[i]
                    try:
                        pil = page.render(scale=scale).to_pil()
                        if pil.mode != "RGB":
                            pil = pil.convert("RGB")
                        pil = _normalize_orientation(pil, rotate_dir)
                        out = out_dir / f"{stem}_p{i + 1:02d}.jpg"
                        pil.save(out, "JPEG", quality=_JPEG_QUALITY)
                        paths.append(out)
                    finally:
                        page.close()
            except PdfIngestError:
                raise
            except Exception as e:  # página danificada a meio do render
                # Atomicidade: não deixar JPEGs órfãos de um PDF que falhou.
                for done in paths:
                    done.unlink(missing_ok=True)
                raise PdfIngestError(
                    "corrupt",
                    f"Falha ao rasterizar a página {len(paths) + 1} do PDF "
                    f"(página danificada?): {e}",
                ) from e
            return paths
        finally:
            doc.close()
