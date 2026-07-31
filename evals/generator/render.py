"""Rendering: structural PDFs for hermetic runs, real HTML→PDF for live runs.

``minimal`` produces a valid one-page PDF whose bytes embed the case id — enough
for the storage/pdfium plumbing that fixture-mode evals exercise (the FakeLLM never
looks at pixels). ``html`` renders the Jinja template families with Playwright
Chromium for live runs, where the model must actually read the document.
"""

import io
import random
from pathlib import Path
from typing import Any

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def render_minimal_pdf(marker: str) -> bytes:
    """A structurally valid single-page PDF; `marker` varies the content hash."""
    out = bytearray(f"%PDF-1.4\n%case:{marker}\n".encode())
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>",
    ]
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_position = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(bodies) + 1} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n"
    ).encode()
    return bytes(out)


def render_html(truth: dict[str, Any], template: str) -> str:
    from jinja2 import Environment, FileSystemLoader

    environment = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR), autoescape=True, trim_blocks=True
    )
    return environment.get_template(f"{template}.html.j2").render(inv=truth)


def html_to_pdf(html: str) -> bytes:
    """Print-to-PDF via Playwright Chromium (live-run rendering path)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        pdf = page.pdf(format="Letter", print_background=True)
        browser.close()
        return pdf


def scan_noise_variant(pdf_bytes: bytes, *, seed: int) -> bytes:
    """Rasterize page 1 and add scan-like noise (rotation, blur, JPEG artifacts)."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    rng = random.Random(seed)
    document = pdfium.PdfDocument(pdf_bytes)
    try:
        image = document[0].render(scale=2.0).to_pil().convert("RGB")
    finally:
        document.close()

    image = image.rotate(rng.uniform(-1.5, 1.5), expand=True, fillcolor="white")
    image = image.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.4, 0.9)))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=rng.randint(45, 70))
    reloaded = Image.open(buffer).convert("RGB")
    out = io.BytesIO()
    reloaded.save(out, format="PNG")
    return out.getvalue()
