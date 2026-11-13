import pytest
from sqlalchemy import func, select, update

import app.services.shipments as shipments_module
from app.errors import Conflict, InsufficientStock, NotFound, ValidationError
from app.models import Event, Warehouse
from app.services.fbs import reserve_fbs
from app.services.queries import in_transit_totals
from app.services.shipments import receive_fbo, receive_transfer, ship_fbo, ship_transfer
from tests.helpers import balance, counts
from tests.invariants import assert_consistent


def physical_plus_transit(s, ids, sku="SKU-101"):
    """Сколько единиц товара в системе всего: на складах + в пути. Должно быть постоянным."""
    p = ids.p[sku]
    on_hand = sum(balance(s, p, w)[0] for w in ids.w.values())
    transit = sum(q for (pid, _), q in in_transit_totals(s).items() if pid == p)
    return on_hand + transit


def test_transfer_full_cycle(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    op = ship_transfer(session, n, y, p, 4)

    # до приёмки: у отправителя уже списано, у получателя ещё нет, 4 в пути
    assert balance(session, p, n) == (6, 0)
    assert balance(session, p, y) == (6, 0)
    assert in_transit_totals(session)[(p, "TRANSFER")] == 4
    assert physical_plus_transit(session, ids) == 16  # нет двойного учёта
    assert_consistent(session)

    receive_transfer(session, op.id)
    assert balance(session, p, n) == (6, 0)
    assert balance(session, p, y) == (10, 0)
    assert (p, "TRANSFER") not in in_transit_totals(session)
    assert physical_plus_transit(session, ids) == 16
    assert_consistent(session)


def test_double_receive_does_not_add_stock_twice(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    op = ship_transfer(session, n, y, p, 4)
    receive_transfer(session, op.id)
    before = counts(session)
    with pytest.raises(Conflict, match="уже принята"):
        receive_transfer(session, op.id)
    assert balance(session, p, y) == (10, 0)
    assert counts(session) == before
    assert_consistent(session)


def test_receive_wrong_type_or_unknown(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    transfer = ship_transfer(session, n, y, p, 1)
    fbo = ship_fbo(session, n, p, 1)
    reservation = reserve_fbs(session, n, p, 1)
    with pytest.raises(Conflict):
        receive_fbo(session, transfer.id)
    with pytest.raises(Conflict):
        receive_transfer(session, fbo.id)
    with pytest.raises(Conflict):  # приёмка того, что ещё не отправлено
        receive_transfer(session, reservation.id)
    with pytest.raises(NotFound):
        receive_transfer(session, 9999)
    assert_consistent(session)


def test_transfer_validation(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    with pytest.raises(ValidationError, match="различаться"):
        ship_transfer(session, n, n, p, 1)
    for bad in (0, -3, True):
        with pytest.raises(ValidationError):
            ship_transfer(session, n, y, p, bad)
    with pytest.raises(NotFound):
        ship_transfer(session, n, y, 9999, 1)
    with pytest.raises(NotFound):
        ship_transfer(session, 9999, y, p, 1)
    with pytest.raises(NotFound):
        ship_transfer(session, n, 9999, p, 1)


def test_transfer_cannot_move_reserved_goods(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    reserve_fbs(session, n, p, 8)  # на Севере доступно 2, на Юге 6
    before = counts(session)
    with pytest.raises(InsufficientStock) as exc:
        ship_transfer(session, n, y, p, 5)
    msg = str(exc.value)
    assert "доступно 2" in msg and "запрошено 5" in msg and "Юг 6" in msg
    assert balance(session, p, n) == (10, 8)
    assert balance(session, p, y) == (6, 0)
    assert counts(session) == before
    assert_consistent(session)


def test_fbo_full_cycle(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = ship_fbo(session, n, p, 3)
    assert balance(session, p, n) == (7, 0)
    assert in_transit_totals(session)[(p, "FBO")] == 3
    assert physical_plus_transit(session, ids) == 16
    assert_consistent(session)

    receive_fbo(session, op.id)
    assert balance(session, p, n) == (7, 0)  # остаток ФФ при приёмке не меняется
    assert (p, "FBO") not in in_transit_totals(session)
    assert_consistent(session)


def test_double_fbo_receive_is_rejected(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = ship_fbo(session, n, p, 3)
    receive_fbo(session, op.id)
    before = counts(session)
    with pytest.raises(Conflict, match="уже принята"):
        receive_fbo(session, op.id)
    assert counts(session) == before


def test_fbo_shortage_does_not_pick_other_warehouse(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    reserve_fbs(session, n, p, 8)
    before = counts(session)
    with pytest.raises(InsufficientStock, match="Юг 6"):
        ship_fbo(session, n, p, 5)
    assert balance(session, p, y) == (6, 0)  # Юг не тронут автоматически
    assert counts(session) == before


def test_history_shows_from_and_to(session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    op = ship_transfer(session, n, y, p, 4)
    receive_transfer(session, op.id)
    events = session.execute(
        select(Event.type, Event.quantity, Event.from_warehouse_id, Event.to_warehouse_id)
        .where(Event.operation_id == op.id)
        .order_by(Event.id)
    ).all()
    assert events == [("SHIP_TRANSFER", 4, n, y), ("RECEIVE_TRANSFER", 4, n, y)]


def test_archived_warehouse_rules(session, ids):
    """Архивный склад: принимать и отправлять FBO нельзя, а вывезти остаток перемещением можно."""
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    session.execute(update(Warehouse).where(Warehouse.id == y).values(archived_at=func.now()))
    session.commit()

    with pytest.raises(Conflict, match="в архиве"):
        ship_transfer(session, n, y, p, 1)  # получатель в архиве
    with pytest.raises(Conflict, match="в архиве"):
        ship_fbo(session, y, p, 1)

    ship_transfer(session, y, n, p, 6)  # вывоз остатка с архивного склада
    assert balance(session, p, y) == (0, 0)
    assert_consistent(session)


def test_failure_mid_transaction_rolls_back_transfer(session, ids, monkeypatch):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    before = counts(session)

    def boom(*a, **k):
        raise RuntimeError("сбой после списания")

    monkeypatch.setattr(shipments_module, "log_event", boom)
    with pytest.raises(RuntimeError):
        ship_transfer(session, n, y, p, 4)

    assert balance(session, p, n) == (10, 0)  # списание откатилось
    assert in_transit_totals(session) == {}
    assert counts(session) == before
    assert_consistent(session)
