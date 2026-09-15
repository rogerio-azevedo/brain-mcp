#!/usr/bin/env python3
"""End-to-end smoke test for a running obsidian-mcp HTTP server."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastmcp import Client

CREATED_CONTENT = "# MCP smoke test\n\ncreated over real HTTP\n"
UPDATED_CONTENT = "# MCP smoke test\n\nupdated over real HTTP\n"
DRY_RUN_CONTENT = "# MCP smoke test\n\nthis preview must never be written\n"
# A revision is a hash of the bytes, so a pinned write has to change them to
# produce a new one; it keeps the updated body as a substring for the on-disk
# assertion at the end.
PINNED_CONTENT = "# MCP smoke test\n\nupdated over real HTTP\n\npinned with expected_revision\n"
EXPECTED_CREATED_CONTENT = "# MCP smoke test\n\ncreated over real HTTP"
EXPECTED_UPDATED_CONTENT = "# MCP smoke test\n\nupdated over real HTTP"
STALE_REVISION = "sha256:" + "0" * 64


def _result_value(result: Any) -> Any:
    if result.data is not None:
        return result.data
    if result.structured_content is not None:
        value = result.structured_content.get("result", result.structured_content)
        return value
    if len(result.content) == 1 and hasattr(result.content[0], "text"):
        return json.loads(result.content[0].text)
    raise RuntimeError(f"Unexpected MCP response: {result!r}")


def _structured(result: Any) -> Any:
    if result.is_error:
        text = "\n".join(getattr(item, "text", repr(item)) for item in result.content)
        raise RuntimeError(text)
    return _result_value(result)


def _envelope(result: Any, description: str) -> dict:
    """Unwrap a tool result and assert it carries the canonical envelope."""
    value = _structured(result)
    if not isinstance(value, dict):
        raise RuntimeError(f"{description} did not return an object: {value!r}")
    if value.get("success") is not True:
        raise RuntimeError(f"{description} did not report success: {value!r}")
    if "data" not in value:
        raise RuntimeError(f"{description} has no data payload: {value!r}")
    if "status" in value:
        raise RuntimeError(f"{description} still returns a legacy status: {value!r}")
    return value


def _require_rejection(result: Any, description: str) -> str:
    text = "\n".join(getattr(item, "text", repr(item)) for item in result.content)
    if result.is_error:
        return text
    value = _result_value(result)
    if isinstance(value, dict) and value.get("error"):
        return json.dumps(value, sort_keys=True)
    raise RuntimeError(f"{description} unexpectedly succeeded: {result!r}")


def _require_revision_conflict(result: Any, description: str) -> str:
    """A stale expected_revision must fail distinguishably, not overwrite."""
    if not result.is_error:
        raise RuntimeError(f"{description} unexpectedly succeeded: {result!r}")
    payload = result.structured_content or {}
    text = "\n".join(getattr(item, "text", repr(item)) for item in result.content)
    error = payload.get("error") if isinstance(payload, dict) else None
    if error != "revision_conflict" and "revision_conflict" not in text:
        raise RuntimeError(
            f"{description} failed, but not as a distinguishable revision conflict: "
            f"{payload or text!r}"
        )
    return error or "revision_conflict"


async def run(args: argparse.Namespace) -> None:
    async with Client(args.url, auth=args.api_key) as client:
        tools = {tool.name for tool in await client.list_tools()}
        required = {
            "list_notes_tool",
            "read_note_tool",
            "write_note_tool",
            "patch_frontmatter_tool",
        }
        missing = required - tools
        if missing:
            raise RuntimeError(f"Missing required tools: {sorted(missing)}")
        # The consolidation removed these; a server still advertising one is stale.
        retired = {
            "render_note_tool",
            "get_note_outline_tool",
            "get_tag_tree_tool",
            "get_notes_by_tag_tool",
            "get_daily_note_tool",
            "get_note_history_tool",
            "patch_frontmatter_batch_tool",
            "delete_note_tool",
            "delete_folder_tool",
            "restore_note_tool",
            "restore_folder_tool",
        } & tools
        if retired:
            raise RuntimeError(f"Server still advertises removed tools: {sorted(retired)}")

        created = _envelope(
            await client.call_tool(
                "write_note_tool",
                {"path": args.note, "content": CREATED_CONTENT},
            ),
            "write_note_tool (create)",
        )
        if not created.get("revision", "").startswith("sha256:"):
            raise RuntimeError(f"Create did not return a revision: {created!r}")

        first_read = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note}),
            "read_note_tool (full)",
        )
        if first_read["data"].get("content") != EXPECTED_CREATED_CONTENT:
            raise RuntimeError("Created note did not round-trip through read_note_tool")

        updated = _envelope(
            await client.call_tool(
                "write_note_tool",
                {"path": args.note, "content": UPDATED_CONTENT},
            ),
            "write_note_tool (update)",
        )
        final_read = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note}),
            "read_note_tool (full, after update)",
        )
        if final_read["data"].get("content") != EXPECTED_UPDATED_CONTENT:
            raise RuntimeError("Updated note did not round-trip through read_note_tool")
        current_revision = final_read.get("revision", "")
        if not current_revision.startswith("sha256:"):
            raise RuntimeError(f"Read did not return a revision: {final_read!r}")

        # ── all three read modes ──
        outline = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note, "mode": "outline"}),
            "read_note_tool (outline)",
        )
        if "headings" not in outline["data"]:
            raise RuntimeError(f"Outline mode returned no headings: {outline!r}")
        if "content" in outline["data"]:
            raise RuntimeError(f"Outline mode leaked body text: {outline!r}")

        rendered = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note, "mode": "rendered"}),
            "read_note_tool (rendered)",
        )
        if EXPECTED_UPDATED_CONTENT not in rendered["data"].get("rendered", ""):
            raise RuntimeError(f"Rendered mode did not return the note: {rendered!r}")

        # ── a dry-run write must preview and change nothing ──
        preview = _envelope(
            await client.call_tool(
                "write_note_tool",
                {"path": args.note, "content": DRY_RUN_CONTENT, "dry_run": True},
            ),
            "write_note_tool (dry_run)",
        )
        if preview.get("meta", {}).get("dry_run") is not True:
            raise RuntimeError(f"Dry run did not flag itself: {preview!r}")
        if "diff" not in preview["data"]:
            raise RuntimeError(f"Dry run returned no diff: {preview!r}")
        after_dry_run = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note}),
            "read_note_tool (after dry_run)",
        )
        if after_dry_run["data"].get("content") != EXPECTED_UPDATED_CONTENT:
            raise RuntimeError("Dry run wrote to the note")

        # ── a batch frontmatter patch with one deliberate failure ──
        absent = f"AI-Memory/mcp-smoke-test-absent-{uuid4().hex}.md"
        batch = _envelope(
            await client.call_tool(
                "patch_frontmatter_tool",
                {"paths": [args.note, absent], "updates": {"status": "active"}},
            ),
            "patch_frontmatter_tool (batch)",
        )
        summary = batch["data"].get("summary", {})
        results = batch["data"].get("results", [])
        if summary.get("total") != 2 or summary.get("succeeded") != 1 or summary.get("failed") != 1:
            raise RuntimeError(f"Batch summary is not 1 ok / 1 failed: {summary!r}")
        if len(results) != 2:
            raise RuntimeError(f"Batch returned {len(results)} items, expected 2: {results!r}")
        good = next((item for item in results if item.get("success")), None)
        bad = next((item for item in results if not item.get("success")), None)
        if good is None or not str(good.get("revision", "")).startswith("sha256:"):
            raise RuntimeError(f"Batch success item carries no revision: {good!r}")
        if bad is None or bad.get("path") != absent or not bad.get("error", {}).get("type"):
            raise RuntimeError(f"Batch failure item is not shaped as expected: {bad!r}")

        # ── a stale expected_revision must conflict, not overwrite ──
        conflict = await client.call_tool(
            "write_note_tool",
            {
                "path": args.note,
                "content": "this stale write must be rejected\n",
                "expected_revision": STALE_REVISION,
            },
            raise_on_error=False,
        )
        conflict_kind = _require_revision_conflict(conflict, "Stale-revision write")

        # ── a fresh expected_revision must still be accepted ──
        fresh_read = _envelope(
            await client.call_tool("read_note_tool", {"path": args.note}),
            "read_note_tool (before pinned write)",
        )
        pinned = _envelope(
            await client.call_tool(
                "write_note_tool",
                {
                    "path": args.note,
                    "content": PINNED_CONTENT,
                    "expected_revision": fresh_read["revision"],
                },
            ),
            "write_note_tool (pinned)",
        )
        if pinned["revision"] == fresh_read["revision"]:
            raise RuntimeError("Pinned write did not produce a new revision")

        denied_write = "not_requested"
        if args.denied_note:
            denied_result = await client.call_tool(
                "write_note_tool",
                {
                    "path": args.denied_note,
                    "content": "this must not be written\n",
                },
                raise_on_error=False,
            )
            _require_rejection(denied_result, "Denied write")
            denied_write = "access_denied"

        if args.vault_path:
            disk_path = args.vault_path / args.note
            on_disk = disk_path.read_text(encoding="utf-8")
            if EXPECTED_UPDATED_CONTENT not in on_disk:
                raise RuntimeError(f"On-disk content differs at {disk_path}")
            if DRY_RUN_CONTENT.strip() in on_disk:
                raise RuntimeError(f"Dry-run content reached disk at {disk_path}")
            if "stale write" in on_disk:
                raise RuntimeError(f"Stale-revision write reached disk at {disk_path}")
            if args.denied_note and (args.vault_path / args.denied_note).exists():
                raise RuntimeError("Denied note was unexpectedly created on disk")
            if (args.vault_path / absent).exists():
                raise RuntimeError("Failed batch entry was unexpectedly created on disk")

        print(
            json.dumps(
                {
                    "status": "ok",
                    "tools_advertised": len(tools),
                    "create_action": created.get("meta", {}).get("action"),
                    "update_action": updated.get("meta", {}).get("action"),
                    "read_modes": ["full", "outline", "rendered"],
                    "dry_run": "previewed_without_writing",
                    "batch_summary": summary,
                    "stale_revision": conflict_kind,
                    "denied_write": denied_write,
                    "note": args.note,
                },
                indent=2,
                sort_keys=True,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--note")
    parser.add_argument(
        "--denied-note",
        help="Optional caller-supplied path known to be outside the server write scope",
    )
    parser.add_argument("--vault-path", type=Path)
    args = parser.parse_args()
    args.api_key = os.environ.get("OBSIDIAN_MCP_API_KEY")
    if not args.api_key:
        args.api_key = getpass.getpass("Obsidian MCP API key: ")
    args.note = args.note or f"AI-Memory/mcp-smoke-test-{uuid4().hex}.md"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
