"""Approval and URL checks for operations beyond the filesystem/process services."""

from __future__ import annotations

import hashlib
import time
import urllib.request
from urllib.parse import urlsplit

from pepsicode.execution.runtime import current_scope, current_tool_context


def authorize(context, operation: str, target: str, *, mutating: bool):
    scope = (getattr(context, "execution", None) if context is not None else None) or current_scope()
    if scope:
        scope.check_cancelled()
    permissions = (context.permissions if context is not None else None) or (scope.permissions if scope else None)
    if mutating:
        if scope and scope.has_unknown:
            raise PermissionError("Resolve the previous unknown operation before external mutations")
        if permissions is None:
            raise PermissionError("External mutation requires an explicit approval provider")
        mode = permissions.mode
        outcome = permissions.check_external_action(operation, target)
        if outcome.is_denied or outcome.is_unavailable:
            raise PermissionError(outcome.denial_message(scope=operation))
        if permissions.mode != mode:
            raise PermissionError("Permissions changed during external action approval")
        if scope:
            scope.check_cancelled()
    if scope:
        scope.record("external/prepared", operation=operation, target=target, mutating=mutating)


def urlopen(request, *, context=None, timeout=30, validate_url=None):
    context = context or current_tool_context()
    target = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    method = request.get_method() if isinstance(request, urllib.request.Request) else "GET"
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("External requests require an HTTP(S) URL without embedded credentials")
    if validate_url is not None:
        allowed, reason = validate_url(target)
        if not allowed:
            raise PermissionError(reason)
    mutation = method not in {"GET", "HEAD", "OPTIONS"}
    details = target
    if mutation:
        body = (request.data or b"") if isinstance(request, urllib.request.Request) else b""
        details += f"\nbody SHA256: {hashlib.sha256(body).hexdigest()}\n{body[:2000].decode('utf-8', errors='replace')}"
    authorize(
        context,
        f"HTTP {method}",
        details,
        mutating=mutation,
    )

    class CheckedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = urlsplit(newurl)
            if new.scheme not in {"http", "https"} or new.username or new.password:
                raise PermissionError("Unsafe redirect refused")
            if validate_url is not None:
                allowed, reason = validate_url(newurl)
                if not allowed:
                    raise PermissionError(reason)
            # A mutation grant applies to one origin and path, not another host.
            if mutation and (new.scheme, new.netloc, new.path, new.query) != (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
            ):
                raise PermissionError("Redirect of an approved external mutation refused")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    if context is not None and method not in {"GET", "HEAD", "OPTIONS"}:
        context.effect_state = "unknown"
    scope = getattr(context, "execution", None) or current_scope()
    if scope:
        scope.check_cancelled()
        if scope.deadline is not None:
            timeout = min(timeout, max(0.01, scope.deadline - time.monotonic()))
    response = urllib.request.build_opener(CheckedRedirect()).open(request, timeout=timeout)
    if context is not None and not mutation:
        context.effect_state = "completed"
    return response
