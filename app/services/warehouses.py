"""Склады ФФ. «Удаление» = архивация: строка, остатки и история остаются нетронутыми."""

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import atomic
from app.errors import Conflict, NotFound, ValidationError
from app.models import ACTIVE, FBS, IN_TRANSIT, TRANSFER, Operation, Warehouse

NAME_TAKEN = "Склад с таким названием уже существует"


def _clean_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("Название склада не может быть пустым")
    name = name.strip()
    if len(name) > 255:
        raise ValidationError("Название склада не длиннее 255 символов")
    return name


def _lock_warehouse(s: Session, warehouse_id: int) -> Warehouse:
    wh = s.execute(
        select(Warehouse)
        .where(Warehouse.id == warehouse_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if wh is None:
        raise NotFound(f"Склад {warehouse_id} не найден")
    return wh


def create_warehouse(s: Session, name: str) -> Warehouse:
    name = _clean_name(name)
    with atomic(s):
        wh = Warehouse(name=name)
        s.add(wh)
        try:
            s.flush()  # уникальность названия гарантирует UNIQUE в БД, а не проверка «до»
        except IntegrityError:
            raise Conflict(NAME_TAKEN) from None
        return wh


def rename_warehouse(s: Session, warehouse_id: int, name: str) -> Warehouse:
    name = _clean_name(name)
    with atomic(s):
        wh = _lock_warehouse(s, warehouse_id)
        wh.name = name
        try:
            s.flush()
        except IntegrityError:
            raise Conflict(NAME_TAKEN) from None
        return wh


def archive_warehouse(s: Session, warehouse_id: int) -> Warehouse:
    """Остатки и история сохраняются. Нельзя архивировать, пока есть активные резервы FBS
    или перемещения, идущие на этот склад: иначе приёмку провести будет некуда."""
    with atomic(s):
        wh = _lock_warehouse(s, warehouse_id)  # FOR UPDATE: ждёт идущие операции со складом
        if wh.archived_at is not None:
            raise Conflict(f"Склад «{wh.name}» уже в архиве")

        reserves_cond = (Operation.type == FBS) & (Operation.status == ACTIVE)
        incoming_cond = (Operation.type == TRANSFER) & (Operation.status == IN_TRANSIT)
        reserves, incoming = s.execute(
            select(
                func.count().filter(reserves_cond & (Operation.source_warehouse_id == wh.id)),
                func.count().filter(incoming_cond & (Operation.dest_warehouse_id == wh.id)),
            ).where(
                or_(
                    reserves_cond & (Operation.source_warehouse_id == wh.id),
                    incoming_cond & (Operation.dest_warehouse_id == wh.id),
                )
            )
        ).one()
        if reserves or incoming:
            raise Conflict(
                f"Нельзя архивировать «{wh.name}»: активных резервов FBS — {reserves}, "
                f"перемещений в пути на склад — {incoming}. Завершите или отмените их."
            )
        wh.archived_at = func.now()
        s.flush()
        return wh
