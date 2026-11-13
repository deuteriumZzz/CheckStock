"""REST API. Роуты только принимают запрос, валидируют и вызывают сервис;
вся бизнес-логика и транзакции живут в app/services."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, StrictInt
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.services import fbs, queries, shipments
from app.services import warehouses as wh_service
from app.services.adjustments import adjust_stock
from app.services.stock import receive

router = APIRouter(prefix="/api")


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


Db = Annotated[Session, Depends(get_session)]


# --- схемы. StrictInt: "5", 5.0 и true отклоняются на входе, а не молча приводятся ---


class StockCommand(BaseModel):
    warehouse_id: StrictInt
    product_id: StrictInt
    quantity: StrictInt


class TransferCommand(BaseModel):
    source_id: StrictInt
    dest_id: StrictInt
    product_id: StrictInt
    quantity: StrictInt


class AdjustCommand(BaseModel):
    warehouse_id: StrictInt
    product_id: StrictInt
    new_on_hand: StrictInt  # фактическое количество на складе после пересчёта
    reason: str


class NameBody(BaseModel):
    name: str


class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    type: str
    status: str
    product_id: int
    quantity: int
    source_warehouse_id: int | None
    dest_warehouse_id: int | None
    note: str | None = None


class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    archived: bool = False


def _warehouse_out(wh) -> WarehouseOut:
    return WarehouseOut(id=wh.id, name=wh.name, archived=wh.archived_at is not None)


# --- чтение ---


@router.get("/stock")
def get_stock(s: Db) -> dict:
    return queries.stock_overview(s)


@router.get("/operations/open", response_model=list[queries.OpenOperationRow])
def get_open_operations(s: Db):
    return queries.open_operations(s)


@router.get("/history", response_model=list[queries.HistoryRow])
def get_history(
    s: Db,
    warehouse_id: int | None = None,
    product_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return queries.history(s, warehouse_id=warehouse_id, product_id=product_id, limit=limit)


@router.get("/warehouses", response_model=list[queries.WarehouseRow])
def get_warehouses(s: Db):
    return queries.list_warehouses(s)


# --- склады ---


@router.post("/warehouses", status_code=201, response_model=WarehouseOut)
def create_warehouse(body: NameBody, s: Db):
    return _warehouse_out(wh_service.create_warehouse(s, body.name))


@router.patch("/warehouses/{warehouse_id}", response_model=WarehouseOut)
def rename_warehouse(warehouse_id: int, body: NameBody, s: Db):
    return _warehouse_out(wh_service.rename_warehouse(s, warehouse_id, body.name))


@router.post("/warehouses/{warehouse_id}/archive", response_model=WarehouseOut)
def archive_warehouse(warehouse_id: int, s: Db):
    return _warehouse_out(wh_service.archive_warehouse(s, warehouse_id))


# --- движения. Остатки напрямую изменить нельзя: только через эти команды ---


@router.post("/receipts", status_code=201, response_model=OperationOut)
def create_receipt(body: StockCommand, s: Db):
    return receive(s, body.warehouse_id, body.product_id, body.quantity)


@router.post("/adjustments", status_code=201, response_model=OperationOut)
def create_adjustment(body: AdjustCommand, s: Db):
    """Исправить остаток по факту пересчёта: разница с причиной попадает в историю."""
    return adjust_stock(s, body.warehouse_id, body.product_id, body.new_on_hand, body.reason)


@router.post("/fbs/reservations", status_code=201, response_model=OperationOut)
def reserve(body: StockCommand, s: Db):
    return fbs.reserve_fbs(s, body.warehouse_id, body.product_id, body.quantity)


@router.post("/fbs/{operation_id}/cancel", response_model=OperationOut)
def cancel_reservation(operation_id: int, s: Db):
    return fbs.cancel_reservation(s, operation_id)


@router.post("/fbs/{operation_id}/ship", response_model=OperationOut)
def ship_fbs(operation_id: int, s: Db):
    return fbs.ship_fbs(s, operation_id)


@router.post("/transfers", status_code=201, response_model=OperationOut)
def create_transfer(body: TransferCommand, s: Db):
    return shipments.ship_transfer(s, body.source_id, body.dest_id, body.product_id, body.quantity)


@router.post("/transfers/{operation_id}/receive", response_model=OperationOut)
def receive_transfer(operation_id: int, s: Db):
    return shipments.receive_transfer(s, operation_id)


@router.post("/fbo", status_code=201, response_model=OperationOut)
def create_fbo(body: StockCommand, s: Db):
    return shipments.ship_fbo(s, body.warehouse_id, body.product_id, body.quantity)


@router.post("/fbo/{operation_id}/receive", response_model=OperationOut)
def receive_fbo(operation_id: int, s: Db):
    return shipments.receive_fbo(s, operation_id)
