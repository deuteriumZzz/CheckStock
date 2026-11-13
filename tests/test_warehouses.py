import pytest
from sqlalchemy import event, func, select

from app.errors import Conflict, NotFound, ValidationError
from app.models import Event, Warehouse
from app.services.fbs import cancel_reservation, reserve_fbs
from app.services.queries import history, list_warehouses, stock_rows
from app.services.shipments import receive_transfer, ship_fbo, ship_transfer
from app.services.stock import receive
from app.services.warehouses import archive_warehouse, create_warehouse, rename_warehouse
from tests.helpers import balance, counts
from tests.invariants import assert_consistent


def test_create_and_use_new_warehouse(session, ids):
    wh = create_warehouse(session, "  Запад ")
    assert wh.name == "Запад"
    receive(session, wh.id, ids.p["SKU-101"], 2)
    assert balance(session, ids.p["SKU-101"], wh.id) == (2, 0)
    assert_consistent(session)


@pytest.mark.parametrize("bad", ["", "   ", None, "x" * 256])
def test_create_validation(session, bad):
    with pytest.raises(ValidationError):
        create_warehouse(session, bad)


def test_duplicate_names_rejected(session, ids):
    with pytest.raises(Conflict, match="уже существует"):
        create_warehouse(session, "Север")
    wh = create_warehouse(session, "Запад")
    with pytest.raises(Conflict, match="уже существует"):
        rename_warehouse(session, wh.id, "Юг")
    assert session.get(Warehouse, wh.id).name == "Запад"  # переименование откатилось


def test_rename_keeps_stock_and_history(session, ids):
    n = ids.w["Север"]
    before = counts(session)
    rename_warehouse(session, n, "Северный ФФ")
    assert balance(session, ids.p["SKU-101"], n) == (10, 0)
    assert counts(session) == before
    assert {r.warehouse_name for r in stock_rows(session) if r.warehouse_id == n} == {"Северный ФФ"}
    with pytest.raises(NotFound):
        rename_warehouse(session, 9999, "X")


def test_archive_warehouse_with_stock_keeps_everything(session, ids):
    """Главный сценарий ТЗ: удаление ФФ с остатком сохраняет остатки и историю."""
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    before_counts = counts(session)
    before_rows = [(r.sku, r.warehouse_name, r.on_hand) for r in stock_rows(session)]

    archive_warehouse(session, y)

    # остатки на месте и по-прежнему видны, склад помечен как архивный
    assert balance(session, p, y) == (6, 0)
    rows = stock_rows(session)
    assert [(r.sku, r.warehouse_name, r.on_hand) for r in rows] == before_rows
    assert all(r.archived for r in rows if r.warehouse_id == y)
    assert not any(r.archived for r in rows if r.warehouse_id == n)
    # история цела и доступна по архивному складу
    assert counts(session) == before_counts
    assert {h.sku for h in history(session, warehouse_id=y)} == {"SKU-101", "SKU-103"}
    # в списке складов он последний и с остатком
    last = list_warehouses(session)[-1]
    assert (last.name, last.archived, last.on_hand) == ("Юг", True, 10)
    assert_consistent(session)


def test_archived_warehouse_rejects_new_operations_but_allows_evacuation(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    archive_warehouse(session, y)

    with pytest.raises(Conflict, match="в архиве"):
        receive(session, y, p, 1)
    with pytest.raises(Conflict, match="в архиве"):
        reserve_fbs(session, y, p, 1)
    with pytest.raises(Conflict, match="в архиве"):
        ship_fbo(session, y, p, 1)
    with pytest.raises(Conflict, match="в архиве"):
        ship_transfer(session, n, y, p, 1)

    op = ship_transfer(session, y, n, p, 6)  # вывоз остатка разрешён
    receive_transfer(session, op.id)
    assert balance(session, p, y) == (0, 0)
    assert balance(session, p, n) == (16, 0)
    assert_consistent(session)


def test_cannot_archive_with_active_reservation(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    with pytest.raises(Conflict, match="активных резервов FBS — 1"):
        archive_warehouse(session, n)
    assert session.get(Warehouse, n).archived_at is None

    cancel_reservation(session, op.id)
    archive_warehouse(session, n)  # теперь можно
    assert session.get(Warehouse, n).archived_at is not None


def test_cannot_archive_with_incoming_transfer(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    op = ship_transfer(session, n, y, p, 4)
    with pytest.raises(Conflict, match="перемещений в пути на склад — 1"):
        archive_warehouse(session, y)

    archive_warehouse(session, n)  # исходящее в пути архивации отправителя не мешает
    receive_transfer(session, op.id)  # и приёмка на активном Юге проходит
    assert balance(session, p, y) == (10, 0)
    assert_consistent(session)


def test_archive_twice_and_unknown(session, ids):
    archive_warehouse(session, ids.w["Юг"])
    with pytest.raises(Conflict, match="уже в архиве"):
        archive_warehouse(session, ids.w["Юг"])
    with pytest.raises(NotFound):
        archive_warehouse(session, 9999)


def test_total_stock_unchanged_by_archiving(session, ids):
    total = session.scalar(select(func.sum(Event.quantity)).where(Event.type == "RECEIPT"))
    archive_warehouse(session, ids.w["Юг"])
    assert sum(r.on_hand for r in stock_rows(session)) == total == 25


def test_listing_and_history_queries_are_single_statements(session, ids, engine):
    ship_transfer(session, ids.w["Север"], ids.w["Юг"], ids.p["SKU-101"], 4)
    statements = []

    def count(conn, cursor, statement, *a):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        assert len(list_warehouses(session)) == 2
        assert len(history(session)) == 5
        assert len(history(session, warehouse_id=ids.w["Юг"])) == 3  # 2 прихода + отправка
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(statements) == 3  # по одному запросу на вызов, без N+1
