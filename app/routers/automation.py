import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.auth import get_current_user
from app.models import User
from app.templates import templates
import app.vacation_orders as vo
import app.buyer_excel as be

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/automation")

MONTHS_LT = [
    (1, "Sausis"), (2, "Vasaris"), (3, "Kovas"), (4, "Balandis"),
    (5, "Gegužė"), (6, "Birželis"), (7, "Liepa"), (8, "Rugpjūtis"),
    (9, "Rugsėjis"), (10, "Spalis"), (11, "Lapkritis"), (12, "Gruodis"),
]


@router.get("/vacation-orders", response_class=HTMLResponse)
def vacation_orders_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "automation/vacation_orders.html", {
        "user": user,
        "months": MONTHS_LT,
    })


@router.post("/vacation-orders/preview", response_class=HTMLResponse)
async def vacation_orders_preview(
    request: Request,
    month: int = Form(...),
    user: User = Depends(get_current_user),
):
    try:
        excel = await vo.download_excel()
        entries_raw = vo.parse_excel(excel, month)
        entries = vo.process_entries(entries_raw)
        order_num = await vo.get_next_order_number()
        order_date = vo.first_working_day(2026, month)

        for e in entries:
            e["text"] = vo.build_entry_text(e, month)

        return templates.TemplateResponse(request, "automation/vacation_orders_preview.html", {
            "user": user,
            "entries": entries,
            "month": month,
            "month_name": vo.MONTH_GEN[month],
            "order_num": order_num,
            "order_date": order_date,
        })
    except Exception as e:
        logger.error(f"Vacation orders preview error: {e}", exc_info=True)
        return HTMLResponse(
            f'<div class="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">Klaida: {e}</div>'
        )


@router.get("/vacation-orders/download")
async def vacation_orders_download(
    month: int,
    user: User = Depends(get_current_user),
):
    excel = await vo.download_excel()
    entries_raw = vo.parse_excel(excel, month)
    entries = vo.process_entries(entries_raw)
    order_num = await vo.get_next_order_number()
    order_date = vo.first_working_day(2026, month)
    docx_bytes = vo.generate_docx(entries, month, order_num, order_date)
    filename = f"ISAK. P-{order_num} PPL atostogos.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/buyer-excel", response_class=HTMLResponse)
def buyer_excel(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "automation/buyer_excel.html", {
        "user": user,
        "months": MONTHS_LT,
    })


@router.post("/buyer-excel/preview", response_class=HTMLResponse)
async def buyer_excel_preview(
    request: Request,
    month: int = Form(...),
    user: User = Depends(get_current_user),
):
    try:
        month_rows = await be.get_month_data(month)
        counts = be.summarize(month_rows)

        return templates.TemplateResponse(request, "automation/buyer_excel_preview.html", {
            "user": user,
            "month": month,
            "month_name": be.MONTH_TITLE[month],
            "counts": counts,
            "file_type_labels": be.FILE_TYPE_LABELS,
        })
    except Exception as e:
        logger.error(f"Buyer excel preview error: {e}", exc_info=True)
        return HTMLResponse(
            f'<div class="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">Klaida: {e}</div>'
        )


@router.get("/buyer-excel/download")
async def buyer_excel_download(
    month: int,
    file_type: str,
    user: User = Depends(get_current_user),
):
    xlsx_bytes, filename = await be.generate(month, file_type)
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "pardavimai.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )
