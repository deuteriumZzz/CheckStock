"""Корректировка остатка (инвентаризация): менеджер вводит фактическое количество и причину,
система сама считает разницу и пишет её в историю отдельной операцией. Остаток по-прежнему
меняется только через операции, поэтому «остаток = история» сохраняется."""

from sqlalchemy.orm import Session

from app.db import atomic
from app.errors import Conflict, NotFound, ValidationError
from app.models import ADJUSTMENT, RECEIVED, Operation, Product
from app.services.stock import get_active_warehouse, lock_inventory, log_event


def adjust_stock(
    s: Session,
    warehouse_id: int,
    product_id: int,
    new_on_hand: int,
    reason: str,
    idempotency_key: str | None = None,
) -> Operation:
    if not isinstance(new_on_hand, int) or isinstance(new_on_hand, bool) or new_on_hand < 0:
        raise ValidationError("Фактическое количество должно быть целым числом, не меньше нуля")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise ValidationError("Укажите причину корректировки")
    if len(reason) > 255:
        raise ValidationError("Причина не длиннее 255 символов")

    with atomic(s):
        if s.get(Product, product_id) is None:
            raise NotFound(f"Товар {product_id} не найден")
        wh = get_active_warehouse(s, warehouse_id)
        inv = lock_inventory(s, product_id, warehouse_id)

        delta = new_on_hand - inv.on_hand
        if delta == 0:
            raise ValidationError(f"На складе «{wh.name}» уже {inv.on_hand}: менять нечего")
        if new_on_hand < inv.reserved:
            raise Conflict(
                f"Нельзя поставить {new_on_hand}: под заказы FBS зарезервировано {inv.reserved}. "
                "Сначала отмените или отправьте резервы."
            )
        inv.on_hand = new_on_hand

        increase = delta > 0
        op = Operation(
            type=ADJUSTMENT,
            status=RECEIVED,
            product_id=product_id,
            quantity=abs(delta),
            dest_warehouse_id=warehouse_id if increase else None,
            source_warehouse_id=None if increase else warehouse_id,
            note=reason,
            idempotency_key=idempotency_key,
        )
        s.add(op)
        s.flush()
        if increase:
            log_event(s, op, "ADJUST_IN", to_warehouse_id=warehouse_id)
        else:
            log_event(s, op, "ADJUST_OUT", from_warehouse_id=warehouse_id)
        s.flush()
        return op
