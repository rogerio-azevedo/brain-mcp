# Architecture

```
src/obsidian_mcp/
├── config.py          # single-/multi-vault env config, path-policy dataclasses
├── envelope.py         # the shared {success, path, revision, data, meta} response shape
├── server.py           # FastMCP entry point, tool/resource/prompt registration, auth
├── domain/
│   ├── models.py        # Note dataclass (frontmatter, tags, wikilinks, tasks, …)
│   ├── parser.py         # Markdown parser (YAML frontmatter, wikilinks, block refs, …)
│   └── index.py          # VaultIndex — alias resolution, backlinks, tag tree, BFS
├── storage/
│   ├── policy.py         # canonical path resolution + the central authorization policy
│   ├── filesystem.py     # VaultStorage — descriptor-relative gateway, atomic writes
│   ├── revisions.py      # optimistic-concurrency helpers (expected_revision, create_only)
│   ├── locking.py        # process locks kept outside the synced vault
│   ├── audit.py          # append-only JSONL audit log, also outside the vault
│   └── watcher.py        # watchdog-based change detection + periodic reconciliation
└── tools/
    ├── read.py            # list_notes, search_notes, read_note (full/outline/rendered)
    ├── write.py            # write_note, patch_note, move_note, manage_tags, …
    ├── query.py             # graph tools, task aggregation, periodic notes, query_notes
    ├── folders.py            # folder management
    ├── similarity.py          # find_similar_notes — TF-IDF + cosine similarity
    ├── lint.py                 # lint_schema — validates frontmatter against _AI_INSTRUCTIONS.md
    ├── audit.py                 # get_audit_log
    ├── canvas.py                 # Obsidian Canvas (.canvas JSON) tools
    ├── excalidraw.py               # Obsidian Excalidraw (*.excalidraw.md) tools
    ├── kanban.py                    # Obsidian Kanban plugin tools
    ├── bases.py                      # Obsidian Bases (.base YAML) tools
    ├── attachments.py                 # binary and text attachment handling
    ├── templates.py                    # template rendering with variable substitution
    └── prompts.py                       # MCP Prompts (weekly_review, daily_note)
```

Two authorization layers sit under the tools:

- **`storage/policy.py`** turns vault-relative strings into filesystem paths
  and is the only place that does so — this is what `READ_PATHS`,
  `WRITE_PATHS`, `DENY_READ_PATHS`, `DENY_WRITE_PATHS`, and `EXCLUDE_PATHS`
  compile down to (see [Configuration](configuration.md)).
- **`storage/revisions.py`** layers optimistic concurrency (`expected_revision`,
  `create_only`) on top, so a read-modify-write tool can detect a concurrent
  edit from Obsidian Sync (see [Response Envelope](response-envelope.md)).

Every tool's return value is built through `envelope.py`'s constructors
rather than assembled ad hoc — see [Response Envelope](response-envelope.md)
for the shape and rationale.
