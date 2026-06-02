from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _seed_meta_criteria(conn) -> None:
    from sqlalchemy import text
    count = conn.execute(text("SELECT COUNT(*) FROM meta_criteria")).scalar()
    if count and count > 0:
        return
    doc_types = [
        ("invoice", "Invoice", "border-blue-300 text-blue-700 bg-blue-50"),
        ("proposal", "Proposal", "border-sky-300 text-sky-700 bg-sky-50"),
        ("client_contract", "Client contract", "border-indigo-300 text-indigo-700 bg-indigo-50"),
        ("supplier_contract", "Supplier contract", "border-violet-300 text-violet-700 bg-violet-50"),
        ("nda", "NDA", "border-purple-300 text-purple-700 bg-purple-50"),
        ("employment_contract", "Employment contract", "border-fuchsia-300 text-fuchsia-700 bg-fuchsia-50"),
        ("freelance_contract", "Freelance contract", "border-pink-300 text-pink-700 bg-pink-50"),
        ("loan_contract", "Loan contract", "border-rose-300 text-rose-700 bg-rose-50"),
        ("power_of_attorney", "Power of attorney", "border-orange-300 text-orange-700 bg-orange-50"),
        ("order", "Order", "border-amber-300 text-amber-700 bg-amber-50"),
        ("request", "Request", "border-yellow-300 text-yellow-700 bg-yellow-50"),
        ("policy", "Policy", "border-lime-300 text-lime-700 bg-lime-50"),
        ("instruction", "Instruction", "border-green-300 text-green-700 bg-green-50"),
        ("announcement", "Announcement", "border-teal-300 text-teal-700 bg-teal-50"),
        ("training", "Training", "border-cyan-300 text-cyan-700 bg-cyan-50"),
        ("marketing", "Marketing", "border-blue-300 text-blue-700 bg-blue-50"),
        ("results", "Results", "border-indigo-300 text-indigo-700 bg-indigo-50"),
        ("template", "Template", "border-gray-300 text-gray-600 bg-gray-50"),
        ("gdpr", "GDPR", "border-red-300 text-red-700 bg-red-50"),
        ("safety", "Safety", "border-orange-300 text-orange-700 bg-orange-50"),
        ("health_insurance", "Health insurance", "border-green-300 text-green-700 bg-green-50"),
        ("health_check", "Health check", "border-teal-300 text-teal-700 bg-teal-50"),
        ("works_council", "Works council", "border-cyan-300 text-cyan-700 bg-cyan-50"),
        ("it_assets", "IT assets", "border-sky-300 text-sky-700 bg-sky-50"),
        ("vehicle", "Vehicle", "border-violet-300 text-violet-700 bg-violet-50"),
        ("office", "Office", "border-slate-300 text-slate-700 bg-slate-50"),
        ("consulting", "Consulting", "border-blue-300 text-blue-700 bg-blue-50"),
        ("assessment", "Assessment", "border-indigo-300 text-indigo-700 bg-indigo-50"),
        ("survey", "Survey", "border-purple-300 text-purple-700 bg-purple-50"),
        ("client_growth", "Client growth", "border-green-300 text-green-700 bg-green-50"),
        ("debt", "Debt", "border-red-300 text-red-700 bg-red-50"),
        ("other", "Other", "border-gray-300 text-gray-600 bg-gray-50"),
    ]
    audiences = [
        ("Managers",    "Managers",    "border-blue-300 text-blue-700 bg-blue-50"),
        ("Everybody",   "Everybody",   "border-green-300 text-green-700 bg-green-50"),
        ("Admin",       "Admin",       "border-amber-300 text-amber-700 bg-amber-50"),
        ("Freelancers", "Freelancers", "border-purple-300 text-purple-700 bg-purple-50"),
    ]
    import uuid as _uuid_mod
    for i, (value, label, color) in enumerate(doc_types):
        conn.execute(text(
            "INSERT INTO meta_criteria (id, criteria_type, value, label, color_class, sort_order, is_builtin) "
            "VALUES (:id, 'doc_type', :value, :label, :color, :sort, true)"
        ), {"id": str(_uuid_mod.uuid4()), "value": value, "label": label, "color": color, "sort": i})
    for i, (value, label, color) in enumerate(audiences):
        conn.execute(text(
            "INSERT INTO meta_criteria (id, criteria_type, value, label, color_class, sort_order, is_builtin) "
            "VALUES (:id, 'audience', :value, :label, :color, :sort, true)"
        ), {"id": str(_uuid_mod.uuid4()), "value": value, "label": label, "color": color, "sort": i})
    conn.commit()


def init_db():
    from app import models  # noqa: F401 – ensure models are registered
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        _seed_meta_criteria(conn)
    with engine.connect() as conn:
        # Safe column migrations
        for col, definition in [
            ("is_2fa_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("twofa_method", "VARCHAR(10)"),
            ("totp_secret", "VARCHAR(64)"),
            ("email_otp", "VARCHAR(6)"),
            ("email_otp_expires_at", "TIMESTAMP"),
        ]:
            try:
                conn.execute(text(f'ALTER TABLE users ADD COLUMN {col} {definition}'))
                conn.commit()
            except Exception:
                conn.rollback()
        for col, definition in [
            ("ai_generated", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("ai_model", "VARCHAR(80)"),
            ("is_archive", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("no_index", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ]:
            try:
                conn.execute(text(f'ALTER TABLE doc_meta ADD COLUMN {col} {definition}'))
                conn.commit()
            except Exception:
                conn.rollback()
        try:
            conn.execute(text("ALTER TABLE knowledge_chunks ADD COLUMN modified TIMESTAMP"))
            conn.commit()
        except Exception:
            conn.rollback()
        # HNSW index for fast ANN search on embeddings
        try:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
        # Trigram GIN index for fast substring search on the file catalog
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.commit()
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS file_catalog_name_norm_trgm "
                "ON file_catalog USING gin (name_norm gin_trgm_ops)"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
        # AI enrichment columns — file_catalog
        for col, definition in [
            ("doc_type",     "VARCHAR(40)"),
            ("company",      "VARCHAR(255)"),
            ("company_norm", "VARCHAR(255)"),
            ("doc_number",   "VARCHAR(50)"),
            ("doc_year",     "INTEGER"),
            ("doc_month",    "INTEGER"),
            ("enriched_at",  "TIMESTAMP"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE file_catalog ADD COLUMN {col} {definition}"))
                conn.commit()
            except Exception:
                conn.rollback()
        # AI enrichment columns — knowledge_chunks
        for col, definition in [
            ("ai_summary",    "TEXT"),
            ("ai_topics",     "TEXT"),
            ("ai_applies_to", "VARCHAR(50)"),
            ("is_archive",    "BOOLEAN NOT NULL DEFAULT FALSE"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE knowledge_chunks ADD COLUMN {col} {definition}"))
                conn.commit()
            except Exception:
                conn.rollback()
        # Index on company_norm for fast company search
        try:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS file_catalog_company_norm_trgm "
                "ON file_catalog USING gin (company_norm gin_trgm_ops)"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
        # Widen file_catalog.size to BIGINT (files >2 GB overflow INTEGER)
        try:
            conn.execute(text("ALTER TABLE file_catalog ALTER COLUMN size TYPE BIGINT"))
            conn.commit()
        except Exception:
            conn.rollback()
        # Widen file_catalog.ext to VARCHAR(50) (filenames with dots but no real extension)
        try:
            conn.execute(text("ALTER TABLE file_catalog ALTER COLUMN ext TYPE VARCHAR(50)"))
            conn.commit()
        except Exception:
            conn.rollback()
