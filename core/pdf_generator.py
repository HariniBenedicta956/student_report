import logging
from datetime import date
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

import config

log = logging.getLogger(__name__)

# --- palette, taken from the Personal Learning Growth Report template ----------
NAVY = colors.HexColor("#1B2A4A")        # headings, rules, the priority panel
ORANGE = colors.HexColor("#C2571E")      # eyebrow, page number, accent bars
TEXT_DARK = colors.HexColor("#1A1A1A")
TEXT_MUTED = colors.HexColor("#6B7280")
LABEL_GREY = colors.HexColor("#8A8F98")  # the small caps card labels
BORDER = colors.HexColor("#E3E3E1")
CREAM = colors.HexColor("#FAF6EF")       # callout background
ACTION_BG = colors.HexColor("#F2F2F0")   # the "Try this:" inner box

# One colour per tier. The set is closed -- prompt_builder.TIERS is the contract,
# and anything outside it has nothing to draw, so it is caught in validation
# rather than guessed at here.
TIER_COLORS = {
    "Strength": colors.HexColor("#2E6B4F"),
    "Developing": colors.HexColor("#C08A0E"),
    "Focus Required": colors.HexColor("#A33A2A"),
    "Blind Spot": colors.HexColor("#5F55A0"),
}
TIER_LEGEND = [
    ("Strength", "consistently demonstrated"),
    ("Developing", "present, not yet consistent"),
    ("Focus Required", "will limit you if unaddressed"),
    ("Blind Spot", "belief and behaviour don't yet match"),
]

PAGE_MARGIN = 16 * mm
CONTENT_WIDTH = A4[0] - 2 * PAGE_MARGIN

PROGRAMME_EYEBROW = "PERSONAL LEARNING GROWTH PROGRAMME"
HOW_TO_READ = (
    "These are qualitative bands describing where you are right now, not scores, "
    "percentiles or a ranking against anyone else. A band can move with practice."
)
FOOTER_DISCLAIMER = (
    "This report reflects one set of self-reported answers at a single point in "
    "time. It is a snapshot to work from, not a permanent label."
)

_styles = getSampleStyleSheet()
S_EYEBROW = ParagraphStyle("Eyebrow", parent=_styles["Normal"], textColor=ORANGE,
                            fontName="Helvetica-Bold", fontSize=7.5, leading=11)
S_TITLE = ParagraphStyle("Title2", parent=_styles["Normal"], textColor=NAVY,
                          fontName="Helvetica-Bold", fontSize=20, leading=25)
S_PAGENUM = ParagraphStyle("PageNum", parent=_styles["Normal"], textColor=ORANGE,
                            fontName="Helvetica-Bold", fontSize=8.5, leading=12)
S_ISSUED = ParagraphStyle("Issued", parent=_styles["Normal"], textColor=TEXT_MUTED,
                           fontSize=8.5, leading=12, alignment=2)
S_H2 = ParagraphStyle("H2", parent=_styles["Normal"], textColor=NAVY,
                       fontName="Helvetica-Bold", fontSize=14, leading=19)
S_SUBTLE = ParagraphStyle("Subtle", parent=_styles["Normal"], textColor=TEXT_MUTED,
                           fontSize=8.5, leading=12)
S_BODY = ParagraphStyle("Body2", parent=_styles["Normal"], textColor=TEXT_DARK,
                         fontSize=9.5, leading=14)
S_CALLOUT = ParagraphStyle("Callout", parent=S_BODY, fontSize=9.5, leading=15)
S_DIM_NAME = ParagraphStyle("DimName", parent=_styles["Normal"], textColor=NAVY,
                             fontName="Helvetica-Bold", fontSize=10.5, leading=14)
S_DIM_DESC = ParagraphStyle("DimDesc", parent=_styles["Normal"], textColor=TEXT_MUTED,
                             fontSize=9, leading=13)
S_CARD_LABEL = ParagraphStyle("CardLabel", parent=_styles["Normal"], textColor=LABEL_GREY,
                               fontName="Helvetica-Bold", fontSize=6.8, leading=10)
S_CARD_HEAD = ParagraphStyle("CardHead", parent=_styles["Normal"], textColor=NAVY,
                              fontName="Helvetica-Bold", fontSize=10.5, leading=14)
S_ACTION = ParagraphStyle("Action", parent=_styles["Normal"], textColor=TEXT_DARK,
                           fontSize=9, leading=13)
S_PRIORITY_LABEL = ParagraphStyle("PriorityLabel", parent=_styles["Normal"],
                                   textColor=ORANGE, fontName="Helvetica-Bold",
                                   fontSize=7, leading=11)
S_PRIORITY_HEAD = ParagraphStyle("PriorityHead", parent=_styles["Normal"],
                                  textColor=colors.white, fontName="Helvetica-Bold",
                                  fontSize=12.5, leading=17)
S_PRIORITY_BODY = ParagraphStyle("PriorityBody", parent=_styles["Normal"],
                                  textColor=colors.HexColor("#D8DCE6"),
                                  fontSize=9.5, leading=14)
S_FOOTER = ParagraphStyle("Footer", parent=_styles["Normal"], textColor=TEXT_MUTED,
                           fontSize=7.5, leading=11)
S_STUDENT_NAME = ParagraphStyle("StudentName", parent=_styles["Normal"], textColor=TEXT_MUTED,
                                 fontName="Helvetica-Bold", fontSize=10.5, leading=14)


def _esc(text):
    """
    ReportLab's Paragraph treats its text as XML-ish markup -- an unescaped '&' in
    real content (a branch name like "AI&DS") gets parsed as a broken entity
    reference and renders mangled. Applied to every piece of dynamic text; never
    to the markup written here (<b>, <br/>, &bull;).
    """
    return _xml_escape("" if text is None else str(text))


class Pill(Flowable):
    """The rounded tier badge. ReportLab tables cannot round their corners, so the
    badge is drawn directly rather than faked with a bordered cell."""

    def __init__(self, label, fill, height=13.5, pad=8, font_size=7):
        super().__init__()
        self.label = (label or "").upper()
        self.fill = fill
        self.height = height
        self.pad = pad
        self.font_size = font_size
        self.width = 0

    def wrap(self, avail_width, avail_height):
        text_w = self.canv.stringWidth(self.label, "Helvetica-Bold", self.font_size)
        self.width = min(text_w + 2 * self.pad, avail_width)
        return (avail_width, self.height)

    def draw(self):
        c = self.canv
        c.setFillColor(self.fill)
        c.roundRect(0, 0, self.width, self.height, self.height / 2, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", self.font_size)
        c.drawCentredString(self.width / 2, self.height / 2 - self.font_size / 2 + 1,
                             self.label)


class Dot(Flowable):
    """Small filled circle for the tier legend."""

    def __init__(self, color, size=5):
        super().__init__()
        self.color = color
        self.size = size
        self.width = size
        self.height = size

    def wrap(self, *_):
        return (self.size, self.size)

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.circle(self.size / 2, self.size / 2, self.size / 2, stroke=0, fill=1)


def _accent_card(inner_flowables, accent, bg=colors.white, border=BORDER,
                  accent_width=3):
    """
    A card with a coloured bar down its left edge -- the shape used for every
    strength / focus / blind-spot block. Built as a two-column table: a thin
    filled column for the bar, then the content.
    """
    table = Table(
        [[" ", inner_flowables]],
        colWidths=[accent_width, CONTENT_WIDTH - accent_width],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), accent),
        ("BACKGROUND", (1, 0), (1, 0), bg),
        ("LINEABOVE", (1, 0), (1, 0), 0.5, border),
        ("LINEBELOW", (1, 0), (1, 0), 0.5, border),
        ("LINEAFTER", (1, 0), (1, 0), 0.5, border),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 0),
        ("TOPPADDING", (0, 0), (0, 0), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
        ("RIGHTPADDING", (1, 0), (1, 0), 12),
        ("TOPPADDING", (1, 0), (1, 0), 8),
        ("BOTTOMPADDING", (1, 0), (1, 0), 8),
    ]))
    return table


def _callout(flowables):
    """Cream panel with an orange left bar -- the intro and 'How to read this' blocks."""
    return _accent_card(flowables, ORANGE, bg=CREAM, border=CREAM)


def _action_box(text):
    """The inset 'Try this: ...' strip inside a focus / blind-spot card."""
    table = Table([[Paragraph(f"<b>Try this:</b> {_esc(text)}", S_ACTION)]],
                   colWidths=[CONTENT_WIDTH - 30])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACTION_BG),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return table


def _page_header(title, page_label, issued=None, title_style=S_TITLE, student_name=None):
    story = [
        Paragraph(f"[ {_esc(PROGRAMME_EYEBROW)} ]", S_EYEBROW),
        Spacer(1, 5),
        Paragraph(_esc(title), title_style),
    ]
    # Rendered directly from identity, not left to the AI's intro_message to
    # mention -- the name on the cover no longer depends on the model choosing
    # to include it in generated text.
    if student_name:
        story += [Spacer(1, 3), Paragraph(f"Prepared for {_esc(student_name)}", S_STUDENT_NAME)]
    story.append(Spacer(1, 7))
    right = Paragraph(f"Issued {_esc(issued)}", S_ISSUED) if issued else ""
    row = Table([[Paragraph(page_label, S_PAGENUM), right]],
                 colWidths=[CONTENT_WIDTH * 0.5, CONTENT_WIDTH * 0.5])
    row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
    ]))
    story.append(row)
    story.append(HRFlowable(width="100%", thickness=1.6, color=NAVY,
                             spaceBefore=7, spaceAfter=14))
    return story


def _tier_legend():
    rows = []
    for tier, meaning in TIER_LEGEND:
        rows.append([
            Dot(TIER_COLORS[tier]),
            Paragraph(f"<b>{_esc(tier)}</b> — {_esc(meaning)}", S_BODY),
        ])
    table = Table(rows, colWidths=[12, CONTENT_WIDTH - 12])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (0, -1), "MIDDLE"),
        ("VALIGN", (1, 0), (1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _dimension_block(dim, is_last):
    tier = (dim.get("tier") or "").strip()
    color = TIER_COLORS.get(tier)
    if color is None:
        # Should never reach here -- validation rejects an unknown tier before the
        # PDF stage. Rendering it grey rather than crashing means a manual re-run
        # still produces a readable document.
        log.warning("Unknown tier %r on dimension %r -- rendering neutral",
                     tier, dim.get("name"))
        color = TEXT_MUTED
    block = [
        Paragraph(_esc(dim.get("name")), S_DIM_NAME),
        Spacer(1, 2),
        Paragraph(_esc(dim.get("description")), S_DIM_DESC),
        Spacer(1, 6),
        Pill(tier or "—", color),
    ]
    if not is_last:
        block.append(HRFlowable(width="100%", thickness=0.5, color=BORDER,
                                 dash=(2, 2), spaceBefore=11, spaceAfter=11))
    else:
        block.append(Spacer(1, 10))
    return KeepTogether(block)


def _detail_card(label, headline, body, action, accent):
    inner = [
        Paragraph(_esc(label), S_CARD_LABEL),
        Spacer(1, 3),
        Paragraph(_esc(headline), S_CARD_HEAD),
        Spacer(1, 3),
        Paragraph(_esc(body), S_BODY),
    ]
    if action:
        inner.extend([Spacer(1, 8), _action_box(action)])
    return KeepTogether([_accent_card(inner, accent), Spacer(1, 7)])


def _priority_panel(priority):
    inner = [
        Paragraph("YOUR FOCUS FOR THE BOOTCAMP", S_PRIORITY_LABEL),
        Spacer(1, 5),
        Paragraph(_esc(priority.get("headline")), S_PRIORITY_HEAD),
        Spacer(1, 4),
        Paragraph(_esc(priority.get("body")), S_PRIORITY_BODY),
    ]
    table = Table([[inner]], colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 13),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
    ]))
    return table


def _footer():
    return [
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.5, color=BORDER, spaceAfter=6),
        Paragraph(f"[ {_esc(FOOTER_DISCLAIMER)} ]", S_FOOTER),
    ]


def _as_list(value):
    """The model occasionally returns a single object where the schema asks for an
    array. Accepting that here rather than dropping the content keeps a usable
    report; validation still records the shape as wrong."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def generate_pdf(identity, report_json, output_path):
    """
    Renders the two-page Personal Learning Growth Report.

    Only draws what is present: a report with no blind spots simply has no
    "Worth Noticing" section rather than an empty heading, which is what makes a
    fallback report (profile only, no written cards) still render sensibly.
    """
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        title="Personal Learning Growth Report",
        author=PROGRAMME_EYEBROW,
    )
    story = []

    # ---------------------------------------------------------------- page 1
    story += _page_header("Personal Learning Growth Report", "PAGE 1 OF 2",
                           issued=date.today().strftime("%d %B %Y"),
                           student_name=(identity or {}).get("name"))

    intro = report_json.get("intro_message")
    if intro:
        story.append(_callout([Paragraph(_esc(intro), S_CALLOUT)]))
        story.append(Spacer(1, 18))

    dimensions = _as_list(report_json.get("dimensions"))
    if dimensions:
        story.append(Paragraph("Your Profile", S_H2))
        story.append(Spacer(1, 2))
        story.append(Paragraph(f"{len(dimensions)} dimensions", S_SUBTLE))
        story.append(Spacer(1, 10))
        story.append(_tier_legend())
        story.append(Spacer(1, 16))
        for i, dim in enumerate(dimensions):
            story.append(_dimension_block(dim, is_last=(i == len(dimensions) - 1)))

    story.append(Spacer(1, 4))
    story.append(_callout([
        Paragraph(f"<b>How to read this:</b> {_esc(HOW_TO_READ)}", S_CALLOUT)
    ]))
    story += _footer()

    # ---------------------------------------------------------------- page 2
    strong = _as_list(report_json.get("strong"))
    focus = _as_list(report_json.get("focus"))
    blindspot = _as_list(report_json.get("blindspot"))
    priority = report_json.get("single_priority")
    if isinstance(priority, list) and priority:
        priority = priority[0]
    has_priority = isinstance(priority, dict) and (
        priority.get("headline") or priority.get("body"))

    if strong or focus or blindspot or has_priority:
        story.append(PageBreak())
        story += _page_header("A Closer Look", "PAGE 2 OF 2", title_style=S_H2)

        if strong:
            story.append(Paragraph("Your Strongest Patterns", S_H2))
            story.append(Spacer(1, 10))
            for item in strong:
                story.append(_detail_card("STRENGTH", item.get("headline"),
                                           item.get("body"), None,
                                           TIER_COLORS["Strength"]))
            story.append(Spacer(1, 5))

        if focus:
            story.append(Paragraph("Where Focus Will Pay Off Most", S_H2))
            story.append(Spacer(1, 10))
            for item in focus:
                story.append(_detail_card("FOCUS REQUIRED", item.get("headline"),
                                           item.get("body"), item.get("action"),
                                           TIER_COLORS["Focus Required"]))
            story.append(Spacer(1, 5))

        if blindspot:
            story.append(Paragraph("Worth Noticing", S_H2))
            story.append(Spacer(1, 2))
            story.append(Paragraph("belief vs. behaviour", S_SUBTLE))
            story.append(Spacer(1, 10))
            for item in blindspot:
                story.append(_detail_card("BLIND SPOT", item.get("headline"),
                                           item.get("body"), item.get("action"),
                                           TIER_COLORS["Blind Spot"]))
            story.append(Spacer(1, 5))

        if has_priority:
            story.append(_priority_panel(priority))

        story += _footer()

    doc.build(story)
    return output_path
