"""Page renders (plan section 1.2): 200 DPI masters, on-demand 300 DPI crops, perceptual hashes."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from ....constants import MASTER_DPI
from ...geometry import bbox_pt_to_px

CROP_DPI = 300


class Renderer:
    def __init__(self, pdf_path: str | Path):
        self._doc = pdfium.PdfDocument(str(pdf_path))

    def __enter__(self) -> Renderer:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._doc.close()

    def page_size(self, page: int) -> tuple[float, float]:
        return self._doc[page - 1].get_size()

    def render(self, page: int, dpi: int = MASTER_DPI) -> Image.Image:
        return self._doc[page - 1].render(scale=dpi / 72).to_pil().convert("RGB")

    def save_master(self, page: int, out_dir: str | Path, dpi: int = MASTER_DPI) -> tuple[Path, tuple[int, int]]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        img = self.render(page, dpi)
        path = out / f"page{page:02d}_master.webp"
        img.save(path, "WEBP", quality=85)
        return path, img.size

    def crop(self, page: int, bbox_pt, dpi: int = CROP_DPI, pad_pt: float = 0.0) -> Image.Image:
        """Crop a point box [x0, y0, x1, y1] (bottom-left origin) from a render at `dpi`."""
        w, h = self.page_size(page)
        x0, y0, x1, y1 = bbox_pt
        box = [max(0.0, x0 - pad_pt), max(0.0, y0 - pad_pt), min(w, x1 + pad_pt), min(h, y1 + pad_pt)]
        u0, v0, u1, v1 = bbox_pt_to_px(box, h, dpi)
        return self.render(page, dpi).crop((round(u0), round(v0), round(u1), round(v1)))


def perceptual_hash(img: Image.Image) -> str:
    """64-bit pHash (32x32 grey DCT, top-left 8x8 against the median) as 16 hex characters."""
    grey = np.asarray(img.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32)
    low = cv2.dct(grey)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return f"{value:016x}"
