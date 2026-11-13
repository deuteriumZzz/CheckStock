"""Корректировка остатка: меняет остаток только через операцию с причиной и записью в историю."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.routes import get_session
from app.errors import Conflict, NotFound, ValidationError
from app.main import app
from app.models import Operation
from app.services.adjustments import adjust_stock
from app.services.fbs import reserve_fbs
from app.services.queries import history, stock_rows
from app.services.warehouses import archive_warehouse
from tests.helpers import balance, counts
from tests.invariants import assert_consistent


def test_decrease_after_recount(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = adjust_stock(session, n, p, 7, "пересчёт: брак 3 шт")
    assert (op.type, op.status, op.quantity, op.note) == (
        "ADJUSTMENT",
        "RECEIVED",
        3,
        "пересчёт: брак 3 шт",
    )
    assert (op.source_warehouse_id, op.dest_warehouse_id) == (n, None)
    assert balance(session, p, n) == (7, 0)
    assert_consistent(session)


def test_increase_fixes_missing_goods(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = adjust_stock(session, n, p, 15, "нашли коробку")
    assert (op.quantity, op.source_warehouse_id, op.dest_warehouse_id) == (5, None, n)
    assert balance(session, p, n) == (15, 0)
    assert_consistent(session)


def test_goods_that_never_existed_can_be_created_by_recount(session, ids):
    p, y = ids.p["SKU-102"], ids.w["Юг"]  # по ТЗ на Юге SKU-102 нет
    adjust_stock(session, y, p, 2, "нашли при пересчёте")
    assert balance(session, p, y) == (2, 0)
    assert_consistent(session)


def test_history_shows_correction_with_reason(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    adjust_stock(session, n, p, 8, "ошибочный приход")
    top = history(session)[0]
    assert (top.type, top.quantity, top.from_warehouse, top.to_warehouse) == (
        "ADJUST_OUT",
        2,
        "Север",
        None,
    )
    assert top.note == "ошибочный приход"


def test_unchanged_value_is_rejected(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    before = counts(session)
    with pytest.raises(ValidationError, match="менять нечего"):
        adjust_stock(session, n, p, 10, "ничего")
    assert counts(session) == before


def test_cannot_go_below_reserved(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    reserve_fbs(session, n, p, 6)
    before = counts(session)
    with pytest.raises(Conflict, match="зарезервировано 6"):
        adjust_stock(session, n, p, 5, "пересчёт")
    assert balance(session, p, n) == (10, 6)
    assert counts(session) == before
    adjust_stock(session, n, p, 6, "ровно по резерву")  # до резерва можно
    assert balance(session, p, n) == (6, 6)
    assert_consistent(session)


@pytest.mark.parametrize("bad", [-1, True, 2.5, "7"])
def test_invalid_quantity(session, ids, bad):
    with pytest.raises(ValidationError):
        adjust_stock(session, ids.w["Север"], ids.p["SKU-101"], bad, "причина")


@pytest.mark.parametrize("reason", ["", "   ", None, "x" * 256])
def test_reason_is_required(session, ids, reason):
    with pytest.raises(ValidationError):
        adjust_stock(session, ids.w["Север"], ids.p["SKU-101"], 5, reason)


def test_unknown_entities_and_archived_warehouse(session, ids):
    with pytest.raises(NotFound):
        adjust_stock(session, ids.w["Север"], 9999, 1, "причина")
    with pytest.raises(NotFound):
        adjust_stock(session, 9999, ids.p["SKU-101"], 1, "причина")
    archive_warehouse(session, ids.w["Юг"])
    with pytest.raises(Conflict, match="в архиве"):
        adjust_stock(session, ids.w["Юг"], ids.p["SKU-101"], 1, "причина")


def test_totals_stay_consistent_with_stock_screen(session, ids):
    adjust_stock(session, ids.w["Север"], ids.p["SKU-101"], 4, "пересчёт")
    rows = {(r.sku, r.warehouse_name): r.on_hand for r in stock_rows(session)}
    assert rows[("SKU-101", "Север")] == 4
    note = session.scalar(select(Operation.note).where(Operation.type == "ADJUSTMENT"))
    assert note == "пересчёт"


def test_adjustment_over_http(engine, ids, session):
    factory = sessionmaker(engine, expire_on_commit=False)

    def override():
        with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            body = {
                "warehouse_id": ids.w["Север"],
                "product_id": ids.p["SKU-101"],
                "new_on_hand": 7,
                "reason": "брак",
            }
            ok = client.post("/api/adjustments", json=body)
            assert ok.status_code == 201
            data = ok.json()
            assert (data["type"], data["quantity"], data["note"]) == ("ADJUSTMENT", 3, "брак")

            blank = client.post("/api/adjustments", json={**body, "reason": " ", "new_on_hand": 5})
            assert (blank.status_code, blank.json()["error"]["code"]) == (422, "validation_error")
            strict = client.post("/api/adjustments", json={**body, "new_on_hand": "5"})
            assert strict.status_code == 422

            top = client.get("/api/history").json()[0]
            assert top["type"] == "ADJUST_OUT" and top["note"] == "брак"
    finally:
        app.dependency_overrides.clear()
    assert_consistent(session)
