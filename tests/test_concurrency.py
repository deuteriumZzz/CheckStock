"""Конкурентные тесты: каждый поток — своя сессия и своё соединение с PostgreSQL.
Последовательный вызов двух функций гонку не воспроизводит, поэтому здесь потоки стартуют
одновременно (Barrier) и упираются в одни и те же строки."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.errors import Conflict, InsufficientStock
from app.models import Warehouse
from app.services.adjustments import adjust_stock
from app.services.fbs import cancel_reservation, reserve_fbs, ship_fbs
from app.services.queries import in_transit_totals
from app.services.shipments import receive_transfer, ship_fbo, ship_transfer
from app.services.stock import receive
from app.services.warehouses import archive_warehouse, create_warehouse
from tests.helpers import balance
from tests.invariants import assert_consistent


@pytest.fixture
def factory(engine):
    """Отдельный пул побольше, чтобы все потоки получили своё соединение сразу."""
    eng = create_engine(engine.url, pool_size=30)
    yield sessionmaker(eng, expire_on_commit=False)
    eng.dispose()


def race(factory, *calls):
    """Запускает все calls одновременно, у каждого своя сессия. Возвращает ('ok'|'err', значение)."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        with factory() as s:
            barrier.wait(timeout=15)
            try:
                return "ok", call(s)
            except Exception as exc:  # noqa: BLE001 — исключение и есть результат гонки
                return "err", exc

    with ThreadPoolExecutor(len(calls)) as pool:
        return list(pool.map(run, calls))


def split(results):
    oks = [v for kind, v in results if kind == "ok"]
    errs = [v for kind, v in results if kind == "err"]
    return oks, errs


def test_two_parallel_reserves_of_4_from_5_only_one_wins(factory, session, ids):
    p, n = ids.p["SKU-102"], ids.w["Север"]  # по ТЗ на Севере 5 единиц SKU-102
    results = race(factory, lambda s: reserve_fbs(s, n, p, 4), lambda s: reserve_fbs(s, n, p, 4))

    oks, errs = split(results)
    assert len(oks) == 1
    assert len(errs) == 1 and isinstance(errs[0], InsufficientStock)  # не deadlock и не БД-ошибка
    assert balance(session, p, n) == (5, 4)
    assert_consistent(session)


def test_many_parallel_reserves_never_oversell(factory, session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]  # 10 единиц
    results = race(factory, *[lambda s: reserve_fbs(s, n, p, 1) for _ in range(16)])

    oks, errs = split(results)
    assert len(oks) == 10 and len(errs) == 6
    assert all(isinstance(e, InsufficientStock) for e in errs)
    assert balance(session, p, n) == (10, 10)
    assert_consistent(session)


def test_parallel_double_ship_writes_off_once(factory, session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    results = race(factory, *[lambda s: ship_fbs(s, op.id) for _ in range(6)])

    oks, errs = split(results)
    assert len(oks) == 1
    assert all(isinstance(e, Conflict) and "уже отправлен" in str(e) for e in errs)
    assert balance(session, p, n) == (7, 0)  # списано один раз
    assert in_transit_totals(session)[(p, "FBS")] == 3
    assert_consistent(session)


def test_parallel_cancel_and_ship_of_same_reservation(factory, session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    op = reserve_fbs(session, n, p, 3)
    results = race(factory, lambda s: cancel_reservation(s, op.id), lambda s: ship_fbs(s, op.id))

    oks, errs = split(results)
    assert len(oks) == 1 and len(errs) == 1 and isinstance(errs[0], Conflict)
    assert balance(session, p, n) in {(10, 0), (7, 0)}  # либо отмена, либо отправка, не обе
    assert_consistent(session)


def test_parallel_double_receive_adds_once(factory, session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    op = ship_transfer(session, n, y, p, 4)
    results = race(factory, *[lambda s: receive_transfer(s, op.id) for _ in range(6)])

    oks, errs = split(results)
    assert len(oks) == 1
    assert all(isinstance(e, Conflict) and "уже принята" in str(e) for e in errs)
    assert balance(session, p, y) == (10, 0)  # +4 один раз
    assert_consistent(session)


def test_parallel_fbo_and_transfers_share_one_stock(factory, session, ids):
    """Спрос 8 x 2 = 16 при остатке 10: успешных ровно 5, остаток не уходит в минус."""
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    calls = [lambda s: ship_fbo(s, n, p, 2) for _ in range(4)]
    calls += [lambda s: ship_transfer(s, n, y, p, 2) for _ in range(4)]
    oks, errs = split(race(factory, *calls))

    assert len(oks) == 5
    assert all(isinstance(e, InsufficientStock) for e in errs)
    assert balance(session, p, n) == (0, 0)
    in_transit = sum(q for (pid, _), q in in_transit_totals(session).items() if pid == p)
    assert in_transit == 10  # 10 единиц переехали в путь, ни одна не потерялась и не задвоилась
    assert_consistent(session)


def test_opposite_transfers_do_not_deadlock(factory, session, ids):
    p, n, y = ids.p["SKU-101"], ids.w["Север"], ids.w["Юг"]
    calls = [lambda s: ship_transfer(s, n, y, p, 1) for _ in range(5)]
    calls += [lambda s: ship_transfer(s, y, n, p, 1) for _ in range(5)]
    oks, errs = split(race(factory, *calls))

    assert len(oks) == 10 and not errs  # ни deadlock, ни отказов: запасов хватает
    assert balance(session, p, n) == (5, 0)
    assert balance(session, p, y) == (1, 0)
    assert_consistent(session)


def test_archive_vs_incoming_transfer_never_strands_goods(factory, session, ids):
    """Либо склад ушёл в архив, а перемещение отклонено, либо перемещение успело, а архивация
    отклонена. Недопустимо: склад в архиве и на него идёт товар (приёмка стала бы невозможной)."""
    p, n = ids.p["SKU-101"], ids.w["Север"]
    receive(session, n, p, 20)  # запас на 15 итераций
    for i in range(15):
        wh = create_warehouse(session, f"Гонка-{i}")
        results = race(
            factory,
            lambda s, wid=wh.id: archive_warehouse(s, wid),
            lambda s, wid=wh.id: ship_transfer(s, n, wid, p, 1),
        )
        oks, errs = split(results)
        assert len(oks) == 1 and len(errs) == 1 and isinstance(errs[0], Conflict), results

        session.expire_all()
        archived = session.get(Warehouse, wh.id).archived_at is not None
        if archived:
            assert "в архиве" in str(errs[0])  # перемещение отклонено
            assert balance(session, p, wh.id) == (0, 0)
        else:
            assert "в пути" in str(errs[0])  # архивация отклонена
            receive_transfer(session, oks[0].id)  # приёмка обязана пройти
            assert balance(session, p, wh.id) == (1, 0)
    assert_consistent(session)


def test_parallel_receipts_sum_correctly(factory, session, ids):
    p, n = ids.p["SKU-101"], ids.w["Север"]
    oks, errs = split(race(factory, *[lambda s: receive(s, n, p, 1) for _ in range(12)]))
    assert len(oks) == 12 and not errs
    assert balance(session, p, n) == (22, 0)  # 10 + 12, без потерянных обновлений
    assert_consistent(session)


def test_parallel_reserve_and_adjustment_keep_invariant(factory, session, ids):
    """Резерв 8 и корректировка до 5 одновременно: успешна ровно одна, резерв не превышает остаток."""
    p = ids.p["SKU-101"]
    for i in range(10):
        wh = create_warehouse(session, f"Корр-{i}")
        receive(session, wh.id, p, 10)
        results = race(
            factory,
            lambda s, wid=wh.id: reserve_fbs(s, wid, p, 8),
            lambda s, wid=wh.id: adjust_stock(s, wid, p, 5, "пересчёт"),
        )
        oks, errs = split(results)
        assert len(oks) == 1 and len(errs) == 1 and isinstance(errs[0], Conflict), results
        assert balance(session, p, wh.id) in {(10, 8), (5, 0)}
    assert_consistent(session)
