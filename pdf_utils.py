"""
pdf_utils.py
Generates a clean, shareable PDF of the finalized itinerary using reportlab.
"""

from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
)


def build_itinerary_pdf(meta, itinerary, packing_list):
    """
    meta: dict with city, days, budget, trip_type, allocation
    itinerary: dict {day_number: {"stops": [...], "total_cost": ..., "budget": ...}}
    packing_list: list of strings
    Returns: BytesIO buffer containing the PDF.
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Title"], fontSize=22, spaceAfter=4)
    subtitle_style = ParagraphStyle("Subtitle", parent=styles["Normal"], fontSize=11,
                                     textColor=colors.grey, spaceAfter=16)
    day_style = ParagraphStyle("DayStyle", parent=styles["Heading2"], fontSize=15,
                                spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1a1a2e"))
    cost_style = ParagraphStyle("CostStyle", parent=styles["Normal"], fontSize=9.5,
                                 textColor=colors.grey, spaceAfter=8)
    section_style = ParagraphStyle("Section", parent=styles["Heading2"], fontSize=14,
                                    spaceBefore=18, spaceAfter=8)

    story = []
    story.append(Paragraph(f"{meta['city']} Travel Itinerary", title_style))
    story.append(Paragraph(
        f"{meta['days']} days &bull; Total budget: Rs {meta['budget']:.0f} &bull; "
        f"Trip type: {meta['trip_type']}", subtitle_style
    ))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#dddddd")))

    # Budget breakdown table
    alloc = meta["allocation"]
    story.append(Paragraph("Budget Breakdown", section_style))
    budget_rows = [["Category", "Amount (Rs)"]]
    for k, v in alloc.items():
        budget_rows.append([k.capitalize(), f"{v:.0f}"])
    budget_table = Table(budget_rows, colWidths=[3 * inch, 2 * inch])
    budget_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5fa")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(budget_table)

    # Day-by-day plan
    story.append(Paragraph("Day-by-Day Plan", section_style))
    for d, plan in itinerary.items():
        status = "within budget" if plan["total_cost"] <= plan["budget"] else "over budget"
        story.append(Paragraph(f"Day {d}", day_style))
        story.append(Paragraph(
            f"Cost: Rs {plan['total_cost']:.0f} / Rs {plan['budget']:.0f} budget ({status})",
            cost_style
        ))
        rows = [["#", "Stop", "Type", "Time", "Cost (Rs)"]]
        for i, stop in enumerate(plan["stops"], 1):
            rows.append([str(i), stop["name"], stop["type"], stop["time"], f"{stop['cost']:.0f}"])
        t = Table(rows, colWidths=[0.3 * inch, 2.6 * inch, 1.1 * inch, 1.1 * inch, 0.9 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8f5")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)

    # Packing list
    story.append(Paragraph("Packing Checklist", section_style))
    for item in packing_list:
        story.append(Paragraph(f"&#8226; {item}", styles["Normal"]))

    doc.build(story)
    buffer.seek(0)
    return buffer
