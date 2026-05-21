from app.templates import templates
from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user
from app.models import User, Assignment, UserPortfolio

router = APIRouter(prefix="/portfolio")


@router.get("", response_class=HTMLResponse)
def portfolio_page(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    portfolio_ids = {p.assignment_id for p in user.portfolio}
    all_assignments = (
        db.query(Assignment)
        .filter(Assignment.status == "Active")
        .order_by(Assignment.reference_number)
        .all()
    )
    return templates.TemplateResponse("portfolio.html", {
        "request": request,
        "user": user,
        "all_assignments": all_assignments,
        "portfolio_ids": portfolio_ids,
    })


@router.post("/add")
def add_to_portfolio(
    request: Request,
    assignment_id: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    existing = db.query(UserPortfolio).filter_by(user_id=user.id, assignment_id=assignment_id).first()
    if not existing:
        db.add(UserPortfolio(user_id=user.id, assignment_id=assignment_id))
        db.commit()
    return RedirectResponse("/portfolio", status_code=302)


@router.post("/remove")
def remove_from_portfolio(
    request: Request,
    assignment_id: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    db.query(UserPortfolio).filter_by(user_id=user.id, assignment_id=assignment_id).delete()
    db.commit()
    return RedirectResponse("/portfolio", status_code=302)

