from __future__ import annotations

from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

GREEN = colors.HexColor("#15543F")
GREEN_DARK = colors.HexColor("#123D31")
MINT = colors.HexColor("#EDF5F1")
INK = colors.HexColor("#17211D")
MUTED = colors.HexColor("#687770")
BORDER = colors.HexColor("#D8E2DD")
AMBER = colors.HexColor("#B87618")


def _font() -> str:
    for path in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ):
        if path.exists():
            if "VentrixSans" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("VentrixSans", str(path)))
            return "VentrixSans"
    return "Helvetica"


def _text(value: Any) -> str:
    return escape(str(value or "—"))


def _number(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return "—"


def _page(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont(doc.ventrix_font, 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 10 * mm, "Ventrix · проверяемая рабочая аналитика")
    canvas.drawRightString(192 * mm, 10 * mm, f"{canvas.getPageNumber()}")
    canvas.restoreState()


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
        "VentrixTitle",
        parent=styles["Title"],
        fontName=font,
        fontSize=24,
        leading=29,
        textColor=GREEN_DARK,
        alignment=TA_LEFT,
        spaceAfter=4 * mm,
    )
    eyebrow = ParagraphStyle(
        "VentrixEyebrow",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=8,
        leading=10,
        textColor=GREEN,
        spaceAfter=2 * mm,
    )
    heading = ParagraphStyle(
        "VentrixHeading",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=14,
        leading=18,
        textColor=INK,
        spaceBefore=7 * mm,
        spaceAfter=3 * mm,
    )
    body = ParagraphStyle(
        "VentrixBody",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=8.5,
        leading=12.5,
        textColor=INK,
    )
    small = ParagraphStyle(
        "VentrixSmall",
        parent=body,
        fontSize=7.2,
        leading=10,
        textColor=MUTED,
    )
    metric_value = ParagraphStyle(
        "VentrixMetric",
        parent=body,
        fontSize=17,
        leading=20,
        textColor=GREEN_DARK,
        alignment=TA_CENTER,
    )
    metric_label = ParagraphStyle(
        "VentrixMetricLabel",
        parent=small,
        alignment=TA_CENTER,
    )
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=17 * mm,
        title=f"Ventrix — {tenant_name}",
        author="Ventrix",
    )
    doc.ventrix_font = font
    story: list[Any] = [
        Paragraph("VENTRIX · РАБОЧАЯ СВОДКА", eyebrow),
        Paragraph(_text(tenant_name), title),
        Paragraph(f"Период: {_text(period_start)} — {_text(period_end)}", body),
        Spacer(1, 5 * mm),
    ]

    metric_cards = [
        ("messages", "Сообщений"),
        ("problems", "Ситуаций"),
        ("high", "Высокий приоритет"),
        ("medium", "Средний приоритет"),
    ]
    cards = [
        [Paragraph(_number(metrics.get(key, 0)), metric_value), Paragraph(label, metric_label)]
        for key, label in metric_cards
    ]
    card_table = Table([cards], colWidths=[42 * mm] * 4)
    card_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), MINT),
                ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.white),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(card_table)

    company = sections.get("company_report", {})
    story.append(Paragraph("Итоги компании", heading))
    company_rows = [
        ["Открытые ситуации", company.get("unresolved_problems", 0)],
        ["Решено за период", company.get("resolved_problems", 0)],
        ["Открытые обещания", company.get("open_commitments", 0)],
        ["Рабочие диалоги", company.get("clients", 0)],
    ]
    company_table = Table(company_rows, colWidths=[130 * mm, 38 * mm])
    company_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("TEXTCOLOR", (0, 0), (-1, -1), INK),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(company_table)

    employee_rows = list(sections.get("employee_report", {}).get("employees") or [])
    if employee_rows:
        story.append(Paragraph("Команда и клиентская работа", heading))
        data: list[list[Any]] = [["Сотрудник", "Диалоги", "Ответ", "Созвоны", "Продажи", "Задачи"]]
        for row in employee_rows:
            response = row.get("average_response_minutes")
            data.append(
                [
                    Paragraph(_text(row.get("name") or "Сотрудник"), body),
                    int(row.get("active_dialogs", 0)),
                    f"{_number(response)} мин." if response is not None else "—",
                    int(row.get("calls_scheduled", 0)),
                    int(row.get("sales_confirmed", 0)),
                    int(row.get("open_promises", 0)) + int(row.get("clients_waiting", 0)),
                ]
            )
        employee_table = Table(
            data,
            colWidths=[55 * mm, 22 * mm, 27 * mm, 22 * mm, 21 * mm, 21 * mm],
            repeatRows=1,
        )
        employee_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("FONTSIZE", (0, 0), (-1, -1), 7.4),
                    ("BACKGROUND", (0, 0), (-1, 0), GREEN),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("ALIGN", (1, 1), (-1, -1), "CENTER"),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, MINT]),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
                    ("PADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(employee_table)
        for row in employee_rows:
            details = [
                f"Исходящих сообщений: <b>{int(row.get('messages_sent', 0))}</b>",
                f"Активных дней: <b>{int(row.get('active_days', 0))}</b>",
            ]
            window = row.get("average_daily_activity_window_minutes")
            if window is not None:
                details.append(
                    f"Среднее окно активности: <b>{_number(window)} мин.</b> (не учёт рабочего времени)"
                )
            change = row.get("response_time_change_percent")
            if change is not None:
                direction = "медленнее" if float(change) > 0 else "быстрее"
                details.append(
                    f"Ответы: <b>{abs(float(change)):g}% {direction}</b> прошлого периода"
                )
            amounts = row.get("confirmed_sales_amounts") or {}
            if amounts:
                details.append(
                    "Подтверждённая сумма: <b>"
                    + ", ".join(
                        f"{_number(value)} {_text(currency)}" for currency, value in amounts.items()
                    )
                    + "</b>"
                )
            outcome_paragraphs = [
                Paragraph(f"• {_text(item.get('summary'))}", small)
                for item in list(row.get("business_outcomes") or [])[:4]
            ]
            story.extend(
                [
                    Spacer(1, 3 * mm),
                    KeepTogether(
                        [
                            Paragraph(_text(row.get("name") or "Сотрудник"), body),
                            Paragraph("<br/>".join(details), small),
                            *outcome_paragraphs,
                        ]
                    ),
                ]
            )

    important = list(sections.get("important_dialogs", {}).get("rows") or [])
    if important:
        story.append(Paragraph("Важные диалоги", heading))
        for item in important[:12]:
            identity = _text(item.get("dialog_username") or item.get("dialog_title") or "Диалог")
            employee = _text(item.get("employee") or "Ответственный не определён")
            problem = Paragraph(
                f"<b>{identity}</b> · {employee}<br/>{_text(item.get('title'))}<br/>"
                f"<font color='#687770'>Следующий шаг: {_text(item.get('recommended_action'))}</font>",
                body,
            )
            problem_table = Table([[problem]], colWidths=[168 * mm])
            problem_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAF9")),
                        ("BOX", (0, 0), (-1, -1), 0.6, BORDER),
                        ("LINEBEFORE", (0, 0), (0, 0), 3, AMBER),
                        ("PADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.extend([problem_table, Spacer(1, 2.5 * mm)])

    recommendations = list(sections.get("recommendations", {}).get("items") or [])
    if recommendations:
        story.append(Paragraph("Рекомендованные действия", heading))
        for item in recommendations[:10]:
            story.append(Paragraph(f"• {_text(item)}", body))

    story.extend(
        [
            Spacer(1, 7 * mm),
            Paragraph(
                "Методика: показатели рассчитаны по разрешённым рабочим Telegram-источникам. "
                "Созвоны, продажи и суммы учитываются только при прямом подтверждении в переписке. "
                "Все важные выводы доступны с исходным контекстом в Mini App.",
                small,
            ),
        ]
    )
    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buffer.getvalue()
