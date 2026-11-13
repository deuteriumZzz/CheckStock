import pytest
from sqlalchemy import select

import app.services.fbs as fbs_module
from app.errors import Conflict, InsufficientStock, NotFound, ValidationError
from app.models import Operation
from app.services.fbs import cancel_reservation, reserve_fbs, ship_fbs
from app.services.queries import in_transit_totals
from tests.helpers import balance, counts
from tests.invariants import assert_consistent


def test_reserve_blocks_but_keeps_physical_stock(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    reserve_fbs(session, n, p, 3)
    assert tuple(balance(session, p, n)) == (10, 3)  # физически 10, в резерве 3, доступно 7
    assert_consistent(session)


def test_cancel_releases_reservation(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    cancel_reservation(session, op.id)
    assert tuple(balance(session, p, n)) == (10, 0)
    assert_consistent(session)


def test_double_cancel_is_rejected_and_changes_nothing(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    cancel_reservation(session, op.id)
    before = counts(session)
    with pytest.raises(Conflict):
        cancel_reservation(session, op.id)
    assert tuple(balance(session, p, n)) == (10, 0)
    assert counts(session) == before


def test_ship_moves_goods_to_client(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    ship_fbs(session, op.id)
    assert tuple(balance(session, p, n)) == (7, 0)
    assert in_transit_totals(session)[(p, "FBS")] == 3
    assert_consistent(session)


def test_double_ship_does_not_write_off_twice(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    ship_fbs(session, op.id)
    before = counts(session)
    with pytest.raises(Conflict, match="уже отправлен"):
        ship_fbs(session, op.id)
    assert tuple(balance(session, p, n)) == (7, 0)
    assert in_transit_totals(session)[(p, "FBS")] == 3
    assert counts(session) == before
    assert_consistent(session)


def test_cannot_cancel_shipped_or_ship_cancelled(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    shipped = reserve_fbs(session, n, p, 2)
    ship_fbs(session, shipped.id)
    with pytest.raises(Conflict):
        cancel_reservation(session, shipped.id)

    cancelled = reserve_fbs(session, n, p, 2)
    cancel_reservation(session, cancelled.id)
    with pytest.raises(Conflict):
        ship_fbs(session, cancelled.id)
    assert tuple(balance(session, p, n)) == (8, 0)
    assert_consistent(session)


def test_shortage_on_chosen_warehouse_with_enough_total(session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    reserve_fbs(session, n, p, 8)  # на Севере доступно 2, на Юге 6
    before = counts(session)
    with pytest.raises(InsufficientStock) as exc:
        reserve_fbs(session, n, p, 5)
    msg = str(exc.value)
    assert "доступно 2" in msg and "запрошено 5" in msg and "Юг 6" in msg
    assert tuple(balance(session, p, n)) == (10, 8)
    assert tuple(balance(session, p, ids.w["Юг"])) == (6, 0)  # другой склад не тронут
    assert counts(session) == before
    assert_consistent(session)


@pytest.mark.parametrize("qty", [0, -1, True, 1.5])
def test_invalid_quantity(session, ids, qty):
    with pytest.raises(ValidationError):
        reserve_fbs(session, ids.w["Север"], ids.p["SKU-101"], qty)


def test_unknown_entities(session, ids):
    with pytest.raises(NotFound):
        reserve_fbs(session, ids.w["Север"], 9999, 1)
    with pytest.raises(NotFound):
        reserve_fbs(session, 9999, ids.p["SKU-101"], 1)
    with pytest.raises(NotFound):
        ship_fbs(session, 9999)


def test_receipt_cannot_be_shipped_as_fbs(session, ids):
    receipt = session.scalar(select(Operation).where(Operation.type == "RECEIPT"))
    with pytest.raises(Conflict, match="не является резервом"):
        ship_fbs(session, receipt.id)


def test_failure_mid_transaction_rolls_everything_back(session, ids, monkeypatch):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    before = counts(session)

    def boom(*a, **k):
        raise RuntimeError("сбой после изменения остатка")

    monkeypatch.setattr(fbs_module, "log_event", boom)
    with pytest.raises(RuntimeError):
        reserve_fbs(session, n, p, 3)

    assert tuple(balance(session, p, n)) == (10, 0)  # резерв не просочился
    assert counts(session) == before
    assert_consistent(session)
