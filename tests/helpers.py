"""Test-only PDF fixture helpers (M1).

PyMuPDF is allowed here because this module lives in the test suite.
Production code must never import from here.
"""

from __future__ import annotations

import pymupdf


def rasterize_page(pdf_path: str, page_index: int, scale: float):
    """Return (samples, width, height, stride) of one page at a scale."""
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False
        )
        return pix.samples, pix.width, pix.height, pix.stride
    finally:
        doc.close()


def rasterize_page_rgba(pdf_path: str, page_index: int, scale: float):
    """Return (samples, width, height, stride) with alpha (renderer contract)."""
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=True
        )
        return bytes(pix.samples), pix.width, pix.height, pix.stride
    finally:
        doc.close()


def page_count_of(pdf_path: str) -> int:
    """Return the page count of a PDF (test introspection helper)."""
    doc = pymupdf.open(pdf_path)
    try:
        return len(doc)
    finally:
        doc.close()


def committed_strokes(viewer, page_index=None) -> list:
    """Committed pen ``InkStroke``s from the owning document's store.

    M6.4: committed pen strokes live in the document's ``AnnotationStore``
    (the authoritative state); the viewer no longer keeps a parallel
    ``_strokes`` list. ``page_index=None`` (default) returns every page
    in page/insertion order, matching the old flat list's "all committed
    strokes" meaning for the single-page suites. Highlighter (and other)
    annotations are filtered out; only pen ``InkStroke``s are returned.
    An unopened/closed document yields an empty list.
    """
    from annotations.models import InkStroke

    try:
        store = viewer._document.annotations
    except Exception:
        return []
    out: list = []
    try:
        if page_index is None:
            source = (annotation for _, annotation in store.iter_all())
        else:
            source = iter(store.page(page_index))
        for annotation in source:
            if isinstance(annotation, InkStroke):
                out.append(annotation)
    except Exception:
        return []
    return out


def make_pdf(
    path: str,
    pages: int = 5,
    title: str = "Inkfall Test",
    author: str = "Inkfall",
    page_width: float = 595.0,
    page_height: float = 842.0,
) -> str:
    """Generate a deterministic multi-page PDF for tests. Returns path."""
    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page(width=page_width, height=page_height)
            page.insert_text((72.0, 72.0), f"Page {i + 1} of {pages}")
        doc.set_metadata({"title": title, "author": author})
        doc.save(path)
    finally:
        doc.close()
    return path


def make_encrypted_pdf(
    path: str,
    pages: int = 3,
    user_password: str = "secret",
    owner_password: str = "owner-secret",
) -> str:
    """Generate an encrypted PDF requiring a password. Returns path."""
    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page(width=595.0, height=842.0)
            page.insert_text((72.0, 72.0), f"Encrypted page {i + 1}")
        doc.save(
            path,
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw=user_password,
            owner_pw=owner_password,
        )
    finally:
        doc.close()
    return path


def make_invalid_pdf(path: str) -> str:
    """Write garbage bytes (not a PDF). Returns path."""
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 garbage\n\x00\x01\x02not-a-real-pdf\n")
    return path


def make_rich_pdf(
    path: str,
    pages: int = 4,
    page_width: float = 595.0,
    page_height: float = 842.0,
    title: str = "Inkfall Render Workload",
) -> str:
    """Generate a PDF with real raster workload (text + vector + gradient).

    Each page gets a title, body text lines, filled rectangles, diagonal
    rules, and a 64-step grayscale gradient band so tiles contain
    non-trivial pixels (never blank pages).
    """
    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page(width=page_width, height=page_height)
            page.insert_text((72.0, 60.0), f"{title} — page {i + 1}/{pages}", fontsize=16)
            for ln in range(12):
                page.insert_text(
                    (72.0, 110.0 + ln * 22.0),
                    f"Lorem ipsum line {ln + 1} on page {i + 1} " + "inkfall " * 8,
                    fontsize=11,
                )
            # Filled accent rectangles.
            for b in range(4):
                x0 = 72.0 + b * 110.0
                shade = (b * 60 + i * 13) % 200
                page.draw_rect(
                    pymupdf.Rect(x0, 420.0, x0 + 90.0, 480.0),
                    color=(0, 0, 0),
                    fill=(shade / 255.0, 0.2, 0.8 - shade / 900.0),
                    width=1.0,
                )
            # Diagonal rules.
            for d in range(5):
                y = 520.0 + d * 14.0
                page.draw_line(
                    pymupdf.Point(72.0, y),
                    pymupdf.Point(page_width - 72.0, y + 30.0),
                    color=(0.1, 0.1, 0.1),
                    width=1.5,
                )
            # Gradient band: 64 thin rects, black -> white.
            band_y, band_h = 640.0, 120.0
            for s in range(64):
                g = s / 63.0
                page.draw_rect(
                    pymupdf.Rect(
                        72.0 + s * (page_width - 144.0) / 64.0,
                        band_y,
                        72.0 + (s + 1) * (page_width - 144.0) / 64.0,
                        band_y + band_h,
                    ),
                    color=(g, g, g),
                    fill=(g, g, g),
                    width=0,
                )
        doc.set_metadata({"title": title, "author": "Inkfall"})
        doc.save(path)
    finally:
        doc.close()
    return path


def make_probe_pdf(path: str, page_width: float = 600.0, page_height: float = 800.0) -> str:
    """Single page with a solid black rect at top-left (orientation probe).

    Rect spans page coords (0,0)-(200,200); rest of the page is blank white.
    Lets tests verify rotation mapping without reading pixels via PyMuPDF.
    """
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=page_width, height=page_height)
        page.draw_rect(
            pymupdf.Rect(0.0, 0.0, 200.0, 200.0),
            color=(0, 0, 0),
            fill=(0, 0, 0),
            width=0,
        )
        doc.save(path)
    finally:
        doc.close()
    return path


def make_sized_pdf(path: str, sizes: list[tuple[float, float]]) -> str:
    """Multi-page PDF with explicit per-page (width, height) in points.

    M5-B.1.2 corpus: deterministic mixed-width documents for
    horizontal-scroll layout tests. Each page gets a probe rect.
    """
    doc = pymupdf.open()
    try:
        for i, (w, h) in enumerate(sizes):
            page = doc.new_page(width=float(w), height=float(h))
            page.draw_rect(
                pymupdf.Rect(0.0, 0.0, min(200.0, float(w)), min(200.0, float(h))),
                color=(0, 0, 0),
                fill=(0, 0, 0),
                width=0,
            )
            page.insert_text((72.0, 72.0), f"Sized page {i + 1} ({w:.0f}x{h:.0f})")
        doc.save(path)
    finally:
        doc.close()
    return path


def make_mixed_pdf(path: str, pages: int = 6) -> str:
    """Alternating portrait/landscape pages with varying sizes (M3-B corpus).

    Deterministic pattern: A4 portrait, A4 landscape, letter, square.
    """
    sizes = [(595.0, 842.0), (842.0, 595.0), (612.0, 792.0), (500.0, 500.0)]
    doc = pymupdf.open()
    try:
        for i in range(pages):
            w, h = sizes[i % len(sizes)]
            page = doc.new_page(width=w, height=h)
            page.insert_text((72.0, 72.0), f"Mixed page {i + 1} ({w:.0f}x{h:.0f})")
            page.draw_rect(
                pymupdf.Rect(72.0, 120.0, w - 72.0, h - 72.0),
                color=(0, 0, 0),
                width=1.0,
            )
        doc.save(path)
    finally:
        doc.close()
    return path


def dark_pixel_bbox_pt(pdf_path: str, page_index: int, scale: float = 0.5,
                       threshold: int = 128):
    """Bounding box of dark pixels on a page, in *displayed* page points.

    Returns ``(x0, y0, x1, y1)`` (half-open, displayed-post-``/Rotate``
    frame) or ``None`` when the page has no dark pixel. Used to verify
    that a page rotation moves the rasterized content to exactly where
    the annotations were remapped (M6.8.2 Stage 5.1).
    """
    doc = pymupdf.open(pdf_path)
    try:
        pix = doc[page_index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False)
        width, height, stride = pix.width, pix.height, pix.stride
        samples = pix.samples
        minx = maxx = miny = maxy = None
        for y in range(height):
            row = y * stride
            for x in range(width):
                i = row + x * 3
                if (samples[i] < threshold and samples[i + 1] < threshold
                        and samples[i + 2] < threshold):
                    if minx is None or x < minx:
                        minx = x
                    if maxx is None or x > maxx:
                        maxx = x
                    if miny is None or y < miny:
                        miny = y
                    if maxy is None or y > maxy:
                        maxy = y
        if minx is None:
            return None
        return (minx / scale, miny / scale, (maxx + 1) / scale,
                (maxy + 1) / scale)
    finally:
        doc.close()


def make_intrinsic_pdf(
    path: str,
    rotations: tuple[int, ...] = (0, 90, 180, 270),
    page_width: float = 600.0,
    page_height: float = 800.0,
) -> str:
    """Pages with intrinsic PDF /Rotate + top-left probe rect (M3-B corpus).

    Each page gets ``set_rotation(rot)`` and a solid black rect at page
    coords (0,0)-(200,200) so tests can verify intrinsic/user composition
    through rendered pixels.
    """
    doc = pymupdf.open()
    try:
        for i, rot in enumerate(rotations):
            page = doc.new_page(width=page_width, height=page_height)
            page.set_rotation(int(rot))
            page.draw_rect(
                pymupdf.Rect(0.0, 0.0, 200.0, 200.0),
                color=(0, 0, 0),
                fill=(0, 0, 0),
                width=0,
            )
            page.insert_text((72.0, 300.0), f"intrinsic={rot}")
        doc.save(path)
    finally:
        doc.close()
    return path


def make_dense_pdf(path: str, pages: int = 2, cells: int = 32) -> str:
    """Raster-heavy pages: dense checkerboard + text (M3-B corpus).

    Pure vector operators (no image codecs needed) that force the
    rasterizer to shade nearly every pixel — a heavy but deterministic
    tile workload. True JPEG/JPX corpora stay local-only manual fixtures.
    """
    doc = pymupdf.open()
    try:
        for i in range(pages):
            page = doc.new_page(width=595.0, height=842.0)
            cw, ch = 595.0 / cells, 700.0 / cells
            for cx in range(cells):
                for cy in range(cells):
                    g = ((cx * 7 + cy * 13 + i * 29) % 2) * 0.85 + 0.1
                    page.draw_rect(
                        pymupdf.Rect(cx * cw, 72.0 + cy * ch, (cx + 1) * cw, 72.0 + (cy + 1) * ch),
                        color=(g, g, g),
                        fill=(g, g, g),
                        width=0,
                    )
            page.insert_text((72.0, 40.0), f"Dense page {i + 1}")
        doc.save(path)
    finally:
        doc.close()
    return path
