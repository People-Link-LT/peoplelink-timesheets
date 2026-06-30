from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import get_current_user
from app.models import User
from app.templates import templates

router = APIRouter(prefix="/automation")


@router.get("/vacation-orders", response_class=HTMLResponse)
def vacation_orders(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "automation/vacation_orders.html", {"user": user})


@router.get("/buyer-excel", response_class=HTMLResponse)
def buyer_excel(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "automation/buyer_excel.html", {"user": user})
