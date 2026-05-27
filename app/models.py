import uuid
from datetime import datetime, date
from sqlalchemy import (
    String, Boolean, Integer, BigInteger, DateTime, Date, ForeignKey, UniqueConstraint, func, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
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
    is_2fa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    twofa_method: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "totp" or "email"
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email_otp: Mapped[str | None] = mapped_column(String(6), nullable=True)
    email_otp_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)   # "invenias", "sharepoint"
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)    # Invenias ItemId or SP file ID
    source_name: Mapped[str] = mapped_column(String(500), nullable=False)  # human-readable label
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(1536), nullable=True)
    modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # AI-generated enrichment
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_topics: Mapped[str | None] = mapped_column(Text, nullable=True)   # JSON array
    ai_applies_to: Mapped[str | None] = mapped_column(String(50), nullable=True)


class DocMeta(Base):
    __tablename__ = "doc_meta"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    item_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    drive: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience: Mapped[str | None] = mapped_column(String(300), nullable=True)  # JSON array
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class FileCatalog(Base):
    """Lightweight index of every SharePoint file (listing only — no content).

    Powers keyword/company file search in Ask PL (e.g. "find all invoices for X").
    Covers every file type, including .xls/.key that the content indexer skips.
    """
    __tablename__ = "file_catalog"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    item_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)  # SharePoint file id
    drive: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    folder_path: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    name_norm: Mapped[str] = mapped_column(Text, nullable=False, default="")  # diacritic-stripped "drive/folder/name" for search
    ext: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    web_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    # AI-enriched metadata
    doc_type: Mapped[str | None] = mapped_column(String(40), nullable=True)   # invoice, proposal, contract, policy, …
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)   # display name from folder
    company_norm: Mapped[str | None] = mapped_column(String(255), nullable=True)  # normalized for search
    doc_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    doc_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AskRule(Base):
    __tablename__ = "ask_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class DriveSync(Base):
    """Stores Microsoft Graph delta links for incremental SharePoint crawling."""
    __tablename__ = "drive_sync"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    drive_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    catalog_delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_catalog_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_content_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
