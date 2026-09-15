# Docker & Deployment

```bash
# 1. Copy and edit the environment variables
cp .env.example .env

# 2. Set HOST_VAULT_PATH and API_KEY in .env, then:
docker compose up -d
```

The `docker-compose.yml` pulls the pre-built image from GHCR — no cloning or
building required. To build locally instead, swap `image:` for `build: .` in
the compose file.

GitHub OAuth state is stored under `/data/fastmcp` by default, inside the
Compose `mcp-data` volume, so logins survive container restarts.

## Health check

The image has a built-in `HEALTHCHECK` against `GET /health`
(unauthenticated, with no vault content or filesystem paths). It reports
index readiness plus the last reconciliation time, duration, and error, and
is visible in `docker ps`/`docker compose ps`. Only meaningful for
`TRANSPORT=http`/`sse`; a no-op for `stdio`.

## Hardened home-server Compose profile

For a home server where Cloudflare Tunnel is the only network entry point,
use [`docker-compose.home-server.yml`](../docker-compose.home-server.yml). It
builds the MCP image from the checked-out source, bind-mounts the complete
vault read-only, overlays only the two configured AI memory/output
directories read-write, sets matching `WRITE_PATHS`, runs as the configured
non-root UID/GID, and publishes no host port. The MCP service is only on an
internal network; `cloudflared` has that network plus a separate normal
egress network so it can reach Cloudflare without making MCP externally
reachable.

Create the nested writable directories before starting it, set
`HOST_VAULT_PATH`, `AI_MEMORY_PATH`, `AI_OUTPUT_PATH`, `PUID`, `PGID`,
`MCP_DATA_PATH`, `API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`, and a digest-pinned
`CLOUDFLARED_IMAGE` (for example,
`cloudflare/cloudflared@sha256:<digest>`) in `.env`. Create the data
directory and make it owned by `PUID:PGID`; it stores `/data/locks` and
application state. Cloudflare Access Managed OAuth authenticates the edge,
but it does not inject this application's bearer API key. Clients must
still send `API_KEY` to the origin; trusted-proxy header injection is a
future phase, not part of this profile. Then run:

```bash
docker compose -f docker-compose.home-server.yml up -d
```

Folder/note trash and restore are intentionally unavailable in this
nested-bind profile: moving from a writable overlay into the read-only
parent vault's `.trash` would cross mounts. Keep `ENABLE_DELETE=false`
(hard-coded here) and do not enable folder restore in this topology.

The static Compose checks are covered by the test suite. A real deployment
test (Docker mount precedence, host UID/GID permissions, and the Cloudflare
Tunnel route) remains environment-specific and must be run on the target
server before relying on it. To update Cloudflared, choose a reviewed
release, resolve its immutable `RepoDigest`, update `CLOUDFLARED_IMAGE`,
then recreate the sidecar; rebuild the MCP service after source changes
with `docker compose -f docker-compose.home-server.yml build --pull`.

## Health-Check Cron (Frontmatter Schema)

Separate from the `/health` liveness check above: `scripts/health_check.py`
runs `lint_schema_tool`'s logic directly (no MCP client needed) and, only if
it finds notes whose frontmatter violates the enums declared in your
`_AI_INSTRUCTIONS.md`, drops a report note into your vault's inbox folder.
Silent when the vault is clean — no note, no noise.

```bash
# One-off / manual run:
VAULT_PATH=/path/to/vault HEALTH_CHECK_INBOX=00-Inbox python scripts/health_check.py
```

To run it weekly via cron against the running container:

```cron
# crontab -e (on the Docker host)
0 6 * * 1 docker exec obsidian-mcp-obsidian-mcp-1 \
  env VAULT_PATH=/vault READ_ONLY=false WRITE_PATHS=00-Inbox/ \
  HEALTH_CHECK_INBOX=00-Inbox python scripts/health_check.py
```

Swap the container name for whatever `docker compose ps` shows, and set
both `HEALTH_CHECK_INBOX` and `WRITE_PATHS` to your vault's actual inbox
folder (default `Inbox`). The command must have write access because it
creates a report when violations are found. With the home-server profile,
choose an inbox inside one of its writable nested mounts (for example
`AI-Output/`).
