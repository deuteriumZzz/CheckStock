from sqlalchemy.orm import Session

from app.db import atomic
from app.errors import Conflict, NotFound
from app.models import ACTIVE, CANCELLED, FBS, IN_TRANSIT, Operation, Product
from app.services.stock import (
    check_quantity,
    get_active_warehouse,
    lock_inventory,
    lock_operation,
    log_event,
    require_available,
)


def reserve_fbs(
    s: Session,
    warehouse_id: int,
    product_id: int,
    quantity: int,
    idempotency_key: str | None = None,
) -> Operation:
    """Резерв блокирует товар на ФФ: on_hand не меняется, растёт reserved."""
    with atomic(s):
        check_quantity(quantity)
        if s.get(Product, product_id) is None:
            raise NotFound(f"Товар {product_id} не найден")
        wh = get_active_warehouse(s, warehouse_id)

        inv = lock_inventory(s, product_id, warehouse_id)
        require_available(s, inv, wh, quantity)
        inv.reserved += quantity

        op = Operation(
            type=FBS,
            status=ACTIVE,
            product_id=product_id,
            quantity=quantity,
            source_warehouse_id=warehouse_id,
            idempotency_key=idempotency_key,
        )
        s.add(op)
        s.flush()
        log_event(s, op, "RESERVE", from_warehouse_id=warehouse_id)
        s.flush()
        return op


def _lock_active_reservation(s: Session, operation_id: int) -> Operation:
    """Блокирует операцию и проверяет переход: только ACTIVE можно отменить или отправить."""
    op = lock_operation(s, operation_id)
    if op.type != FBS:
        raise Conflict(f"Операция {operation_id} не является резервом FBS")
    if op.status == CANCELLED:
        raise Conflict(f"Резерв {operation_id} уже отменён")
    if op.status == IN_TRANSIT:
        raise Conflict(f"Заказ {operation_id} уже отправлен клиенту")
    return op


def cancel_reservation(s: Session, operation_id: int) -> Operation:
    """ACTIVE -> CANCELLED: резерв освобождается, on_hand не меняется."""
    with atomic(s):
        op = _lock_active_reservation(s, operation_id)
        inv = lock_inventory(s, op.product_id, op.source_warehouse_id)
        inv.reserved -= op.quantity
        op.status = CANCELLED
        log_event(s, op, "CANCEL_RESERVE", from_warehouse_id=op.source_warehouse_id)
        s.flush()
        return op


def ship_fbs(s: Session, operation_id: int) -> Operation:
    """ACTIVE -> IN_TRANSIT: списываем физический остаток и снимаем резерв в одной транзакции."""
    with atomic(s):
        op = _lock_active_reservation(s, operation_id)
        inv = lock_inventory(s, op.product_id, op.source_warehouse_id)
        inv.on_hand -= op.quantity
        inv.reserved -= op.quantity
        op.status = IN_TRANSIT
        log_event(s, op, "SHIP_FBS", from_warehouse_id=op.source_warehouse_id)
        s.flush()
        return op
