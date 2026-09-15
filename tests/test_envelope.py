"""The canonical response envelope, and the tools that build it."""

from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server
from obsidian_mcp.envelope import (
    batch_error,
    batch_item,
    batch_result,
    batch_summary,
    list_result,
    read_result,
    write_result,
)


@pytest.fixture
def served_vault(vault_factory, monkeypatch):
    """A vault whose index is registered with the server, so the module-level
    `_index` proxy resolves for tools that update it after a write."""
    def _make(files: dict[str, str] | None = None):
        index = vault_factory(files)
        monkeypatch.setattr(
            server, "_indices", {cfg_mod.get_config().default_vault_name: index}
        )
        return index

    return _make


# ── constructors ──────────────────────────────────────────────────────────

def test_read_result_promotes_path_and_revision():
    result = read_result("a.md", {"content": "hi"}, revision="sha256:" + "ab" * 32)
    assert result == {
        "success": True,
        "path": "a.md",
        "revision": "sha256:" + "ab" * 32,
        "data": {"content": "hi"},
    }


def test_read_result_omits_path_for_vault_wide_reads():
    result = read_result(None, {"notes": 3})
    assert "path" not in result
    assert result == {"success": True, "data": {"notes": 3}}


def test_read_result_omits_revision_when_absent():
    assert "revision" not in read_result("a.md", {})


def test_list_result_counts_items_and_merges_meta():
    result = list_result(["a.md", "b.md"], meta={"truncated": True})
    assert result == {
        "success": True,
        "data": {"items": ["a.md", "b.md"]},
        "meta": {"count": 2, "truncated": True},
    }
    assert "path" not in result


def test_list_result_of_nothing_still_reports_a_count():
    assert list_result([])["meta"]["count"] == 0


def test_write_result_puts_the_verb_in_meta_not_at_the_top_level():
    result = write_result("a.md", "written", revision="sha256:x", data={"diff": "-"})
    assert result == {
        "success": True,
        "path": "a.md",
        "revision": "sha256:x",
        "data": {"diff": "-"},
        "meta": {"action": "written"},
    }
    assert "status" not in result


def test_batch_result_reports_partial_failure_per_item():
    items = [batch_item("a.md", revision="sha256:x"), batch_error("b.md", KeyError("nope"))]
    result = batch_result(items, batch_summary(items))
    assert result["success"] is True, "the batch itself ran; per-item state is in data.results"
    assert result["data"]["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert result["data"]["results"][0] == {
        "success": True,
        "path": "a.md",
        "revision": "sha256:x",
    }
    assert result["data"]["results"][1] == {
        "success": False,
        "path": "b.md",
        "error": {"type": "KeyError", "message": "'nope'"},
    }


@pytest.mark.parametrize(
    "result",
    [
        read_result("a.md", {}),
        read_result(None, {}),
        list_result([]),
        write_result("a.md", "written"),
        batch_result([], {}),
    ],
)
def test_every_envelope_is_an_object_with_success_and_data(result):
    assert isinstance(result, dict)
    assert isinstance(result["success"], bool)
    assert isinstance(result["data"], dict)
    assert "status" not in result


# ── the two shape fixes this phase makes ──────────────────────────────────

def test_rendered_read_returns_an_object_not_a_bare_string(vault_factory):
    vault_factory({
        "target.md": "Embedded body",
        "note.md": "Before\n![[target]]\nAfter",
    })
    result = server.read_note_tool("note.md", mode="rendered")
    assert result["success"] is True
    assert result["path"] == "note.md"
    assert "Embedded body" in result["data"]["rendered"]


def test_patch_note_tool_supports_dry_run(tmp_path, served_vault):
    served_vault({"note.md": "## Section\nold body\n"})
    result = server.patch_note_tool("note.md", "Section", "new body", dry_run=True)

    assert result["success"] is True
    assert result["meta"]["dry_run"] is True
    assert "new body" in result["data"]["preview"]
    assert "new body" in result["data"]["diff"]
    # Nothing was written.
    assert (tmp_path / "note.md").read_text() == "## Section\nold body\n"


def test_patch_note_tool_write_reports_a_revision(served_vault):
    served_vault({"note.md": "## Section\nold body\n"})
    result = server.patch_note_tool("note.md", "Section", "new body")

    assert result["meta"]["action"] == "patched"
    assert "dry_run" not in result["meta"]
    assert result["revision"].startswith("sha256:")


def test_read_then_write_round_trips_the_revision(served_vault):
    served_vault({"note.md": "body\n"})
    read = server.read_note_tool("note.md")
    written = server.write_note_tool(
        "note.md", "new body\n", expected_revision=read["revision"]
    )
    assert written["success"] is True
    assert written["revision"] != read["revision"]
