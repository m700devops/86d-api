"""database.get_db under load: with every connection checked out, a caller
WAITS for one (up to POOL_WAIT_SECONDS) instead of failing on the spot, gives
its slot back however its own code ends, and a connection returned to a pool
drained meanwhile is closed rather than turned into a 500. Plus the rule that
keeps the wait harmless: no async function calls get_db() directly — a wait on
the event loop would stall every request the process serves.

No database: the pool is faked, the semaphore and threads are real.
"""
import ast
import glob
import importlib.util
import os
import threading
import time

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


class Conn:
    cursor_factory = None

    def __init__(self):
        self.closed = False

    def cursor(self):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                assert not conn.closed

        return Cur()

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class Pool:
    """psycopg2's ThreadedConnectionPool as get_db sees it: getconn raises
    PoolError the moment `size` connections are out — it never waits."""
    def __init__(self, size, drained_on_return=False):
        self.size, self.out, self.closed = size, 0, False
        self.drained_on_return = drained_on_return
        self.lock = threading.Lock()

    def getconn(self):
        with self.lock:
            if self.out >= self.size:
                raise psycopg2.pool.PoolError("connection pool exhausted")
            self.out += 1
            return Conn()

    def putconn(self, conn, close=False):
        with self.lock:
            if self.drained_on_return:
                raise psycopg2.pool.PoolError("trying to put unkeyed connection")
            self.out -= 1


@pytest.fixture
def pool(monkeypatch):
    """A pool of two behind two slots, as POOL_MAX of each would be."""
    fake = Pool(2)
    monkeypatch.setattr(db, "_pool", fake)
    monkeypatch.setattr(db, "_slots", threading.BoundedSemaphore(2))
    return fake


def _hold(release: threading.Event, holding: threading.Barrier):
    """A request that has its connection and is busy with it until `release`."""
    with db.get_db():
        holding.wait(timeout=5)
        release.wait(timeout=5)


def _busy(n, release):
    holding = threading.Barrier(n + 1)
    threads = [threading.Thread(target=_hold, args=(release, holding)) for _ in range(n)]
    for t in threads:
        t.start()
    holding.wait(timeout=5)          # every connection is now checked out
    return threads


def test_the_next_caller_waits_for_a_free_connection(pool):
    release = threading.Event()
    threads = _busy(2, release)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    with db.get_db() as conn:        # the pool alone would raise here, at once
        waited = time.monotonic() - started
        assert isinstance(conn, Conn)
    for t in threads:
        t.join(timeout=5)
    assert waited >= 0.25 and pool.out == 0


def test_it_gives_up_after_the_wait(pool, monkeypatch, capsys):
    monkeypatch.setattr(db, "POOL_WAIT_SECONDS", 0.2)
    release = threading.Event()
    threads = _busy(2, release)
    started = time.monotonic()
    try:
        with pytest.raises(psycopg2.pool.PoolError):
            with db.get_db():
                pass
        assert 0.15 <= time.monotonic() - started < 2
        assert "DB_POOL_WAIT_TIMEOUT" in capsys.readouterr().out
    finally:
        release.set()
        for t in threads:
            t.join(timeout=5)


def test_a_failed_request_gives_its_slot_back(pool, monkeypatch):
    monkeypatch.setattr(db, "POOL_WAIT_SECONDS", 0.2)
    for _ in range(5):               # more failures than there are slots
        with pytest.raises(RuntimeError):
            with db.get_db():
                raise RuntimeError("the route's own bug")
    started = time.monotonic()
    with db.get_db():
        pass
    assert time.monotonic() - started < 0.1 and pool.out == 0


def test_a_connection_returned_to_a_drained_pool_is_closed_not_an_error(pool):
    pool.drained_on_return = True
    with db.get_db() as conn:
        pass                         # the work succeeded; giving it back must not 500
    assert conn.closed


def test_no_async_function_calls_get_db_directly():
    """A wait for a connection on the event loop would stall every request, so
    async code reaches the database through a thread (asyncio.to_thread, the
    scan path's _on_scan_thread), never with get_db() in its own body."""
    offenders = []
    for path in sorted(glob.glob("*.py")):
        if path.startswith("test_"):
            continue
        with open(path) as f:
            tree = ast.parse(f.read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            stack = list(ast.iter_child_nodes(fn))
            while stack:
                node = stack.pop()
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue          # a nested def runs wherever it is called from
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_db":
                    offenders.append(f"{path}:{node.lineno} {fn.name}")
                stack.extend(ast.iter_child_nodes(node))
    assert offenders == []
