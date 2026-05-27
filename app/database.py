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


def init_db():
    from app import models  # noqa: F401 – ensure models are registered
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
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
