from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ReportSection


def own_report_rows(data: dict[str, Any], employee_id: str | None) -> list[dict[str, Any]]:
    if not employee_id:
        return []
    return [
        row
        for row in (data.get("employees") or data.get("rows") or [])
        if isinstance(row, dict) and row.get("employee_id") == employee_id
    ]


async def personal_report_ids(
    session: AsyncSession, tenant_id: str, employee_id: str | None, report_ids: list[str]
) -> set[str]:
    if not employee_id or not report_ids:
        return set()
    sections = await session.scalars(
        select(ReportSection).where(
            ReportSection.tenant_id == tenant_id,
            ReportSection.report_id.in_(report_ids),
            ReportSection.section_key == "employee_report",
        )
    )
    return {item.report_id for item in sections if own_report_rows(item.data_json, employee_id)}
