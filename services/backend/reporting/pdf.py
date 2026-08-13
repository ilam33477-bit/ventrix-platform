from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _font() -> str:
    for path in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ):
        if path.exists():
            pdfmetrics.registerFont(TTFont("VentrixSans", str(path)))
            return "VentrixSans"
    return "Helvetica"


def build_report_pdf(
    *,
    tenant_name: str,
    period_start: str,
    period_end: str,
    metrics: dict[str, float],
    sections: dict[str, dict[str, Any]],
) -> bytes:
    buffer = BytesIO()
    font = _font()
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "TitleRu",
        parent=styles["Title"],
        fontName=font,
        textColor=colors.HexColor("#15543f"),
        alignment=TA_CENTER,
    )
    heading = ParagraphStyle(
        "HeadingRu",
        parent=styles["Heading2"],
        fontName=font,
        textColor=colors.HexColor("#173b31"),
        spaceBefore=12,
        spaceAfter=7,
    )
    body = ParagraphStyle(
        "BodyRu", parent=styles["BodyText"], fontName=font, fontSize=9, leading=13
    )
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Ventrix — {tenant_name}",
    )
    story: list[Any] = [
        Paragraph("VENTRIX", title),
        Paragraph(f"Рабочая сводка · {tenant_name}", heading),
        Paragraph(f"Период: {period_start} — {period_end}", body),
        Spacer(1, 6 * mm),
    ]
    labels = {
        "messages": "Сообщений изучено",
        "problems": "Ситуаций найдено",
        "high": "Высокий приоритет",
        "medium": "Средний приоритет",
        "low": "Низкий приоритет",
    }
    table = Table(
        [[labels[key], int(metrics.get(key, 0))] for key in labels], colWidths=[125 * mm, 25 * mm]
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf5f1")),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd9d2")),
                ("PADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.extend([table, Paragraph("Итоги по компании", heading)])
    company = sections.get("company_report", {})
    company_rows = [
        ("Открытые ситуации", company.get("unresolved_problems", 0)),
        ("Решено", company.get("resolved_problems", 0)),
        ("Открытые обещания", company.get("open_commitments", 0)),
        ("Рабочие диалоги", company.get("clients", 0)),
    ]
    company_table = Table(company_rows, colWidths=[125 * mm, 25 * mm])
    company_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9e2dd")),
                ("PADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(company_table)
    employee_rows = sections.get("employee_report", {}).get("rows", [])
    if employee_rows:
        story.append(Paragraph("Команда", heading))
        data = [["Сотрудник", "Ждут", "Обещания", "Решено"]] + [
            [
                str(row.get("name", "Сотрудник")),
                row.get("clients_waiting", 0),
                row.get("open_promises", 0),
                row.get("resolved", 0),
            ]
            for row in employee_rows
        ]
        employee_table = Table(data, colWidths=[90 * mm, 20 * mm, 25 * mm, 20 * mm], repeatRows=1)
        employee_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15543f")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d9e2dd")),
                    ("PADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(employee_table)
    story.extend(
        [
            Spacer(1, 8 * mm),
            Paragraph(
                "Сводка сформирована Ventrix на основе разрешённых рабочих Telegram-источников. Каждый вывод можно проверить по исходной переписке в Mini App.",
                body,
            ),
        ]
    )
    doc.build(story)
    return buffer.getvalue()
