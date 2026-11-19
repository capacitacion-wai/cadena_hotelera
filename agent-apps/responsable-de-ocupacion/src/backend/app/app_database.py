"""Async engine for the local SQLite living on the /app-data PVC.

This is the ONLY writable store in the agent-app. The project DB
stays read-only forever.

CONCURRENCY (R-3): SQLite — even in WAL — admits exactly ONE writer at a
time. This app has MANY concurrent writers in one process: the executor
worker POOL (up to 8), the channel poller, the periodic stale-recovery
loop, the watcher tick and every HTTP write. Left to fight over the file
they pile up on the single write lock; ``busy_timeout`` alone is a trap
because a blocked writer keeps HOLDING its pooled connection while it
spins — so under a burst the pool drains, reads and the health probes
starve for a connection, and readiness/liveness time out (exactly the
symptom the cluster watcher flagged: 'database is locked' on the poller's
telemetry UPDATE + worker crashes + probe timeouts, all in the same
seconds). The fix is to serialize writers ABOVE the pool with an asyncio
lock (``app_write``): a waiter blocks on the lock holding NO connection,
so the pool stays free for reads/health, and SQLite never sees two
writers → no 'database is locked'.

WHY THE QUEUE MUST BE UNIVERSAL (INC-VIG-59). ``busy_timeout`` is NOT an
equivalent fallback for a writer left outside the queue, because SQLite's
busy handler is a **retry lottery with no fairness**: a waiter just sleeps
and retries, so under sustained churn it can lose repeatedly and blow even
a 15 s timeout. That is what happened when 7.139.4 wired ``app_write``
into the executor only: 8 workers claiming every 2 s (plus the poller)
kept the file busy, and the writes still going straight to SQLite —
including the operator's ``triggers_active`` toggle — died with 'database
is locked'. The kill switch failed exactly when it was needed most, while
the employee was working. ``asyncio.Lock`` is FIFO, so joining the queue
turns "win a lottery" into "wait your turn": bounded, fair, and it lets a
one-row governance write through even mid-round. Therefore EVERY writer
belongs in the queue (``app_write``, or ``app_write_conn`` when the path
needs a raw connection), ``busy_timeout`` is only a last-resort backstop,
and a write that MUST NOT be lost uses ``write_with_retry``.

Corollary that is easy to get wrong: **reads must NOT take the queue.**
A read holding the write slot adds no safety (SQLite readers are
concurrent under WAL) and inflates the queue everyone else waits behind —
the executor's 24 h budget scan used to do this 4×/second.
"""

import asyncio
import contextlib
import contextvars
import logging
import sys
import weakref

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from app.config import APP_DATABASE_URL

logger = logging.getLogger(__name__)

AppBase = declarative_base()

app_engine = create_async_engine(
    APP_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"timeout": 30},
    future=True,
)


# R-2 (P3): apply WAL + sane SQLite PRAGMAs on EVERY new connection,
# not just the boot one. Pool reuses connections, so a one-shot
# PRAGMA in lifespan only protects the boot connection; subsequent
# pool members default back to ``synchronous=FULL`` and
# ``busy_timeout=0`` — exactly when the watcher and executor are
# fighting for the lock.
#
# ``journal_mode=WAL`` is database-level and persists, so it's safe
# to issue per-connection — SQLite no-ops it after the first.
# ``synchronous`` and ``busy_timeout`` are connection-level and
# matter on every connect.
@event.listens_for(app_engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    """Apply SQLite tuning PRAGMAs on every new connection.

    SQLAlchemy fires this for both sync and async drivers (aiosqlite
    DBAPI connections expose a sync ``.cursor()`` via greenlet
    bridging). Failures are swallowed: if the underlying driver
    rejects a PRAGMA we'd rather keep the connection alive than fail
    pool init.
    """
    try:
        cursor = dbapi_conn.cursor()
    except Exception:
        return
    try:
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            # R-3: backstop for writes NOT routed through ``app_write`` (HTTP
            # handlers, watcher units). Raised 5000→15000 so a brief burst
            # waits instead of raising 'database is locked'. The primary
            # defence is ``app_write`` (serialize above the pool); this only
            # has to cover the residual, un-serialized writers.
            "PRAGMA busy_timeout=15000",
            # G-6: explicit auto-checkpoint cadence (matches SQLite's
            # default of 1000 pages, but an operator reading this
            # shouldn't have to know that).
            "PRAGMA wal_autocheckpoint=1000",
            "PRAGMA foreign_keys=ON",
        ):
            try:
                cursor.execute(pragma)
            except Exception:
                # PRAGMA support varies across builds; skip silently.
                continue
    finally:
        try:
            cursor.close()
        except Exception:
            pass


app_session = async_sessionmaker(app_engine, expire_on_commit=False)


async def get_app_session():
    async with app_session() as s:
        yield s


# ── R-3: global write serialization ──────────────────────────────────────
# One process, many concurrent writers, one SQLite writer slot. This lock
# lets exactly ONE write transaction run at a time. Crucially it is acquired
# BEFORE a pooled connection is checked out, so a waiting writer holds no
# connection — reads and health probes keep flowing while writes queue.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def write_lock() -> asyncio.Lock:
    """The write queue for the CURRENT event loop.

    In the pod there is exactly one loop, so this is the single process-wide queue
    the module docstring describes. It is built per loop, lazily, instead of as one
    module-level ``asyncio.Lock`` because such a lock silently remembers the first
    loop that awaited it and then raises "is bound to a different event loop" from
    any other — turning a serialized write into a crash in a script, a maintenance
    job or a test. Honest limitation: two loops in the SAME process would get two
    queues and could present SQLite with two writers; that is not how the app runs
    (uvicorn drives one loop), and ``busy_timeout`` remains the backstop for it.
    """
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _locks[loop] = lock
    return lock


# Re-entrancy: a task already inside ``app_write`` (e.g. the executor opens a
# nested write session for arrival-automation matching) must NOT deadlock on
# its own lock. A contextvar tracks "this task already holds the write lock";
# the nested call reuses it and just opens another session. asyncio.Lock is
# not re-entrant, so without this a legitimate nested write would hang forever.
_in_write: contextvars.ContextVar[bool] = contextvars.ContextVar("_in_write", default=False)



# Contention telemetry (INC-VIG-59). This pathology used to be visible ONLY as a
# traceback in the pod log — an operator watching the UI just saw a 500. Counting
# it here is what lets the platform-health surface answer "is my employee choking
# on its own database?" without kubectl.
_contention = {
    "waits": 0,            # writers that had to queue (slot was taken)
    "wait_ms_total": 0.0,  # cumulative queueing time
    "wait_ms_max": 0.0,    # worst single wait, SINCE BOOT (only ever grows)
    "locked_errors": 0,    # 'database is locked' that still reached a caller
    "retries": 0,          # write_with_retry re-attempts
    # v7.169.2 — WHEN the worst case was, and when the last wait.
    #
    # `wait_ms_max` is a historical maximum that only grows and is never reset, and
    # the screen presented it IN THE PRESENT: "writes are waiting up to 14,404 ms".
    # That number could come from a one-off load hours ago, with the database
    # perfectly calm since then — and there was no way to tell by looking. A warning
    # that never turns off stops being a warning: you get used to seeing it and the
    # day it is real it tells you nothing.
    "last_wait_at": None,  # epoch of the last queueing
    "max_wait_at": None,   # epoch of the worst case
    # v7.171.0 — WHO holds the lock, not just how long one waits.
    #
    # Until here the telemetry counted the WAIT: "writes waited 15 s". True, and
    # actionable for nobody. The wait is the shadow; the fact is that some writer held
    # the lock those 15 s, and the only place where it is known which one is in here.
    # Without this, diagnosing contention required guessing by reading the code —which
    # was done three times— instead of looking at a log.
    "hold_ms_max": 0.0,     # the longest hold since boot
    "hold_max_at": None,    # epoch of that hold
    "hold_max_owner": None, # "file.py:line (function)" that caused it
    "slow_holds": 0,        # how many times it went over the threshold (LENTO_MS)
}

# A write that holds the lock longer than this leaves a trace in the log: every other
# writer in the process is waiting behind it during that time.
LENTO_MS = 2000.0


def _quien_escribe() -> str:
    """The call site that is going to hold the lock, in one line.

    It walks up the stack to the first frame of APP code. Skipping only this module is
    not enough, and it showed in the first real measurement: `app_write` is an
    `asynccontextmanager`, so the frame above is always `contextlib.py (__aenter__)` — a
    headline that points at nobody. Frames of the standard library and of the
    dependencies (`site-packages`) are ignored too.

    Cheap on purpose (it reads frame attributes, it does not format tracebacks) because it
    runs on EVERY write.
    """
    try:
        f = sys._getframe(1)
        for _ in range(20):
            if f is None:
                break
            fichero = f.f_code.co_filename
            interesante = (
                fichero != __file__
                and "/contextlib.py" not in fichero
                and "site-packages" not in fichero
                and "/lib/python" not in fichero
            )
            if interesante:
                nombre = fichero.rsplit("/", 2)[-2:]
                return f"{'/'.join(nombre)}:{f.f_lineno} ({f.f_code.co_name})"
            f = f.f_back
    except Exception:  # noqa: BLE001 — telemetry never breaks a write
        pass
    return "?"


def write_contention_stats() -> dict:
    """Snapshot of write-queue pressure, for the health/diagnostics surface."""
    waits = _contention["waits"]
    try:
        waiters = len(getattr(write_lock(), "_waiters", None) or ())
    except RuntimeError:  # called from outside a running loop
        waiters = 0
    import time as _t
    ahora = _t.time()

    def _hace(ts):
        return round(ahora - ts, 1) if ts else None

    return {
        **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in _contention.items()},
        "wait_ms_avg": round(_contention["wait_ms_total"] / waits, 1) if waits else 0.0,
        # Who held the lock longest and when: it is the ONLY thing in this block that
        # can be acted on (the others' wait is its consequence).
        "hold_max_seconds_ago": _hace(_contention["hold_max_at"]),
        # Present: how many writes are queued NOW. It is the only thing that justifies
        # a warning in the present tense.
        "queue_waiting": waiters,
        # Past, dated: without this the historical maximum reads as if it were
        # happening now.
        "last_wait_seconds_ago": _hace(_contention["last_wait_at"]),
        "max_wait_seconds_ago": _hace(_contention["max_wait_at"]),
    }


def note_locked_error() -> None:
    """Record a 'database is locked' that escaped to a caller (a real symptom)."""
    _contention["locked_errors"] += 1


@contextlib.asynccontextmanager
async def _write_slot():
    """Take the process-wide write slot, accounting for the queueing AND the hold.

    Measuring only the wait describes the symptom and hides the cause: if a write holds
    the lock for 15 s, the others wait 15 s and the warning says "there is contention"
    without saying whose. Here it also measures how long it is held and WHO did it, and a
    long hold leaves a WARNING with the exact site — which is what turns contention into
    something someone can fix.
    """
    import time
    lock = write_lock()
    if lock.locked():
        _contention["waits"] += 1
        t0 = time.monotonic()
        await lock.acquire()
        waited = (time.monotonic() - t0) * 1000.0
        _contention["wait_ms_total"] += waited
        _contention["last_wait_at"] = time.time()
        if waited > _contention["wait_ms_max"]:
            _contention["wait_ms_max"] = waited
            _contention["max_wait_at"] = _contention["last_wait_at"]
    else:
        await lock.acquire()
    token = _in_write.set(True)
    quien = _quien_escribe()
    t_hold = time.monotonic()
    try:
        yield
    finally:
        held = (time.monotonic() - t_hold) * 1000.0
        _in_write.reset(token)
        lock.release()
        if held > _contention["hold_ms_max"]:
            _contention["hold_ms_max"] = held
            _contention["hold_max_at"] = time.time()
            _contention["hold_max_owner"] = quien
        if held >= LENTO_MS:
            _contention["slow_holds"] += 1
            # Honest severity: this is NOT an error (nothing has failed), it is a write
            # that made all the others wait. WARNING with the site inside, so the log
            # classifier groups it by author.
            logger.warning(
                "write lock held %.1fs by %s — every other writer queued behind it",
                held / 1000.0, quien,
            )


@contextlib.asynccontextmanager
async def app_write():
    """Serialized WRITE session — use for ANY code path that writes to the
    local SQLite (INSERT/UPDATE/DELETE + commit). Only one write transaction
    runs process-wide at a time, so SQLite never sees concurrent writers and
    'database is locked' cannot happen on serialized paths.

    Reads MUST keep using ``app_session``/``get_app_session`` (no lock) so
    they stay concurrent under WAL. Keep the ``async with app_write()`` block
    to the DB work only — do slow I/O (network, file downloads, indexing)
    BEFORE/AFTER it, never inside, or you serialize every other writer behind
    that I/O.
    """
    if _in_write.get():
        # Already serialized on this task — reuse the slot, just open a session.
        async with app_session() as s:
            yield s
        return
    async with _write_slot():
        async with app_session() as s:
            yield s


@contextlib.asynccontextmanager
async def app_write_conn():
    """Same queue, but yields a raw CONNECTION with ``BEGIN IMMEDIATE``.

    For the write paths that can't use the ORM session: today the event claim
    (``UPDATE … RETURNING`` driven by 8 workers — the loudest writer in the app,
    and the one whose churn starved everybody else in INC-VIG-59). Sharing the
    lock with ``app_write`` is the whole point: two queues would not serialize
    against each other and SQLite would see concurrent writers again.
    """
    if _in_write.get():
        async with app_engine.connect() as conn:
            yield conn
        return
    async with _write_slot():
        async with app_engine.connect() as conn:
            if conn.dialect.name == "sqlite":
                try:
                    # We hold the slot, so nobody else is writing: taking the
                    # RESERVED lock up front just avoids an upgrade mid-statement.
                    await conn.exec_driver_sql("BEGIN IMMEDIATE")
                except Exception:
                    # SQLAlchemy already auto-began. The statement is atomic anyway.
                    pass
            yield conn


class WriteBusy(RuntimeError):
    """A write that MUST NOT be lost could not get through.

    Raised by ``write_with_retry`` after exhausting its attempts. Callers turn it
    into an honest "couldn't save, try again" (503) instead of a raw 500 — the
    difference between telling the operator the truth and showing them a crash.
    """


async def queued_commit(session) -> None:
    """Commits a READ session taking the write turn only for that.

    ⚠️ IT DOES NOT SOLVE the watcher tick's contention, and the attempt is documented
    here so nobody repeats it. **SQLite's lock is not taken at `commit`: it is taken at
    the FIRST write of the transaction.** The tick's session executes INSERT/UPDATE long
    before committing, so by the time it gets here the file is already taken — wrapping
    the commit does not put those writes in the queue, and on top of that it adds one
    more take of the asyncio slot at the end.

    Measured in `demo` when trying it: the Kommo employee's maximum wait went from
    ~15 s to **28 s and rising**, with the queue growing live. It was reverted.

    What would really be needed: that the WRITES of each unit of the tick happen inside
    the turn, not just their commit — or that the tick uses short sessions per unit
    instead of one spanning everything. It is a change in the watcher's shape, not a
    wrapper.

    The function is kept because it is correct for what it says it is (commit a session
    taking the turn) and may serve a path that DOES write only at the end; but it is not
    used from the tick.
    """
    async with _write_slot():
        await session.commit()


async def write_with_retry(op, *, attempts: int = 4, base_delay: float = 0.15,
                           what: str = "la escritura"):
    """Run ``op`` (an async callable that writes) retrying only on 'locked'.

    Belt and braces for writes where losing is not acceptable — the autonomy kill
    switch above all. Joining the fair queue already prevents the failure mode;
    this covers the residual (another process on the same volume, a checkpoint
    stall) so the operator never eats a 500 on a governance action. Only the
    'database is locked' family is retried: any other OperationalError is a real
    error and must surface unchanged.
    """
    import asyncio as _asyncio
    import random
    from sqlalchemy.exc import OperationalError

    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except OperationalError as err:
            if "locked" not in str(err).lower() and "busy" not in str(err).lower():
                raise
            note_locked_error()
            if attempt == attempts:
                raise WriteBusy(what) from err
            _contention["retries"] += 1
            # Jittered backoff: without jitter, N retriers re-collide in lockstep.
            await _asyncio.sleep(base_delay * attempt * (1.0 + random.random()))
