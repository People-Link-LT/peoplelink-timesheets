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
        # HNSW index for fast ANN search on embeddings
        try:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_hnsw "
                "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
            ))
            conn.commit()
        except Exception:
            conn.rollback()
