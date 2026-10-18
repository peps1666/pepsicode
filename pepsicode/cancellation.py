"""Cooperative cancellation shared by a turn, its model calls and tools."""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_current_event: ContextVar[threading.Event | None] = ContextVar("turn_cancellation", default=None)


class TurnCancelledError(Exception):
    """The user stopped the current turn."""


def cancellation_event() -> threading.Event | None:
    return _current_event.get()


def raise_if_cancelled() -> None:
    event = cancellation_event()
    if event is not None and event.is_set():
        raise TurnCancelledError("Task stopped by user.")


def cancellable_turn(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        event = kwargs.get("cancellation_event") or cancellation_event()
        token = _current_event.set(event)
        try:
            return function(*args, **kwargs)
        finally:
            _current_event.reset(token)

    return wrapped


@contextmanager
def cancellable_response(response):
    """Interrupt a blocked HTTP body read when a turn is stopped.

    Opening a connection still uses urllib's connection timeout. Once headers
    arrive, shutdown wakes a blocked socket read before closing the response.
    """
    event = cancellation_event()
    finished = threading.Event()

    def interrupt():
        while not finished.wait(0.05):
            if event.is_set():
                raw = getattr(getattr(response, "fp", None), "raw", None)
                connection = getattr(raw, "_sock", None)
                if connection is not None:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                try:
                    response.close()
                except OSError:
                    pass
                return

    watcher = None
    if event is not None:
        watcher = threading.Thread(target=interrupt, daemon=True)
        watcher.start()
    try:
        raise_if_cancelled()
        yield response
        raise_if_cancelled()
    except Exception:
        raise_if_cancelled()
        raise
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=0.2)
        try:
            response.close()
        except OSError:
            pass
