"""Split a Kanban photo into 3 region crops.

Layout assumption (validated against the 19 Apr-2026 sample sheets):

```
0%   ┌────────────────────────────────┐
     │ KANBAN logo · PRODUÇÃO · OPER. │   header
     │ N° / SETOR / DATA              │
22%  ├────────────────────────────────┤
     │ PRI | CLIENTE | OV | OF | ...  │
     │  ...rows...                    │   table
     │                                │
85%  ├────────────────────────────────┤
     │ COLUNAS PRODUZIDAS | HORAS     │   footer
100% └────────────────────────────────┘
```

Each crop is widened by 4 pp on the boundaries (so header overlaps
table by ~4 pp on the seam) — gives the model a bit of vertical
context to disambiguate cells that touch the boundary.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from PIL import Image

# Boundaries as fractions of total image height. Seams overlap by a
# few percent so the model can read cells that straddle them.
_HEADER_END = 0.22
_TABLE_START = 0.18
_TABLE_END = 0.87
_FOOTER_START = 0.83


@dataclass(frozen=True)
class Crops:
    """Three crops of a single Kanban image, plus a stable id."""

    header: Image.Image
    table: Image.Image
    footer: Image.Image
    source_sha: str  # sha256 of the original image bytes (short)

    def to_dict(self) -> dict[str, Image.Image]:
        return {"header": self.header, "table": self.table, "footer": self.footer}


def _hash_image(image: Image.Image) -> str:
    """Cheap stable id for the image. Used in audit logs."""
    h = hashlib.sha256()
    h.update(f"{image.size[0]}x{image.size[1]}".encode())
    # Sample 64x64 thumbnail bytes — enough to differentiate sheets,
    # cheap to compute. Not a cryptographic identifier.
    thumb = image.copy()
    thumb.thumbnail((64, 64))
    h.update(thumb.tobytes())
    return h.hexdigest()[:12]


def split_kanban(image: Image.Image) -> Crops:
    """Return :class:`Crops` of the input image. Caller controls
    encoding and resize downstream."""
    width, height = image.size
    header = image.crop((0, 0, width, int(height * _HEADER_END)))
    table = image.crop(
        (0, int(height * _TABLE_START), width, int(height * _TABLE_END))
    )
    footer = image.crop((0, int(height * _FOOTER_START), width, height))
    return Crops(
        header=header,
        table=table,
        footer=footer,
        source_sha=_hash_image(image),
    )
