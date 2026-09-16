"""Per-identity path policy overrides: config parsing, merge semantics, and
the per-call read scoping every index-backed tool applies to its output."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import obsidian_mcp.config as cfg_mod
from obsidian_mcp.config import (
    Config,
    ConfigError,
    Identity,
    PolicyOverride,
    load_vaults_file,
    reset_current_identity,
    reset_current_vault,
    set_current_identity,
    set_current_vault,
)
from obsidian_mcp.domain.index import VaultIndex
from obsidian_mcp.storage.policy import (
    EffectiveAccessPolicy,
    PolicyMergeError,
    VaultAccessPolicy,
    merge_allowlist,
    merge_denylist,
    merge_readonly,
)
from obsidian_mcp.tools.lint import lint_schema
from obsidian_mcp.tools.query import (
    get_backlinks,
    get_broken_links,
    get_link_graph,
    get_orphans,
    get_tag_tree,
    get_tasks,
    get_vault_stats,
    list_all_tags,
    query_notes,
    resolve_alias,
)
from obsidian_mcp.tools.read import list_notes, search_notes
from obsidian_mcp.tools.similarity import find_similar_notes


def _write_config(tmp_path, identity_vaults, *, vault_policy=None, vaults=None):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    data = {
        "vaults": vaults
        or {"main": {"path": str(vault), **(vault_policy or {})}},
        "identities": [
            {"type": "api_key", "value": "sk-test", "vaults": identity_vaults}
        ],
    }
    config_path = tmp_path / "vaults.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    return config_path, vault


# ── vaults.json parsing ─────────────────────────────────────────────────────

def test_array_form_still_parses_and_carries_no_overrides(tmp_path):
    config_path, _vault = _write_config(tmp_path, ["main"])

    _vaults, identities = load_vaults_file(str(config_path))

    assert identities[0].vaults == ("main",)
    assert identities[0].overrides == ()
    assert identities[0].override_for("main") is None


def test_object_form_parses_vault_names_and_overrides(tmp_path):
    config_path, _vault = _write_config(
        tmp_path,
        {
            "main": {
                "read_paths": ["Public/"],
                "deny_read_paths": ["Public/drafts/"],
                "write_paths": ["Public/notes/"],
                "deny_write_paths": ["Public/notes/frozen.md"],
                "read_only": True,
            }
        },
    )

    _vaults, identities = load_vaults_file(str(config_path))
    override = identities[0].override_for("main")

    assert identities[0].vaults == ("main",)
    assert override == PolicyOverride(
        write_paths=("Public/notes/",),
        read_paths=("Public/",),
        deny_read_paths=("Public/drafts/",),
        deny_write_paths=("Public/notes/frozen.md",),
        read_only=True,
    )


def test_object_form_empty_override_inherits_and_is_dropped(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"main": {}})

    _vaults, identities = load_vaults_file(str(config_path))

    assert identities[0].vaults == ("main",)
    assert identities[0].overrides == ()


def test_object_form_unknown_override_field_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"main": {"exclude_paths": ["x"]}})

    with pytest.raises(ConfigError, match="unsupported override field"):
        load_vaults_file(str(config_path))


def test_object_form_empty_path_list_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"main": {"read_paths": []}})

    with pytest.raises(ConfigError, match="at least one path"):
        load_vaults_file(str(config_path))


def test_object_form_non_string_path_list_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"main": {"read_paths": [1]}})

    with pytest.raises(ConfigError, match="must be a list of strings"):
        load_vaults_file(str(config_path))


def test_object_form_non_bool_read_only_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"main": {"read_only": "yes"}})

    with pytest.raises(ConfigError, match="read_only must be a boolean"):
        load_vaults_file(str(config_path))


def test_object_form_unsafe_override_rule_raises(tmp_path):
    config_path, _vault = _write_config(
        tmp_path, {"main": {"read_paths": ["../outside/"]}}
    )

    with pytest.raises(ConfigError, match="escapes vault root"):
        load_vaults_file(str(config_path))


def test_vaults_neither_list_nor_object_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, "main")

    with pytest.raises(ConfigError, match="list of strings or an object"):
        load_vaults_file(str(config_path))


def test_object_form_unknown_vault_raises(tmp_path):
    config_path, _vault = _write_config(tmp_path, {"nope": {"read_only": True}})

    with pytest.raises(ConfigError, match="unknown vault"):
        load_vaults_file(str(config_path))


def test_widening_allowlist_override_fails_at_startup(tmp_path):
    config_path, _vault = _write_config(
        tmp_path,
        {"main": {"read_paths": ["Other/"]}},
        vault_policy={"read_paths": ["Public/"]},
    )

    with pytest.raises(ConfigError, match="not inside the vault's own read_paths"):
        load_vaults_file(str(config_path))


def test_widening_write_allowlist_override_fails_at_startup(tmp_path):
    config_path, _vault = _write_config(
        tmp_path,
        {"main": {"write_paths": ["Public/"]}},
        vault_policy={"write_paths": ["Public/notes/"]},
    )

    with pytest.raises(ConfigError, match="not inside the vault's own write_paths"):
        load_vaults_file(str(config_path))


def test_narrowing_allowlist_override_loads(tmp_path):
    config_path, _vault = _write_config(
        tmp_path,
        {"main": {"read_paths": ["Public/notes/"]}},
        vault_policy={"read_paths": ["Public/"]},
    )

    _vaults, identities = load_vaults_file(str(config_path))

    assert identities[0].override_for("main").read_paths == ("Public/notes/",)


def test_startup_validation_accounts_for_global_read_only(tmp_path):
    """A read_only override on a globally read-only server stays loadable."""
    config_path, _vault = _write_config(tmp_path, {"main": {"read_only": True}})

    _vaults, identities = load_vaults_file(str(config_path), global_read_only=True)

    assert identities[0].override_for("main").read_only is True


def test_config_loads_object_form_identities(tmp_path, monkeypatch):
    config_path, vault = _write_config(
        tmp_path, {"main": {"deny_read_paths": ["Secret/"]}}
    )
    monkeypatch.setenv("VAULTS_CONFIG", str(config_path))
    monkeypatch.setenv("TRANSPORT", "sse")
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("LOCK_PATH", str(tmp_path / "locks"))

    cfg = Config()

    assert cfg.identities[0].override_for("main").deny_read_paths == ("Secret/",)


def test_documented_example_config_loads(tmp_path):
    """vaults.json.example must stay a valid config, overrides included."""
    example = json.loads(
        (Path(__file__).resolve().parents[1] / "vaults.json.example").read_text(
            encoding="utf-8"
        )
    )
    for name, vault in example["vaults"].items():
        root = tmp_path / name
        root.mkdir()
        vault["path"] = str(root)
    config_path = tmp_path / "vaults.json"
    config_path.write_text(json.dumps(example), encoding="utf-8")

    _vaults, identities = load_vaults_file(str(config_path))

    # The example documents both forms, and at least one real override.
    assert any(identity.overrides for identity in identities)
    assert any(not identity.overrides for identity in identities)


# ── merge helpers ───────────────────────────────────────────────────────────

def test_merge_allowlist_inherits_on_none():
    assert merge_allowlist(("A/",), None) == ("A/",)


def test_merge_allowlist_override_wins_when_vault_is_open():
    assert merge_allowlist((), ("A/",)) == ("A/",)


def test_merge_allowlist_accepts_narrower_rule():
    assert merge_allowlist(("A/",), ("A/b/",)) == ("A/b/",)
    assert merge_allowlist(("A/",), ("A/b.md",)) == ("A/b.md",)


def test_merge_allowlist_rejects_widening_rule():
    with pytest.raises(PolicyMergeError):
        merge_allowlist(("A/b/",), ("A/",))
    with pytest.raises(PolicyMergeError):
        merge_allowlist(("A/",), ("B/",))


def test_merge_allowlist_rejects_recursive_under_exact_file_rule():
    with pytest.raises(PolicyMergeError):
        merge_allowlist(("A/b.md",), ("A/b.md/",))


def test_merge_allowlist_is_component_aware():
    with pytest.raises(PolicyMergeError):
        merge_allowlist(("A/",), ("Abc/",))


def test_merge_denylist_is_union_only():
    assert merge_denylist(("A/",), ("B/",)) == ("A/", "B/")
    # An override cannot drop a vault-level deny by omitting it.
    assert merge_denylist(("A/",), ()) == ("A/",)
    assert merge_denylist(("A/",), None) == ("A/",)
    assert merge_denylist(("A/",), ("A/",)) == ("A/",)


def test_merge_readonly_ors_toward_true():
    assert merge_readonly(False, True) is True
    assert merge_readonly(True, None) is True
    # False cannot undo a read-only vault.
    assert merge_readonly(True, False) is True
    assert merge_readonly(False, None) is False


def _identity(**override_fields):
    return Identity(
        type="api_key",
        value="sk-test",
        vaults=("main",),
        overrides=(("main", PolicyOverride(**override_fields)),),
    )


def test_effective_policy_merges_identity_override(tmp_path):
    policy = VaultAccessPolicy(
        tmp_path,
        vault_name="main",
        read_paths=["Public/"],
        deny_read_paths=["Public/x/"],
    )
    identity = _identity(read_paths=("Public/notes/",), deny_read_paths=("Public/y/",))

    effective = policy.effective(identity)

    assert effective.read_paths == ("Public/notes/",)
    assert effective.deny_read_paths == ("Public/x/", "Public/y/")


def test_effective_policy_denies_writes_for_read_only_identity(tmp_path):
    policy = VaultAccessPolicy(tmp_path, vault_name="main")
    identity = _identity(read_only=True)

    assert policy.effective(identity).read_only is True
    assert not policy.can_write("note.md", identity=identity)
    assert policy.can_write("note.md", identity=None)


def test_identity_overrides_can_be_opted_out(tmp_path):
    """The vault-wide index/watcher policy stays identity-agnostic."""
    policy = VaultAccessPolicy(
        tmp_path, vault_name="main", apply_identity_overrides=False
    )
    token = set_current_identity(_identity(read_only=True))
    try:
        assert policy.effective().read_only is False
    finally:
        reset_current_identity(token)


def test_effective_policy_without_vault_name_ignores_overrides(tmp_path):
    policy = VaultAccessPolicy(tmp_path)

    assert policy.effective(_identity(read_only=True)).read_only is False


def test_effective_access_policy_merged_with_none_is_identity():
    base = EffectiveAccessPolicy(False, ("A/",), ("A/",), (), ())

    assert base.merged_with(None) is base


# ── read scoping across the index-backed tools ──────────────────────────────

_NOTES = {
    "_AI_INSTRUCTIONS.md": (
        "## Frontmatter Schema\n\n```yaml\nstatus: active | done\n```\n"
    ),
    "Public/a.md": (
        "---\nstatus: bogus\n---\n"
        "Links to [[secret]] and [[b]] and [[nowhere]].\n"
        "#public\n"
        "- [ ] public task 📅 2026-01-01\n"
    ),
    "Public/b.md": "---\nstatus: active\n---\n#public/nested\nJust b.\n",
    "Secret/secret.md": (
        "---\nstatus: bogus\n---\n"
        "Links to [[b]].\n"
        "#classified\n"
        "- [ ] secret task 📅 2026-01-02\n"
    ),
}


@pytest.fixture
def scoped_vault(tmp_path, monkeypatch):
    """A two-folder vault plus an identity denied read access to ``Secret/``."""
    def _make(identity_vaults):
        config_path, vault = _write_config(tmp_path, identity_vaults)
        for rel, content in _NOTES.items():
            target = vault / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        monkeypatch.setenv("VAULTS_CONFIG", str(config_path))
        monkeypatch.setenv("TRANSPORT", "sse")
        monkeypatch.setenv("VAULT_PATH", str(vault))
        monkeypatch.setenv("LOCK_PATH", str(tmp_path / "locks"))
        monkeypatch.delenv("API_KEY", raising=False)
        cfg_mod._config = None
        cfg = cfg_mod.get_config()
        tokens.append((reset_current_vault, set_current_vault("main")))
        tokens.append(
            (reset_current_identity, set_current_identity(cfg.identities[0]))
        )
        # The index itself stays vault-wide — scoping is a per-call filter.
        index = VaultIndex(vault)
        index.build()
        return index

    tokens: list = []
    try:
        yield _make
    finally:
        for reset, token in reversed(tokens):
            reset(token)
        cfg_mod._config = None


@pytest.fixture
def denied_index(scoped_vault):
    return scoped_vault({"main": {"deny_read_paths": ["Secret/"]}})


@pytest.fixture
def allowlisted_index(scoped_vault):
    return scoped_vault({"main": {"read_paths": ["Public/", "_AI_INSTRUCTIONS.md"]}})


@pytest.fixture
def unscoped_index(scoped_vault):
    return scoped_vault(["main"])


def _flatten(value) -> str:
    return json.dumps(value, default=str)


def test_index_itself_remains_vault_wide(denied_index):
    assert "Secret/secret.md" in denied_index.get_all_notes()


def test_backlinks_hide_unreadable_sources(denied_index):
    assert get_backlinks("Public/b.md", denied_index) == ["Public/a.md"]


def test_backlinks_unscoped_identity_sees_everything(unscoped_index):
    assert get_backlinks("Public/b.md", unscoped_index) == [
        "Public/a.md",
        "Secret/secret.md",
    ]


def test_link_graph_does_not_traverse_or_report_unreadable_notes(denied_index):
    graph = get_link_graph("Public/b.md", denied_index, depth=3)

    # "nowhere" is a dangling wikilink target, not a vault note — unchanged.
    assert {node["path"] for node in graph["nodes"]} == {
        "Public/a.md",
        "Public/b.md",
        "nowhere",
    }
    assert "Secret" not in _flatten(graph)


def test_link_graph_from_an_unreadable_root_is_empty(denied_index):
    graph = get_link_graph("Secret/secret.md", denied_index, depth=3)

    assert graph["nodes"] == []
    assert graph["edges"] == []


def test_link_graph_outgoing_edge_to_unreadable_target_is_dropped(denied_index):
    graph = get_link_graph("Public/a.md", denied_index, depth=2, direction="outgoing")

    assert "Secret" not in _flatten(graph)
    assert {edge["to"] for edge in graph["edges"]} == {"Public/b.md", "nowhere"}


def test_orphans_ignore_invisible_backlinks(denied_index):
    orphans = get_orphans(denied_index)

    # b keeps a visible backlink from a; secret.md is invisible entirely.
    assert orphans == ["Public/a.md", "_AI_INSTRUCTIONS.md"]


def test_vault_stats_count_only_visible_notes(denied_index):
    stats = get_vault_stats(denied_index)

    assert stats["total_notes"] == 3  # a, b and _AI_INSTRUCTIONS
    assert "Secret" not in _flatten(stats)


def test_tag_tree_drops_tags_only_unreadable_notes_carry(denied_index):
    tree = get_tag_tree(denied_index)

    assert "classified" not in tree
    assert "public" in tree
    assert "Secret" not in _flatten(tree)


def test_list_all_tags_excludes_unreadable_notes_from_counts(denied_index):
    tags = {entry["tag"]: entry["count"] for entry in list_all_tags(denied_index)}

    assert "classified" not in tags
    assert tags["public"] == 1


def test_tasks_skip_unreadable_notes(denied_index):
    tasks = get_tasks(denied_index)

    assert [task["source"] for task in tasks] == ["Public/a.md"]


def test_query_notes_skips_unreadable_notes(denied_index):
    paths = [row["path"] for row in query_notes(denied_index)]

    assert "Secret/secret.md" not in paths
    assert "Public/a.md" in paths


def test_broken_links_skip_unreadable_sources(denied_index):
    broken = get_broken_links(denied_index)

    assert {entry["source"] for entry in broken} == {"Public/a.md"}


def test_resolve_alias_refuses_unreadable_targets(denied_index):
    assert resolve_alias("secret", denied_index) is None
    assert resolve_alias("b", denied_index) == "Public/b.md"


def test_resolve_alias_unscoped_identity_resolves_everything(unscoped_index):
    assert resolve_alias("secret", unscoped_index) == "Secret/secret.md"


def test_lint_schema_skips_unreadable_notes(denied_index):
    result = lint_schema(denied_index)

    assert {v["path"] for v in result["violations"]} == {"Public/a.md"}


def test_find_similar_notes_skips_unreadable_notes(denied_index):
    results = find_similar_notes("secret", denied_index, min_score=0.0, limit=10)

    assert "Secret/secret.md" not in {row["path"] for row in results}


def test_list_and_search_are_scoped_by_the_storage_walk(denied_index):
    assert "Secret/secret.md" not in list_notes()
    assert "Secret/secret.md" not in {row["path"] for row in search_notes("Links")}


def test_read_allowlist_override_scopes_the_same_tools(allowlisted_index):
    assert get_backlinks("Public/b.md", allowlisted_index) == ["Public/a.md"]
    assert resolve_alias("secret", allowlisted_index) is None
    assert get_vault_stats(allowlisted_index)["total_notes"] == 3
    assert "Secret" not in _flatten(get_link_graph("Public/a.md", allowlisted_index, depth=3))
