"""The consolidated tool surface: merged read/tag tools, batch and delete
dispatch, and the guarantee that nothing advertises a tool that isn't there."""

from __future__ import annotations

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp import server


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


# ── read_note_tool(mode=...) ──────────────────────────────────────────────

_NOTE = """---
status: active
tags: [alpha]
---
Body text here.

## Section One
more body ^block-a
"""


def test_read_note_full_mode_returns_body_and_revision(vault_factory):
    vault_factory({"note.md": _NOTE})
    result = server.read_note_tool("note.md")

    assert result["meta"]["mode"] == "full"
    assert result["path"] == "note.md"
    assert result["revision"].startswith("sha256:")
    assert "Body text here." in result["data"]["content"]
    assert result["data"]["frontmatter"]["status"] == "active"


def test_read_note_outline_mode_omits_body_but_keeps_structure(vault_factory):
    vault_factory({"note.md": _NOTE})
    result = server.read_note_tool("note.md", mode="outline")

    assert result["meta"]["mode"] == "outline"
    assert result["revision"].startswith("sha256:")
    assert "content" not in result["data"], "outline must not carry body text"
    assert [h["text"] for h in result["data"]["headings"]] == ["Section One"]
    assert result["data"]["frontmatter_keys"] == ["status", "tags"]
    assert result["data"]["line_count"] > 0


def test_read_note_rendered_mode_resolves_embeds(vault_factory):
    vault_factory({
        "target.md": "Embedded body",
        "note.md": "Before\n![[target]]\nAfter",
    })
    result = server.read_note_tool("note.md", mode="rendered")

    assert result["meta"] == {"mode": "rendered", "depth": 1}
    assert "Embedded body" in result["data"]["rendered"]


def test_read_note_rendered_depth_zero_leaves_embeds_alone(vault_factory):
    vault_factory({
        "target.md": "Embedded body",
        "note.md": "Before\n![[target]]\nAfter",
    })
    result = server.read_note_tool("note.md", mode="rendered", depth=0)

    assert "![[target]]" in result["data"]["rendered"]
    assert "Embedded body" not in result["data"]["rendered"]


def test_read_note_rejects_an_unknown_mode(vault_factory):
    vault_factory({"note.md": _NOTE})
    with pytest.raises(ValueError, match="Unknown mode"):
        server.read_note_tool("note.md", mode="outlin")


def test_the_merged_read_tool_replaced_its_three_registrations():
    assert not hasattr(server, "render_note_tool")
    assert not hasattr(server, "get_note_outline_tool")


# ── list_all_tags_tool(mode=...) ──────────────────────────────────────────

def test_list_all_tags_flat_mode_counts_notes(served_vault):
    served_vault({
        "a.md": "---\ntags: [konzept/python]\n---\nA",
        "b.md": "---\ntags: [konzept/python, ki]\n---\nB",
    })
    result = server.list_all_tags_tool()

    assert result["meta"]["mode"] == "flat"
    counts = {item["tag"]: item["count"] for item in result["data"]["items"]}
    assert counts == {"konzept/python": 2, "ki": 1}
    assert result["meta"]["count"] == 2


def test_list_all_tags_tree_mode_nests_by_slash(served_vault):
    served_vault({"a.md": "---\ntags: [konzept/python]\n---\nA"})
    result = server.list_all_tags_tool(mode="tree")

    assert result["meta"]["mode"] == "tree"
    assert "python" in result["data"]["tree"]["konzept"]
    assert "path" not in result, "a vault-wide read addresses no single path"


def test_list_all_tags_rejects_an_unknown_mode(served_vault):
    served_vault({})
    with pytest.raises(ValueError, match="Unknown mode"):
        server.list_all_tags_tool(mode="nested")


def test_the_merged_tag_tool_replaced_get_tag_tree_tool():
    assert not hasattr(server, "get_tag_tree_tool")


# ── the alias tools Phase 2 deleted ───────────────────────────────────────

@pytest.mark.parametrize(
    "removed",
    ["get_notes_by_tag_tool", "get_daily_note_tool", "get_note_history_tool"],
)
def test_alias_tools_are_gone(removed):
    assert not hasattr(server, removed)


# ── the instructions must describe the tools that actually exist ──────────

REMOVED_TOOLS = (
    # Phase 2 — true aliases.
    "get_notes_by_tag_tool",
    "get_daily_note_tool",
    "get_note_history_tool",
    # Phase 3 — folded into read_note_tool / list_all_tags_tool.
    "render_note_tool",
    "get_note_outline_tool",
    "get_tag_tree_tool",
)


def _documented_tool_names() -> set[str]:
    import re
    return set(re.findall(r"`(\w+_tool)[(`]", server._DEFAULT_INSTRUCTIONS))


@pytest.mark.parametrize("removed", REMOVED_TOOLS)
def test_instructions_never_mention_a_removed_tool(removed):
    assert removed not in server._DEFAULT_INSTRUCTIONS


def test_every_registered_tool_is_documented():
    """The opt-in format groups aren't module attributes when their flag is
    off, so this checks the other direction: nothing registered goes
    undocumented."""
    registered = {
        name
        for name in dir(server)
        if name.endswith("_tool") and callable(getattr(server, name))
    }
    assert registered, "sanity: the server should expose some tools"
    undocumented = registered - _documented_tool_names()
    assert not undocumented, f"registered but undocumented: {sorted(undocumented)}"
