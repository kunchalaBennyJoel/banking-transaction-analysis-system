"""Bounded transaction report rendering for CSV, PDF, and Word formats."""

import csv
from datetime import datetime, timezone
from io import BytesIO, StringIO
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle


HEADERS = (
    "Transaction reference",
    "Occurred at (UTC)",
    "Type",
    "Amount",
    "Currency",
    "Status",
    "Channel",
    "Source account",
    "Destination account",
)


def _csv_safe(value: str) -> str:
    """Prefix spreadsheet formula-triggering text with a literal apostrophe."""
    text = str(value)
    probe = text.lstrip(" \t\r\n")
    if probe.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def _table_rows(rows) -> list[tuple[str, ...]]:
    def timestamp(row) -> str:
        value = row.occurred_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="seconds").replace("+00:00", "Z")

    return [
        (
            row.reference,
            timestamp(row),
            row.transaction_type,
            format(row.amount, ".2f"),
            row.currency,
            row.status,
            row.channel or "Not recorded",
            row.source_account.synthetic_reference if row.source_account else "",
            row.destination_account.synthetic_reference if row.destination_account else "",
        )
        for row in rows
    ]


def render_csv(rows) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    for row in _table_rows(rows):
        writer.writerow([_csv_safe(value) for value in row])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _report_metadata(row_count: int, filter_summary: str) -> tuple[str, str]:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    summary = f"Transactions: {row_count:,} · Filters: {filter_summary or 'None'}"
    return generated, summary


def render_pdf(rows, filter_summary: str) -> bytes:
    output = BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(
        output,
        pagesize=page_size,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        pageCompression=0,
        title="Transaction Export",
        author="Banking Transaction Analysis System",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ExportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
        fontSize=17, leading=21, alignment=TA_LEFT, textColor=colors.HexColor("#244459"),
        spaceAfter=5,
    )
    meta_style = ParagraphStyle(
        "ExportMeta", parent=styles["Normal"], fontName="Helvetica",
        fontSize=8, leading=11, textColor=colors.HexColor("#53616a"), spaceAfter=3,
    )
    cell_style = ParagraphStyle("ExportCell", parent=styles["Normal"], fontName="Helvetica", fontSize=6.5, leading=8)
    generated, summary = _report_metadata(len(rows), filter_summary)
    story = [
        Paragraph("Banking Transaction Analysis System", title_style),
        Paragraph("Transaction Export", styles["Heading2"]),
        Paragraph(f"Generated: {escape(generated)}", meta_style),
        Paragraph(escape(summary), meta_style),
        Spacer(1, 5 * mm),
    ]
    data = [[Paragraph(escape(header), cell_style) for header in HEADERS]]
    for row in _table_rows(rows):
        data.append([Paragraph(escape(value) or "&nbsp;", cell_style) for value in row])
    widths = [28, 28, 17, 18, 13, 17, 16, 29, 32]
    available_width = page_size[0] - doc.leftMargin - doc.rightMargin
    widths = [available_width * width / sum(widths) for width in widths]
    table = LongTable(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#244459")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d5dcd9")),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5f3")]),
    ]))
    story.append(table)
    doc.build(story)
    return output.getvalue()


def _set_cell_shading(cell, fill: str) -> None:
    shading = OxmlElement("w:shd")
    shading.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shading)


def _mark_header_row_repeat(row) -> None:
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(header)


def render_docx(rows, filter_summary: str) -> bytes:
    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Inches(11.7)
    section.page_height = Inches(8.3)
    section.left_margin = Inches(0.35)
    section.right_margin = Inches(0.35)
    section.top_margin = Inches(0.45)
    section.bottom_margin = Inches(0.45)

    title = document.add_heading("Banking Transaction Analysis System", level=0)
    title.runs[0].font.color.rgb = RGBColor(36, 68, 89)
    document.add_heading("Transaction Export", level=1)
    generated, summary = _report_metadata(len(rows), filter_summary)
    document.add_paragraph(f"Generated: {generated}")
    document.add_paragraph(summary)

    table = document.add_table(rows=1, cols=len(HEADERS))
    table.style = "Table Grid"
    table.autofit = False
    _mark_header_row_repeat(table.rows[0])
    widths = [1.4, 1.6, 0.9, 0.8, 0.6, 0.8, 0.8, 2.0, 2.0]
    for index, header in enumerate(HEADERS):
        cell = table.rows[0].cells[index]
        cell.text = header
        cell.width = Inches(widths[index])
        _set_cell_shading(cell, "244459")
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.font.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)
                run.font.size = Pt(7)
    for row_index, record in enumerate(_table_rows(rows)):
        cells = table.add_row().cells
        for index, value in enumerate(record):
            cells[index].text = value
            cells[index].width = Inches(widths[index])
            cells[index].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if row_index % 2:
                _set_cell_shading(cells[index], "F2F5F3")
            for paragraph in cells[index].paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(7)
    output = BytesIO()
    document.save(output)
    return output.getvalue()
