"""Testes do módulo de ingestão de PDF (rev01) — ``app.web.pdf_ingest``.

Sem rede e sem fixtures binárias: os PDFs de teste são gerados com Pillow
(``Image.save(path, "PDF", save_all=True, ...)``), que já é dependência do
projeto. Exercita rasterização multipágina, normalização de orientação, e os
quatro modos de erro tipado (`corrupt`/`encrypted`/`empty`/`too_many_pages`).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from app.web import pdf_ingest
from PIL import Image


def _make_pdf(path: Path, sizes: list[tuple[int, int]]) -> None:
    """Cria um PDF com uma página por tamanho (largura, altura) em pontos.

    Pillow grava a 72 DPI por default → 1 pixel = 1 pt de página, portanto o
    tamanho da imagem define a forma (landscape vs portrait) da página."""
    imgs = [Image.new("RGB", s, (255, 255, 255)) for s in sizes]
    imgs[0].save(path, "PDF", save_all=True, append_images=imgs[1:])


def test_is_pdf_bytes():
    assert pdf_ingest.is_pdf_bytes(b"%PDF-1.4 blah")
    assert pdf_ingest.is_pdf_bytes(b"\x00\x00\x00%PDF-1.7")  # lixo antes é tolerado
    assert not pdf_ingest.is_pdf_bytes(b"hello world")
    assert not pdf_ingest.is_pdf_bytes(b"")
    assert pdf_ingest.is_pdf_bytes(b"%PDF-" + b"x" * 2000)  # assinatura no início conta
    # assinatura para lá dos 1024 bytes iniciais não conta
    assert not pdf_ingest.is_pdf_bytes(b"x" * 1100 + b"%PDF-")


def test_rasterize_multipage_returns_n_paths(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, [(842, 595)] * 3)  # 3 páginas A4 landscape
    out = tmp_path / "images"
    paths = pdf_ingest.rasterize_pdf(pdf, out, dpi=150)
    assert len(paths) == 3
    for i, p in enumerate(paths, 1):
        assert p.exists()
        assert p.name == f"scan_p{i:02d}.jpg"      # convenção de nomes _pNN
        with Image.open(p) as im:
            assert im.mode == "RGB"
            assert im.width >= im.height            # landscape


def test_rasterize_uses_stem_override(tmp_path):
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, [(842, 595)])
    paths = pdf_ingest.rasterize_pdf(pdf, tmp_path / "images", stem="TOKEN123", dpi=100)
    assert paths[0].name == "TOKEN123_p01.jpg"


def test_orientation_portrait_becomes_landscape(tmp_path):
    pdf = tmp_path / "portrait.pdf"
    _make_pdf(pdf, [(595, 842)])                    # portrait
    paths = pdf_ingest.rasterize_pdf(pdf, tmp_path / "images", dpi=100)  # default cw
    with Image.open(paths[0]) as im:
        assert im.width >= im.height                # rodado para landscape


def test_orientation_none_keeps_portrait(tmp_path):
    pdf = tmp_path / "portrait.pdf"
    _make_pdf(pdf, [(595, 842)])
    paths = pdf_ingest.rasterize_pdf(pdf, tmp_path / "images", dpi=100, rotate_dir="none")
    with Image.open(paths[0]) as im:
        assert im.height > im.width                 # portrait preservado


def test_orientation_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_ROTATE_DIR", "none")
    pdf = tmp_path / "portrait.pdf"
    _make_pdf(pdf, [(595, 842)])
    paths = pdf_ingest.rasterize_pdf(pdf, tmp_path / "images", dpi=100)
    with Image.open(paths[0]) as im:
        assert im.height > im.width                 # env respeitado


def test_rasterize_corrupt_raises(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4\n<<< isto nao e um pdf real >>>")  # passa o sniff, falha o parse
    with pytest.raises(pdf_ingest.PdfIngestError) as ei:
        pdf_ingest.rasterize_pdf(bad, tmp_path / "images")
    assert ei.value.reason == "corrupt"


def test_rasterize_page_cap_rejects(tmp_path, monkeypatch):
    monkeypatch.setenv("PDF_MAX_PAGES", "2")
    pdf = tmp_path / "big.pdf"
    _make_pdf(pdf, [(842, 595)] * 5)
    out = tmp_path / "images"
    with pytest.raises(pdf_ingest.PdfIngestError) as ei:
        pdf_ingest.rasterize_pdf(pdf, out)
    assert ei.value.reason == "too_many_pages"
    assert "Divida" in str(ei.value)
    # rejeita ANTES de renderizar → nenhum JPEG deixado no disco
    assert not list(out.glob("*.jpg"))


def test_rasterize_encrypted_raises(tmp_path, monkeypatch):
    def _raise(*_a, **_k):
        raise pdf_ingest.pdfium.PdfiumError(
            "Failed to load document (PDFium: Incorrect password error)."
        )
    monkeypatch.setattr(pdf_ingest.pdfium, "PdfDocument", _raise)
    pdf = tmp_path / "enc.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    with pytest.raises(pdf_ingest.PdfIngestError) as ei:
        pdf_ingest.rasterize_pdf(pdf, tmp_path / "images")
    assert ei.value.reason == "encrypted"
    assert "palavra-passe" in str(ei.value)


def test_rasterize_render_failure_cleans_partials(tmp_path, monkeypatch):
    """Se uma página falha a render a meio, limpa os JPEGs já escritos e levanta
    PdfIngestError (não deixa folhas órfãs de um PDF danificado)."""
    pdf = tmp_path / "scan.pdf"
    _make_pdf(pdf, [(842, 595)] * 3)
    out = tmp_path / "images"
    real = pdf_ingest._normalize_orientation
    calls = {"n": 0}

    def flaky(pil, direction):
        calls["n"] += 1
        if calls["n"] == 2:                 # falha na 2ª página
            raise RuntimeError("pagina danificada")
        return real(pil, direction)

    monkeypatch.setattr(pdf_ingest, "_normalize_orientation", flaky)
    with pytest.raises(pdf_ingest.PdfIngestError) as ei:
        pdf_ingest.rasterize_pdf(pdf, out)
    assert ei.value.reason == "corrupt"
    assert not list(out.glob("*.jpg"))      # o p01 já escrito foi removido


def test_rasterize_empty_raises(tmp_path, monkeypatch):
    class _FakeDoc:
        def __len__(self) -> int:
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr(pdf_ingest.pdfium, "PdfDocument", lambda *_a, **_k: _FakeDoc())
    pdf = tmp_path / "empty.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    with pytest.raises(pdf_ingest.PdfIngestError) as ei:
        pdf_ingest.rasterize_pdf(pdf, tmp_path / "images")
    assert ei.value.reason == "empty"
