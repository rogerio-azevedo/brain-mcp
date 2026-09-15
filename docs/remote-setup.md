# Remote Setup

Run obsidian-mcp on a server and connect to it remotely. Network transports
(`http`/`sse`) need at least one of the two auth variants below — **API key**
and **GitHub OAuth** are independent and can be used at the same time: keep
the API key for Claude Code/Desktop/curl while adding OAuth only for
claude.ai, or use either one alone.

> **Security:** Always put a TLS-terminating reverse proxy (e.g.
> [Caddy](https://caddyserver.com)) in front when exposing to the internet —
> required for GitHub OAuth callbacks in particular, since GitHub rejects
> plain `http://` callback URLs except on `localhost`. `API_KEY`/OAuth are
> not needed for stdio transport (local use only).

## Option A: API key (Claude Code, Claude Desktop, curl, mcp-remote)

**1. Generate an API key:**
```bash
openssl rand -hex 32
```

**2. Configure the server** (`docker-compose.yml` or `.env`):
```env
VAULT_PATH=/data/vault
TRANSPORT=http
API_KEY=your-generated-key
```
(`sse` also works here, but see the note under Option B — `http` is the
more robust choice and works identically for this bearer-key setup.)

**3. Start:**
```bash
docker compose up -d   # or: uv run obsidian-remote-mcp
```

**4. Connect from your MCP client** (anywhere on the network):
```json
{
  "mcpServers": {
    "obsidian": {
      "type": "http",
      "url": "https://your-server/mcp",
      "headers": {
        "Authorization": "Bearer your-generated-key"
      }
    }
  }
}
```

## Option B: GitHub OAuth (claude.ai Web/Mobile Custom Connector)

claude.ai's "Custom Connector" UI only has fields for OAuth (Authorization
URL, Token URL, Client ID/Secret) — no field for a bearer token or custom
header. obsidian-mcp handles the whole OAuth protocol for you (discovery
endpoints, PKCE, token exchange); you only ever hand claude.ai your server's
URL.

**1. Create a GitHub OAuth App:** GitHub → Settings → Developer settings →
[OAuth Apps](https://github.com/settings/developers) → New OAuth App.
- Homepage URL: your server's public URL (e.g. `https://obsidian.example.com`)
- Authorization callback URL: the same URL + `/auth/callback`
  (e.g. `https://obsidian.example.com/auth/callback`)

Copy the generated **Client ID** and **Client Secret**.

**2. Configure the server** (`docker-compose.yml` or `.env`):
```env
VAULT_PATH=/data/vault
TRANSPORT=http
PUBLIC_BASE_URL=https://obsidian.example.com   # must match the GitHub callback host
OAUTH_GITHUB_CLIENT_ID=your-client-id
OAUTH_GITHUB_CLIENT_SECRET=your-client-secret
OAUTH_GITHUB_ALLOWED_LOGINS=your-github-username # comma-separated; required, no "allow anyone" fallback
```
`OAUTH_GITHUB_ALLOWED_LOGINS` is enforced at login: only the listed GitHub
accounts can authenticate, everyone else is rejected, even with a valid
GitHub account.

> **Use `TRANSPORT=http`, not `sse`.** `sse` caused OAuth authorization
> errors with claude.ai specifically (token issued fine server-side, but
> claude.ai never followed up with a request) — `http` fixed it.

**3. Start:**
```bash
docker compose up -d   # or: uv run obsidian-remote-mcp
```

**4. Connect from claude.ai:** Settings → Connectors → Add Custom Connector,
and enter just the server URL (`https://obsidian.example.com/mcp`). claude.ai
discovers everything else (`/.well-known/oauth-authorization-server`, PKCE,
etc.) automatically and redirects you to GitHub to log in on first connect.

> **Persistence in Docker:** OAuth client registrations and tokens are stored
> under FastMCP's own data directory, which is *not* inside the vault volume
> by default. Without a persistent mount there, every container restart logs
> claude.ai out and forces re-authentication. Set `FASTMCP_HOME` to a mounted
> path (see `docker-compose.yml`) to avoid that.

Keep the vault synced on the server with Syncthing, git+cron, rclone, or
Obsidian Sync. The file watcher picks up normal changes, while periodic
Markdown reconciliation repairs missed watcher events (see
[Configuration](configuration.md#indexing-and-sync)).
