import os
from types import SimpleNamespace

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.models import Product, Warehouse
from app.seed import seed

TEST_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://checkstock:checkstock@localhost:5433/checkstock_test",
)


@pytest.fixture(scope="session")
def engine():
    url = make_url(TEST_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        exists = c.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
        ).scalar()
        if not exists:
            c.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_URL.replace("%", "%%"))
    command.upgrade(cfg, "head")  # тесты гоняют настоящие миграции
    eng = create_engine(TEST_URL)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_db(engine):
    """Каждый тест начинает с пустой БД: независимость от порядка выполнения."""
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE events, operations, inventory, warehouses, products RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def session(engine):
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def ids(session):
    """Данные из ТЗ + словари id: ids.p['SKU-101'], ids.w['Север']."""
    seed(session)
    return SimpleNamespace(
        p={x.sku: x.id for x in session.scalars(select(Product))},
        w={x.name: x.id for x in session.scalars(select(Warehouse))},
    )
