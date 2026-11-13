"""Идемпотентность команд создания (приход, резерв, перемещение, FBO, корректировка).

Клиент присылает заголовок Idempotency-Key (например, UUID). Повтор запроса с тем же ключом
возвращает уже созданную операцию и ничего не списывает второй раз: это защита от двойного клика
и повторной отправки после обрыва сети.

Ключ хранится в самой операции под уникальным индексом. Если два одинаковых запроса пришли
одновременно, база пропустит только один, проигравший откатывается целиком (остатки не меняются)
и возвращает операцию победителя. Неудачный запрос (например, нехватка) ключ не «сжигает»:
откат убирает и его, повторить с тем же ключом можно."""

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict
from app.models import ADJUSTMENT, Operation


def run_idempotent(
    s: Session,
    key: str | None,
    create: Callable[[str | None], Operation],
    matches: Callable[[Operation], bool],
) -> Operation:
    """create(key) создаёт операцию; matches(op) проверяет, что найденная по ключу операция
    действительно соответствует этому запросу (тот же ключ с другим телом — ошибка клиента)."""
    if key is None:
        return create(None)

    def stored() -> Operation | None:
        return s.scalar(select(Operation).where(Operation.idempotency_key == key))

    op = stored()
    if op is None:
        try:
            return create(key)
        except Exception:
            # Параллельный дубль мог успеть первым: тогда его операция уже сохранена, и честный
            # ответ на повтор — она же, а не «нехватка» или ошибка уникальности.
            op = stored()
            if op is None:
                raise
    if not matches(op):
        raise Conflict("Idempotency-Key уже использован для другого запроса")
    return op


def same_request(
    op: Operation,
    op_type: str,
    product_id: int,
    quantity: int,
    source_id: int | None = None,
    dest_id: int | None = None,
) -> bool:
    return (op.type, op.product_id, op.quantity, op.source_warehouse_id, op.dest_warehouse_id) == (
        op_type,
        product_id,
        quantity,
        source_id,
        dest_id,
    )


def same_adjustment(op: Operation, warehouse_id: int, product_id: int, reason: str) -> bool:
    return (
        op.type == ADJUSTMENT
        and op.product_id == product_id
        and warehouse_id in (op.source_warehouse_id, op.dest_warehouse_id)
        and op.note == reason.strip()
    )
