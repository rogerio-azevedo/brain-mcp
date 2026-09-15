# Vault Conventions (Customization)

Create `_AI_INSTRUCTIONS.md` in your vault root to teach the AI how your
specific vault is organized:

```markdown
## Structure
- `Notes/` — evergreen notes and concepts
- `Projects/` — active and archived projects (tag: #project/active, #project/done)
- `Journal/` — daily notes (YYYY-MM-DD.md)

## Frontmatter Schema
- status: active | done | inbox
- tags: nested with / (e.g. #concept/programming)

## Conventions
- Link by stem only, never by full path
- Every note needs a created: date in frontmatter
```

In single-vault mode, the server loads this file at startup and sends it to
the AI as system instructions. Without it, built-in generic Obsidian syntax
guidance is used. Multi-vault servers always use the generic startup
instructions because MCP server instructions are shared by every identity;
use `get_vault_conventions_tool(vault=...)` to load the selected vault's
conventions after authorization. The `_AI_INSTRUCTIONS.md` is the right
place for everything vault-specific — folder layout, tag schema, naming
conventions, and any workflow rules.

A `## Frontmatter Schema` heading with a fenced `field: value` list is also
what `lint_schema_tool` parses to validate notes against — see
[Tool Reference](tools.md) and the [Health-Check Cron](deployment.md#health-check-cron-frontmatter-schema).
