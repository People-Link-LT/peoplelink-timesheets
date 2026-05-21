import uuid
from datetime import datetime, date
from sqlalchemy import (
    String, Boolean, Integer, DateTime, Date, ForeignKey, UniqueConstraint, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)

    users: Mapped[list["User"]] = relationship("User", back_populates="team")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("teams.id"), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    team: Mapped["Team | None"] = relationship("Team", back_populates="users")
    portfolio: Mapped[list["UserPortfolio"]] = relationship("UserPortfolio", back_populates="user", cascade="all, delete-orphan")
    timesheet_entries: Mapped[list["TimesheetEntry"]] = relationship("TimesheetEntry", back_populates="user", cascade="all, delete-orphan")


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # Invenias ItemId
    reference_number: Mapped[str] = mapped_column(String(50), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="Active")
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    portfolio_entries: Mapped[list["UserPortfolio"]] = relationship("UserPortfolio", back_populates="assignment")
    timesheet_entries: Mapped[list["TimesheetEntry"]] = relationship("TimesheetEntry", back_populates="assignment")

    @property
    def display_name(self) -> str:
        return f"{self.reference_number} — {self.company_name or ''}"


class UserPortfolio(Base):
    __tablename__ = "user_portfolio"
    __table_args__ = (UniqueConstraint("user_id", "assignment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("assignments.id"), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    user: Mapped["User"] = relationship("User", back_populates="portfolio")
    assignment: Mapped["Assignment"] = relationship("Assignment", back_populates="portfolio_entries")


class Week(Base):
    __tablename__ = "weeks"
    __table_args__ = (UniqueConstraint("start_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)  # Monday
    end_date: Mapped[date] = mapped_column(Date, nullable=False)    # Friday

    entries: Mapped[list["TimesheetEntry"]] = relationship("TimesheetEntry", back_populates="week")


TASK_CHOICES = ["Sourcing", "Assessment", "Delivery Management", "Sales"]


class TimesheetEntry(Base):
    __tablename__ = "timesheet_entries"
    __table_args__ = (UniqueConstraint("user_id", "week_id", "assignment_id", "task"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    week_id: Mapped[str] = mapped_column(String(36), ForeignKey("weeks.id"), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(36), ForeignKey("assignments.id"), nullable=False)
    task: Mapped[str] = mapped_column(String(50), nullable=False)
    monday_minutes: Mapped[int] = mapped_column(Integer, default=0)
    tuesday_minutes: Mapped[int] = mapped_column(Integer, default=0)
    wednesday_minutes: Mapped[int] = mapped_column(Integer, default=0)
    thursday_minutes: Mapped[int] = mapped_column(Integer, default=0)
    friday_minutes: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship("User", back_populates="timesheet_entries")
    week: Mapped["Week"] = relationship("Week", back_populates="entries")
    assignment: Mapped["Assignment"] = relationship("Assignment", back_populates="timesheet_entries")

    @property
    def total_minutes(self) -> int:
        return (self.monday_minutes + self.tuesday_minutes + self.wednesday_minutes
                + self.thursday_minutes + self.friday_minutes)
