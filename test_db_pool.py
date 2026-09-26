"""database._getconn: a caller waits for a free connection instead of failing
the moment all of them are checked out — except on the event loop, where
waiting would stall every request. No database: the pool is faked.
"""
import asyncio
import importlib.util
import os

import psycopg2.pool
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://dummy/dummy")


def _real_database_module():
    """database.py itself, even where another test file has stubbed
    sys.modules["database"]. Its pool is lazy: nothing connects."""
    spec = importlib.util.spec_from_file_location("database_for_pool_test", "database.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


db = _real_database_module()


class Pool:
    """Hands out a connection once `busy` getconn calls have found none free."""
    def __init__(self, busy, closed=False):
        self.busy, self.closed, self.calls = busy, closed, 0

    def getconn(self):
        self.calls += 1
        if self.closed:
            raise psycopg2.pool.PoolError("connection pool is closed")
        if self.calls <= self.busy:
            raise psycopg2.pool.PoolError("connection pool exhausted")
        return "conn"


@pytest.fixture
def naps(monkeypatch):
    slept = []
    monkeypatch.setattr(db.time, "sleep", slept.append)
    return slept


def test_a_burst_waits_for_a_free_connection(naps):
    pool = Pool(busy=3)
    assert db._getconn(pool) == "conn" and pool.calls == 4
    assert naps == [0.02, 0.04, 0.08]                  # short, growing waits


def test_it_gives_up_after_the_wait(monkeypatch, naps):
    clock = iter([0.0] + [0.0] * 5 + [db.POOL_WAIT_SEC + 1] * 50)
    monkeypatch.setattr(db.time, "monotonic", lambda: next(clock))
    with pytest.raises(psycopg2.pool.PoolError):
        db._getconn(Pool(busy=10 ** 6))
    assert len(naps) < 20


def test_a_closed_pool_is_not_waited_on(naps):
    with pytest.raises(psycopg2.pool.PoolError):
        db._getconn(Pool(busy=0, closed=True))
    assert naps == []


def test_never_waits_on_the_event_loop(naps):
    async def on_loop():
        return db._getconn(Pool(busy=1))
    with pytest.raises(psycopg2.pool.PoolError):
        asyncio.run(on_loop())
    assert naps == []


def test_get_db_uses_it():
    import inspect
    assert "_getconn(pool)" in inspect.getsource(db.get_db.__wrapped__)
