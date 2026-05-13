"""Generate watermarked sample PDFs for Anuvia diagnostic deliverables.

Usage:
    python3 scripts/generate_sample_pdfs.py

Outputs 6 PDFs in static/samples/.
"""
from __future__ import annotations

import os
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, PageBreak,
    Table, TableStyle, KeepTogether, Flowable,
)
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------------- Brand ---------------- #
PAPER = HexColor("#fafaf9")
INK = HexColor("#1a1a1a")
SUBTLE = HexColor("#78716c")
RULE = HexColor("#e7e5e4")
ACCENT = HexColor("#0c4a6e")
WATERMARK = HexColor("#e7e5e4")

# Use built-in fonts (Times-Roman serif, Helvetica sans) — universally available.
SERIF = "Times-Roman"
SERIF_BOLD = "Times-Bold"
SERIF_ITALIC = "Times-Italic"
SANS = "Helvetica"
SANS_BOLD = "Helvetica-Bold"

PAGE_W, PAGE_H = A4
MARGIN_L = 22 * mm
MARGIN_R = 22 * mm
MARGIN_T = 24 * mm
MARGIN_B = 22 * mm

# ---------------- Styles ---------------- #
def make_styles():
    ss = getSampleStyleSheet()
    s = {}
    s["title"] = ParagraphStyle(
        "title", parent=ss["Normal"], fontName=SERIF_BOLD, fontSize=28,
        leading=34, textColor=INK, alignment=TA_LEFT, spaceAfter=14,
    )
    s["h1"] = ParagraphStyle(
        "h1", parent=ss["Normal"], fontName=SERIF_BOLD, fontSize=20,
        leading=26, textColor=INK, alignment=TA_LEFT, spaceBefore=4, spaceAfter=10,
    )
    s["h2"] = ParagraphStyle(
        "h2", parent=ss["Normal"], fontName=SERIF_BOLD, fontSize=14,
        leading=18, textColor=INK, alignment=TA_LEFT, spaceBefore=10, spaceAfter=6,
    )
    s["eyebrow"] = ParagraphStyle(
        "eyebrow", parent=ss["Normal"], fontName=SANS_BOLD, fontSize=8,
        leading=10, textColor=ACCENT, alignment=TA_LEFT, spaceAfter=4,
    )
    s["body"] = ParagraphStyle(
        "body", parent=ss["Normal"], fontName=SANS, fontSize=10,
        leading=15, textColor=INK, alignment=TA_LEFT, spaceAfter=6,
    )
    s["body_just"] = ParagraphStyle(
        "body_just", parent=s["body"], alignment=TA_JUSTIFY,
    )
    s["subtle"] = ParagraphStyle(
        "subtle", parent=ss["Normal"], fontName=SANS, fontSize=9,
        leading=13, textColor=SUBTLE, alignment=TA_LEFT, spaceAfter=4,
    )
    s["small"] = ParagraphStyle(
        "small", parent=ss["Normal"], fontName=SANS, fontSize=8,
        leading=11, textColor=SUBTLE, alignment=TA_LEFT,
    )
    s["pull"] = ParagraphStyle(
        "pull", parent=ss["Normal"], fontName=SERIF_ITALIC, fontSize=12,
        leading=18, textColor=INK, alignment=TA_LEFT, leftIndent=10,
        borderColor=ACCENT, borderPadding=(4, 0, 4, 8),
    )
    s["bullet"] = ParagraphStyle(
        "bullet", parent=s["body"], leftIndent=14, bulletIndent=0,
        spaceAfter=3,
    )
    return s

STYLES = make_styles()

# ---------------- Page Frame ---------------- #
def _draw_watermark(c: canvas.Canvas):
    c.saveState()
    # proper PDF alpha ~10% on a mid-gray so on cream paper the watermark
    # reads as a soft tint that does not obscure the content
    try:
        c.setFillAlpha(0.10)
    except Exception:
        pass
    c.translate(PAGE_W / 2, PAGE_H / 2)
    c.rotate(45)
    c.setFont(SERIF_BOLD, 40)
    c.setFillColor(HexColor("#5f5650"))
    txt = "SAMPLE  ·  CONFIDENTIAL  ·  NOT FOR DISTRIBUTION"
    c.drawCentredString(0, 150, txt)
    c.drawCentredString(0, 0, txt)
    c.drawCentredString(0, -150, txt)
    c.restoreState()


def _draw_header_footer(c: canvas.Canvas, doc):
    # background paper color
    c.saveState()
    c.setFillColor(PAPER)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    c.restoreState()

    # watermark behind content
    _draw_watermark(c)

    # header
    c.saveState()
    c.setFont(SERIF_BOLD, 10)
    c.setFillColor(INK)
    c.drawString(MARGIN_L, PAGE_H - 14 * mm, "Anuvia")
    c.setFont(SANS, 8)
    c.setFillColor(SUBTLE)
    c.drawRightString(PAGE_W - MARGIN_R, PAGE_H - 14 * mm, f"Page {doc.page}")
    # header rule
    c.setStrokeColor(RULE)
    c.setLineWidth(0.4)
    c.line(MARGIN_L, PAGE_H - 17 * mm, PAGE_W - MARGIN_R, PAGE_H - 17 * mm)
    # footer rule
    c.line(MARGIN_L, MARGIN_B - 8 * mm, PAGE_W - MARGIN_R, MARGIN_B - 8 * mm)
    # footer
    c.setFont(SANS, 8)
    c.setFillColor(SUBTLE)
    c.drawCentredString(PAGE_W / 2, MARGIN_B - 14 * mm, "anuvia.com.br")
    c.restoreState()


def _draw_cover(c: canvas.Canvas, doc):
    """Cover page — same chrome as inner pages so watermark still applies."""
    _draw_header_footer(c, doc)


def build_doc(path: str, story: list):
    doc = BaseDocTemplate(
        path,
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T + 4 * mm, bottomMargin=MARGIN_B + 4 * mm,
        title="Anuvia · Sample Deliverable",
        author="Anuvia",
        subject="Sample · Confidential · Not for distribution",
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height,
        leftPadding=0, bottomPadding=0, rightPadding=0, topPadding=0,
        id="normal",
    )
    template = PageTemplate(id="all", frames=[frame], onPage=_draw_header_footer)
    doc.addPageTemplates([template])
    doc.build(story)


# ---------------- Helpers ---------------- #
def hr(width=None, color=RULE, thickness=0.5, space=8):
    class HR(Flowable):
        def __init__(self):
            super().__init__()
            self.width = width or (PAGE_W - MARGIN_L - MARGIN_R)
            self.height = space
        def draw(self):
            self.canv.setStrokeColor(color)
            self.canv.setLineWidth(thickness)
            self.canv.line(0, self.height/2, self.width, self.height/2)
    return HR()


def kv_table(rows, col_widths=None):
    cw = col_widths or [55 * mm, 110 * mm]
    t = Table(rows, colWidths=cw)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), SANS, 9),
        ("TEXTCOLOR", (0, 0), (0, -1), SUBTLE),
        ("TEXTCOLOR", (1, 0), (1, -1), INK),
        ("FONT", (0, 0), (0, -1), SANS_BOLD, 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
    ]))
    return t


def data_table(header, rows, col_widths=None, align_right_cols=None):
    align_right_cols = align_right_cols or []
    data = [header] + rows
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("FONT", (0, 0), (-1, 0), SANS_BOLD, 8.5),
        ("FONT", (0, 1), (-1, -1), SANS, 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), SUBTLE),
        ("TEXTCOLOR", (0, 1), (-1, -1), INK),
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#f5f4f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, INK),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, RULE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PAPER, HexColor("#f9f8f4")]),
    ]
    for c in align_right_cols:
        style.append(("ALIGN", (c, 1), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def p(txt, style="body"):
    return Paragraph(txt, STYLES[style])


def bullets(items):
    return [Paragraph(f"<bullet>·</bullet> {it}", STYLES["bullet"]) for it in items]


# Gantt-style roadmap bar
class GanttRow(Flowable):
    def __init__(self, label, start_week, end_week, total=26, color=ACCENT, width=None):
        super().__init__()
        self.label = label
        self.start = start_week
        self.end = end_week
        self.total = total
        self.color = color
        self.width = width or (PAGE_W - MARGIN_L - MARGIN_R)
        self.height = 14
        self.label_w = 55 * mm
        self.track_w = self.width - self.label_w

    def draw(self):
        c = self.canv
        c.setFont(SANS, 8.5)
        c.setFillColor(INK)
        c.drawString(0, 4, self.label)
        # track background
        c.setFillColor(RULE)
        c.rect(self.label_w, 2, self.track_w, 9, fill=1, stroke=0)
        # bar
        x = self.label_w + (self.start - 1) / self.total * self.track_w
        w = (self.end - self.start + 1) / self.total * self.track_w
        c.setFillColor(self.color)
        c.rect(x, 2, w, 9, fill=1, stroke=0)
        # week label on bar
        c.setFillColor(colors.white)
        c.setFont(SANS_BOLD, 7)
        if w > 28:
            c.drawString(x + 4, 5, f"W{self.start}-{self.end}")


def gantt_axis(total=26):
    """Render an axis with week markers."""
    class Axis(Flowable):
        def __init__(self):
            super().__init__()
            self.width = PAGE_W - MARGIN_L - MARGIN_R
            self.height = 14
            self.label_w = 55 * mm
            self.track_w = self.width - self.label_w
            self.total = total
        def draw(self):
            c = self.canv
            c.setFont(SANS_BOLD, 7)
            c.setFillColor(SUBTLE)
            for w in [1, 5, 10, 15, 20, 26]:
                x = self.label_w + (w - 1) / self.total * self.track_w
                c.drawString(x, 4, f"W{w}")
    return Axis()


# Scorecard radial-ish bar (horizontal score 0-10)
class ScoreBar(Flowable):
    def __init__(self, label, score, max_score=10, width=None, color=ACCENT):
        super().__init__()
        self.label = label
        self.score = score
        self.max = max_score
        self.width = width or (PAGE_W - MARGIN_L - MARGIN_R)
        self.height = 18
        self.color = color
        self.label_w = 65 * mm
        self.score_w = 18 * mm
        self.track_w = self.width - self.label_w - self.score_w

    def draw(self):
        c = self.canv
        c.setFont(SANS, 10)
        c.setFillColor(INK)
        c.drawString(0, 6, self.label)
        c.setFillColor(RULE)
        c.rect(self.label_w, 4, self.track_w, 10, fill=1, stroke=0)
        w = self.score / self.max * self.track_w
        # color tint by score
        col = HexColor("#0c4a6e") if self.score >= 7 else (HexColor("#a16207") if self.score >= 5 else HexColor("#991b1b"))
        c.setFillColor(col)
        c.rect(self.label_w, 4, w, 10, fill=1, stroke=0)
        c.setFont(SANS_BOLD, 10)
        c.setFillColor(INK)
        c.drawRightString(self.width, 6, f"{self.score}/{self.max}")


# Funnel rectangle
class FunnelStage(Flowable):
    def __init__(self, label, value, conv_to_next=None, top_w_pct=1.0, bot_w_pct=0.7, color=ACCENT):
        super().__init__()
        self.label = label
        self.value = value
        self.conv = conv_to_next
        self.top_w_pct = top_w_pct
        self.bot_w_pct = bot_w_pct
        self.color = color
        self.width = PAGE_W - MARGIN_L - MARGIN_R
        self.height = 50

    def draw(self):
        c = self.canv
        cx = self.width / 2
        top_w = self.width * 0.7 * self.top_w_pct
        bot_w = self.width * 0.7 * self.bot_w_pct
        # trapezoid via polygon
        from reportlab.graphics.shapes import Polygon
        p = c.beginPath()
        p.moveTo(cx - top_w/2, self.height - 5)
        p.lineTo(cx + top_w/2, self.height - 5)
        p.lineTo(cx + bot_w/2, 15)
        p.lineTo(cx - bot_w/2, 15)
        p.close()
        c.setFillColor(self.color)
        c.setStrokeColor(self.color)
        c.drawPath(p, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont(SANS_BOLD, 10)
        c.drawCentredString(cx, self.height - 18, self.label)
        c.setFont(SANS, 9)
        c.drawCentredString(cx, self.height - 30, self.value)
        if self.conv:
            c.setFillColor(SUBTLE)
            c.setFont(SANS_BOLD, 8)
            c.drawString(cx + top_w/2 + 8, 18, f"↓ {self.conv}")


# ---------------- Common cover ---------------- #
def cover_block(title, eyebrow, client, date, engagement, subtitle=None):
    out = []
    out.append(Spacer(1, 60))
    out.append(p(eyebrow, "eyebrow"))
    out.append(Spacer(1, 6))
    out.append(p(title, "title"))
    if subtitle:
        out.append(Spacer(1, 6))
        out.append(p(subtitle, "subtle"))
    out.append(Spacer(1, 30))
    out.append(hr())
    out.append(Spacer(1, 10))
    out.append(kv_table([
        ["Client", client],
        ["Engagement ID", engagement],
        ["Period", date],
        ["Issued by", "Anuvia · Mila Vernazza, Principal Consultant"],
        ["Classification", "SAMPLE · Confidential · Not for distribution"],
    ]))
    out.append(Spacer(1, 30))
    out.append(p(
        "This document is a sample preview of an Anuvia diagnostic deliverable. "
        "All figures, customer names, and findings are illustrative and anonymized. "
        "No real-client data is contained within.",
        "subtle",
    ))
    out.append(Spacer(1, 200))
    out.append(p("anuvia.com.br · contato@anuvia.com.br", "small"))
    out.append(PageBreak())
    return out


# =================== PDF 1 — FinOps Audit =================== #
def pdf_finops(out_path: str):
    story = []
    story += cover_block(
        title="AWS Cost Audit · Final Report — SAMPLE",
        eyebrow="FINOPS · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima S.A.",
        date="Q2 2026 · April 18 – May 16, 2026",
        engagement="#ANV-AUDIT-2604-0917",
        subtitle="4-week risk-free engagement · 3× ROI guarantee",
    )

    # Page 2 — Executive Summary
    story.append(p("EXECUTIVE SUMMARY", "eyebrow"))
    story.append(p("Identified annualized savings: R$ 287,400", "h1"))
    story.append(p(
        "Across a 4-week structured audit of the client's AWS estate (4 production accounts, "
        "single payer, R$ 1.84M annualized run-rate at engagement start), we identified "
        "<b>R$ 287,400</b> in annualized savings — a <b>15.6%</b> reduction against current run-rate "
        "and a <b>5.2× ROI</b> against engagement fees. Of these, R$ 41,200 were already "
        "implemented during weeks 3–4 of the audit (\"quick wins\"). The remainder is "
        "sequenced in a 6-month remediation roadmap.",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(data_table(
        ["Category", "Lever", "Annualized Savings (R$)", "Effort", "Risk"],
        [
            ["Compute", "RI rebalancing + Spot migration (stateless workloads)", "128,400", "M", "Low"],
            ["Storage", "Idle EBS, snapshot cleanup, S3 lifecycle", "64,200", "L", "Low"],
            ["Network", "NAT egress consolidation, Transit Gateway redesign", "52,300", "M", "Med"],
            ["RDS / Aurora", "Right-sizing 4 instances, IO optimization", "27,800", "L", "Low"],
            ["3rd-party SaaS", "Migration to AWS Marketplace billing", "14,700", "L", "Low"],
            ["", "Total identified", "287,400", "", ""],
        ],
        col_widths=[26*mm, 70*mm, 32*mm, 16*mm, 22*mm],
        align_right_cols=[2],
    ))
    story.append(Spacer(1, 10))
    story.append(p(
        '"The single highest-leverage move is consolidating cross-AZ chatter in the messaging '
        'tier — it alone explains 38% of the NAT egress bill."',
        "pull",
    ))
    story.append(PageBreak())

    # Page 3 — Compute
    story.append(p("FINDINGS · 01 / 05", "eyebrow"))
    story.append(p("Compute — R$ 128,400 annualized", "h1"))
    story.append(p("Reserved Instance & Savings Plan rebalancing", "h2"))
    story.append(p(
        "Current coverage stands at 41% of steady-state compute against an optimal band of "
        "70–80%. Existing 1-year zonal RIs cover m5.xlarge instances that have since been "
        "rotated to m6i.xlarge — a mismatch worth R$ 38,200/yr in unrecovered discount.",
        "body_just",
    ))
    story.append(Spacer(1, 4))
    story += bullets([
        "Convert 12 zonal RIs to Compute Savings Plans (3-yr, no upfront) → R$ 38,200/yr",
        "Increase steady-state coverage from 41% → 74% via R$ 0 commit purchases → R$ 46,800/yr",
        "Migrate batch ETL fleet (32 vCPU avg) to Spot via mixed-instances ASG → R$ 43,400/yr",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Right-sizing opportunities", "h2"))
    story.append(data_table(
        ["Instance family", "Count", "Current avg CPU", "Recommended", "Annualized save (R$)"],
        [
            ["m5.2xlarge", "8", "11%", "m5.large", "22,400"],
            ["c5.4xlarge", "3", "18%", "c5.xlarge", "14,100"],
            ["r5.xlarge", "5", "9% / 24% RAM", "r5.large", "9,600"],
        ],
        col_widths=[30*mm, 14*mm, 30*mm, 30*mm, 38*mm],
        align_right_cols=[1, 4],
    ))
    story.append(PageBreak())

    # Page 4 — Storage & Network
    story.append(p("FINDINGS · 02–03 / 05", "eyebrow"))
    story.append(p("Storage & Network — R$ 116,500 annualized", "h1"))

    story.append(p("Storage (R$ 64,200)", "h2"))
    story.append(data_table(
        ["Lever", "Current state", "Action", "Save (R$)"],
        [
            ["Idle EBS volumes", "84 unattached vols, 6.2 TB total", "Snapshot then delete", "18,900"],
            ["Orphan snapshots", "1,247 snapshots, >180 days, no source AMI", "Lifecycle policy + purge", "11,400"],
            ["S3 Intelligent-Tiering", "Standard tier on 12 cold buckets (38 TB)", "Enable Int-Tiering org-wide", "22,600"],
            ["S3 Glacier Deep Archive", "Audit logs in Standard-IA", "Lifecycle → Glacier DA after 90d", "11,300"],
        ],
        col_widths=[42*mm, 56*mm, 42*mm, 22*mm],
        align_right_cols=[3],
    ))
    story.append(Spacer(1, 10))
    story.append(p("Network (R$ 52,300)", "h2"))
    story += bullets([
        "Consolidate 6 NAT Gateways across 3 AZs into 1 per-AZ pattern → R$ 24,800/yr (data processing fees)",
        "Replace cross-AZ inter-VPC peering with Transit Gateway + service endpoints → R$ 18,200/yr egress",
        "Enable S3 Gateway VPC Endpoints in 2 remaining accounts → R$ 9,300/yr eliminated NAT data charges",
    ])
    story.append(PageBreak())

    # Page 5 — RDS + SaaS
    story.append(p("FINDINGS · 04–05 / 05", "eyebrow"))
    story.append(p("RDS / Aurora & SaaS — R$ 42,500 annualized", "h1"))
    story.append(p("RDS / Aurora right-sizing (R$ 27,800)", "h2"))
    story.append(p(
        "Production OLTP cluster (db.r6g.4xlarge × 2) sustains a P99 CPU below 28% across "
        "a rolling 30-day window with no observed IO ceiling. Aurora storage IOPS billing accounts "
        "for 22% of monthly RDS spend, with predictable hot-pages access — candidate for IO-Optimized cluster mode.",
        "body_just",
    ))
    story.append(Spacer(1, 4))
    story += bullets([
        "Step-down primary + reader to db.r6g.2xlarge (validated via load test) → R$ 18,400/yr",
        "Switch to Aurora IO-Optimized for the analytics replica → R$ 9,400/yr (break-even at 27% IOPS share)",
    ])
    story.append(Spacer(1, 10))
    story.append(p("3rd-party SaaS via AWS Marketplace (R$ 14,700)", "h2"))
    story.append(p(
        "Four SaaS subscriptions currently invoiced direct (Datadog, Snyk, MongoDB Atlas, Confluent) "
        "are eligible for Private Marketplace migration with EDP credit eligibility.",
        "body_just",
    ))
    story.append(Spacer(1, 4))
    story += bullets([
        "Migrate Datadog billing to Marketplace → 8% private offer + EDP eligibility → R$ 6,200/yr",
        "Consolidate Snyk + Mongo Atlas under Marketplace → R$ 5,800/yr",
        "Confluent: convert annual prepay to Marketplace metered → R$ 2,700/yr",
    ])
    story.append(PageBreak())

    # Page 6 — Roadmap (Gantt)
    story.append(p("REMEDIATION ROADMAP", "eyebrow"))
    story.append(p("6-month rollout · 26 weeks · sequenced by blast radius", "h1"))
    story.append(p(
        "Sequencing prioritizes (a) zero-risk reversible actions first, (b) RI/SP commitments "
        "after right-sizing is validated, and (c) network changes during the team's existing "
        "freeze windows. Effort color-coded: dark = high engineering load.",
        "subtle",
    ))
    story.append(Spacer(1, 14))
    story.append(gantt_axis())
    story.append(Spacer(1, 4))
    story.append(GanttRow("Idle EBS / snapshot cleanup", 1, 3, color=HexColor("#a3b18a")))
    story.append(GanttRow("S3 lifecycle policies", 1, 4, color=HexColor("#a3b18a")))
    story.append(GanttRow("RDS right-sizing (validated)", 3, 6, color=ACCENT))
    story.append(GanttRow("S3 Intelligent-Tiering rollout", 4, 8, color=HexColor("#a3b18a")))
    story.append(GanttRow("RI/SP repurchase wave", 6, 10, color=ACCENT))
    story.append(GanttRow("NAT consolidation", 9, 14, color=HexColor("#5a3e36")))
    story.append(GanttRow("Transit Gateway redesign", 12, 18, color=HexColor("#5a3e36")))
    story.append(GanttRow("Spot migration · batch ETL", 14, 20, color=ACCENT))
    story.append(GanttRow("Aurora IO-Optimized switch", 16, 19, color=ACCENT))
    story.append(GanttRow("SaaS Marketplace migration", 18, 24, color=HexColor("#a3b18a")))
    story.append(GanttRow("Tagging governance + FinOps cadence", 20, 26, color=HexColor("#a3b18a")))
    story.append(Spacer(1, 14))
    story.append(p(
        "<font color='#a3b18a'>■</font> Low effort   "
        "<font color='#0c4a6e'>■</font> Medium effort   "
        "<font color='#5a3e36'>■</font> High effort (cross-team)",
        "small",
    ))
    story.append(PageBreak())

    # Page 7 — Quick wins
    story.append(p("QUICK WINS · IMPLEMENTED DURING AUDIT", "eyebrow"))
    story.append(p("R$ 41,200 captured in weeks 3–4", "h1"))
    story.append(p(
        "Per the audit charter, low-risk reversible actions were executed live (with client "
        "approval gates) during the audit itself. The below ledger documents every change, "
        "approver, and rollback procedure.",
        "body_just",
    ))
    story.append(Spacer(1, 6))
    story.append(data_table(
        ["Date", "Action", "Approver", "Savings (R$/yr)"],
        [
            ["2026-05-02", "Delete 84 idle EBS vols (snapshotted prior)", "Head of Eng.", "18,900"],
            ["2026-05-03", "Purge 1,247 orphan snapshots >180d", "Head of Eng.", "11,400"],
            ["2026-05-06", "Enable S3 Gateway VPC Endpoint · acct-prod-2", "Cloud Eng.", "5,100"],
            ["2026-05-08", "Lifecycle rule · audit logs → Glacier DA", "Head of Eng.", "5,800"],
        ],
        col_widths=[26*mm, 80*mm, 36*mm, 30*mm],
        align_right_cols=[3],
    ))
    story.append(Spacer(1, 10))
    story.append(p(
        "All quick-win actions are logged in CloudTrail with the engagement-tagged IAM role "
        "<font face='Helvetica-Bold'>arn:aws:iam::***:role/AnuviaAuditExecutor</font>. "
        "Rollback procedures for each are documented in Appendix C.",
        "subtle",
    ))
    story.append(PageBreak())

    # Page 8 — ADRs
    story.append(p("ARCHITECTURE DECISION RECORDS", "eyebrow"))
    story.append(p("ADR Excerpts · 3 of 11", "h1"))

    story.append(p("ADR-007 · Reserved Instance / Savings Plan posture", "h2"))
    story.append(kv_table([
        ["Status", "Accepted · 2026-05-12"],
        ["Context", "Coverage at 41%, mismatched zonal RIs from prior m5 fleet. Spot share <5%."],
        ["Decision", "Adopt 3-yr no-upfront Compute Savings Plans as primary commit vehicle. Cap RI usage to GPU/legacy workloads. Target 70–80% steady-state coverage. Re-evaluate quarterly."],
        ["Consequences", "Locks ~R$ 720k of committed spend over 3 years. Recovers R$ 85k/yr in unrecovered discount. Reduces optimization toil — Compute SP applies fleet-wide."],
    ]))
    story.append(Spacer(1, 8))

    story.append(p("ADR-009 · Cross-region data residency", "h2"))
    story.append(kv_table([
        ["Status", "Accepted · 2026-05-13"],
        ["Context", "Replicating customer PII to us-east-1 for analytics; LGPD obligation to keep "
                    "personal data in-region (sa-east-1)."],
        ["Decision", "Drop cross-region replica. Move analytics to sa-east-1 Redshift; aggregate non-PII metrics only via cross-region S3 Replication with tag-based filter."],
        ["Consequences", "Eliminates R$ 18k/yr egress + LGPD risk reduction. Adds 6-week analytics migration in roadmap W14–W20."],
    ]))
    story.append(Spacer(1, 8))

    story.append(p("ADR-011 · S3 Intelligent-Tiering rollout", "h2"))
    story.append(kv_table([
        ["Status", "Accepted · 2026-05-14"],
        ["Context", "12 buckets > 1 TB in Standard tier with <8% monthly access rate."],
        ["Decision", "Enable Intelligent-Tiering org-wide via SCP + remediation Lambda. Override allowed for hot buckets via tag <font face='Helvetica-Bold'>storage-class=manual</font>."],
        ["Consequences", "R$ 22,600/yr saving. Negligible cold-read latency for present workloads (<1% of access)."],
    ]))
    story.append(PageBreak())

    # Page 9 — Appendix (IAM)
    story.append(p("APPENDIX A · IAM PERMISSIONS USED", "eyebrow"))
    story.append(p("Read-only audit role · least-privilege", "h1"))
    story.append(p(
        "All findings were derived using the IAM role <font face='Helvetica-Bold'>"
        "AnuviaAuditViewer</font> with the policies listed below. No write actions were "
        "performed via this role. Quick-win execution used a separate, time-bound role "
        "(<font face='Helvetica-Bold'>AnuviaAuditExecutor</font>) revoked at engagement close.",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(data_table(
        ["Policy ARN / Name", "Scope"],
        [
            ["arn:aws:iam::aws:policy/ReadOnlyAccess", "Org-wide read"],
            ["arn:aws:iam::aws:policy/job-function/Billing (Read)", "Billing, Cost Explorer, CUR"],
            ["arn:aws:iam::aws:policy/AWSTrustedAdvisorReadOnlyAccess", "TA findings"],
            ["arn:aws:iam::aws:policy/ComputeOptimizerReadOnlyAccess", "Right-sizing data"],
            ["arn:aws:iam::aws:policy/AWSResourceExplorerReadOnlyAccess", "Inventory"],
        ],
        col_widths=[100*mm, 60*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p("METHODOLOGY", "eyebrow"))
    story.append(p(
        "Findings derived from: (1) Cost & Usage Report (CUR) at hourly granularity for the 90 days "
        "preceding kickoff; (2) Trusted Advisor & Compute Optimizer recommendations; (3) CloudWatch "
        "metrics across compute, storage, and RDS; (4) bottom-up service-by-service review per "
        "the AWS Well-Architected Cost pillar checklist. Savings are conservative — modelled at "
        "the 30th percentile of typical realization rates observed across Anuvia's prior engagements.",
        "body_just",
    ))
    story.append(PageBreak())

    # Page 10 — Appendix B Glossary
    story.append(p("APPENDIX B · GLOSSARY", "eyebrow"))
    story.append(p("Glossary of acronyms used in this report", "h1"))
    story.append(data_table(
        ["Term", "Definition"],
        [
            ["RI", "Reserved Instance — committed compute discount, 1 or 3-year terms, zonal or regional."],
            ["SP", "Savings Plan — flexible compute commitment by R$/hr, applies across families/regions."],
            ["CUR", "Cost & Usage Report — hourly billing export, source of truth for spend analysis."],
            ["ADR", "Architecture Decision Record — short document capturing a single architectural decision and its consequences."],
            ["Egress", "Outbound data transfer from AWS to internet or other regions — typically billed per GB."],
            ["NAT GW", "NAT Gateway — managed network address translation for outbound internet traffic from private subnets."],
            ["TGW", "Transit Gateway — hub-and-spoke VPC interconnect, replaces VPC peering at scale."],
            ["LGPD", "Lei Geral de Proteção de Dados — Brazilian general data protection law."],
            ["EDP", "Enterprise Discount Program — AWS spend-commitment-based pricing tier."],
            ["P99", "99th percentile observation — common SLO measurement reference."],
        ],
        col_widths=[26*mm, 134*mm],
    ))
    story.append(Spacer(1, 12))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== PDF 2 — AWS Well-Architected =================== #
def pdf_wa(out_path: str):
    story = []
    story += cover_block(
        title="AWS Well-Architected Review · Final Report — SAMPLE",
        eyebrow="WELL-ARCHITECTED · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima S.A.",
        date="Q2 2026 · April 06 – May 04, 2026",
        engagement="#ANV-WA-2604-0428",
        subtitle="6-pillar review · HRI / MRI / LRI classification",
    )

    # Page 2 — Scorecard
    story.append(p("SCORECARD", "eyebrow"))
    story.append(p("6-Pillar Scorecard", "h1"))
    story.append(p(
        "Scores are calibrated on a 0–10 scale derived from the AWS Well-Architected Tool "
        "(85 questions across 6 pillars) and verified by direct evidence review. "
        "Aggregate posture: <b>6.0 / 10</b> — \"Solid foundation, security & sustainability gap\".",
        "body_just",
    ))
    story.append(Spacer(1, 14))
    story.append(ScoreBar("Operational Excellence", 7))
    story.append(Spacer(1, 4))
    story.append(ScoreBar("Security", 5))
    story.append(Spacer(1, 4))
    story.append(ScoreBar("Reliability", 6))
    story.append(Spacer(1, 4))
    story.append(ScoreBar("Performance Efficiency", 8))
    story.append(Spacer(1, 4))
    story.append(ScoreBar("Cost Optimization", 6))
    story.append(Spacer(1, 4))
    story.append(ScoreBar("Sustainability", 4))
    story.append(Spacer(1, 18))
    story.append(p("Risk inventory", "h2"))
    story.append(data_table(
        ["Severity", "Count", "Definition"],
        [
            ["HRI · High Risk", "7", "Could plausibly cause outage, data loss, or compliance breach within 90 days."],
            ["MRI · Medium Risk", "14", "Material posture gap; should be remediated in current quarter."],
            ["LRI · Low Risk", "23", "Polish / hygiene items; bundle into normal backlog."],
        ],
        col_widths=[40*mm, 18*mm, 102*mm],
        align_right_cols=[1],
    ))
    story.append(PageBreak())

    # Page 3 — Security
    story.append(p("PILLAR · 02 / 06", "eyebrow"))
    story.append(p("Security — 5 / 10", "h1"))
    story.append(p(
        "Strongest gap. AWS-native foundations are partially deployed (GuardDuty in 3/4 accounts, "
        "Security Hub disabled, Config rules sparse) and the IAM blast radius is wider than warranted by "
        "the team's role model.",
        "body_just",
    ))
    story.append(Spacer(1, 6))
    story.append(p("Findings", "h2"))
    story.append(data_table(
        ["#", "Finding", "Severity", "Effort"],
        [
            ["SEC-01", "Root-account MFA missing on payer account", "HRI", "S"],
            ["SEC-02", "GuardDuty disabled in 1 of 4 accounts (acct-dev)", "HRI", "S"],
            ["SEC-03", "Security Hub not enabled · no central findings aggregation", "HRI", "M"],
            ["SEC-04", "23 IAM users with console access · should be SSO/IdC", "HRI", "M"],
            ["SEC-05", "S3 public access block not enforced at org level (SCP missing)", "HRI", "S"],
            ["SEC-06", "Secrets in environment variables (ECS task defs) · 11 instances", "MRI", "M"],
            ["SEC-07", "KMS key rotation disabled on 6 CMKs holding customer PII", "MRI", "S"],
            ["SEC-08", "VPC Flow Logs not enabled on 2 of 7 production VPCs", "MRI", "S"],
            ["SEC-09", "IAM Access Analyzer not enabled in any account", "MRI", "S"],
            ["SEC-10", "Inline IAM policies on 47 roles · drift risk", "LRI", "M"],
        ],
        col_widths=[18*mm, 100*mm, 22*mm, 18*mm],
    ))
    story.append(PageBreak())

    # Page 4 — Reliability
    story.append(p("PILLAR · 03 / 06", "eyebrow"))
    story.append(p("Reliability — 6 / 10", "h1"))
    story.append(p(
        "Single-AZ deployment on the primary OLTP database is the dominant risk; failure modes "
        "during simulated AZ degradation were not exercised in the last 12 months.",
        "body_just",
    ))
    story.append(Spacer(1, 6))
    story.append(p("Findings", "h2"))
    story.append(data_table(
        ["#", "Finding", "Severity", "Effort"],
        [
            ["REL-01", "Primary RDS cluster Single-AZ · no automated failover", "HRI", "M"],
            ["REL-02", "No documented RTO/RPO targets per service", "HRI", "M"],
            ["REL-03", "Backups not periodically restore-tested (last test: 14 mo ago)", "HRI", "M"],
            ["REL-04", "ALB health checks too tolerant · 60s threshold", "MRI", "S"],
            ["REL-05", "No chaos / game-day program · no AZ-failure rehearsal", "MRI", "L"],
            ["REL-06", "Stateless services pinned to single AZ ASG", "MRI", "S"],
            ["REL-07", "Critical Lambda DLQ unconfigured on 8 functions", "MRI", "S"],
            ["REL-08", "No multi-region DR target (RTO ill-defined)", "LRI", "L"],
        ],
        col_widths=[18*mm, 100*mm, 22*mm, 18*mm],
    ))
    story.append(Spacer(1, 12))
    story.append(p(
        "Recommended sequence: REL-01 (Multi-AZ enablement) is gated only on a brief failover "
        "window — see ADR on page 9. REL-03 should be paired with REL-08 in a single quarterly DR exercise.",
        "subtle",
    ))
    story.append(PageBreak())

    # Page 5 — Cost
    story.append(p("PILLAR · 05 / 06", "eyebrow"))
    story.append(p("Cost Optimization — 6 / 10", "h1"))
    story.append(p(
        "Spend governance is partially mature: cost allocation tags exist but coverage is 64%. "
        "RI/SP coverage is mid-range. Detailed FinOps audit recommended (see linked engagement "
        "ANV-AUDIT for separate deliverable).",
        "body_just",
    ))
    story.append(Spacer(1, 6))
    story.append(p("Findings", "h2"))
    story.append(data_table(
        ["#", "Finding", "Severity", "Effort"],
        [
            ["COST-01", "Tag coverage at 64% · billing allocation gaps", "HRI", "M"],
            ["COST-02", "No budget alerts on 2 of 4 accounts", "MRI", "S"],
            ["COST-03", "RI/SP coverage at 41% (target: 70-80%)", "MRI", "M"],
            ["COST-04", "84 idle EBS volumes · R$ 18.9k/yr waste", "MRI", "S"],
            ["COST-05", "S3 lifecycle missing on 12 buckets", "LRI", "S"],
            ["COST-06", "Cross-AZ chatter on RDS replica · R$ 6k/yr egress", "LRI", "M"],
        ],
        col_widths=[18*mm, 100*mm, 22*mm, 18*mm],
    ))
    story.append(PageBreak())

    # Page 6 — Performance + Op Ex highlights
    story.append(p("PILLARS · 01, 04, 06 / 06", "eyebrow"))
    story.append(p("Operational Excellence · Performance · Sustainability", "h1"))
    story.append(p("Operational Excellence — 7/10", "h2"))
    story += bullets([
        "Strong runbook coverage on top-5 services; 4 critical services without runbooks (OPS-01).",
        "Post-mortems written for 9 of 14 incidents in last 12 months · culture is forming.",
        "IaC coverage at 78% (Terraform) · 22% click-ops residual mostly in legacy networking.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Performance Efficiency — 8/10", "h2"))
    story += bullets([
        "Strong. Right-instance-type discipline. Some latency budget over-provisioning detected.",
        "Caching layer (Redis) under-utilized — 22% of hit-eligible queries bypass.",
        "GP3 EBS adoption complete; gp2 fully retired.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Sustainability — 4/10", "h2"))
    story += bullets([
        "No carbon dashboard reviewed; AWS Customer Carbon Footprint Tool not consulted.",
        "Graviton (ARM64) adoption at 6% across compute fleet · target 50% within 12 months.",
        "No data-lifecycle policy → cold data persists in high-energy storage tiers.",
    ])
    story.append(PageBreak())

    # Page 7 — Roadmap
    story.append(p("REMEDIATION ROADMAP", "eyebrow"))
    story.append(p("Sequenced by blast radius", "h1"))
    story.append(p(
        "Priority order is determined by (1) failure-mode severity in the next 90 days, "
        "(2) regulatory exposure, (3) cross-team dependency depth. Quick wins first; multi-team "
        "changes (Transit Gateway, SSO migration) staged after foundations.",
        "subtle",
    ))
    story.append(Spacer(1, 12))
    story.append(gantt_axis(total=26))
    story.append(Spacer(1, 4))
    story.append(GanttRow("SEC-01/05/02 · MFA + GuardDuty + S3 SCP", 1, 2, color=HexColor("#991b1b")))
    story.append(GanttRow("REL-01 · RDS Multi-AZ enablement", 2, 4, color=HexColor("#991b1b")))
    story.append(GanttRow("SEC-03 · Security Hub org rollout", 3, 6, color=ACCENT))
    story.append(GanttRow("COST-01 · Tagging governance + SCP", 4, 8, color=ACCENT))
    story.append(GanttRow("SEC-04 · SSO / IdC migration", 6, 12, color=HexColor("#5a3e36")))
    story.append(GanttRow("REL-03 · Backup restore-test program", 8, 12, color=ACCENT))
    story.append(GanttRow("REL-02 · RTO/RPO definition per service", 10, 14, color=ACCENT))
    story.append(GanttRow("SEC-06/07 · Secrets + KMS rotation", 12, 16, color=ACCENT))
    story.append(GanttRow("REL-05 · Chaos / Game Day program", 14, 22, color=HexColor("#5a3e36")))
    story.append(GanttRow("Sustainability · Graviton migration wave", 18, 26, color=HexColor("#5a3e36")))
    story.append(PageBreak())

    # Page 8 — Roadmap continued + KPIs
    story.append(p("REMEDIATION ROADMAP · TARGETS", "eyebrow"))
    story.append(p("Post-roadmap target posture", "h1"))
    story.append(data_table(
        ["Pillar", "Current", "Target (6 mo)", "Target (12 mo)"],
        [
            ["Operational Excellence", "7", "8", "9"],
            ["Security", "5", "7", "8"],
            ["Reliability", "6", "7", "8"],
            ["Performance Efficiency", "8", "8", "9"],
            ["Cost Optimization", "6", "8", "8"],
            ["Sustainability", "4", "6", "7"],
            ["Aggregate", "6.0", "7.3", "8.2"],
        ],
        col_widths=[60*mm, 30*mm, 35*mm, 35*mm],
        align_right_cols=[1, 2, 3],
    ))
    story.append(Spacer(1, 14))
    story.append(p("Sequencing rationale", "h2"))
    story += bullets([
        "Weeks 1-4 close all open HRIs whose cost is low (\"free\" risk reduction).",
        "Weeks 5-12 attack identity & data perimeter — these unblock downstream cost work.",
        "Weeks 13-26 invest in the long-cycle structural items (SSO, chaos, Graviton).",
    ])
    story.append(PageBreak())

    # Page 9 — ADR Multi-AZ
    story.append(p("ARCHITECTURE DECISION RECORD", "eyebrow"))
    story.append(p("ADR-WA-003 · Multi-AZ database failover strategy", "h1"))
    story.append(kv_table([
        ["Status", "Accepted · 2026-04-29"],
        ["Date", "2026-04-29"],
        ["Authors", "Anuvia Cloud Practice + Empresa Anônima Eng. Lead"],
        ["Pillar", "Reliability · REL-01"],
        ["Severity addressed", "HRI"],
        ["Context", "Primary production OLTP cluster is Single-AZ (db.r6g.4xlarge). AZ-level outage would result in an estimated 90-minute restore-from-snapshot RTO and ~15-min RPO. Business RTO target is 15 min."],
        ["Considered options",
            "A) Multi-AZ deployment (synchronous replica, automated failover) — adds ~38% to RDS spend.<br/>"
            "B) Read-replica + manual promotion runbook — cheaper, slower RTO (~12 min).<br/>"
            "C) Aurora Global Database — cross-region, higher cost, longer migration."],
        ["Decision", "Adopt option A: Multi-AZ deployment for primary cluster. Schedule a 45-minute "
                     "controlled failover during off-peak window."],
        ["Consequences",
            "+ RTO reduces from 90 min to ≤2 min · meets business target.<br/>"
            "+ RPO reduces from ~15 min to near-zero.<br/>"
            "+ Patching becomes near-zero-downtime.<br/>"
            "- RDS cost increases by ~R$ 31k/yr.<br/>"
            "- One-time controlled-failover window required (W2)."],
        ["Validation criteria",
            "1. Successful failover dry-run within 90 sec.<br/>"
            "2. CloudWatch alarm fires within 30 sec of AZ degradation.<br/>"
            "3. App reconnects within 60 sec without manual intervention."],
    ]))
    story.append(PageBreak())

    # Page 10 — Appendix
    story.append(p("APPENDIX · METHODOLOGY & REFERENCES", "eyebrow"))
    story.append(p("Methodology", "h1"))
    story.append(p(
        "This review follows the AWS Well-Architected Framework (April 2026 revision). "
        "Each of the 85 questions across the 6 pillars was evaluated against direct evidence "
        "(IaC source, AWS Config, CloudTrail, runbook artifacts) rather than self-attestation. "
        "Findings are classified per the AWS WA Tool risk taxonomy: HRI (High Risk Issue), "
        "MRI (Medium Risk Issue), LRI (Low Risk Issue).",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(p("Evidence sources", "h2"))
    story += bullets([
        "AWS Config aggregator export · 4 accounts · 2026-04-12",
        "CloudTrail event lake · 90-day window prior to engagement start",
        "Terraform repo audit · git log analysis · 12-month window",
        "Incident retrospective archive · 14 post-mortems reviewed",
        "Interviews · 9 engineers across platform, app, and security teams (6 hr total)",
    ])
    story.append(Spacer(1, 10))
    story.append(p("References", "h2"))
    story += bullets([
        "AWS Well-Architected Framework · docs.aws.amazon.com/wellarchitected",
        "AWS Foundational Security Best Practices (Security Hub standard)",
        "AWS Resilience Hub · application resiliency policies",
        "AWS Customer Carbon Footprint Tool · 12-month emissions baseline",
    ])
    story.append(Spacer(1, 12))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== PDF 3 — DevOps Maturity =================== #
def pdf_devops(out_path: str):
    story = []
    story += cover_block(
        title="DevOps Maturity Assessment · DORA Report — SAMPLE",
        eyebrow="DEVOPS · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima S.A.",
        date="Q2 2026 · April 13 – May 11, 2026",
        engagement="#ANV-DORA-2604-1107",
        subtitle="4-week assessment · DORA 2023 benchmark calibration",
    )

    # Page 2 — DORA Dashboard
    story.append(p("DORA METRICS DASHBOARD", "eyebrow"))
    story.append(p("Current state vs. DORA 2023 benchmarks", "h1"))
    story.append(p(
        "Measured over the 90-day window preceding kickoff. Source: GitLab pipelines, "
        "PagerDuty incident records, deployment ledger. Classification per the DORA 2023 "
        "<i>State of DevOps</i> Elite/High/Medium/Low banding.",
        "body_just",
    ))
    story.append(Spacer(1, 10))
    story.append(data_table(
        ["Metric", "Current", "Tier", "High threshold", "Elite threshold"],
        [
            ["Deploy Frequency", "Weekly (avg 1.4 / wk)", "Medium", "Daily", "On-demand (multi/day)"],
            ["Lead Time for Changes", "8 days", "Medium", "<1 day", "<1 hour"],
            ["MTTR", "4 hours (P50)", "High", "<1 day", "<1 hour"],
            ["Change Failure Rate", "22%", "Medium", "0-15%", "0-5%"],
        ],
        col_widths=[44*mm, 36*mm, 20*mm, 30*mm, 32*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p("Tier summary", "h2"))
    story += bullets([
        "Current overall tier: <b>Medium</b> (3 of 4 metrics in Medium band).",
        "Closest to High: MTTR — already at the High threshold ceiling.",
        "Largest gap: Lead Time — 8× the High threshold.",
        "Highest CFR contributor: insufficient automated regression coverage on the checkout path (CFR-Δ +14 points).",
    ])
    story.append(Spacer(1, 8))
    story.append(p(
        "Target posture in 6 months: <b>High</b> across all four metrics. Roadmap on page 8.",
        "pull",
    ))
    story.append(PageBreak())

    # Page 3 — Deploy Frequency + Lead Time deep-dive
    story.append(p("METRIC DEEP-DIVE · 01 / 04", "eyebrow"))
    story.append(p("Deploy Frequency — Weekly", "h1"))
    story.append(kv_table([
        ["Current", "1.4 deploys / week (production) · 86 in last 90 days"],
        ["Tier", "Medium (DORA 2023)"],
        ["Target (6 mo)", "Daily — High tier (≥5/wk per service)"],
        ["Gap explained", "Batch-style release cadence; manual approval gate on every prod deploy; 6-hour smoke window blocks parallel deploys."],
    ]))
    story.append(Spacer(1, 6))
    story.append(p("Remediation actions", "h2"))
    story += bullets([
        "Replace human approval on low-risk paths (toggled features, internal services) with policy-as-code gate (OPA).",
        "Add canary deploy strategy → eliminate 6h smoke window; rely on telemetry-based promotion.",
        "Decompose monolithic release pipeline into per-service deploys (currently 3 services share 1 pipeline).",
        "Adopt trunk-based development on the 2 highest-velocity services in the first 8 weeks.",
    ])
    story.append(Spacer(1, 12))
    story.append(p("METRIC DEEP-DIVE · 02 / 04", "eyebrow"))
    story.append(p("Lead Time for Changes — 8 days", "h1"))
    story.append(kv_table([
        ["Current", "8 days (median commit → production)"],
        ["Tier", "Medium"],
        ["Target (6 mo)", "<24 hours — High tier"],
        ["Gap explained", "PR review queues average 2.4 days; integration test suite takes 38 min; manual UAT cycles add 3-5 days."],
    ]))
    story.append(p("Remediation actions", "h2"))
    story += bullets([
        "PR aging SLO: auto-nudge after 24h; round-robin reviewer assignment.",
        "Parallelize integration suite (currently sequential) → target 8 min wall time.",
        "Shift UAT to ephemeral preview environments per PR; reduce manual UAT to release-train cadence only.",
    ])
    story.append(PageBreak())

    # Page 4 — MTTR
    story.append(p("METRIC DEEP-DIVE · 03 / 04", "eyebrow"))
    story.append(p("MTTR — 4 hours (P50)", "h1"))
    story.append(kv_table([
        ["Current", "P50 4h · P90 11h · 28 production incidents in last 90 days"],
        ["Tier", "High (just within band)"],
        ["Target (6 mo)", "<1 hour P50 — Elite tier"],
        ["Gap explained", "Detection-to-diagnosis time dominates (avg 2.1h); runbook coverage on top-5 services only; observability gaps on async workers."],
    ]))
    story.append(p("Remediation actions", "h2"))
    story += bullets([
        "Service-level objective definitions + alarms for all production services (currently 60% coverage).",
        "Runbook coverage to 100% of services with traffic above defined threshold.",
        "Adopt structured incident command (IC role) — currently ad-hoc resolution.",
        "Investigate exemplar-driven debugging (OpenTelemetry exemplars) for tail-latency cases.",
    ])
    story.append(Spacer(1, 10))
    story.append(p("Incident timeline · last 90 days", "h2"))
    story.append(data_table(
        ["Severity", "Count", "Median MTTR", "Recurrence (root same?)"],
        [
            ["SEV1", "2", "1h 50m", "0"],
            ["SEV2", "9", "3h 40m", "2"],
            ["SEV3", "17", "5h 20m", "5"],
        ],
        col_widths=[28*mm, 18*mm, 38*mm, 60*mm],
        align_right_cols=[1],
    ))
    story.append(PageBreak())

    # Page 5 — CFR
    story.append(p("METRIC DEEP-DIVE · 04 / 04", "eyebrow"))
    story.append(p("Change Failure Rate — 22%", "h1"))
    story.append(kv_table([
        ["Current", "22% (19 of 86 production deploys triggered rollback or hotfix)"],
        ["Tier", "Medium"],
        ["Target (6 mo)", "<15% — High tier (stretch: <10%)"],
        ["Gap explained", "Checkout-path service shows 41% CFR vs. fleet 22% — outsized contributor. Insufficient regression coverage on payment edges (saved cards, 3DS challenge paths)."],
    ]))
    story.append(p("Remediation actions", "h2"))
    story += bullets([
        "Expand contract-test coverage on payment-gateway integration; add 11 specifically-identified edge cases.",
        "Adopt feature-flag-based rollouts on checkout-path; 1% → 10% → 50% → 100% gates with auto-rollback on error budget burn.",
        "Pre-prod load profile in staging using sanitized production-traffic shadow.",
    ])
    story.append(Spacer(1, 10))
    story.append(p("CFR by service", "h2"))
    story.append(data_table(
        ["Service", "Deploys", "Failures", "CFR"],
        [
            ["checkout-api", "22", "9", "41%"],
            ["catalog-api", "14", "2", "14%"],
            ["identity-service", "11", "1", "9%"],
            ["search-service", "18", "4", "22%"],
            ["notification-worker", "13", "2", "15%"],
            ["billing-service", "8", "1", "13%"],
        ],
        col_widths=[60*mm, 24*mm, 24*mm, 24*mm],
        align_right_cols=[1, 2, 3],
    ))
    story.append(PageBreak())

    # Page 6 — Pipeline / IaC / GitOps
    story.append(p("PIPELINE & TOOLING ASSESSMENT", "eyebrow"))
    story.append(p("CI/CD · IaC · GitOps readiness", "h1"))
    story.append(p("CI / CD maturity", "h2"))
    story.append(data_table(
        ["Dimension", "Current", "Target"],
        [
            ["CI tool", "GitLab CI (self-hosted runners)", "Stay"],
            ["Build time (P50)", "12 min", "≤6 min"],
            ["Test layers", "Unit · Integration · Smoke", "Add contract + canary"],
            ["Parallel jobs", "Sequential", "Fan-out by service"],
            ["Test flakiness", "8% job-flake rate", "<1%"],
        ],
        col_widths=[44*mm, 60*mm, 56*mm],
    ))
    story.append(Spacer(1, 8))
    story.append(p("IaC adoption", "h2"))
    story.append(data_table(
        ["Domain", "Coverage", "Gap"],
        [
            ["Networking (VPC, TGW, NAT)", "62%", "Legacy peering still click-ops"],
            ["Compute (ECS, EKS)", "94%", "—"],
            ["Data (RDS, ElastiCache)", "78%", "Parameter groups untracked"],
            ["IAM / SSO", "55%", "Manual SSO group sync"],
            ["Observability (alarms, dashboards)", "31%", "Mostly click-ops"],
        ],
        col_widths=[60*mm, 22*mm, 78*mm],
        align_right_cols=[1],
    ))
    story.append(Spacer(1, 8))
    story.append(p("GitOps readiness", "h2"))
    story += bullets([
        "Argo CD partially adopted (1 cluster, 4 apps). Recommend extending to all clusters by W12.",
        "Application manifests live in 3 repos with inconsistent structure — consolidate before scaling Argo CD.",
        "App-of-Apps pattern recommended for the second wave.",
    ])
    story.append(PageBreak())

    # Page 7 — Incident response
    story.append(p("INCIDENT RESPONSE REVIEW", "eyebrow"))
    story.append(p("Runbook · on-call · post-mortem culture", "h1"))
    story.append(p("Runbook coverage", "h2"))
    story.append(data_table(
        ["Service tier", "Services", "Runbook coverage", "Last review"],
        [
            ["Tier-0 (revenue path)", "5", "5/5 · 100%", "Avg 41 days ago"],
            ["Tier-1 (customer-facing)", "11", "7/11 · 64%", "Avg 6 months"],
            ["Tier-2 (internal)", "17", "3/17 · 18%", ">12 months"],
        ],
        col_widths=[44*mm, 22*mm, 40*mm, 38*mm],
        align_right_cols=[1],
    ))
    story.append(Spacer(1, 10))
    story.append(p("On-call rotation", "h2"))
    story += bullets([
        "Single rotation (8 engineers, 1 week shifts). No secondary rotation → escalation gaps observed in 4 incidents.",
        "Avg page volume: 11 / week (high — investigate alert quality before adding people).",
        "Compensation policy informal — recommend codifying for retention.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Post-mortem culture", "h2"))
    story += bullets([
        "9 of 14 incidents (last 12 mo) have a post-mortem document. 5 missing — predominantly SEV3.",
        "Quality: 3/9 reach blameless standard; rest read as RCA-only without contributing-factors analysis.",
        "Action item closure rate: 47% within target date.",
    ])
    story.append(PageBreak())

    # Page 8 — Roadmap
    story.append(p("ROADMAP · 6 MONTHS TO HIGH TIER", "eyebrow"))
    story.append(p("Sequenced quarterly plan", "h1"))
    story.append(p(
        "The path from Medium to High tier converges on three structural moves: (1) shorten the path "
        "from commit to production via parallelization & policy-as-code gates, (2) reduce variance "
        "by feature-flag rollout discipline, (3) raise observability coverage to make MTTR collapse achievable.",
        "subtle",
    ))
    story.append(Spacer(1, 12))
    story.append(gantt_axis(26))
    story.append(Spacer(1, 4))
    story.append(GanttRow("PR aging SLO + reviewer round-robin", 1, 4, color=HexColor("#a3b18a")))
    story.append(GanttRow("Test parallelization · integration", 2, 6, color=ACCENT))
    story.append(GanttRow("Canary deploy strategy (top-3 svc)", 3, 8, color=ACCENT))
    story.append(GanttRow("Ephemeral preview envs per PR", 6, 12, color=HexColor("#5a3e36")))
    story.append(GanttRow("OPA policy-as-code deploy gates", 6, 10, color=ACCENT))
    story.append(GanttRow("Feature flag rollouts · checkout", 8, 14, color=ACCENT))
    story.append(GanttRow("Observability uplift · SLOs", 9, 16, color=ACCENT))
    story.append(GanttRow("Runbooks Tier-1 to 100%", 10, 16, color=HexColor("#a3b18a")))
    story.append(GanttRow("Incident command (IC) program", 12, 18, color=HexColor("#a3b18a")))
    story.append(GanttRow("Argo CD rollout · all clusters", 14, 22, color=HexColor("#5a3e36")))
    story.append(GanttRow("Quarterly chaos game-day cadence", 18, 26, color=ACCENT))
    story.append(Spacer(1, 12))
    story.append(p("Target metrics (W26)", "h2"))
    story.append(data_table(
        ["Metric", "W0 (today)", "W13", "W26 target"],
        [
            ["Deploy Freq", "Weekly", "2-3 / day", "Daily+"],
            ["Lead Time", "8 days", "2 days", "<24h"],
            ["MTTR", "4h", "2h", "<1h"],
            ["CFR", "22%", "16%", "<15%"],
        ],
        col_widths=[40*mm, 36*mm, 36*mm, 38*mm],
    ))
    story.append(PageBreak())

    # Page 9 — Appendix
    story.append(p("APPENDIX A · DATA SOURCES", "eyebrow"))
    story.append(p("Methodology & evidence", "h1"))
    story.append(p("Data sources", "h2"))
    story += bullets([
        "GitLab CI/CD events · 90-day window · all pipelines",
        "PagerDuty incident archive · 90-day window",
        "Git history · all repos · 90-day commit log",
        "Production deploy ledger (custom audit channel)",
        "Interviews · 9 engineers + 3 EM + 1 VPE (12h total)",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Calibration", "h2"))
    story.append(p(
        "DORA tier classifications use the 2023 DORA <i>State of DevOps Report</i> banding. "
        "We do not invent thresholds — the bands cited (Elite / High / Medium / Low) are "
        "verbatim from that survey. CFR is calculated as (rolled-back-or-hotfixed deploys / "
        "total prod deploys) per the same survey definition.",
        "body_just",
    ))
    story.append(PageBreak())

    # Page 10 — Appendix B
    story.append(p("APPENDIX B · GLOSSARY", "eyebrow"))
    story.append(p("Glossary", "h1"))
    story.append(data_table(
        ["Term", "Definition"],
        [
            ["DORA", "DevOps Research & Assessment · annual State of DevOps Report."],
            ["CFR", "Change Failure Rate · % deploys requiring rollback or hotfix within a defined window."],
            ["MTTR", "Mean Time to Restore · from incident detection to restored service."],
            ["Lead Time", "From source commit to production deployment."],
            ["SLO", "Service Level Objective · target performance threshold."],
            ["IC", "Incident Commander · single coordinator during an active incident."],
            ["GitOps", "Operational model where the desired state lives in Git; controllers reconcile reality."],
            ["OPA", "Open Policy Agent · policy-as-code engine for admission control."],
            ["Argo CD", "Kubernetes GitOps continuous-delivery controller."],
        ],
        col_widths=[28*mm, 132*mm],
    ))
    story.append(Spacer(1, 12))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== PDF 4 — AI Readiness =================== #
def pdf_ai(out_path: str):
    story = []
    story += cover_block(
        title="AI Readiness Sprint · Final Report — SAMPLE",
        eyebrow="AI · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima S.A.",
        date="Q2 2026 · April 20 – May 18, 2026",
        engagement="#ANV-AIR-2604-1115",
        subtitle="4-week sprint · use case discovery, prioritization, build-vs-buy",
    )

    # Page 2 — Executive
    story.append(p("EXECUTIVE SUMMARY", "eyebrow"))
    story.append(p("12 use cases inventoried · 4 prioritized · 6 deferred · 2 no-go", "h1"))
    story.append(p(
        "Across 17 stakeholder interviews and a review of 8 strategic initiatives, we inventoried "
        "<b>12 candidate AI/GenAI use cases</b>. Each was scored on five dimensions: business value, "
        "data readiness, compliance risk, build complexity, and time-to-value. Four cases were "
        "promoted to the 12-month roadmap; six were deferred (mostly pending data foundations); "
        "two were explicitly classified \"no-go\" due to disproportionate risk or unclear ROI.",
        "body_just",
    ))
    story.append(Spacer(1, 10))
    story.append(p("Prioritization summary", "h2"))
    story.append(data_table(
        ["Use case", "Verdict", "Value (1-5)", "Data ready (1-5)", "TTV (months)"],
        [
            ["Sales call summarization pipeline", "Build (Q3)", "4", "5", "2"],
            ["Customer support triage with RAG", "Build (Q3-Q4)", "5", "4", "3"],
            ["Inventory demand forecasting refresh", "Buy + tune (Q4)", "4", "4", "3"],
            ["Document parsing for procurement contracts", "Build (Q4-Q1)", "4", "3", "4"],
            ["Personalized homepage recommendations", "Deferred", "3", "2", "—"],
            ["Sales rep coaching from call transcripts", "Deferred", "3", "3", "—"],
            ["Automated job-description generation", "Deferred", "2", "5", "—"],
            ["Lead scoring (revisit)", "Deferred", "3", "3", "—"],
            ["Marketing copy generation", "Deferred", "2", "4", "—"],
            ["Internal search across wikis", "Deferred", "3", "3", "—"],
            ["AI-driven dynamic pricing", "No-go (risk)", "—", "—", "—"],
            ["AI-driven hiring screening", "No-go (compliance)", "—", "—", "—"],
        ],
        col_widths=[72*mm, 28*mm, 20*mm, 24*mm, 22*mm],
        align_right_cols=[2, 3, 4],
    ))
    story.append(PageBreak())

    # Pages 3-6 — Top 4 use cases (one per page)
    cases = [
        {
            "title": "Sales call summarization pipeline",
            "rank": "#1",
            "kv": [
                ["Owner sponsor", "VP Sales"],
                ["Business value", "Each rep saves 35 min/day · est. R$ 410k/yr productivity capture."],
                ["Data readiness", "5/5 — Gong call recordings already in S3 (12-month archive)."],
                ["Latency budget", "Async · 1h post-call acceptable."],
                ["Compliance", "Low. Calls are with B2B counterparts under existing contractual NDAs. Speaker consent banner in place. LGPD: pseudonymize before storage."],
                ["Cost per inference", "~R$ 0.18 / call (10k input + 800 output tokens · Claude Sonnet)."],
                ["Build vs. buy", "Build. Off-the-shelf summarizers exist but the structured fields needed (next-step, blocker, deal-stage signal) require domain-specific prompt + eval suite."],
                ["12-month staging", "M1 PoV (50 calls) → M2 internal pilot (1 rep team) → M3 broad rollout → M4-M12 quality feedback loop & eval-driven prompt iteration."],
            ],
        },
        {
            "title": "Customer support triage with RAG over knowledge base",
            "rank": "#2",
            "kv": [
                ["Owner sponsor", "VP Customer Success"],
                ["Business value", "AHT (avg handle time) reduction est. 18% on Tier-1 tickets · est. R$ 680k/yr capacity reclaim."],
                ["Data readiness", "4/5 — Zendesk KB (1,400 articles) needs deduplication + 22% of articles have stale screenshots."],
                ["Latency budget", "Real-time · ≤2s p95 (agent-assist) · ≤6s for in-app self-serve."],
                ["Compliance", "Medium. PII present in tickets (account holder names, addresses). Required: PII redaction pre-retrieval, prompt-injection defenses, audit log of every generation, opt-out for sensitive accounts."],
                ["Cost per inference", "~R$ 0.09 / generation. 8k tickets/mo → ~R$ 8.6k/mo at full coverage."],
                ["Build vs. buy", "Hybrid. Use managed Bedrock Knowledge Base for retrieval; custom orchestration for triage routing + guardrails. Do not buy turnkey \"AI support agent\" — vendor lock-in risk on KB indexing."],
                ["12-month staging", "M1-M2 KB cleanup + dedup → M3 PoV with 1 agent team → M4-M6 agent-assist GA → M7-M9 self-serve beta → M10-M12 escalation routing."],
            ],
        },
        {
            "title": "Inventory demand forecasting refresh",
            "rank": "#3",
            "kv": [
                ["Owner sponsor", "VP Operations"],
                ["Business value", "Inventory carrying cost reduction est. R$ 520k/yr (target: 6-week-of-supply down to 4-week)."],
                ["Data readiness", "4/5 — SKU-level POS data present (24 months); promo calendar partially structured; supplier lead times in spreadsheets."],
                ["Latency budget", "Batch weekly forecasts · 4h SLA per cycle."],
                ["Compliance", "Low — no PII."],
                ["Cost per inference", "Negligible at batch cadence (~R$ 12 / weekly cycle on SageMaker)."],
                ["Build vs. buy", "Buy + tune. Adopt managed forecasting (SageMaker Canvas or RelEx) for the base model; tune locally on promo + supplier signals. Avoid bespoke from-scratch — well-trodden problem space."],
                ["12-month staging", "M1 vendor shortlist → M2-M3 PoV on 200 SKUs → M4-M5 backtest vs. status quo → M6-M9 phased rollout by category → M10-M12 supplier-lead-time integration."],
            ],
        },
        {
            "title": "Document parsing for procurement contracts",
            "rank": "#4",
            "kv": [
                ["Owner sponsor", "Head of Procurement + General Counsel"],
                ["Business value", "Contract review cycle time reduction est. 50% on standard supplier contracts (avg 60 / quarter)."],
                ["Data readiness", "3/5 — 14k PDFs in SharePoint; OCR quality variable; no labelled training set yet (acceptable for in-context extraction)."],
                ["Latency budget", "Async · 24h acceptable."],
                ["Compliance", "Medium-High. Contract content is confidential. Required: in-region (sa-east-1) processing only, no model training on data, audit log per extraction, redaction of counter-party PII in eval sets."],
                ["Cost per inference", "~R$ 1.40 / contract (avg 30 pages, structured extraction). 240/year → R$ 340/yr (negligible)."],
                ["Build vs. buy", "Build. Extraction schemas are organization-specific (clause taxonomies). Use Claude Sonnet + structured output + reviewer-in-the-loop for the first 12 months."],
                ["12-month staging", "M1 extraction schema design w/ legal → M2-M3 PoV on 50 historical contracts (gold-set) → M4-M6 pilot with 2 procurement managers → M7-M12 broader rollout + clause-deviation alerting."],
            ],
        },
    ]
    for case in cases:
        story.append(p(f"USE CASE · {case['rank']} OF 4", "eyebrow"))
        story.append(p(case["title"], "h1"))
        story.append(kv_table(case["kv"]))
        story.append(PageBreak())

    # Page 7 — Continued info (we have 4 cases each on own page so 4 pages = 3,4,5,6 — let's add a use-case roundup)
    # Insert page 7 — eval & guardrail principles
    story.append(p("CROSS-CUTTING PRINCIPLES", "eyebrow"))
    story.append(p("Evals, guardrails, & gates", "h1"))
    story.append(p("Eval-first development", "h2"))
    story += bullets([
        "Every use case ships with a gold-set of ≥50 examples scored by domain owner before any LLM call.",
        "Eval suite runs in CI; regression on the gold-set blocks deploy.",
        "Quarterly eval refresh — gold-set drifts as the world drifts.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Guardrail layers (defense in depth)", "h2"))
    story += bullets([
        "Input layer: PII detection + redaction · prompt-injection classifier.",
        "Model layer: system prompt + tool allow-list + structured output enforcement.",
        "Output layer: schema validation · toxicity / brand-safety check · citation requirement.",
        "Observability: every generation logged with redacted I/O · sample rate 100% for first 30 days.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Gate criteria · Discovery → PoV → Build → Operate", "h2"))
    story += bullets([
        "Discovery → PoV: hypothesis written; gold-set exists; owner identified.",
        "PoV → Build: eval ≥ baseline AND latency budget met AND cost-per-inference within model.",
        "Build → Operate: shadow run ≥ 2 weeks · zero P0 incidents · runbook complete.",
        "Operate → Sunset: usage below threshold or eval regression sustained ≥ 4 weeks.",
    ])
    story.append(PageBreak())

    # Page 8 — Roadmap
    story.append(p("12-MONTH ROADMAP", "eyebrow"))
    story.append(p("Discovery → PoV → Build → Operate gates", "h1"))
    story.append(gantt_axis(26))
    story.append(Spacer(1, 4))
    story.append(GanttRow("Sales summarization · PoV", 1, 4, color=HexColor("#a3b18a")))
    story.append(GanttRow("Sales summarization · Build", 5, 10, color=ACCENT))
    story.append(GanttRow("Sales summarization · Operate", 11, 26, color=HexColor("#5a3e36")))
    story.append(GanttRow("Support triage · KB cleanup", 1, 6, color=HexColor("#a3b18a")))
    story.append(GanttRow("Support triage · PoV", 7, 10, color=ACCENT))
    story.append(GanttRow("Support triage · Build (agent-assist)", 11, 18, color=HexColor("#5a3e36")))
    story.append(GanttRow("Demand forecasting · vendor PoV", 4, 12, color=ACCENT))
    story.append(GanttRow("Demand forecasting · Build & roll", 14, 22, color=HexColor("#5a3e36")))
    story.append(GanttRow("Contract parsing · schema + gold-set", 8, 14, color=HexColor("#a3b18a")))
    story.append(GanttRow("Contract parsing · Pilot", 15, 22, color=ACCENT))
    story.append(Spacer(1, 12))
    story.append(p(
        "<font color='#a3b18a'>■</font> Discovery / setup   "
        "<font color='#0c4a6e'>■</font> Build / pilot   "
        "<font color='#5a3e36'>■</font> Operate / scale",
        "small",
    ))
    story.append(PageBreak())

    # Page 9 — Build vs Buy
    story.append(p("BUILD vs. BUY SUMMARY", "eyebrow"))
    story.append(p("Decision matrix · 4 priority use cases", "h1"))
    story.append(data_table(
        ["Use case", "Verdict", "Rationale", "Vendor short-list"],
        [
            ["Sales summarization", "Build",
             "Structured field extraction is org-specific; eval suite is the moat.",
             "—"],
            ["Support triage (RAG)", "Hybrid",
             "Buy retrieval (Bedrock KB) + build orchestration + guardrails.",
             "Bedrock KB, Pinecone (alt)"],
            ["Demand forecasting", "Buy + tune",
             "Well-trodden problem; mature managed options.",
             "SageMaker Canvas, RelEx, ToolsGroup"],
            ["Contract parsing", "Build",
             "Clause taxonomy is org-specific; reviewer-in-loop is mandatory.",
             "—"],
        ],
        col_widths=[40*mm, 22*mm, 60*mm, 38*mm],
    ))
    story.append(Spacer(1, 12))
    story.append(p("Cross-cutting platform decisions", "h2"))
    story += bullets([
        "Model provider: Anthropic Claude (Sonnet/Haiku tiers) — best fit for structured-output + safety profile.",
        "Hosting: AWS Bedrock for in-region (sa-east-1) compliance with LGPD.",
        "Vector store: Bedrock Knowledge Base for managed; OpenSearch (managed) as escape hatch.",
        "Eval tooling: open-source <i>promptfoo</i> + custom gold-set runner integrated in CI.",
        "Observability: Langfuse self-hosted for trace inspection + cost attribution.",
    ])
    story.append(PageBreak())

    # Page 10 — Appendix
    story.append(p("APPENDIX · METHODOLOGY", "eyebrow"))
    story.append(p("Methodology & references", "h1"))
    story.append(p(
        "Use cases were sourced from (1) 17 stakeholder interviews across 6 functions, (2) review of "
        "the 2026 strategic plan, (3) inventory of current vendor evaluations, (4) review of "
        "in-flight engineering experiments. Each use case was scored on a 1-5 scale across 5 dimensions; "
        "scoring rubrics are documented in the engagement workspace.",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(p("Scoring rubric (abridged)", "h2"))
    story.append(data_table(
        ["Dimension", "1 (low)", "5 (high)"],
        [
            ["Business value", "Vague qualitative benefit", "Quantified P&L impact, owner-validated"],
            ["Data readiness", "Data does not exist", "Clean, labelled, accessible, governed"],
            ["Compliance risk", "Severe (PII / regulated)", "No regulated data touched"],
            ["Build complexity", "Novel research problem", "Off-the-shelf with prompt tuning"],
            ["Time-to-value", ">12 months", "<3 months"],
        ],
        col_widths=[32*mm, 60*mm, 70*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== PDF 5 — Sales Ops =================== #
def pdf_salesops(out_path: str):
    story = []
    story += cover_block(
        title="Sales Ops Diagnostic · Final Report — SAMPLE",
        eyebrow="GROWTH · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima S.A.",
        date="Q2 2026 · April 27 – May 18, 2026",
        engagement="#ANV-SOPS-2604-0721",
        subtitle="3-week diagnostic · funnel, SLA, automation, stack",
    )

    # Page 2 — Funnel
    story.append(p("FUNNEL MAP", "eyebrow"))
    story.append(p("Stage conversion + dwell time", "h1"))
    story.append(p(
        "Funnel measured across all sources, last 90 days (Jan-Mar 2026). N = 4,820 leads at top. "
        "Conversion rates are computed cohort-style: each rate is the share of the prior-stage cohort "
        "that reached the current stage within the cohort window.",
        "body_just",
    ))
    story.append(Spacer(1, 14))
    story.append(FunnelStage("Lead", "4,820 / 90d", "32%", top_w_pct=1.0, bot_w_pct=0.85))
    story.append(FunnelStage("MQL", "1,542", "41%", top_w_pct=0.85, bot_w_pct=0.65))
    story.append(FunnelStage("SQL", "632", "18%", top_w_pct=0.65, bot_w_pct=0.40))
    story.append(FunnelStage("Won", "114", None, top_w_pct=0.40, bot_w_pct=0.18))
    story.append(Spacer(1, 12))
    story.append(p("Dwell time per stage (median)", "h2"))
    story.append(data_table(
        ["Stage", "Median dwell", "P90 dwell", "Benchmark · B2B SaaS BR"],
        [
            ["Lead → MQL", "1.4 days", "9.2 days", "≤24 h ideal"],
            ["MQL → SQL", "3.7 days", "18 days", "≤3 days ideal"],
            ["SQL → Won", "47 days", "118 days", "varies by ticket"],
        ],
        col_widths=[42*mm, 30*mm, 30*mm, 60*mm],
    ))
    story.append(PageBreak())

    # Page 3 — Leakage
    story.append(p("LEAKAGE ANALYSIS", "eyebrow"))
    story.append(p("Top 3 leak stages · quantified loss", "h1"))
    story.append(p(
        "We measure leak as the gap between observed conversion and a credible benchmark (peer cohort "
        "or theoretical floor). Loss values are gross — converting at the benchmark rate would imply "
        "the indicated incremental Won deals at the segment's average contract value (R$ 38k / deal).",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(data_table(
        ["#", "Stage", "Current", "Benchmark", "Implied loss / quarter"],
        [
            ["L1", "Lead → MQL", "32%", "48% (top quartile)", "R$ 3.0M (77 deals)"],
            ["L2", "SQL → Won", "18%", "28% (peer cohort)", "R$ 2.4M (63 deals)"],
            ["L3", "MQL → SQL", "41%", "55% (top quartile)", "R$ 1.5M (40 deals)"],
        ],
        col_widths=[14*mm, 36*mm, 22*mm, 38*mm, 52*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p("L1 · Lead → MQL", "h2"))
    story += bullets([
        "<b>Root cause:</b> form-fill leads receive first touch in 6.2h on average (target ≤30 min).",
        "Drop-off heaviest in the 0-2h window — speed-to-lead is the single largest leverage point.",
        "Lead routing rules dump 23% of leads to a default queue (\"unassigned\"), creating triage friction.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("L2 · SQL → Won", "h2"))
    story += bullets([
        "<b>Root cause:</b> 41% of stalled opportunities have no next-step logged after 14 days.",
        "Discovery-call quality varies wildly across reps — strongest 3 reps convert at 29%; bottom 3 at 9%.",
        "Pricing objection cited in 38% of lost deals · pricing collateral inconsistent across team.",
    ])
    story.append(PageBreak())

    # Page 4 — Response time SLA
    story.append(p("RESPONSE-TIME SLA", "eyebrow"))
    story.append(p("Speed-to-lead across channels", "h1"))
    story.append(p(
        "Speed-to-lead is the dominant driver of L1 conversion. We measured first-meaningful-touch "
        "time across each channel over the 90-day window. The chasm between Messaging (best) and "
        "Referral (worst) is operational, not structural — both are routable to the same SDR pool.",
        "body_just",
    ))
    story.append(Spacer(1, 14))
    story.append(data_table(
        ["Channel", "Volume / 90d", "Avg response", "P90 response", "Target SLA"],
        [
            ["Form (website)", "1,940", "6.2 hours", "37 hours", "≤30 min"],
            ["Messaging (WhatsApp / chat)", "1,210", "2.4 hours", "11 hours", "≤15 min"],
            ["Referral", "240", "18 hours", "82 hours", "≤2 hours"],
            ["Inbound (call/email)", "1,430", "4.1 hours", "22 hours", "≤30 min"],
        ],
        col_widths=[44*mm, 26*mm, 28*mm, 26*mm, 26*mm],
        align_right_cols=[1, 2, 3],
    ))
    story.append(Spacer(1, 14))
    story.append(p("Key observations", "h2"))
    story += bullets([
        "Referral leads — the highest-converting category (SQL→Won 38%) — receive the slowest touch.",
        "Messaging is closest to target but has the highest weekend-volume gap (Sat/Sun 8h+ response).",
        "Form-fill volumes spike Tue-Thu 14:00-17:00; SDR coverage is uniform — re-staff to traffic.",
        "Existing CRM routing rules do not consider source — proposed change in §5 automations.",
    ])
    story.append(PageBreak())

    # Page 5 — Automation playbook
    story.append(p("AUTOMATION PLAYBOOK", "eyebrow"))
    story.append(p("5 prioritized automations · impact / effort", "h1"))
    story.append(data_table(
        ["#", "Automation", "Impact", "Effort", "Owner"],
        [
            ["A1", "Form-lead instant routing + SLA timer + nudge", "High", "Low", "RevOps + SDR Lead"],
            ["A2", "Referral lead expedited lane (5-min page)", "High", "Low", "RevOps"],
            ["A3", "Stalled-opp auto-task after 14d silence", "Medium", "Low", "RevOps + AE Manager"],
            ["A4", "Pricing-objection playbook + collateral lib", "High", "Medium", "Sales Enablement"],
            ["A5", "Weekend coverage rotation for messaging channel", "Medium", "Medium", "SDR Lead"],
        ],
        col_widths=[12*mm, 70*mm, 22*mm, 22*mm, 38*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p("Expected uplift if all 5 ship", "h2"))
    story += bullets([
        "Lead → MQL: 32% → 41% (recovery of ~60% of the benchmark gap).",
        "SQL → Won: 18% → 22% (incremental ~25 deals/quarter).",
        "Aggregate quarterly Won-deal lift: ~52 deals → ~R$ 2.0M ARR.",
        "Net new SDR/AE headcount required: 0 (recapture from operational waste).",
    ])
    story.append(Spacer(1, 10))
    story.append(p(
        "\"The fastest revenue lift here is not better leads — it's a 30-minute, end-to-end check on "
        "the existing routing rules.\"",
        "pull",
    ))
    story.append(PageBreak())

    # Page 6 — Stack assessment
    story.append(p("STACK ASSESSMENT", "eyebrow"))
    story.append(p("Keep · integrate · replace", "h1"))
    story.append(data_table(
        ["Tool", "Role", "Verdict", "Rationale"],
        [
            ["HubSpot", "CRM + marketing automation", "Keep", "Adequate. Underused — workflows + lead scoring need rebuild."],
            ["RD Station Marketing", "Email + landing pages", "Replace (12-18mo)", "Overlaps with HubSpot; sunset reduces stack cost + dual-source-of-truth headache."],
            ["Twilio (Programmable Messaging)", "WhatsApp business API", "Keep", "Working well; add HubSpot integration via custom workflow."],
            ["Outreach", "Sales engagement", "Keep", "Healthy adoption (74%). Coach the bottom-3 reps on cadence usage."],
            ["Apollo", "Prospecting / contact data", "Integrate", "Add bidirectional sync with HubSpot (currently one-way export, drift)."],
            ["Looker Studio", "Reporting", "Keep + extend", "Add funnel & SLA dashboards (current dashboards are pipeline-only)."],
            ["Slack", "Internal comms", "Integrate", "Add hot-lead alerts via HubSpot → Slack webhook (currently manual)."],
            ["Chili Piper", "Meeting scheduler", "Evaluate", "Compare against HubSpot Meetings + Cal.com — possible consolidation."],
        ],
        col_widths=[34*mm, 36*mm, 28*mm, 62*mm],
    ))
    story.append(PageBreak())

    # Page 7 — 90-day roadmap
    story.append(p("90-DAY ROADMAP", "eyebrow"))
    story.append(p("Phase plan with KPIs per phase", "h1"))
    story.append(p("Phase 1 · Days 1-30 — Foundation", "h2"))
    story += bullets([
        "A1 (instant routing) and A2 (referral lane) in production.",
        "Define funnel & SLA dashboards in Looker Studio.",
        "Pricing-objection playbook drafted with 2 senior AEs.",
    ])
    story.append(p("<b>KPI:</b> Form lead avg response 6.2h → ≤45 min; Referral 18h → ≤3h.", "subtle"))
    story.append(Spacer(1, 10))
    story.append(p("Phase 2 · Days 31-60 — Operationalize", "h2"))
    story += bullets([
        "A3 (stalled-opp task) live; AE manager review cadence weekly.",
        "Lead-scoring rebuild in HubSpot — collaborate with Marketing.",
        "Weekend coverage rotation (A5) staffed.",
        "Rep-level Outreach coaching for bottom-3 reps.",
    ])
    story.append(p("<b>KPI:</b> Lead → MQL 32% → 36%; SDR utilization gap narrowed by 50%.", "subtle"))
    story.append(Spacer(1, 10))
    story.append(p("Phase 3 · Days 61-90 — Measure & iterate", "h2"))
    story += bullets([
        "Quarterly funnel review · validate uplift against forecast.",
        "Begin RD Station sunset planning · target Q4 cutover.",
        "Pricing collateral library complete · usage measured.",
    ])
    story.append(p("<b>KPI:</b> Quarterly Won-deal volume +30 vs. baseline; SLA compliance ≥85%.", "subtle"))
    story.append(PageBreak())

    # Page 8 — Appendix
    story.append(p("APPENDIX · METHODOLOGY", "eyebrow"))
    story.append(p("Data sources & method", "h1"))
    story += bullets([
        "HubSpot CRM data export · 90 days · all objects (contact, deal, activity, task, email).",
        "Twilio messaging logs · 90 days.",
        "Outreach engagement data · 90 days.",
        "Looker Studio dashboards · current state.",
        "Interviews · 4 AEs · 3 SDRs · 1 SDR Lead · 1 VP Sales · 1 RevOps.",
    ])
    story.append(Spacer(1, 10))
    story.append(p("Benchmark sources", "h2"))
    story += bullets([
        "Pavilion State of Sales 2025 (B2B SaaS BR cohort).",
        "Hubspot Sales Benchmarks — Brazil cut · 2025.",
        "Anuvia internal anonymized benchmark pool (n=22 engagements).",
    ])
    story.append(Spacer(1, 12))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== PDF 6 — Industry Assessment =================== #
def pdf_industry(out_path: str):
    story = []
    story += cover_block(
        title="Industry Assessment · Manufacturing Vertical — SAMPLE",
        eyebrow="INDUSTRY · DIAGNOSTIC DELIVERABLE",
        client="Empresa Anônima Indústria S.A.",
        date="Q2 2026 · April 06 – May 04, 2026",
        engagement="#ANV-IND-2604-0312",
        subtitle="4-week vertical assessment · AI & automation use cases for manufacturing",
    )

    # Page 2 — Vertical context
    story.append(p("VERTICAL CONTEXT", "eyebrow"))
    story.append(p("Manufacturing · Brazil · mid-market", "h1"))
    story.append(p(
        "The Brazilian mid-market manufacturer (R$ 200M-R$ 1B revenue band) operates within a "
        "specific set of constraints: a maturing Industry 4.0 adoption curve, mixed OT/IT estate "
        "with legacy PLCs, increasing pressure on energy efficiency, and the LGPD plus sector-specific "
        "regulations (NR-12 for machine safety, ABNT operational standards).",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(p("Regulatory environment", "h2"))
    story.append(data_table(
        ["Regulation", "Scope", "Impact on AI / automation"],
        [
            ["LGPD", "Personal data of employees, suppliers, B2B contacts", "Pseudonymize employee operational data; supplier data lawful basis review."],
            ["NR-12 (MTE)", "Machine & equipment safety", "AI-driven control loops require fail-safe certification; advisory-only mode preferred."],
            ["ANVISA / sector-specific", "Where applicable (food, pharma)", "Process-change traceability mandatory."],
            ["MROSC / fiscal reqs", "Documentation, audit trails", "Any AI-driven decision affecting fiscal/operational reporting must log inputs & outputs."],
        ],
        col_widths=[34*mm, 50*mm, 76*mm],
    ))
    story.append(Spacer(1, 10))
    story.append(p("Market patterns observed", "h2"))
    story += bullets([
        "OEE (Overall Equipment Effectiveness) avg in segment: 58-68% — significant headroom.",
        "Predictive-maintenance adoption is partial: 38% of mid-market manufacturers run sensors but only 12% drive a closed loop with CMMS.",
        "Energy cost share growing — 7-12% of cost-of-goods, up from 5-7% three years ago.",
        "Supply chain volatility persists — forecast accuracy below 65% for most.",
    ])
    story.append(PageBreak())

    # Page 3 — Top 5 use cases
    story.append(p("USE CASE RANKING", "eyebrow"))
    story.append(p("Top 5 AI / automation use cases · manufacturing", "h1"))
    story.append(data_table(
        ["Rank", "Use case", "Impact horizon", "Capex", "Verdict"],
        [
            ["#1", "Predictive maintenance · sensors + anomaly + CMMS", "12-18 mo", "Med", "Build · phased"],
            ["#2", "Quality vision (defect detection at the line)", "6-12 mo", "Med", "Build · pilot first"],
            ["#3", "Energy optimization · HVAC + compressors", "9-18 mo", "Low", "Buy + tune"],
            ["#4", "Demand forecasting refresh", "6-12 mo", "Low", "Buy + tune"],
            ["#5", "Document AI · purchase orders + invoices", "3-6 mo", "Low", "Build · light"],
        ],
        col_widths=[14*mm, 76*mm, 26*mm, 18*mm, 28*mm],
    ))
    story.append(Spacer(1, 14))
    story.append(p("Scoring rationale", "h2"))
    story += bullets([
        "Predictive maintenance ranked #1: highest expected NPV across the band, strong data foundations from existing PLC instrumentation, regulatory path well-trodden.",
        "Quality vision ranked #2: best fit for incremental rollout (line-by-line), strong evidence of ROI in adjacent verticals.",
        "Energy optimization ranked #3: lowest capex, but ROI sensitive to local energy tariff schedule.",
        "Demand forecasting ranked #4: well-defined; many mature vendors; benefits diminish as upstream data quality plateaus.",
        "Document AI ranked #5: smallest ticket but quickest payback — \"first win\" candidate.",
    ])
    story.append(PageBreak())

    # Page 4 — #1 deep dive
    story.append(p("USE CASE DEEP-DIVE · #1", "eyebrow"))
    story.append(p("Predictive maintenance · sensors + anomaly + CMMS", "h1"))
    story.append(p(
        "A closed-loop predictive maintenance program reduces unplanned downtime by 18-32% in "
        "comparable mid-market manufacturing settings (CARS-D consortium benchmarks, 2024). The "
        "system requires three components stitched together — sensors generating high-frequency "
        "vibration / temperature / current data, an anomaly-detection layer, and integration with "
        "the existing CMMS to convert anomalies into prioritized work orders.",
        "body_just",
    ))
    story.append(Spacer(1, 8))
    story.append(p("Component stack", "h2"))
    story.append(data_table(
        ["Layer", "Recommended", "Capex / vendor"],
        [
            ["Edge sensors (vibration + temp)", "Wireless IIoT sensors · 200 units phase 1", "R$ 380k (one-time)"],
            ["Edge gateway", "Industrial gateway + buffering · 8 units", "R$ 140k"],
            ["Data ingest", "AWS IoT Core · sa-east-1", "Opex: R$ 4k/mo"],
            ["Anomaly model", "Isolation forest baseline + per-asset tuning", "Build (internal)"],
            ["CMMS integration", "REST adapter to existing CMMS (Engeman / MV2)", "Build (4 wks)"],
            ["Visualization", "Grafana dashboards + ops mobile alert", "Build (2 wks)"],
        ],
        col_widths=[42*mm, 70*mm, 48*mm],
    ))
    story.append(Spacer(1, 10))
    story.append(p("Risk register", "h2"))
    story += bullets([
        "Sensor mount quality is a top risk — 15% of pilot deployments report data drift from poor mounting.",
        "False-positive alerts erode operator trust within 90 days if not tuned.",
        "Spare-parts logistics — CMMS-triggered work orders only deliver ROI if parts are stocked.",
    ])
    story.append(PageBreak())

    # Page 5 — ROI model
    story.append(p("ROI MODEL · USE CASE #1", "eyebrow"))
    story.append(p("Predictive maintenance — 36-month model", "h1"))
    story.append(p("Assumptions (explicit)", "h2"))
    story.append(data_table(
        ["Assumption", "Value", "Source"],
        [
            ["Current unplanned downtime", "412 hr/yr", "Client CMMS export 2024-2025"],
            ["Downtime cost per hour", "R$ 28,000", "Client finance · GM·throughput-based"],
            ["Expected downtime reduction (steady-state)", "24% (mid of 18-32% band)", "CARS-D 2024 benchmark"],
            ["Time-to-steady-state", "Month 9", "Anuvia engagement pattern"],
            ["Sensor + gateway capex", "R$ 520k", "Vendor quote · BR market"],
            ["Annual opex (cloud + software)", "R$ 96k", "AWS IoT Core + CMMS integration support"],
            ["Spare parts buffer increase", "R$ 80k one-time", "Stock-level adjustment"],
        ],
        col_widths=[78*mm, 36*mm, 46*mm],
    ))
    story.append(Spacer(1, 10))
    story.append(p("36-month cash flow (R$ thousands)", "h2"))
    story.append(data_table(
        ["Year", "Capex", "Opex", "Downtime savings", "Net"],
        [
            ["Y1", "-600", "-96", "+1,389 (partial)", "+693"],
            ["Y2", "0", "-96", "+2,769", "+2,673"],
            ["Y3", "0", "-96", "+2,769", "+2,673"],
            ["Total", "-600", "-288", "+6,927", "+6,039"],
        ],
        col_widths=[24*mm, 28*mm, 28*mm, 46*mm, 28*mm],
        align_right_cols=[1, 2, 3, 4],
    ))
    story.append(Spacer(1, 10))
    story.append(p(
        "Payback: <b>Month 9</b>.  NPV (12% discount, 36mo): <b>R$ 4.7M</b>.  IRR: <b>183%</b>.",
        "pull",
    ))
    story.append(PageBreak())

    # Page 6 — Compliance
    story.append(p("COMPLIANCE CONSIDERATIONS", "eyebrow"))
    story.append(p("LGPD + sector regulations", "h1"))
    story.append(p("LGPD", "h2"))
    story += bullets([
        "Operational sensor data is generally non-personal — outside LGPD primary scope.",
        "Operator identification (badge → machine → output) is personal data — requires lawful basis (legitimate interest or contract).",
        "Build a data inventory + DPIA for use cases that link operator identity with performance metrics.",
        "Retention policy: aggregate operational data may be retained indefinitely; operator-attributable data ≤ 24 months unless compelling justification.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("NR-12 (machine safety)", "h2"))
    story += bullets([
        "AI driving direct machine actuation must integrate with NR-12-compliant fail-safe interlocks.",
        "Recommend advisory-only deployment for the first 12 months — operator-in-loop.",
        "Document the change-management process per ABNT NBR ISO/IEC 27001-aligned controls.",
    ])
    story.append(Spacer(1, 8))
    story.append(p("Audit-trail requirements", "h2"))
    story += bullets([
        "Every AI-driven recommendation logged with: input features (hashed), model version, output, downstream action taken, operator confirming.",
        "Retention 5 years (alignment with fiscal/operational audit standards).",
        "Quarterly model-review minutes archived.",
    ])
    story.append(PageBreak())

    # Page 7 — Implementation roadmap
    story.append(p("IMPLEMENTATION ROADMAP", "eyebrow"))
    story.append(p("12-month phased rollout", "h1"))
    story.append(gantt_axis(26))
    story.append(Spacer(1, 4))
    story.append(GanttRow("Document AI · POs + invoices pilot", 1, 5, color=HexColor("#a3b18a")))
    story.append(GanttRow("Document AI · production rollout", 6, 14, color=ACCENT))
    story.append(GanttRow("Predictive maint · sensor selection & install (line 1)", 2, 8, color=ACCENT))
    story.append(GanttRow("Predictive maint · anomaly model + CMMS integration", 8, 14, color=ACCENT))
    story.append(GanttRow("Predictive maint · phase 2 expansion (lines 2-3)", 16, 26, color=HexColor("#5a3e36")))
    story.append(GanttRow("Quality vision · POC on critical line", 6, 12, color=ACCENT))
    story.append(GanttRow("Quality vision · production rollout", 14, 22, color=HexColor("#5a3e36")))
    story.append(GanttRow("Energy optimization · vendor PoV", 4, 10, color=HexColor("#a3b18a")))
    story.append(GanttRow("Demand forecasting · refresh program", 8, 18, color=ACCENT))
    story.append(Spacer(1, 14))
    story.append(p("Gate criteria", "h2"))
    story += bullets([
        "Pilot → Rollout: defined success metric achieved on 1 line for 6 weeks; runbook complete.",
        "Rollout → Scale: zero P0 events for 4 weeks; ops team owns operations.",
        "Scale → Operate: financial review against business case; quarterly committee.",
    ])
    story.append(PageBreak())

    # Page 8 — Reference architecture
    story.append(p("REFERENCE ARCHITECTURE", "eyebrow"))
    story.append(p("Predictive maintenance · target topology", "h1"))

    # Render a simple architecture diagram via Flowable
    class Arch(Flowable):
        def __init__(self):
            super().__init__()
            self.width = PAGE_W - MARGIN_L - MARGIN_R
            self.height = 280
        def draw(self):
            c = self.canv
            W = self.width
            H = self.height
            # Three horizontal swimlanes: OT/Edge, Cloud, Apps
            lane_h = (H - 30) / 3
            lanes = [("OT / Plant edge", 0), ("AWS sa-east-1 cloud", lane_h + 10), ("Apps & ops", 2 * (lane_h + 5))]
            for name, y in lanes:
                c.setFillColor(HexColor("#f5f4f0"))
                c.rect(0, y, W, lane_h, fill=1, stroke=0)
                c.setFillColor(SUBTLE)
                c.setFont(SANS_BOLD, 8)
                c.drawString(6, y + lane_h - 12, name)
            # Boxes per lane
            def box(x, y, w, h, label, sub=""):
                c.setStrokeColor(INK)
                c.setFillColor(PAPER)
                c.setLineWidth(0.6)
                c.rect(x, y, w, h, fill=1, stroke=1)
                c.setFillColor(INK)
                c.setFont(SANS_BOLD, 9)
                c.drawCentredString(x + w/2, y + h - 14, label)
                if sub:
                    c.setFillColor(SUBTLE)
                    c.setFont(SANS, 7.5)
                    c.drawCentredString(x + w/2, y + h - 26, sub)

            def arrow(x1, y1, x2, y2):
                c.setStrokeColor(ACCENT)
                c.setLineWidth(1)
                c.line(x1, y1, x2, y2)
                # arrowhead
                import math
                ang = math.atan2(y2 - y1, x2 - x1)
                ah = 5
                c.line(x2, y2, x2 - ah * math.cos(ang - 0.4), y2 - ah * math.sin(ang - 0.4))
                c.line(x2, y2, x2 - ah * math.cos(ang + 0.4), y2 - ah * math.sin(ang + 0.4))

            # Lane 1 - OT
            y1 = 10
            box(20, y1, 90, 50, "Sensors", "vibration · temp · current")
            box(140, y1, 90, 50, "Edge gateway", "buffer · pre-process")
            box(260, y1, 90, 50, "PLC / SCADA", "(existing OT)")
            box(380, y1, 90, 50, "Local Historian", "fallback storage")

            # Lane 2 - Cloud
            y2 = lane_h + 20
            box(20, y2, 90, 50, "AWS IoT Core", "MQTT broker")
            box(140, y2, 90, 50, "Kinesis Streams", "real-time bus")
            box(260, y2, 90, 50, "Anomaly model", "isolation forest")
            box(380, y2, 90, 50, "S3 + Athena", "historian / training")

            # Lane 3 - Apps
            y3 = 2 * (lane_h + 5) + 10
            box(20, y3, 90, 50, "CMMS adapter", "REST → Engeman")
            box(140, y3, 90, 50, "Work orders", "auto-prioritized")
            box(260, y3, 90, 50, "Grafana dashboard", "ops view")
            box(380, y3, 90, 50, "Mobile alerts", "on-call ops")

            # Arrows
            arrow(110, y1 + 25, 140, y1 + 25)  # sensor → gateway
            arrow(230, y1 + 25, 260, y1 + 25)  # gateway → plc passthrough
            arrow(185, y1 + 50, 65, y2)        # gateway → iot core
            arrow(110, y2 + 25, 140, y2 + 25)  # iot → kinesis
            arrow(230, y2 + 25, 260, y2 + 25)  # kinesis → anomaly
            arrow(350, y2 + 25, 380, y2 + 25)  # anomaly → s3
            arrow(305, y2 + 50, 65, y3 + 50)   # anomaly → cmms
            arrow(110, y3 + 25, 140, y3 + 25)  # cmms → wo
            arrow(230, y3 + 25, 260, y3 + 25)  # wo → grafana
            arrow(350, y3 + 25, 380, y3 + 25)  # grafana → mobile

    story.append(Arch())
    story.append(Spacer(1, 14))
    story.append(p("Topology notes", "h2"))
    story += bullets([
        "All control-plane traffic stays in sa-east-1 (LGPD + data sovereignty).",
        "Edge gateways buffer up to 24h to survive WAN outages.",
        "Anomaly model runs serverless (Lambda + SageMaker endpoint for batch retraining).",
        "CMMS integration is one-way (advisory) for the first 12 months.",
        "Mobile alerts use existing on-call rotation; no new tooling.",
    ])
    story.append(Spacer(1, 14))
    story.append(p(
        "— End of report — Anuvia · contato@anuvia.com.br · anuvia.com.br",
        "small",
    ))
    build_doc(out_path, story)


# =================== Entrypoint =================== #
def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "samples")
    os.makedirs(out_dir, exist_ok=True)
    targets = [
        ("sample_finops_audit_report.pdf", pdf_finops),
        ("sample_aws_well_architected.pdf", pdf_wa),
        ("sample_devops_maturity_assessment.pdf", pdf_devops),
        ("sample_ai_readiness_sprint.pdf", pdf_ai),
        ("sample_sales_ops_diagnostic.pdf", pdf_salesops),
        ("sample_industry_assessment.pdf", pdf_industry),
    ]
    total = 0
    for name, fn in targets:
        path = os.path.join(out_dir, name)
        fn(path)
        sz = os.path.getsize(path)
        total += sz
        print(f"{name:48s} {sz/1024:7.1f} KB")
    print(f"{'TOTAL':48s} {total/1024:7.1f} KB")


if __name__ == "__main__":
    main()
