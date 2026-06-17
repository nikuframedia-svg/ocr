"""Recover canonical OCR payloads from JSON or Nanonets-style HTML tables."""
from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from app.templates_registry import DEFAULT_TEMPLATE, get_template


class OCRResponseParseError(ValueError):
    """Raised when a VLM response is neither valid JSON nor mappable HTML."""


_MISSING_COMMA_BETWEEN_OBJECTS = re.compile(r"}(\s*\n\s*){")
_TAG_RE = re.compile(r"<[^>]+>")


def repair_llm_json(text: str) -> str:
    """Patch the common missing comma between sibling JSON row objects."""
    return _MISSING_COMMA_BETWEEN_OBJECTS.sub(r"},\1{", text)


def html_table_detected(raw: object) -> bool:
    return isinstance(raw, str) and "<table" in raw.lower()


def _collapse_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\xa0", " ")
    return " ".join(text.split()).strip()


def _label_key(value: object) -> str:
    text = _collapse_text(value).upper()
    text = text.replace("º", "").replace("°", "")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", "", text)


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _json_candidate(raw: str) -> str:
    text = _strip_think(raw)
    if "```" in text:
        for part in text.split("```"):
            stripped = part.strip()
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].strip()
            if stripped.startswith("{"):
                text = stripped
                break
    start, end = text.find("{"), text.rfind("}") + 1
    return text[start:end] if start != -1 and end > start else text


def parse_json_response(raw: str) -> dict[str, Any]:
    candidate = _json_candidate(raw)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = json.loads(repair_llm_json(candidate))
    if not isinstance(parsed, dict):
        raise OCRResponseParseError("JSON response is not an object")
    return parsed


def _iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_iter_strings(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_iter_strings(item))
        return out
    return []


def _find_html_source(raw: str, parsed: dict[str, Any] | None = None) -> str | None:
    if html_table_detected(raw):
        return raw
    if parsed is not None:
        chunks = [s for s in _iter_strings(parsed) if html_table_detected(s)]
        if chunks:
            return "\n".join(chunks)
    return None


class _TableExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            colspan = 1
            for name, value in attrs:
                if name.lower() == "colspan":
                    try:
                        colspan = max(1, int(value or "1"))
                    except ValueError:
                        colspan = 1
            if colspan > 1:
                self._cell.append(f"__COLSPAN_{colspan}__")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            colspan = 1
            parts = self._cell
            if parts and parts[0].startswith("__COLSPAN_"):
                try:
                    colspan = int(parts[0].replace("__COLSPAN_", "").replace("__", ""))
                except ValueError:
                    colspan = 1
                parts = parts[1:]
            value = _collapse_text(" ".join(parts))
            self._row.append(value)
            for _ in range(max(0, colspan - 1)):
                self._row.append("")
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell.strip() for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _html_tables(raw: str) -> list[list[list[str]]]:
    parser = _TableExtractor()
    parser.feed(raw)
    parser.close()
    return parser.tables


def _alias_map(aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for field, labels in aliases.items():
        for label in (field, *labels):
            out[_label_key(label)] = field
    return out


_HEADER_ALIASES = _alias_map({
    "operador": ("OPERADOR", "OPERATOR"),
    "n_operador": ("N", "N OPERADOR", "NO OPERADOR", "NUM OPERADOR", "NUMERO OPERADOR"),
    "setor_maquina": ("SETOR", "SETOR MAQUINA", "SETOR/MAQUINA", "MAQUINA"),
    "cod_maquina": ("COD MAQUINA", "CODIGO MAQUINA", "COD MAQ", "COD"),
    "data": ("DATA", "DATE"),
    "turno": ("TURNO", "T"),
})

_FOOTER_ALIASES = _alias_map({
    "colunas_produzidas": (
        "COLUNAS PRODUZIDAS", "TOTAL QTD", "TOTAL QUANTIDADE", "TOTAL COLUNAS",
    ),
    "horas_trabalhadas": ("HORAS TRABALHADAS", "HORAS", "HORAS TRAB"),
})

_ROW_ALIASES = _alias_map({
    "pri": ("PRI", "PR", "PRIORIDADE"),
    "pf": ("PF",),
    "cliente": ("CLIENTE", "CLIENT"),
    "ov": ("OV",),
    "of": ("OF",),
    "modelo": ("MODELO", "REFERENCIA", "REFERENCIA PECA", "REF PECA", "PECA"),
    "qtd": ("QTD", "QUANTIDADE", "QTD COLUNAS", "QTD COL", "QTD PECAS"),
    "comp_mm": ("COMP", "COMP MM", "COMPRIMENTO", "COMPRIMENTO MM"),
    "larg_mm": ("LARG", "LARG MM", "LARGURA", "LARGURA MM"),
    "lote": ("LOTE", "LOTE SAP"),
    "coni": ("CONI", "FERRAMENTA"),
    "esp": ("ESP", "ESPESSURA"),
    "lbase": ("LBASE", "L BASE", "LBASE MM"),
    "ltopo": ("LTOPO", "L TOPO", "LTOPO MM"),
    "qtd_metros": ("QTD METROS", "QTD M", "METROS", "QTD MT"),
    "qtd_m2": ("QTD M2",),
    "sobras": ("SOBRAS",),
    "cesta_n": ("CESTA", "CESTA N", "CESTA NUMERO"),
    "dbase": ("DBASE", "D BASE"),
    "dtopo": ("DTOPO", "D TOPO"),
    "cf": ("CF",),
    "m2": ("M2", "M 2"),
    "nesting": ("NESTING",),
    "inicio": ("INICIO", "INICIO"),
    "fim": ("FIM",),
    "np": ("NP", "N P"),
    "motivo": ("MOTIVO", "MOTIVO DA PARAGEM", "PARAGEM"),
    "duracao": ("DURACAO", "DURACAO PARAGEM"),
    "resolvido": ("RESOLVIDO",),
})


def _header_field(label: object) -> str | None:
    return _HEADER_ALIASES.get(_label_key(label))


def _footer_field(label: object) -> str | None:
    return _FOOTER_ALIASES.get(_label_key(label))


def _row_field(label: object) -> str | None:
    return _ROW_ALIASES.get(_label_key(label))


def _assign_key_values(row: list[str], header: dict[str, str], footer: dict[str, str]) -> None:
    if len(row) < 2:
        return
    for idx in range(0, len(row) - 1, 2):
        key = row[idx]
        value = row[idx + 1]
        if not key or not value:
            continue
        h_field = _header_field(key)
        if h_field is not None:
            header[h_field] = value
            continue
        f_field = _footer_field(key)
        if f_field is not None:
            footer[f_field] = value


def _assign_horizontal_kv(
    rows: list[list[str]], header: dict[str, str], footer: dict[str, str]
) -> None:
    for idx, row in enumerate(rows[:-1]):
        next_row = rows[idx + 1]
        if any(_header_field(cell) or _footer_field(cell) for cell in next_row):
            continue
        h_mapping = {i: f for i, cell in enumerate(row) if (f := _header_field(cell))}
        f_mapping = {i: f for i, cell in enumerate(row) if (f := _footer_field(cell))}
        if len(h_mapping) >= 2:
            for col, field in h_mapping.items():
                if col < len(next_row) and next_row[col]:
                    header[field] = next_row[col]
        if len(f_mapping) >= 1:
            for col, field in f_mapping.items():
                if col < len(next_row) and next_row[col]:
                    footer[field] = next_row[col]


def _horizontal_metadata_value_rows(rows: list[list[str]]) -> set[int]:
    value_rows: set[int] = set()
    for idx, row in enumerate(rows[:-1]):
        next_row = rows[idx + 1]
        if any(_header_field(cell) or _footer_field(cell) for cell in next_row):
            continue
        header_labels = sum(1 for cell in row if _header_field(cell))
        footer_labels = sum(1 for cell in row if _footer_field(cell))
        if header_labels >= 2 or footer_labels >= 1:
            value_rows.add(idx + 1)
    return value_rows


def _row_header_mapping(row: list[str], row_fields: tuple[str, ...]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    allowed = set(row_fields)
    used: set[str] = set()
    for idx, cell in enumerate(row):
        field = _row_field(cell)
        if field in allowed and field not in used:
            mapping[idx] = field
            used.add(field)
    return mapping


def _is_label_only(row: list[str]) -> bool:
    nonempty = [cell for cell in row if cell]
    if not nonempty:
        return True
    known = 0
    for cell in nonempty:
        if _row_field(cell) or _header_field(cell) or _footer_field(cell):
            known += 1
    return known == len(nonempty)


def _is_kv_row(row: list[str]) -> bool:
    return bool(row and (_header_field(row[0]) or _footer_field(row[0])))


def _row_from_mapping(
    cells: list[str], mapping: dict[int, str], row_fields: tuple[str, ...]
) -> dict[str, str] | None:
    out = {field: "" for field in row_fields}
    for idx, field in mapping.items():
        if idx < len(cells):
            out[field] = cells[idx]
    return out if any(out.values()) else None


def _row_from_order(cells: list[str], row_fields: tuple[str, ...]) -> dict[str, str] | None:
    min_cells = min(3, len(row_fields))
    if len([cell for cell in cells if cell]) < min_cells:
        return None
    if _is_label_only(cells) or _is_kv_row(cells):
        return None
    out = {field: "" for field in row_fields}
    for field, value in zip(row_fields, cells, strict=False):
        out[field] = value
    return out if any(out.values()) else None


def _extract_rows(
    table: list[list[str]],
    row_fields: tuple[str, ...],
    *,
    skip_indices: set[int] | None = None,
) -> list[dict[str, str]]:
    if not row_fields:
        return []
    skip_indices = skip_indices or set()
    best_idx = -1
    best_mapping: dict[int, str] = {}
    for idx, row in enumerate(table):
        mapping = _row_header_mapping(row, row_fields)
        if len(mapping) > len(best_mapping):
            best_idx = idx
            best_mapping = mapping
    rows: list[dict[str, str]] = []
    if len(best_mapping) >= 2:
        for idx, cells in enumerate(table[best_idx + 1:], start=best_idx + 1):
            if idx in skip_indices:
                continue
            if _is_label_only(cells) or _is_kv_row(cells):
                continue
            parsed = _row_from_mapping(cells, best_mapping, row_fields)
            if parsed:
                rows.append(parsed)
        return rows
    for idx, cells in enumerate(table):
        if idx in skip_indices:
            continue
        parsed = _row_from_order(cells, row_fields)
        if parsed:
            rows.append(parsed)
    return rows


def parse_html_tables(
    raw: str,
    *,
    row_fields: tuple[str, ...] | list[str] | None = None,
    header_fields: tuple[str, ...] | list[str] | None = None,
    footer_fields: tuple[str, ...] | list[str] | None = None,
    template_name: str | None = None,
) -> dict[str, Any]:
    template = get_template(template_name) if template_name else DEFAULT_TEMPLATE
    row_fields_t = tuple(row_fields or template.row_fields)
    header_fields_t = tuple(header_fields or template.header_fields)
    footer_fields_t = tuple(template.footer_fields if footer_fields is None else footer_fields)
    tables = _html_tables(raw)
    if not tables:
        raise OCRResponseParseError("No HTML tables found in response")
    header = {field: "" for field in header_fields_t}
    footer = {field: "" for field in footer_fields_t}
    rows: list[dict[str, str]] = []
    for table in tables:
        for table_row in table:
            _assign_key_values(table_row, header, footer)
        _assign_horizontal_kv(table, header, footer)
        rows.extend(
            _extract_rows(
                table,
                row_fields_t,
                skip_indices=_horizontal_metadata_value_rows(table),
            )
        )
    return {"header": header, "rows": rows, "footer": footer}


def _has_invalid_html_rows(payload: dict[str, Any]) -> bool:
    rows = payload.get("rows")
    if isinstance(rows, str) and html_table_detected(rows):
        return True
    return "rows" in payload and not isinstance(rows, list)


def parse_ocr_response(
    raw: str,
    *,
    row_fields: tuple[str, ...] | list[str] | None = None,
    header_fields: tuple[str, ...] | list[str] | None = None,
    footer_fields: tuple[str, ...] | list[str] | None = None,
    template_name: str | None = None,
) -> dict[str, Any]:
    """Parse VLM OCR output as JSON first, then as HTML tables."""
    parsed: dict[str, Any] | None = None
    json_error: Exception | None = None
    try:
        parsed = parse_json_response(raw)
    except Exception as exc:  # noqa: BLE001 - keep original for message below
        json_error = exc
    html_source = _find_html_source(raw, parsed)
    if (
        parsed is not None
        and not _has_invalid_html_rows(parsed)
        and not (html_source and parsed.get("rows") == [])
    ):
        return parsed
    if html_source:
        return parse_html_tables(
            html_source,
            row_fields=row_fields,
            header_fields=header_fields,
            footer_fields=footer_fields,
            template_name=template_name,
        )
    if json_error is not None:
        raise OCRResponseParseError(str(json_error)) from json_error
    raise OCRResponseParseError("JSON response has invalid rows and no HTML table fallback")


def detect_fustes_side(raw: str) -> str | None:
    """Return F/V from raw JSON, HTML or text side-detect responses."""
    try:
        parsed = parse_json_response(raw)
        side = str(parsed.get("side", "")).strip().upper()
        if side in ("F", "V"):
            return side
    except Exception:  # noqa: BLE001
        pass
    text = _label_key(_TAG_RE.sub(" ", _strip_think(raw)))
    if "MOTIVODAPARAGEM" in text or ("DURACAO" in text and "RESOLVIDO" in text):
        return "V"
    if "PRI" in text and ("CLIENTE" in text or "OV" in text or "MODELO" in text):
        return "F"
    return None


__all__ = [
    "OCRResponseParseError",
    "detect_fustes_side",
    "html_table_detected",
    "parse_html_tables",
    "parse_json_response",
    "parse_ocr_response",
    "repair_llm_json",
]
