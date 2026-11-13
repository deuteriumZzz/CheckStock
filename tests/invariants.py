"""Сверка остатков с историей: если это сходится после каждого шага, учёт не врёт."""

from collections import defaultdict

from sqlalchemy import func, select

from app.models import ACTIVE, FBS, Event, Inventory, Operation

CREDIT = ("RECEIPT", "RECEIVE_TRANSFER", "ADJUST_IN")  # +on_hand на складе назначения
DEBIT = ("SHIP_FBS", "SHIP_TRANSFER", "SHIP_FBO", "ADJUST_OUT")  # -on_hand на складе отправления


def assert_consistent(s) -> None:
    s.expire_all()
    expected = defaultdict(int)
    for pid, wid, qty in s.execute(
        select(Event.product_id, Event.to_warehouse_id, func.sum(Event.quantity))
        .where(Event.type.in_(CREDIT))
        .group_by(Event.product_id, Event.to_warehouse_id)
    ):
        expected[(pid, wid)] += qty
    for pid, wid, qty in s.execute(
        select(Event.product_id, Event.from_warehouse_id, func.sum(Event.quantity))
        .where(Event.type.in_(DEBIT))
        .group_by(Event.product_id, Event.from_warehouse_id)
    ):
        expected[(pid, wid)] -= qty

    reserved = {
        (pid, wid): int(qty)
        for pid, wid, qty in s.execute(
            select(
                Operation.product_id, Operation.source_warehouse_id, func.sum(Operation.quantity)
            )
            .where(Operation.type == FBS, Operation.status == ACTIVE)
            .group_by(Operation.product_id, Operation.source_warehouse_id)
        )
    }

    for inv in s.scalars(select(Inventory)):
        key = (inv.product_id, inv.warehouse_id)
        assert inv.on_hand == expected.get(key, 0), f"on_hand != история для {key}"
        assert inv.reserved == reserved.get(key, 0), f"reserved != активные резервы для {key}"
        assert 0 <= inv.reserved <= inv.on_hand
