"""Anuvia delivery branding — shared visual identity for client deliverables.

Single source of truth for HTML/PDF + PPTX rendering across all delivery
agents (finops_audit, ai_readiness, devops_maturity, ...). Tied to the LP
brand tokens defined in ``templates/_base.html``.

Public API:

  * ``render_deliverable_html(...)`` — full HTML document with cover page,
    running headers/footers, table styling, blockquotes, code blocks.
  * ``md_to_html_rich(md)`` — markdown -> HTML using ``markdown`` package
    with the ``tables``, ``fenced_code``, ``nl2br`` extensions, then a
    light post-processing pass to bolt Anuvia classnames onto tables,
    blockquotes, etc.
  * ``generate_pptx_deck(...)`` — build a light-theme PPTX deck in memory
    and return the binary bytes.

Design rules — DO NOT INVENT colors/fonts. Everything below is pulled
directly from ``templates/_base.html``.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Brand tokens — single source of truth (mirror of templates/_base.html)
# ---------------------------------------------------------------------------

BG_PAPER = "#fafaf9"          # warm off-white (page bg, card bg)
INK = "#1a1a1a"               # near-black (primary text, headings)
INK_MUTED = "#475569"         # secondary text on dark bg
STONE_500 = "#78716c"         # muted text (eyebrow)
STONE_200 = "#e7e5e4"         # borders / rules
WHITE = "#ffffff"

FONT_BODY = "'Inter', system-ui, -apple-system, 'Helvetica Neue', sans-serif"
FONT_HEAD = "'Playfair Display', Georgia, serif"
FONT_MONO = "'JetBrains Mono', 'SF Mono', ui-monospace, monospace"

GOOGLE_FONTS_LINK = (
    "https://fonts.googleapis.com/css2?"
    "family=Inter:wght@400;500;600;700"
    "&family=Playfair+Display:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@400;500&display=swap"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_pt() -> str:
    return _now().strftime("%d/%m/%Y")


# ---------------------------------------------------------------------------
# Markdown -> HTML rich
# ---------------------------------------------------------------------------


def md_to_html_rich(md: str) -> str:
    """Convert markdown to HTML with Anuvia styling.

    Uses the ``markdown`` package with ``tables``, ``fenced_code``, ``nl2br``
    so GFM-style tables actually render. Falls back to a tiny hand-rolled
    converter if the package is unavailable (keeps the delivery pipeline
    alive on minimal images).
    """
    if not md:
        return ""

    try:
        import markdown  # type: ignore
    except Exception:  # noqa: BLE001
        return _md_fallback(md)

    try:
        html = markdown.markdown(
            md,
            extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
            output_format="html5",
        )
    except Exception:  # noqa: BLE001
        # Re-try without sane_lists, which some older markdown versions
        # don't ship.
        try:
            html = markdown.markdown(
                md,
                extensions=["tables", "fenced_code", "nl2br"],
                output_format="html5",
            )
        except Exception:  # noqa: BLE001
            return _md_fallback(md)

    # Bolt Anuvia classnames onto the generated tags so the CSS hooks line up.
    html = re.sub(r"<table>", '<table class="anuvia-table">', html)
    html = re.sub(r"<blockquote>", '<blockquote class="anuvia-bq">', html)
    return html


def _md_fallback(md: str) -> str:
    """Last-resort tiny converter — preserves headings, lists, bold, code.

    Used only if the ``markdown`` package import fails. Matches the older
    hand-rolled converter so existing tests pass.
    """
    lines: List[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                lines.append("</ul>")
                in_list = False
            lines.append("")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line.strip())
        if m:
            if in_list:
                lines.append("</ul>")
                in_list = False
            level = len(m.group(1))
            tag = f"h{min(level + 1, 6)}"
            lines.append(f"<{tag}>{m.group(2)}</{tag}>")
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            if not in_list:
                lines.append("<ul>")
                in_list = True
            content = m.group(1)
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
            lines.append(f"<li>{content}</li>")
            continue
        if in_list:
            lines.append("</ul>")
            in_list = False
        content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
        lines.append(f"<p>{content}</p>")
    if in_list:
        lines.append("</ul>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML deliverable rendering
# ---------------------------------------------------------------------------


def _html_escape(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _cover_meta_block(meta: Dict[str, Any]) -> str:
    """Render the engagement-meta card on the cover page."""
    if not meta:
        return ""
    order = (
        "Cliente",
        "Período",
        "Baseline mensal",
        "Economia identificada",
        "Payback",
        "Analista responsável",
        "Analista",
    )
    rows: List[str] = []
    seen: set = set()
    for key in order:
        if key in seen or key not in meta:
            continue
        seen.add(key)
        value = meta.get(key)
        if value in (None, ""):
            continue
        rows.append(
            f'<div class="meta-row">'
            f'<div class="meta-label">{_html_escape(key)}</div>'
            f'<div class="meta-value">{_html_escape(value)}</div>'
            f"</div>"
        )
    # Any extra keys not in the canonical order — keep them, but at the end.
    for key, value in meta.items():
        if key in seen or value in (None, ""):
            continue
        seen.add(key)
        rows.append(
            f'<div class="meta-row">'
            f'<div class="meta-label">{_html_escape(key)}</div>'
            f'<div class="meta-value">{_html_escape(value)}</div>'
            f"</div>"
        )
    if not rows:
        return ""
    return f'<div class="meta-card">{"".join(rows)}</div>'


def _stylesheet(practice_label: str) -> str:
    """The full Anuvia stylesheet. Inlined so Gotenberg can render offline."""
    label_esc = _html_escape(practice_label)
    return f"""
@import url('{GOOGLE_FONTS_LINK}');

@page {{
  size: A4;
  margin: 22mm 18mm 22mm 18mm;
  @top-left {{
    content: "ANUVIA · {label_esc}";
    font-family: {FONT_BODY};
    font-size: 9px;
    letter-spacing: 0.18em;
    color: {STONE_500};
    font-weight: 600;
    text-transform: uppercase;
  }}
  @top-right {{
    content: "";
    border-bottom: 1px solid {STONE_200};
  }}
  @bottom-left {{
    content: "Anuvia Cloud & AI Consulting · Mila Vernazza";
    font-family: {FONT_BODY};
    font-size: 9px;
    color: {STONE_500};
  }}
  @bottom-right {{
    content: counter(page) " / " counter(pages);
    font-family: {FONT_BODY};
    font-size: 9px;
    color: {STONE_500};
  }}
}}

@page :first {{
  @top-left {{ content: ""; }}
  @top-right {{ content: ""; }}
  @bottom-left {{ content: ""; }}
  @bottom-right {{ content: ""; }}
}}

* {{ box-sizing: border-box; }}

html, body {{
  margin: 0;
  padding: 0;
  background: {WHITE};
  color: {INK};
  font-family: {FONT_BODY};
  font-size: 11px;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}}

.eyebrow {{
  font-family: {FONT_BODY};
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-weight: 600;
  color: {STONE_500};
}}

/* -------- Cover page -------- */

.cover-page {{
  page-break-after: always;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 32px 24px;
}}

.cover-top .cover-eyebrow {{
  margin: 0 0 8px;
}}

.cover-top .cover-rule {{
  height: 1px;
  background: {INK};
  width: 56px;
  margin-top: 12px;
}}

.cover-middle {{
  margin: 48px 0;
}}

.cover-title {{
  font-family: {FONT_HEAD};
  font-weight: 600;
  font-size: 44pt;
  line-height: 1.05;
  letter-spacing: -0.015em;
  color: {INK};
  margin: 0 0 18px;
}}

.cover-subtitle {{
  font-family: {FONT_BODY};
  font-size: 14pt;
  color: {INK_MUTED};
  margin: 0 0 36px;
  font-weight: 400;
}}

.meta-card {{
  background: {BG_PAPER};
  border: 1px solid {STONE_200};
  padding: 22px 26px;
  margin-top: 28px;
  display: block;
}}

.meta-row {{
  display: flex;
  align-items: baseline;
  gap: 20px;
  padding: 10px 0;
  border-bottom: 1px solid {STONE_200};
}}

.meta-row:last-child {{
  border-bottom: none;
}}

.meta-label {{
  flex: 0 0 38%;
  font-size: 10px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  font-weight: 600;
  color: {STONE_500};
}}

.meta-value {{
  flex: 1 1 auto;
  font-size: 12px;
  color: {INK};
  font-weight: 500;
}}

.cover-bottom {{
  border-top: 1px solid {STONE_200};
  padding-top: 18px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 10px;
  color: {STONE_500};
  letter-spacing: 0.04em;
}}

.cover-bottom .cover-brand {{
  font-family: {FONT_HEAD};
  font-size: 14pt;
  color: {INK};
  letter-spacing: -0.01em;
}}

/* -------- Body -------- */

.body-wrap {{
  padding: 0;
}}

.body-eyebrow {{
  margin: 0 0 4px;
}}

.body-title {{
  font-family: {FONT_HEAD};
  font-weight: 600;
  font-size: 28pt;
  letter-spacing: -0.015em;
  color: {INK};
  margin: 0 0 8px;
  line-height: 1.1;
}}

.body-subtitle {{
  font-family: {FONT_BODY};
  font-size: 11pt;
  color: {INK_MUTED};
  margin: 0 0 24px;
  font-weight: 400;
}}

.body-rule {{
  height: 1px;
  background: {STONE_200};
  margin: 0 0 24px;
}}

.body-content h1 {{
  font-family: {FONT_HEAD};
  font-size: 28px;
  font-weight: 600;
  color: {INK};
  margin: 32px 0 8px;
  letter-spacing: -0.015em;
}}

.body-content h2 {{
  font-family: {FONT_HEAD};
  font-size: 18px;
  font-weight: 600;
  color: {INK};
  margin: 32px 0 12px;
  padding-bottom: 6px;
  border-bottom: 1px solid {STONE_200};
  letter-spacing: -0.01em;
}}

.body-content h3 {{
  font-family: {FONT_BODY};
  font-size: 11px;
  font-weight: 600;
  color: {INK};
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin: 20px 0 8px;
}}

.body-content h4 {{
  font-family: {FONT_BODY};
  font-size: 12px;
  font-weight: 600;
  color: {INK_MUTED};
  margin: 14px 0 6px;
}}

.body-content p {{
  margin: 8px 0 10px;
}}

.body-content ul, .body-content ol {{
  margin: 6px 0 12px;
  padding-left: 22px;
}}

.body-content li {{
  margin: 4px 0;
}}

.body-content li > ul, .body-content li > ol {{
  margin: 4px 0 4px;
}}

.body-content a {{
  color: {INK};
  text-decoration: underline;
  text-decoration-color: {STONE_200};
}}

.body-content strong {{
  font-weight: 600;
  color: {INK};
}}

/* -------- Anuvia table -------- */

.anuvia-table {{
  width: 100%;
  border-collapse: collapse;
  margin: 16px 0;
  font-size: 10.5px;
  page-break-inside: auto;
}}

.anuvia-table thead {{
  display: table-header-group;
}}

.anuvia-table thead th {{
  background: {INK};
  color: {BG_PAPER};
  text-align: left;
  padding: 10px 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
  font-size: 10.5px;
}}

.anuvia-table tbody td {{
  padding: 8px 12px;
  border-bottom: 1px solid {STONE_200};
  vertical-align: top;
}}

.anuvia-table tbody tr:nth-child(even) {{
  background: {BG_PAPER};
}}

.anuvia-table tbody tr {{
  page-break-inside: avoid;
}}

/* -------- Blockquote -------- */

.anuvia-bq {{
  margin: 16px 0;
  padding: 12px 16px;
  border-left: 3px solid {INK};
  background: {BG_PAPER};
  color: {INK_MUTED};
  font-style: italic;
}}

.anuvia-bq p {{
  margin: 4px 0;
}}

/* -------- Code -------- */

code {{
  font-family: {FONT_MONO};
  font-size: 10px;
  background: {BG_PAPER};
  border: 1px solid {STONE_200};
  padding: 1px 5px;
  border-radius: 3px;
}}

pre {{
  background: {INK};
  color: {BG_PAPER};
  padding: 14px 18px;
  border-radius: 6px;
  font-family: {FONT_MONO};
  font-size: 10px;
  line-height: 1.6;
  overflow-x: auto;
  margin: 14px 0;
}}

pre code {{
  background: transparent;
  border: none;
  padding: 0;
  color: inherit;
  font-size: inherit;
}}

hr {{
  border: none;
  border-top: 1px solid {STONE_200};
  margin: 24px 0;
}}

/* Avoid orphan headings */
.body-content h1, .body-content h2, .body-content h3 {{
  page-break-after: avoid;
}}
"""


def render_deliverable_html(
    *,
    practice_label: str,
    title: str,
    subtitle: str,
    body_md: str,
    engagement_meta: Optional[dict] = None,
    show_cover: bool = True,
) -> str:
    """Return a full HTML document ready for Gotenberg PDF conversion.

    Args:
        practice_label: e.g. ``"FINOPS AUDIT"``. Rendered in eyebrow + footer.
        title: Document title (Playfair big).
        subtitle: Smaller line under the title.
        body_md: Raw markdown — converted with ``md_to_html_rich``.
        engagement_meta: Optional dict rendered on the cover meta card.
        show_cover: If True, render a dedicated cover page (default).
    """
    body_html = md_to_html_rich(body_md)
    css = _stylesheet(practice_label)
    label_esc = _html_escape(practice_label)
    title_esc = _html_escape(title)
    subtitle_esc = _html_escape(subtitle)
    today = _today_pt()

    if show_cover:
        cover_block = f"""
<section class="cover-page">
  <div class="cover-top">
    <p class="eyebrow cover-eyebrow">ANUVIA · {label_esc}</p>
    <div class="cover-rule"></div>
  </div>
  <div class="cover-middle">
    <h1 class="cover-title">{title_esc}</h1>
    <p class="cover-subtitle">{subtitle_esc}</p>
    {_cover_meta_block(engagement_meta or {})}
  </div>
  <div class="cover-bottom">
    <div class="cover-brand">Anuvia</div>
    <div>{today} · Documento confidencial</div>
  </div>
</section>
""".strip()
    else:
        cover_block = ""

    body_block = f"""
<section class="body-wrap">
  <p class="eyebrow body-eyebrow">ANUVIA · {label_esc}</p>
  <h1 class="body-title">{title_esc}</h1>
  <p class="body-subtitle">{subtitle_esc}</p>
  <div class="body-rule"></div>
  <div class="body-content">
{body_html}
  </div>
</section>
""".strip()

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{title_esc}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{GOOGLE_FONTS_LINK}" rel="stylesheet">
<style>{css}</style>
</head>
<body>
{cover_block}
{body_block}
</body>
</html>"""


# ---------------------------------------------------------------------------
# PPTX deck generation (light theme — matches LP, NOT proposal-sidecar dark)
# ---------------------------------------------------------------------------


# Brand tokens in pptx RGBColor form. Defined as hex tuples here and lifted
# at call time so module-level import doesn't blow up if python-pptx is
# missing (we want the module to import cleanly even without it).
_PPTX_INK_HEX = (0x1A, 0x1A, 0x1A)
_PPTX_PAPER_HEX = (0xFA, 0xFA, 0xF9)
_PPTX_WHITE_HEX = (0xFF, 0xFF, 0xFF)
_PPTX_STONE_500_HEX = (0x78, 0x71, 0x6C)
_PPTX_STONE_200_HEX = (0xE7, 0xE5, 0xE4)
_PPTX_MUTED_HEX = (0x47, 0x55, 0x69)

# 16:9 widescreen — same as proposal sidecar.
_PPTX_SLIDE_WIDTH_IN = 13.333
_PPTX_SLIDE_HEIGHT_IN = 7.5


def _import_pptx():
    """Lazy import — raises a clear error if python-pptx isn't installed."""
    from pptx import Presentation  # type: ignore
    from pptx.util import Inches, Pt, Emu  # type: ignore
    from pptx.dml.color import RGBColor  # type: ignore
    from pptx.enum.shapes import MSO_SHAPE  # type: ignore
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR  # type: ignore

    return {
        "Presentation": Presentation,
        "Inches": Inches,
        "Pt": Pt,
        "Emu": Emu,
        "RGBColor": RGBColor,
        "MSO_SHAPE": MSO_SHAPE,
        "PP_ALIGN": PP_ALIGN,
        "MSO_ANCHOR": MSO_ANCHOR,
    }


def _add_rect(slide, mod, left, top, width, height, color_hex):
    shp = slide.shapes.add_shape(mod["MSO_SHAPE"].RECTANGLE, left, top, width, height)
    shp.fill.solid()
    shp.fill.fore_color.rgb = mod["RGBColor"](*color_hex)
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _add_text(
    slide,
    mod,
    text,
    left,
    top,
    width,
    height,
    *,
    size=14,
    bold=False,
    italic=False,
    color_hex=_PPTX_INK_HEX,
    font="Calibri",
    align=None,
    anchor=None,
    letter_spacing=None,
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = mod["Emu"](0)
    tf.margin_right = mod["Emu"](0)
    tf.margin_top = mod["Emu"](0)
    tf.margin_bottom = mod["Emu"](0)
    if anchor is not None:
        tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    if align is not None:
        p.alignment = align
    run = p.add_run()
    run.text = text or ""
    run.font.size = mod["Pt"](size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = mod["RGBColor"](*color_hex)
    run.font.name = font
    return tb


def _add_bullets(
    slide,
    mod,
    bullets: List[str],
    left,
    top,
    width,
    height,
    *,
    size=16,
    color_hex=_PPTX_INK_HEX,
    font="Calibri",
    bullet_char="—",
):
    tb = slide.shapes.add_textbox(left, top, width, height)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = mod["Emu"](0)
    tf.margin_right = mod["Emu"](0)
    tf.margin_top = mod["Emu"](0)
    tf.margin_bottom = mod["Emu"](0)
    for idx, raw in enumerate(bullets or []):
        text = (raw or "").strip()
        if not text:
            continue
        if idx == 0:
            p = tf.paragraphs[0]
        else:
            p = tf.add_paragraph()
        p.space_after = mod["Pt"](6)
        run = p.add_run()
        run.text = f"{bullet_char}  {text}"
        run.font.size = mod["Pt"](size)
        run.font.color.rgb = mod["RGBColor"](*color_hex)
        run.font.name = font
    return tb


def _set_speaker_notes(slide, notes: Optional[str]) -> None:
    if not notes:
        return
    try:
        slide.notes_slide.notes_text_frame.text = notes
    except Exception:  # noqa: BLE001
        # Some pptx versions are picky; non-fatal.
        pass


def _slide_chrome(slide, mod, practice_label: str, page_num: int, total: int):
    """Top eyebrow + bottom footer with page number. Skip on cover/section."""
    # Top eyebrow.
    _add_text(
        slide, mod,
        f"ANUVIA · {practice_label}",
        mod["Inches"](0.5),
        mod["Inches"](0.35),
        mod["Inches"](8),
        mod["Inches"](0.3),
        size=9,
        bold=True,
        color_hex=_PPTX_STONE_500_HEX,
        font="Calibri",
    )
    # Thin rule.
    _add_rect(
        slide, mod,
        mod["Inches"](0.5),
        mod["Inches"](0.7),
        mod["Inches"](12.33),
        mod["Emu"](9525),  # ~1px
        _PPTX_STONE_200_HEX,
    )
    # Bottom footer left.
    _add_text(
        slide, mod,
        f"Anuvia Cloud & AI Consulting · {_today_pt()}",
        mod["Inches"](0.5),
        mod["Inches"](7.05),
        mod["Inches"](8),
        mod["Inches"](0.3),
        size=8,
        color_hex=_PPTX_STONE_500_HEX,
    )
    # Bottom footer right (page x / y).
    _add_text(
        slide, mod,
        f"{page_num} / {total}",
        mod["Inches"](11.5),
        mod["Inches"](7.05),
        mod["Inches"](1.33),
        mod["Inches"](0.3),
        size=8,
        color_hex=_PPTX_STONE_500_HEX,
        align=mod["PP_ALIGN"].RIGHT,
    )


def _build_cover_slide(prs, mod, *, practice_label, title, subtitle, client_name, engagement_id):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    # White bg (default), but explicit shape for safety.
    _add_rect(
        slide, mod,
        0, 0,
        mod["Inches"](_PPTX_SLIDE_WIDTH_IN),
        mod["Inches"](_PPTX_SLIDE_HEIGHT_IN),
        _PPTX_WHITE_HEX,
    )
    # Top eyebrow.
    _add_text(
        slide, mod,
        f"ANUVIA · {practice_label}",
        mod["Inches"](0.7),
        mod["Inches"](0.7),
        mod["Inches"](10),
        mod["Inches"](0.4),
        size=11,
        bold=True,
        color_hex=_PPTX_STONE_500_HEX,
    )
    # Rule under eyebrow.
    _add_rect(
        slide, mod,
        mod["Inches"](0.7),
        mod["Inches"](1.15),
        mod["Inches"](0.6),
        mod["Emu"](19050),  # ~2px
        _PPTX_INK_HEX,
    )
    # Big Playfair title — fall back to Georgia.
    _add_text(
        slide, mod,
        title,
        mod["Inches"](0.7),
        mod["Inches"](2.3),
        mod["Inches"](11.9),
        mod["Inches"](2.0),
        size=44,
        bold=True,
        color_hex=_PPTX_INK_HEX,
        font="Playfair Display",
    )
    # Subtitle.
    if subtitle:
        _add_text(
            slide, mod,
            subtitle,
            mod["Inches"](0.7),
            mod["Inches"](4.3),
            mod["Inches"](11.9),
            mod["Inches"](0.6),
            size=18,
            color_hex=_PPTX_MUTED_HEX,
        )
    # Bottom-right client + date.
    _add_text(
        slide, mod,
        client_name or "—",
        mod["Inches"](7.5),
        mod["Inches"](6.5),
        mod["Inches"](5.3),
        mod["Inches"](0.4),
        size=12,
        bold=True,
        color_hex=_PPTX_INK_HEX,
        align=mod["PP_ALIGN"].RIGHT,
    )
    _add_text(
        slide, mod,
        f"{_today_pt()} · Engagement {engagement_id}",
        mod["Inches"](7.5),
        mod["Inches"](6.9),
        mod["Inches"](5.3),
        mod["Inches"](0.4),
        size=10,
        color_hex=_PPTX_STONE_500_HEX,
        align=mod["PP_ALIGN"].RIGHT,
    )
    # Bottom-left brand mark.
    _add_text(
        slide, mod,
        "Anuvia",
        mod["Inches"](0.7),
        mod["Inches"](6.5),
        mod["Inches"](4),
        mod["Inches"](0.8),
        size=22,
        bold=False,
        color_hex=_PPTX_INK_HEX,
        font="Playfair Display",
    )


def _build_section_slide(prs, mod, *, title, subtitle=None):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(
        slide, mod,
        0, 0,
        mod["Inches"](_PPTX_SLIDE_WIDTH_IN),
        mod["Inches"](_PPTX_SLIDE_HEIGHT_IN),
        _PPTX_INK_HEX,
    )
    _add_text(
        slide, mod,
        title,
        mod["Inches"](0.7),
        mod["Inches"](3.0),
        mod["Inches"](11.9),
        mod["Inches"](1.5),
        size=40,
        bold=True,
        color_hex=_PPTX_WHITE_HEX,
        font="Playfair Display",
        align=mod["PP_ALIGN"].CENTER,
    )
    if subtitle:
        _add_text(
            slide, mod,
            subtitle,
            mod["Inches"](0.7),
            mod["Inches"](4.3),
            mod["Inches"](11.9),
            mod["Inches"](0.6),
            size=16,
            color_hex=_PPTX_STONE_200_HEX,
            align=mod["PP_ALIGN"].CENTER,
        )


def _build_content_slide(prs, mod, *, practice_label, title, subtitle, bullets, page_num, total):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(
        slide, mod,
        0, 0,
        mod["Inches"](_PPTX_SLIDE_WIDTH_IN),
        mod["Inches"](_PPTX_SLIDE_HEIGHT_IN),
        _PPTX_WHITE_HEX,
    )
    _slide_chrome(slide, mod, practice_label, page_num, total)
    # Title.
    _add_text(
        slide, mod,
        title,
        mod["Inches"](0.5),
        mod["Inches"](1.0),
        mod["Inches"](12.3),
        mod["Inches"](0.9),
        size=28,
        bold=True,
        color_hex=_PPTX_INK_HEX,
        font="Playfair Display",
    )
    if subtitle:
        _add_text(
            slide, mod,
            subtitle,
            mod["Inches"](0.5),
            mod["Inches"](1.85),
            mod["Inches"](12.3),
            mod["Inches"](0.4),
            size=13,
            color_hex=_PPTX_MUTED_HEX,
        )
    # Bullets.
    _add_bullets(
        slide, mod,
        bullets or [],
        mod["Inches"](0.6),
        mod["Inches"](2.5),
        mod["Inches"](12.1),
        mod["Inches"](4.3),
        size=16,
        color_hex=_PPTX_INK_HEX,
    )
    return slide


def _build_two_col_slide(
    prs, mod, *,
    practice_label, title, subtitle,
    left_title, left_bullets,
    right_title, right_bullets,
    page_num, total,
):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(
        slide, mod,
        0, 0,
        mod["Inches"](_PPTX_SLIDE_WIDTH_IN),
        mod["Inches"](_PPTX_SLIDE_HEIGHT_IN),
        _PPTX_WHITE_HEX,
    )
    _slide_chrome(slide, mod, practice_label, page_num, total)
    _add_text(
        slide, mod,
        title,
        mod["Inches"](0.5),
        mod["Inches"](1.0),
        mod["Inches"](12.3),
        mod["Inches"](0.9),
        size=28,
        bold=True,
        color_hex=_PPTX_INK_HEX,
        font="Playfair Display",
    )
    if subtitle:
        _add_text(
            slide, mod,
            subtitle,
            mod["Inches"](0.5),
            mod["Inches"](1.85),
            mod["Inches"](12.3),
            mod["Inches"](0.4),
            size=13,
            color_hex=_PPTX_MUTED_HEX,
        )
    # Column titles.
    if left_title:
        _add_text(
            slide, mod, left_title,
            mod["Inches"](0.6),
            mod["Inches"](2.4),
            mod["Inches"](5.8),
            mod["Inches"](0.4),
            size=12, bold=True,
            color_hex=_PPTX_STONE_500_HEX,
        )
    if right_title:
        _add_text(
            slide, mod, right_title,
            mod["Inches"](6.9),
            mod["Inches"](2.4),
            mod["Inches"](5.8),
            mod["Inches"](0.4),
            size=12, bold=True,
            color_hex=_PPTX_STONE_500_HEX,
        )
    # Bullets.
    _add_bullets(
        slide, mod,
        left_bullets or [],
        mod["Inches"](0.6),
        mod["Inches"](2.9),
        mod["Inches"](5.8),
        mod["Inches"](3.9),
        size=15,
        color_hex=_PPTX_INK_HEX,
    )
    _add_bullets(
        slide, mod,
        right_bullets or [],
        mod["Inches"](6.9),
        mod["Inches"](2.9),
        mod["Inches"](5.8),
        mod["Inches"](3.9),
        size=15,
        color_hex=_PPTX_INK_HEX,
    )
    return slide


def _build_closing_slide(prs, mod, *, practice_label, title, subtitle, bullets, page_num, total):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(
        slide, mod,
        0, 0,
        mod["Inches"](_PPTX_SLIDE_WIDTH_IN),
        mod["Inches"](_PPTX_SLIDE_HEIGHT_IN),
        _PPTX_WHITE_HEX,
    )
    _slide_chrome(slide, mod, practice_label, page_num, total)
    _add_text(
        slide, mod,
        title or "Próximos passos",
        mod["Inches"](0.5),
        mod["Inches"](1.0),
        mod["Inches"](12.3),
        mod["Inches"](0.9),
        size=32,
        bold=True,
        color_hex=_PPTX_INK_HEX,
        font="Playfair Display",
    )
    if subtitle:
        _add_text(
            slide, mod, subtitle,
            mod["Inches"](0.5),
            mod["Inches"](1.95),
            mod["Inches"](12.3),
            mod["Inches"](0.5),
            size=14,
            color_hex=_PPTX_MUTED_HEX,
        )
    _add_bullets(
        slide, mod,
        bullets or [],
        mod["Inches"](0.6),
        mod["Inches"](2.7),
        mod["Inches"](12.1),
        mod["Inches"](3.6),
        size=16,
        color_hex=_PPTX_INK_HEX,
    )
    # Signature.
    _add_rect(
        slide, mod,
        mod["Inches"](0.5),
        mod["Inches"](6.2),
        mod["Inches"](12.3),
        mod["Emu"](9525),
        _PPTX_STONE_200_HEX,
    )
    _add_text(
        slide, mod,
        "Mila Vernazza · founder@anuvia.com.br",
        mod["Inches"](0.5),
        mod["Inches"](6.35),
        mod["Inches"](12.3),
        mod["Inches"](0.4),
        size=12,
        bold=True,
        color_hex=_PPTX_INK_HEX,
    )
    _add_text(
        slide, mod,
        "Anuvia Cloud & AI Consulting",
        mod["Inches"](0.5),
        mod["Inches"](6.7),
        mod["Inches"](12.3),
        mod["Inches"](0.3),
        size=10,
        color_hex=_PPTX_STONE_500_HEX,
    )


async def generate_pptx_deck(
    *,
    practice_label: str,
    title: str,
    client_name: str,
    engagement_id: str,
    slides: List[dict],
) -> bytes:
    """Build a .pptx Presentation in-memory and return the binary bytes.

    ``slides`` is a list of dicts shaped like::

        {
            "type": "cover" | "section" | "content" | "two_col" | "closing",
            "title": str,
            "subtitle": str | None,
            "bullets": list[str] | None,
            "notes": str | None,
            "left_bullets": list[str] | None,
            "right_bullets": list[str] | None,
            "left_title": str | None,
            "right_title": str | None,
        }

    The first slide in the list is rendered as a cover (overriding type),
    the last as a closing slide (also overriding) so callers can pass a
    plain list of content-shaped specs and still get bookends.
    """
    mod = _import_pptx()
    prs = mod["Presentation"]()
    prs.slide_width = mod["Inches"](_PPTX_SLIDE_WIDTH_IN)
    prs.slide_height = mod["Inches"](_PPTX_SLIDE_HEIGHT_IN)

    if not slides:
        slides = [{
            "type": "content",
            "title": title,
            "subtitle": "",
            "bullets": ["(deck vazio — engenharia revisar)"],
        }]

    total = len(slides)

    for idx, spec in enumerate(slides):
        page_num = idx + 1
        # Force bookends.
        if idx == 0:
            stype = "cover"
        elif idx == total - 1 and total > 1:
            stype = spec.get("type") or "closing"
            if stype not in ("closing", "section"):
                stype = "closing"
        else:
            stype = spec.get("type") or "content"

        s_title = spec.get("title") or ""
        s_subtitle = spec.get("subtitle") or ""
        bullets = spec.get("bullets") or []

        if stype == "cover":
            _build_cover_slide(
                prs, mod,
                practice_label=practice_label,
                title=s_title or title,
                subtitle=s_subtitle,
                client_name=client_name,
                engagement_id=engagement_id,
            )
            slide_obj = prs.slides[-1]
        elif stype == "section":
            _build_section_slide(prs, mod, title=s_title, subtitle=s_subtitle)
            slide_obj = prs.slides[-1]
        elif stype == "two_col":
            slide_obj = _build_two_col_slide(
                prs, mod,
                practice_label=practice_label,
                title=s_title,
                subtitle=s_subtitle,
                left_title=spec.get("left_title"),
                left_bullets=spec.get("left_bullets") or [],
                right_title=spec.get("right_title"),
                right_bullets=spec.get("right_bullets") or [],
                page_num=page_num,
                total=total,
            )
        elif stype == "closing":
            _build_closing_slide(
                prs, mod,
                practice_label=practice_label,
                title=s_title or "Próximos passos",
                subtitle=s_subtitle,
                bullets=bullets,
                page_num=page_num,
                total=total,
            )
            slide_obj = prs.slides[-1]
        else:  # content
            slide_obj = _build_content_slide(
                prs, mod,
                practice_label=practice_label,
                title=s_title,
                subtitle=s_subtitle,
                bullets=bullets,
                page_num=page_num,
                total=total,
            )

        _set_speaker_notes(slide_obj, spec.get("notes"))

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Markdown deck parser — turns Claude's "### Slide N — Title" md into specs
# ---------------------------------------------------------------------------


_SLIDE_HEADER_RE = re.compile(
    r"^#{2,4}\s*(?:Slide\s*)?(\d+)?\s*[—\-:.]?\s*(.*?)\s*$",
    re.IGNORECASE,
)
_NOTES_RE = re.compile(r"^\s*\(?\s*notas?\s*:\s*(.+?)\)?\s*$", re.IGNORECASE)


def parse_deck_markdown(md: str) -> List[dict]:
    """Parse a Claude-generated deck markdown into slide specs.

    Recognised structure::

        ### Slide 1 — Capa
        - bullet 1
        - bullet 2

        (notas: speaker notes)

        ### Slide 2 — Sumário
        - bullet

    Heuristics:
      * First slide -> cover
      * Last slide -> closing
      * Slides whose title contains "Vetor N" / "Quick Win" / etc -> content
      * Notes captured from any "(notas: ...)" or "Notas: ..." line
    """
    if not md:
        return []

    slides: List[dict] = []
    current: Optional[dict] = None
    notes_lines: List[str] = []

    def _flush_notes_to_current():
        nonlocal notes_lines
        if current is not None and notes_lines:
            joined = "\n".join(line.strip() for line in notes_lines if line.strip())
            if joined:
                existing = current.get("notes") or ""
                current["notes"] = (existing + "\n\n" + joined).strip() if existing else joined
        notes_lines = []

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        # New slide header?
        if stripped.startswith("#"):
            m = _SLIDE_HEADER_RE.match(stripped.lstrip("#").strip())
            title_text = stripped.lstrip("#").strip()
            # Strip leading "Slide N —" prefix from the title.
            cleaned = re.sub(
                r"^Slide\s*\d+\s*[—\-:.]?\s*", "", title_text, flags=re.IGNORECASE,
            )
            if current is not None:
                _flush_notes_to_current()
                slides.append(current)
            current = {
                "type": "content",
                "title": cleaned or title_text,
                "subtitle": "",
                "bullets": [],
                "notes": "",
            }
            continue

        # Speaker notes line — "(notas: ...)" or "Notas: ..."
        n = _NOTES_RE.match(stripped)
        if n:
            notes_lines.append(n.group(1).strip())
            continue

        # Bullet?
        bm = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bm and current is not None:
            bullet = bm.group(1).strip()
            # Strip markdown bold markers — they don't render in PPTX runs.
            bullet = re.sub(r"\*\*(.+?)\*\*", r"\1", bullet)
            bullet = re.sub(r"`([^`]+)`", r"\1", bullet)
            current.setdefault("bullets", []).append(bullet)
            continue

        # Free-floating text → subtitle (first line) or extra notes.
        if current is not None:
            if not current.get("subtitle"):
                # Strip md formatting.
                txt = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped)
                txt = re.sub(r"`([^`]+)`", r"\1", txt)
                # Don't promote bullets-disguised-as-text to subtitle.
                current["subtitle"] = txt[:160]
            else:
                notes_lines.append(stripped)

    if current is not None:
        _flush_notes_to_current()
        slides.append(current)

    # Assign types: first=cover, last=closing.
    total = len(slides)
    for idx, s in enumerate(slides):
        title_low = (s.get("title") or "").lower()
        if idx == 0:
            s["type"] = "cover"
        elif idx == total - 1 and total > 1:
            # Closing if the title hints at it, otherwise still closing
            # (consistent bookends).
            s["type"] = "closing"
        else:
            # Section dividers when title is purely a thematic break.
            if any(k in title_low for k in ("sumário", "agenda", "índice")):
                s["type"] = "content"
            else:
                s["type"] = "content"

    return slides
