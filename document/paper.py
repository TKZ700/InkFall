"""Blank-paper templates (M8 New Blank customization).

Single source of truth for a customized blank page: one
:class:`PaperSpec` drives BOTH the creation dialog's live preview and
the backend PDF generation, so a template can never drift between what
the user saw and what gets written. Qt-free and pymupdf-free by design
(``document.backend`` is the only pymupdf boundary; the dialog preview
uses its own QPainter interpreter of the same primitives).

Drawing vocabulary (page-point coords, shared interpreters):

("line",   x0, y0, x1, y1, alpha, width)     stroke segment
("dot",    cx, cy, r)                        filled circle
("rect",   x0, y0, w, h, alpha, width)       stroked rectangle
("band",   x0, y0, w, h, alpha)              translucent fill
``alpha`` is 0..1 opacity; ``width`` is stroke width in points.
Unknown kinds are skipped by both interpreters (forward-compatible).
"""

from __future__ import annotations

import dataclasses
import json
import math

__all__ = [
    "PaperSpec", "TEMPLATE_ORDER", "TEMPLATE_LABELS", "PAPER_SIZES",
    "page_size_pt", "pattern_primitives", "pt_from_mm",
    "DEFAULT_SPACING_MM", "MIN_SPACING_MM", "MAX_SPACING_MM",
    "PAPER_METADATA_PREFIX", "paper_metadata_value",
    "paper_spec_from_metadata",
]

#: Prefix for the compact JSON ``PaperSpec`` carried in PDF keywords.
#: Deliberately distinctive so a regular PDF's own keywords can never be
#: mistaken for an Inkfall paper definition.
PAPER_METADATA_PREFIX = "inkfall.paper.v1:"

# 72 PDF points per 25.4 mm (the app-wide mm->pt convention).
PT_PER_MM = 72.0 / 25.4


def pt_from_mm(value_mm: float) -> float:
    """mm -> PDF points (clamped to non-negative)."""
    try:
        value = float(value_mm)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value) * PT_PER_MM


#: Named paper sizes in millimetres (portrait base). US Letter keeps the
#: inch-derived 215.9 x 279.4 so it saves as true Letter, not "A4-ish".
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "Letter": (215.9, 279.4),
}

#: Stable UI order for the template chooser + preview generation.
TEMPLATE_ORDER: tuple[str, ...] = (
    "plain", "ruled", "striped", "dotted", "square", "graph",
    "cornell", "iso",
)

#: Human labels (dialog chips).
TEMPLATE_LABELS: dict[str, str] = {
    "plain": "Plain",
    "ruled": "Ruled",
    "striped": "Striped",
    "dotted": "Dotted",
    "square": "Square grid",
    "graph": "Graph",
    "cornell": "Cornell",
    "iso": "Isometric",
}

#: Default pattern spacing (mm) for the busy templates.
DEFAULT_SPACING_MM = 8.0
MIN_SPACING_MM = 4.0
MAX_SPACING_MM = 15.0

# Common page margins for the ruled/family templates (points).
_EDGE = 18.0

#: Isometric family angles (degrees, 0 = horizontal, y down).
_ISO_ANGLES = (0.0, 60.0, 120.0)


@dataclasses.dataclass(frozen=True, slots=True)
class PaperSpec:
    """One customized blank page definition.

    ``template`` / ``size_name`` / ``orientation`` are normalized by
    :meth:`normalized`; unknown values fall back to the closest safe
    default so the Create action can never produce an invalid spec.
    Colors are ``#rrggbb`` hex strings.
    """

    template: str = "plain"
    size_name: str = "A4"
    orientation: str = "portrait"
    paper_color: str = "#ffffff"
    pattern_color: str = "#3b4252"
    spacing_mm: float = DEFAULT_SPACING_MM

    def normalized(self) -> "PaperSpec":
        """Sanitized copy (never raises for stored/forwarded values)."""
        template = self.template if self.template in TEMPLATE_LABELS else "plain"
        size = self.size_name if self.size_name in PAPER_SIZES else "A4"
        orientation = ("landscape"
                       if self.orientation == "landscape" else "portrait")
        try:
            spacing = float(self.spacing_mm)
        except (TypeError, ValueError):
            spacing = DEFAULT_SPACING_MM
        spacing = max(MIN_SPACING_MM, min(MAX_SPACING_MM, spacing))
        if not math.isfinite(spacing):
            spacing = DEFAULT_SPACING_MM
        return PaperSpec(
            template=template,
            size_name=size,
            orientation=orientation,
            paper_color=str(self.paper_color or "#ffffff"),
            pattern_color=str(self.pattern_color or "#3b4252"),
            spacing_mm=spacing,
        )


def page_size_pt(spec: PaperSpec) -> tuple[float, float]:
    """Displayed page size (width, height) in points for the spec."""
    spec = spec.normalized()
    base_w_mm, base_h_mm = PAPER_SIZES[spec.size_name]
    if spec.orientation == "landscape":
        base_w_mm, base_h_mm = base_h_mm, base_w_mm
    return (base_w_mm * PT_PER_MM, base_h_mm * PT_PER_MM)


def paper_metadata_value(spec: PaperSpec) -> str:
    """Encode a spec as one PDF-keywords token (survives save/reopen)."""
    spec = spec.normalized()
    payload = {
        "template": spec.template,
        "size_name": spec.size_name,
        "orientation": spec.orientation,
        "paper_color": spec.paper_color,
        "pattern_color": spec.pattern_color,
        "spacing_mm": spec.spacing_mm,
    }
    return PAPER_METADATA_PREFIX + json.dumps(
        payload, separators=(",", ":"), sort_keys=True)


def paper_spec_from_metadata(value) -> PaperSpec | None:
    """Decode a spec from PDF keywords; ``None`` when absent/invalid.

    Tolerates other keywords before/after the Inkfall token and never
    raises: an ordinary PDF (or a damaged token) simply has no
    recoverable paper definition.
    """
    try:
        text = str(value or "")
        start = text.find(PAPER_METADATA_PREFIX)
        if start < 0:
            return None
        decoder = json.JSONDecoder()
        data, _end = decoder.raw_decode(
            text, start + len(PAPER_METADATA_PREFIX))
        if not isinstance(data, dict):
            return None
        return PaperSpec(
            template=str(data.get("template", "plain")),
            size_name=str(data.get("size_name", "A4")),
            orientation=str(data.get("orientation", "portrait")),
            paper_color=str(data.get("paper_color", "#ffffff")),
            pattern_color=str(data.get("pattern_color", "#3b4252")),
            spacing_mm=data.get("spacing_mm", DEFAULT_SPACING_MM),
        ).normalized()
    except Exception:
        return None


def _rule_lines(spacing_pt: float, page_width: float, page_height: float,
                alpha: float, stroke_width: float):
    """Horizontal rule lines for ruled / square zones (shared helper)."""
    items = []
    y = _EDGE + spacing_pt
    while y < page_height - _EDGE + 0.01:
        items.append(("line", 0.0, y, page_width, y, alpha, stroke_width))
        y += spacing_pt
    return items


def _dot_grid(spacing_pt: float, page_width: float, page_height: float):
    items = []
    radius = max(0.6, min(1.4, spacing_pt * 0.055))
    y = _EDGE + spacing_pt
    row = 0
    while y < page_height - _EDGE + 0.01:
        # Every other row staggers half a step (quincunx dot paper).
        x = _EDGE + spacing_pt * (1.5 if row % 2 == 1 else 1.0)
        while x < page_width - _EDGE + 0.01:
            items.append(("dot", x, y, radius))
            x += spacing_pt
        y += spacing_pt
        row += 1
    return items


def _clip_ray(origin_pt: tuple[float, float], direction: tuple[float, float],
              w: float, h: float, half_diag: float):
    """One family line: origin + t*direction clipped to the page rect.

    Returns ((x0, y0), (x1, y1)) or None when the line misses the page
    entirely. The direction never degenerates (family angles are exact
    multiples of 30 degrees, never the y axis).
    """
    # Parametrize: p(t) = origin + t * direction; solve the two axis
    # slabs (x = 0, x = w, y = 0, y = h) for the extreme t values.
    dx, dy = direction
    t_lo, t_hi = -half_diag, half_diag
    for o, d, bound_lo, bound_hi in (
            (origin_pt[0], dx, 0.0, w), (origin_pt[1], dy, 0.0, h)):
        if abs(d) < 1e-9:
            if not bound_lo - 1e-6 <= o <= bound_hi + 1e-6:
                return None
            continue
        t0 = (bound_lo - o) / d
        t1 = (bound_hi - o) / d
        t_lo = max(t_lo, min(t0, t1))
        t_hi = min(t_hi, max(t0, t1))
    if t_hi <= t_lo + 1e-6:
        return None
    return ((origin_pt[0] + t_lo * dx, origin_pt[1] + t_lo * dy),
            (origin_pt[0] + t_hi * dx, origin_pt[1] + t_hi * dy))


def pattern_primitives(spec: PaperSpec, width_pt: float | None = None,
                       height_pt: float | None = None) -> tuple:
    """Every pattern primitive for the spec, in page-point coords.

    Shared by the dialog preview and the backend generator. Returns a
    tuple of the tuples documented at module level; deterministic and
    cheap (a few hundred items at most).

    ``width_pt`` / ``height_pt`` override the spec's named page size.
    Inserted pages use this to render the same template on the actual
    target page dimensions (including landscape/rotated targets) without
    duplicating any geometry here.
    """
    spec = spec.normalized()
    default_width, default_height = page_size_pt(spec)
    try:
        width = (float(default_width) if width_pt is None
                 else float(width_pt))
        height = (float(default_height) if height_pt is None
                  else float(height_pt))
    except (TypeError, ValueError):
        width, height = default_width, default_height
    if width <= 0.0 or height <= 0.0:
        return ()
    spacing_pt = pt_from_mm(spec.spacing_mm)
    if spacing_pt <= 0.0:
        return ()
    rule_alpha = 0.55
    rule_width = 0.7
    hard_width = 1.0
    template = spec.template

    if template == "plain":
        return ()

    if template == "ruled":
        return _rule_lines(spacing_pt, width, height,
                           rule_alpha, rule_width)

    if template == "striped":
        # Soft tinted bands with hard top edges: a lined sheet whose
        # "rules" are the band boundaries (reading-paper convention).
        band_h = spacing_pt * 0.35
        pitch = spacing_pt * 1.6
        items = []
        y = _EDGE
        row = 0
        while y + band_h < height - _EDGE + 0.01:
            items.append(("band", 0.0, y, width, band_h,
                          0.10 if row % 2 == 0 else 0.05))
            items.append(("line", 0.0, y, width, y, 0.45, rule_width))
            y += pitch
            row += 1
        return tuple(items)

    if template == "dotted":
        return _dot_grid(spacing_pt, width, height)

    if template == "square":
        items = []
        x = _EDGE
        while x < width - _EDGE + 0.01:
            items.append(("line", x, _EDGE, x, height - _EDGE,
                          rule_alpha, rule_width))
            x += spacing_pt
        items.extend(_rule_lines(spacing_pt, width, height,
                                 rule_alpha, rule_width))
        return tuple(items)

    if template == "graph":
        # Same grid at two weights: minor rules are fainter/thinner,
        # every fifth rule is the solid major line.
        items = []
        x = _EDGE
        column = 0
        while x < width - _EDGE + 0.01:
            major = column % 5 == 0
            items.append(("line", x, _EDGE, x, height - _EDGE,
                          0.85 if major else 0.30,
                          hard_width if major else rule_width))
            x += spacing_pt
            column += 1
        y = _EDGE + spacing_pt
        row = 1
        while y < height - _EDGE + 0.01:
            major = row % 5 == 0
            items.append(("line", _EDGE, y, width - _EDGE, y,
                          0.85 if major else 0.30,
                          hard_width if major else rule_width))
            y += spacing_pt
            row += 1
        # Frame the graph area with the hard weight.
        items.append(("line", _EDGE, _EDGE, width - _EDGE, _EDGE,
                      0.85, hard_width))
        items.append(("line", _EDGE, _EDGE, _EDGE, height - _EDGE,
                      0.85, hard_width))
        return tuple(items)

    if template == "cornell":
        # Classic note-taking sheet: header band on top, narrow cue
        # column on the left, note rules full width, summary band on
        # the bottom. Zones share the pattern color.
        items = []
        header_y = height * 0.16
        summary_y = height * 0.82
        cue_x = width * 0.30
        # Header + summary separators.
        items.append(("line", 0.0, header_y, width, header_y,
                      0.85, hard_width))
        items.append(("line", 0.0, summary_y, width, summary_y,
                      0.85, hard_width))
        items.append(("line", cue_x, header_y, cue_x, summary_y,
                      0.85, hard_width))
        # Ruled line feel inside the note zone (right of the cue
        # column), then lighter rules in the cue and summary zones.
        note_spacing = min(spacing_pt, 26.0)
        y = header_y + note_spacing
        while y < summary_y - 2.0:
            items.append(("line", cue_x + 6.0, y, width - 4.0, y,
                          rule_alpha, rule_width))
            y += note_spacing
        cue_spacing = min(spacing_pt * 0.75, 22.0)
        y = header_y + cue_spacing
        while y < summary_y - 2.0:
            items.append(("line", 4.0, y, cue_x - 6.0, y,
                          rule_alpha * 0.8, rule_width))
            y += cue_spacing
        return tuple(items)

    if template == "iso":
        # Three line families (0/60/120 degrees) clipped to the page;
        # triangle side length equals the spacing in points. Lines grow
        # from a staggered family origin so the tiling stays uniform.
        half_diag = math.hypot(width, height) + spacing_pt
        items = []
        step = spacing_pt * (math.sqrt(3.0) / 2.0)
        for index, angle in enumerate(_ISO_ANGLES):
            rad = math.radians(angle)
            direction = (math.cos(rad), math.sin(rad))
            normal = (-math.sin(rad), math.cos(rad))
            # Stagger family origins across the page diagonal so every
            # family covers the full sheet with even perpendicular
            # pitch.
            base = (width * 0.5, height * 0.5) if index != 0 else (0.0, 0.0)
            offset = -half_diag
            while offset <= half_diag:
                origin = (base[0] + normal[0] * offset,
                          base[1] + normal[1] * offset)
                clipped = _clip_ray(origin, direction, width, height,
                                    half_diag * 2.0)
                if clipped is not None:
                    (x0, y0), (x1, y1) = clipped
                    items.append(("line", x0, y0, x1, y1,
                                  rule_alpha, rule_width))
                offset += step
        return tuple(items)

    # Unknown template names normalize to plain (defensive: the spec
    # itself sanitizes, this only guards callers bypassing it).
    return ()
