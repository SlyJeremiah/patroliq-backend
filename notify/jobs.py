"""
Background delivery (spec v1.6 §1): provider calls never block or fail the phone's request.

:func:`dispatch` registers the job with ``transaction.on_commit`` so nothing is sent for a request
whose writes roll back, then hands it to a small process-wide thread pool (``NOTIFY_WORKERS``,
default 2). Everything the job needs (recipients, rendered text) is prepared by the caller *inside*
the request, where the tenant context for PostgreSQL RLS is set; the job itself only talks to the
provider and writes ``NotificationLog`` rows (inside ``tenant_context``).

``NOTIFY_ASYNC=false`` (the test settings) runs the job inline, immediately, in the calling thread.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection, transaction

logger = logging.getLogger("patroliq.notify")

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _pool() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=max(1, int(getattr(settings, "NOTIFY_WORKERS", 2))),
                                           thread_name_prefix="patroliq-notify")
        return _executor


def _run(fn, args, kwargs, close_connection: bool) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — a delivery job must never take anything else down
        logger.exception("notification job %s failed", getattr(fn, "__name__", fn))
    finally:
        if close_connection:
            connection.close()  # this worker thread's own DB connection


def dispatch(fn, *args, **kwargs) -> None:
    """Run ``fn(*args, **kwargs)`` after the current transaction commits, on the notify thread pool."""
    if not getattr(settings, "NOTIFY_ASYNC", True):
        _run(fn, args, kwargs, close_connection=False)
        return

    def submit():
        try:
            _pool().submit(_run, fn, args, kwargs, True)
        except RuntimeError:  # interpreter shutting down: deliver inline rather than drop an SOS
            _run(fn, args, kwargs, close_connection=False)

    transaction.on_commit(submit, robust=True)
