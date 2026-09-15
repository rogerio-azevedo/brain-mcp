"""Canonical response envelope shared by every MCP tool.

Every tool returns a JSON **object** built through one of the constructors
below — never a bare list, string, or hand-rolled dict. That gives clients a
single shape to parse regardless of which tool they called:

    {
      "success": true,
      "path": "02-Areas/monari/ticket-123.md",
      "revision": "sha256:ab12…",
      "data": {...},      # tool-specific payload
      "meta": {...}       # action, dry_run, count, truncated, …
    }

Rules
-----
* ``success`` is always present and always a boolean — never a ``status`` string.
* ``path`` is present for every single-item result and omitted for vault-wide
  and list results, which are not addressed by one path.
* ``revision`` is present on every read and every successful write of a single
  file, and is the opaque token to feed back as ``expected_revision`` for
  compare-and-swap (see :mod:`obsidian_mcp.storage.revisions`).
* ``data`` is always present so clients can index into it unconditionally.
* ``meta`` is present only when there is something to say. The operation verb
  of a write lives in ``meta.action`` — operation metadata, not payload.

Hard failures are *not* expressed here. They stay on the MCP exception channel
(``VaultPathError``, ``WritePermissionError``, ``RevisionConflictError``, …).
The per-item ``success``/``error`` fields of :func:`batch_result` are the one
exception: a batch reports partial failure in-band, because some of its items
may have succeeded.
"""

from __future__ import annotations

from typing import Any


def _envelope(
    success: bool,
    *,
    path: str | None = None,
    revision: str | None = None,
    data: Any = None,
    meta: dict | None = None,
) -> dict:
    result: dict[str, Any] = {"success": success}
    if path is not None:
        result["path"] = path
    if revision is not None:
        result["revision"] = revision
    result["data"] = {} if data is None else data
    if meta:
        result["meta"] = meta
    return result


def read_result(
    path: str | None,
    data: Any,
    revision: str | None = None,
    meta: dict | None = None,
) -> dict:
    """A successful read of one item.

    ``path`` may be ``None`` for a vault-wide read (statistics, conventions,
    a tag tree) that no single path addresses.
    """
    return _envelope(True, path=path, revision=revision, data=data, meta=meta)


def list_result(items: list, meta: dict | None = None) -> dict:
    """A successful listing. ``meta.count`` is always filled in.

    Callers add ``truncated`` (and anything else) through ``meta``; an
    explicitly passed ``count`` wins over the derived one.
    """
    return _envelope(True, data={"items": items}, meta={"count": len(items), **(meta or {})})


def write_result(
    path: str,
    action: str,
    revision: str | None = None,
    data: Any = None,
    meta: dict | None = None,
) -> dict:
    """A successful mutation of one item.

    ``action`` is the verb that was performed (``"written"``, ``"patched"``,
    ``"deleted"``, …) and is surfaced as ``meta.action``. ``revision`` is the
    committed revision, and is omitted only when the tool wrote nothing (a
    dry run) or the target no longer exists (a delete).
    """
    return _envelope(
        True,
        path=path,
        revision=revision,
        data=data,
        meta={"action": action, **(meta or {})},
    )


def batch_result(results: list[dict], summary: dict) -> dict:
    """A batch of same-shape mutations, some of which may have failed.

    ``success`` is ``True`` whenever the batch itself ran to completion — it
    is not an error flag. Per-item outcomes live in ``data.results`` (built
    with :func:`batch_item` / :func:`batch_error`) and the tallies in
    ``data.summary``.
    """
    return _envelope(True, data={"summary": summary, "results": results})


def batch_item(path: str, revision: str | None = None, data: Any = None) -> dict:
    """One successful entry inside :func:`batch_result`."""
    item: dict[str, Any] = {"success": True, "path": path}
    if revision is not None:
        item["revision"] = revision
    if data is not None:
        item["data"] = data
    return item


def batch_error(path: str, exc: BaseException) -> dict:
    """One failed entry inside :func:`batch_result`.

    ``error.type`` is the raised exception's class name, so a client can
    branch on it the same way it would on a hard MCP error.
    """
    return {
        "success": False,
        "path": path,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def batch_summary(results: list[dict]) -> dict:
    """Tally ``total``/``succeeded``/``failed`` over :func:`batch_result` items."""
    succeeded = sum(1 for item in results if item.get("success"))
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
    }
