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
    Base.metadata.create_all(bind=engine)
    # Safe migrations for columns added after initial deploy
    from sqlalchemy import text
    with engine.connect() as conn:
        for col, definition in [
            ("is_2fa_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("totp_secret", "VARCHAR(64)"),
        ]:
            try:
                conn.execute(text(f'ALTER TABLE users ADD COLUMN {col} {definition}'))
                conn.commit()
            except Exception:
                conn.rollback()
