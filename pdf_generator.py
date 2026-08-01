import logging
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config

log = logging.getLogger(__name__)

TEXT_DARK = colors.HexColor("#1A1A1A")
TEXT_MUTED = colors.HexColor("#6B6B6B")
TRACK_GRAY = colors.HexColor("#E5E5E5")
BORDER_GRAY = colors.HexColor("#D8D8D6")
CARD_BG = colors.HexColor("#F7F7F6")

PAGE_MARGIN = 20 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN

styles = getSampleStyleSheet()
STYLE_TITLE = ParagraphStyle("ReportTitle", parent=styles["Title"], textColor=TEXT_DARK, fontSize=19, leading=23)
STYLE_SUBTITLE = ParagraphStyle("ReportSubtitle", parent=styles["Normal"], textColor=TEXT_MUTED, fontSize=10)
STYLE_INFO_LABEL = ParagraphStyle("InfoLabel", parent=styles["Normal"], textColor=TEXT_MUTED, fontSize=7.5, leading=10)
STYLE_INFO_VALUE = ParagraphStyle("InfoValue", parent=styles["Normal"], textColor=TEXT_DARK, fontSize=10.5, leading=13, fontName="Helvetica-Bold")
STYLE_HEADING = ParagraphStyle("SectionHeading", parent=styles["Heading2"], textColor=TEXT_DARK, fontSize=13, spaceBefore=4, spaceAfter=8)
STYLE_BODY = ParagraphStyle("Body", parent=styles["Normal"], textColor=TEXT_DARK, fontSize=10.5, leading=15.5)
STYLE_BULLET = ParagraphStyle("Bullet", parent=STYLE_BODY, leftIndent=16, bulletIndent=2, spaceAfter=4)
STYLE_LEGEND = ParagraphStyle("Legend", parent=styles["Normal"], textColor=TEXT_MUTED, fontSize=8.5)
STYLE_CLOSING = ParagraphStyle("Closing", parent=styles["Normal"], textColor=TEXT_MUTED, fontSize=9, leading=13, fontName="Helvetica-Oblique")

KNOWN_HEADINGS = {
    "strengths": "Strengths",
    "where_to_focus_next": "Where to focus next",
    "next_steps": "Suggested next steps",
    "interest_zone": "Interest zone",
    "closing_note": None,  # rendered without a heading, as a closing line
}


def _coerce_score(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _esc(text):
    """
    ReportLab's Paragraph treats its text as XML-ish markup -- an unescaped '&' in
    real content (e.g. a branch name like "AI&DS") gets parsed as a broken entity
    reference and renders mangled ("AI&DS;"). Escapes dynamic text before it's
    interpolated into any markup string; never apply this to the markup we write
    ourselves (<b>, <br/>, &bull; etc).
    """
    return _xml_escape("" if text is None else str(text))


class SectionBar(Flowable):
    def __init__(self, label, score, height=14, track_height=9):
        super().__init__()
        self.label = label
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0
        self.score = max(0, min(100, score))
        self.height = height
        self.track_height = track_height
        self.width = 0  # set for real in wrap(), which knows the actual available width
        self.color = (
            colors.HexColor(config.COLOR_MEETS_EXPECTATIONS)
            if self.score >= config.SCORE_PASS_THRESHOLD
            else colors.HexColor(config.COLOR_NEEDS_IMPROVEMENT)
        )

    def wrap(self, avail_width, avail_height):
        # Match the bar's drawn width to whatever space it's actually laid out in
        # (e.g. inside a padded table cell) instead of a hardcoded value that could
        # be wider or narrower than the real slot and clip or leave a gap.
        self.width = avail_width
        return (avail_width, self.height + 16)

    def draw(self):
        canv = self.canv
        canv.setFont("Helvetica", 9.5)
        canv.setFillColor(TEXT_DARK)
        canv.drawString(0, self.height + 4, self.label)
        canv.setFont("Helvetica-Bold", 9.5)
        canv.drawRightString(self.width, self.height + 4, str(round(self.score)))

        canv.setFillColor(TRACK_GRAY)
        canv.roundRect(0, 0, self.width, self.track_height, 2.5, stroke=0, fill=1)

        fill_width = self.width * (self.score / 100.0)
        if fill_width > 0:
            canv.setFillColor(self.color)
            canv.roundRect(0, 0, fill_width, self.track_height, 2.5, stroke=0, fill=1)


def _legend_flowable():
    return Paragraph(
        f'<font color="{config.COLOR_NEEDS_IMPROVEMENT}">&#9632;</font> Needs improvement &nbsp;&nbsp;&nbsp; '
        f'<font color="{config.COLOR_MEETS_EXPECTATIONS}">&#9632;</font> Meets expectations',
        STYLE_LEGEND,
    )


def _divider():
    return HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY, spaceBefore=10, spaceAfter=14)


def _info_table(identity):
    """A bordered, gridded identity block (in the spirit of a clean certificate-style
    'Candidate / Exam Date' table) instead of a loose block of right-aligned text --
    scans faster and reads as more considered/professional."""
    def cell(label, value):
        return [Paragraph(label.upper(), STYLE_INFO_LABEL), Paragraph(_esc(value) or "—", STYLE_INFO_VALUE)]

    data = [
        [*cell("Student", identity.get("name", "")), *cell("Institution", identity.get("institution", ""))],
        [*cell("Branch", identity.get("branch", "")), *cell("Year", identity.get("year", ""))],
    ]
    label_w = CONTENT_WIDTH * 0.14
    value_w = CONTENT_WIDTH * 0.36
    table = Table(data, colWidths=[label_w, value_w, label_w, value_w])
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _score_card(bars):
    """Wraps the section bars in a bordered, lightly-shaded card -- same idea as the
    bordered tables in the reference layout, kept simple: one box, one background
    tint, no extra graphics."""
    rows = [[b] for b in bars]
    table = Table(rows, colWidths=[CONTENT_WIDTH - 28])
    table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _bullets(items):
    return [Paragraph(f"&bull;&nbsp;&nbsp;{_esc(item)}", STYLE_BULLET) for item in items]


def _render_known_or_generic_block(key, value, story):
    if key == "closing_note":
        return
    heading = KNOWN_HEADINGS.get(key, key.replace("_", " ").title())
    if heading:
        story.append(Paragraph(_esc(heading), STYLE_HEADING))
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            for item in value:
                q = item.get("question", "")
                a = item.get("answer", "")
                story.append(Paragraph(f"<b>{_esc(q)}</b>", STYLE_BODY))
                story.append(Paragraph(_esc(a), STYLE_BULLET))
                story.append(Spacer(1, 4))
        else:
            story.extend(_bullets(value))
    else:
        story.append(Paragraph(_esc(value), STYLE_BODY))
    story.append(Spacer(1, 6))


def generate_pdf(identity, report_json, output_path):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
    )
    story = []

    story.append(Paragraph("Career Readiness Report", STYLE_TITLE))
    story.append(Paragraph("A personalized snapshot based on your survey answers", STYLE_SUBTITLE))
    story.append(Spacer(1, 14))
    story.append(_info_table(identity))
    story.append(Spacer(1, 18))

    section_scores = report_json.get("section_scores")
    if section_scores:
        bars = []
        for section_key, section in section_scores.items():
            score = _coerce_score(section.get("score"))
            if score is None:
                # The AI declined to score this section (e.g. returned null with a
                # "not computed" note) -- fabricating a 0 here would render as a real
                # "needs improvement" bar and misrepresent "not scored" as "scored badly".
                log.warning("Skipping bar for %r -- AI returned no numeric score", section.get("label"))
                continue
            # section.get("label") covers the odd case where the model uses a slightly
            # different key (e.g. "link" instead of "label") -- fall back to the
            # section's own dict key rather than showing a blank bar label.
            label = section.get("label") or section_key
            bars.append(SectionBar(label, score))
        if bars:
            story.append(_score_card(bars))
            story.append(Spacer(1, 8))
            story.append(_legend_flowable())
            story.append(Spacer(1, 18))

    if report_json.get("where_you_stand"):
        story.append(Paragraph("Where you stand", STYLE_HEADING))
        story.append(Paragraph(_esc(report_json["where_you_stand"]), STYLE_BODY))

    if report_json.get("how_this_shows_up"):
        story.append(_divider())
        story.append(Paragraph("How this shows up in your answers", STYLE_HEADING))
        story.append(Paragraph(_esc(report_json["how_this_shows_up"]), STYLE_BODY))

    page1_keys = {"section_scores", "where_you_stand", "how_this_shows_up"}
    remaining_keys = [
        k for k in report_json.keys()
        if k not in page1_keys and k != "closing_note" and not k.startswith("_")
    ]

    if remaining_keys:
        story.append(PageBreak())
        for i, key in enumerate(remaining_keys):
            value = report_json[key]
            if value in (None, "", [], {}):
                continue
            if i > 0:
                story.append(_divider())
            _render_known_or_generic_block(key, value, story)

    if report_json.get("closing_note"):
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="35%", thickness=0.5, color=BORDER_GRAY, spaceAfter=8))
        story.append(Paragraph(_esc(report_json["closing_note"]), STYLE_CLOSING))

    doc.build(story)
    return output_path
