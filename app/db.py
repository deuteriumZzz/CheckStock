from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def atomic(session: Session):
    """Одна складская команда = одна транзакция: commit при успехе, rollback при любой ошибке."""
    try:
        yield
        session.commit()
    except BaseException:
        session.rollback()
        raise
