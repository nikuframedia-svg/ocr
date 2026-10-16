"""Unchanged reference workbooks must not be parsed again every minute."""
import os

import openpyxl

from app.cross_check import ref_importer


def test_inspection_cache_reuses_only_identical_workbooks(tmp_path, monkeypatch):
    path = tmp_path / "plan.xlsx"

    def write(order):
        wb = openpyxl.Workbook()
        wb.active.append(["of", "ov", "quanttrp"])
        wb.active.append([order, "240001", 10])
        wb.save(path)
        wb.close()

    write("260001")
    original = ref_importer._inspect_refs_xlsx_uncached
    calls = []

    def inspect(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(ref_importer, "_inspect_refs_xlsx_uncached", inspect)
    first = ref_importer.inspect_refs_xlsx(path, "plan")
    assert first[0] is None
    first[1]["n_rows"] = 999
    assert ref_importer.inspect_refs_xlsx(path, "plan")[1]["n_rows"] == 1
    assert calls == [1]

    stat = path.stat()
    write("260002")
    # Even a writer preserving the timestamp cannot reuse another content's result.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert ref_importer.inspect_refs_xlsx(path, "plan")[1]["max_of"] == 260002
    assert calls == [1, 1]


def test_transient_open_failures_are_retried(tmp_path, monkeypatch):
    path = tmp_path / "locked.xlsx"
    path.write_bytes(b"temporary")
    calls = []

    def inspect(*args):
        calls.append(1)
        return "não consegui abrir o Excel (temporarily locked)", {}

    monkeypatch.setattr(ref_importer, "_inspect_refs_xlsx_uncached", inspect)
    ref_importer.inspect_refs_xlsx(path, "plan")
    ref_importer.inspect_refs_xlsx(path, "plan")
    assert len(calls) == 2


def test_same_size_and_timestamp_still_requires_same_content(tmp_path, monkeypatch):
    path = tmp_path / "same-metadata.xlsx"
    path.write_bytes(b"AAAA")
    stat = path.stat()
    monkeypatch.setattr(ref_importer, "_inspect_refs_xlsx_uncached",
                        lambda p, kind: (None, {"observed": p.read_bytes().decode()}))
    assert ref_importer.inspect_refs_xlsx(path, "plan")[1]["observed"] == "AAAA"
    path.write_bytes(b"BBBB")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    assert ref_importer.inspect_refs_xlsx(path, "plan")[1]["observed"] == "BBBB"
