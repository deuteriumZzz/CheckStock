"""Защита от N+1: число SQL-запросов не зависит от количества строк."""

import pytest
from sqlalchemy import event, select

from app.models import Product, Warehouse
from app.seed import seed
from app.services.fbs import reserve_fbs, ship_fbs
from app.services.queries import in_transit_totals, stock_rows
from tests.invariants import assert_consistent


@pytest.fixture
def query_counter(engine):
    statements = []

    def count(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    yield statements
    event.remove(engine, "before_cursor_execute", count)


def test_stock_rows_is_one_query(session, query_counter):
    seed(session)
    query_counter.clear()
    rows = stock_rows(session)
    assert len(rows) == 4
    assert len(query_counter) == 1


def test_in_transit_is_one_query_regardless_of_volume(session, query_counter):
    seed(session)
    p = session.scalar(select(Product).where(Product.sku == "SKU-101")).id
    n = session.scalar(select(Warehouse).where(Warehouse.name == "Север")).id
    for _ in range(5):
        ship_fbs(session, reserve_fbs(session, n, p, 1).id)
    query_counter.clear()
    assert in_transit_totals(session)[(p, "FBS")] == 5
    assert len(query_counter) == 1
    assert_consistent(session)


def test_stock_rows_values(session):
    seed(session)
    n = session.scalar(select(Warehouse).where(Warehouse.name == "Север")).id
    p = session.scalar(select(Product).where(Product.sku == "SKU-101")).id
    reserve_fbs(session, n, p, 3)
    row = next(r for r in stock_rows(session) if r.sku == "SKU-101" and r.warehouse_name == "Север")
    assert (row.on_hand, row.reserved, row.available) == (10, 3, 7)
