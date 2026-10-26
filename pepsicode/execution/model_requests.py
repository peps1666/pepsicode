"""Persist the provider-facing request, including ephemeral context and tool schemas."""

from __future__ import annotations

import json
import time
import urllib.error
import uuid
from contextlib import nullcontext
from functools import wraps
from urllib.parse import urlsplit

from pepsicode.execution.runtime import current_scope

_REJECTED = {400, 401, 403, 404, 413, 422, 429, 529}


def model_timeout(default):
    scope = current_scope()
    if scope:
        scope.check_cancelled()
        if scope.deadline is not None:
            return min(default, max(0.01, scope.deadline - time.monotonic()))
    return default


def _begin(scope, request):
    body = json.loads(request.data)
    request_id = uuid.uuid4().hex
    url = urlsplit(request.full_url)
    scope.record("model/prepared", request_id=request_id, endpoint=f"{url.scheme}://{url.netloc}{url.path}", body=body)
    return request_id, body


def recorded_request(function):
    @wraps(function)
    def wrapped(self, request):
        scope = current_scope()
        if scope is None:
            return function(self, request)
        self._usage_recorded = False
        body = json.loads(request.data)
        gate = scope.budget.request(scope, body) if scope.budget else nullcontext()
        with gate:
            request_id, body = _begin(scope, request)
            try:
                data, status = function(self, request)
                from pepsicode.llm.anthropic_adapter import _normalize_usage

                raw_usage = data.get("usage") if isinstance(data, dict) else None
                usage = (
                    _normalize_usage(raw_usage)
                    if isinstance(raw_usage, dict) and "input_tokens" in raw_usage and "output_tokens" in raw_usage
                    else None
                )
                if usage is None and status in _REJECTED:
                    usage = {"input_tokens": 0, "output_tokens": 0}
                if scope.budget:
                    scope.budget.record(body["model"], usage)
                    self._usage_recorded = True
                scope.record("model/settled", request_id=request_id, status=status, usage=usage)
                return data, status
            except BaseException as error:
                if scope.budget and not self._usage_recorded:
                    scope.budget.record(body["model"], None)
                scope.record("model/failed", request_id=request_id, error_type=type(error).__name__)
                raise

    return wrapped


def recorded_stream(function):
    @wraps(function)
    def wrapped(self, request):
        scope = current_scope()
        if scope is None:
            yield from function(self, request)
            return
        self._usage_recorded = False
        gate = scope.budget.request(scope, json.loads(request.data)) if scope.budget else nullcontext()
        with gate:
            request_id, body = _begin(scope, request)
            succeeded = False
            failure = None
            try:
                for token in function(self, request):
                    # Consumers close at the terminal token rather than
                    # resuming the generator once more to observe StopIteration.
                    if token.type == "done":
                        succeeded = token.stop_reason != "stream_incomplete"
                    yield token
            except BaseException as error:
                failure = error
                raise
            finally:
                usage = self.last_usage
                rejected = isinstance(failure, urllib.error.HTTPError) and failure.code in _REJECTED
                if rejected and usage is None:
                    usage = {"input_tokens": 0, "output_tokens": 0}
                if scope.budget:
                    scope.budget.record(body["model"], usage)
                    if not succeeded and not rejected:
                        scope.budget.unmetered = True
                    self._usage_recorded = True
                scope.record("model/settled" if succeeded else "model/failed", request_id=request_id, usage=usage)

    return wrapped
