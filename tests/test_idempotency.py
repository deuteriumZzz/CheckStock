"""Идемпотентность: повтор запроса создания с тем же Idempotency-Key не списывает товар второй раз."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import get_session
from app.errors import Conflict, InsufficientStock
from app.main import app
from app.models import TRANSFER, Operation
from app.services.idempotency import run_idempotent, same_request
from app.services.shipments import ship_transfer
from tests.helpers import balance
from tests.invariants import assert_consistent
from tests.test_concurrency import race, split


@pytest.fixture
def factory(engine):
    eng = create_engine(engine.url, pool_size=30)
    yield sessionmaker(eng, expire_on_commit=False)
    eng.dispose()


def transfer(s, ids, qty, key):
    """Перемещение SKU-101 с Севера на Юг через ту же обёртку, что использует API."""
    n, y, p = ids.w["Север"], ids.w["Юг"], ids.p["SKU-101"]
    return run_idempotent(
        s,
        key,
        lambda k: ship_transfer(s, n, y, p, qty, k),
        lambda op: same_request(op, TRANSFER, p, qty, n, y),
    )


def transfers_count(session):
    return session.scalar(
        select(func.count()).select_from(Operation).where(Operation.type == TRANSFER)
    )


def test_same_key_returns_same_operation_and_writes_off_once(session, ids):
    first = transfer(session, ids, 4, "k1")
    second = transfer(session, ids, 4, "k1")
    assert first.id == second.id
    assert transfers_count(session) == 1
    assert balance(session, ids.p["SKU-101"], ids.w["Север"]) == (6, 0)  # 10 - 4, а не 10 - 8
    assert_consistent(session)


def test_without_key_each_request_is_a_new_operation(session, ids):
    transfer(session, ids, 4, None)
    transfer(session, ids, 4, None)
    assert transfers_count(session) == 2
    assert balance(session, ids.p["SKU-101"], ids.w["Север"]) == (2, 0)


def test_same_key_with_another_body_is_rejected(session, ids):
    transfer(session, ids, 4, "k1")
    with pytest.raises(Conflict, match="другого запроса"):
        transfer(session, ids, 3, "k1")
    assert balance(session, ids.p["SKU-101"], ids.w["Север"]) == (6, 0)
    assert transfers_count(session) == 1


def test_failed_request_does_not_burn_the_key(session, ids):
    with pytest.raises(InsufficientStock):
        transfer(session, ids, 50, "k1")  # на Севере всего 10
    assert transfers_count(session) == 0
    op = transfer(session, ids, 4, "k1")  # тот же ключ после отказа работает
    assert op.quantity == 4
    assert transfers_count(session) == 1


@pytest.mark.parametrize(
    "qty", [4, 10]
)  # 10 = весь остаток: проигравшие дубли упираются в «нехватку»
def test_parallel_duplicates_create_one_operation(factory, session, ids, qty):
    results = race(factory, *[lambda s: transfer(s, ids, qty, "dup") for _ in range(8)])
    oks, errs = split(results)
    assert not errs, errs
    assert len({op.id for op in oks}) == 1  # все восемь получили одну и ту же операцию
    assert transfers_count(session) == 1
    assert balance(session, ids.p["SKU-101"], ids.w["Север"]) == (10 - qty, 0)
    assert_consistent(session)


def test_idempotency_over_http(engine, ids, session):
    factory = sessionmaker(engine, expire_on_commit=False)

    def override():
        with factory() as s:
            yield s

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            body = {
                "source_id": ids.w["Север"],
                "dest_id": ids.w["Юг"],
                "product_id": ids.p["SKU-101"],
                "quantity": 4,
            }
            h = {"Idempotency-Key": "http-1"}
            a = client.post("/api/transfers", json=body, headers=h)
            b = client.post("/api/transfers", json=body, headers=h)
            assert (a.status_code, b.status_code) == (201, 201)
            assert a.json()["id"] == b.json()["id"]
            assert balance(session, ids.p["SKU-101"], ids.w["Север"]) == (6, 0)

            other = client.post("/api/transfers", json={**body, "quantity": 3}, headers=h)
            assert (other.status_code, other.json()["error"]["code"]) == (409, "conflict")

            empty = client.post("/api/transfers", json=body, headers={"Idempotency-Key": ""})
            assert empty.status_code == 422

            plain = client.post("/api/transfers", json=body)  # без ключа — обычная новая операция
            assert plain.status_code == 201 and plain.json()["id"] != a.json()["id"]
    finally:
        app.dependency_overrides.clear()
    assert_consistent(session)
