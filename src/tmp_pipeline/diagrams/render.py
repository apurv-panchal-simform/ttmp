"""The sheet without its photograph: images blanked to white so only the drawn design remains (X8 image 1)."""
from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as raw
from PIL import Image

from ..constants import MASTER_DPI


def render_overlay(pdf_path: str | Path, page: int, dpi: int = MASTER_DPI) -> Image.Image:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        pg = doc[page - 1]
        white = pdfium.PdfBitmap.new_native(2, 2, raw.FPDFBitmap_BGR)
        white.fill_rect((255, 255, 255, 255), 0, 0, 2, 2)
        for obj in pg.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            obj.set_bitmap(white)
        pg.gen_content()
        return pg.render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        doc.close()


def save_overlay(pdf_path: str | Path, page: int, out_dir: str | Path, dpi: int = MASTER_DPI) -> tuple[Path, tuple[int, int]]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    img = render_overlay(pdf_path, page, dpi)
    path = out / f"page{page:02d}_overlay.webp"
    img.save(path, "WEBP", quality=85)
    return path, img.size
