# Development

```bash
uv run pytest -q       # complete automated suite
uv run ruff check src/ tests/ scripts/run_local_smoke_test.py scripts/smoke_test_mcp.py
```

See [CHANGELOG.md](../CHANGELOG.md) for what changed release to release, and
[Architecture](architecture.md) for how the codebase is laid out.

## Local HTTP functional smoke test

The automated suite exercises most behavior in process. This command also
starts the installed server entry point and connects a real authenticated
MCP client over HTTP:

```bash
uv run python scripts/run_local_smoke_test.py
```

The runner chooses a free localhost port, creates a disposable vault and API
key, starts and health-checks the server, then invokes `smoke_test_mcp.py`.
The client lists tools, creates and reads a uniquely named note, overwrites
it, verifies the bytes on disk, and confirms that a write outside
`WRITE_PATHS` is rejected. The runner always stops the server and removes
successful test data; pass `--keep` to retain the disposable vault and
server log for inspection.

Use `smoke_test_mcp.py` directly for an already-running local, containerized,
or remote server. It reads the bearer token from `OBSIDIAN_MCP_API_KEY` (or
prompts securely), so secrets do not need to appear in command history or
process arguments. Its denied-write probe is opt-in via
`--denied-note PATH`; only provide a path known to be outside that server's
configured write scope.
