# Client setup

The Zammad MCP server speaks MCP over Streamable HTTP. Every modern MCP
client can connect to it. Below are the verified flows for each major
client.

In every case, `BASE = https://your-zammad-mcp.example.com`. Replace with
your own deployment URL.

## Claude.ai (Web)

1. Open <https://claude.ai> and sign in.
2. **Settings → Connectors → + Add custom connector**.
3. Fill in:
   - **Name:** `Zammad`
   - **URL:** `BASE/mcp`
4. Click **Connect**.
5. Claude opens the consent screen, which redirects to your IdP
   (Zammad in Mode 1, external OIDC in Mode 2). Sign in and approve.
6. The connector appears in the Claude UI. Tools are now usable in
   conversations.

The connector survives logouts. Revoking the OAuth grant in Zammad
(or the external IdP) forces Claude to prompt for re-authentication on
the next call.

## Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (macOS / Linux) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "zammad": {
      "command": "npx",
      "args": ["mcp-remote", "https://your-zammad-mcp.example.com/mcp"]
    }
  }
}
```

Restart Claude Desktop. The connector appears in the sidebar and the
first invocation triggers the OAuth flow in your default browser.

## Microsoft 365 Copilot Studio

1. In Copilot Studio, **Tools → Add tool → Model Context Protocol**.
2. **URL:** `BASE/mcp`.
3. **Authentication:** OAuth 2.0.
4. Copilot Studio walks through the OAuth flow on first use.

## ChatGPT (with Connectors)

1. Open ChatGPT settings → **Connectors → Add custom**.
2. **URL:** `BASE/mcp`.
3. OAuth dialog appears on first call.

## Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "zammad": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-zammad-mcp.example.com/mcp"]
    }
  }
}
```

Restart Cursor. The first tool invocation opens the OAuth flow.

## Continue (VS Code / JetBrains)

`~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "Zammad",
      "transport": {
        "type": "streamable-http",
        "url": "https://your-zammad-mcp.example.com/mcp"
      }
    }
  ]
}
```

## MCP Inspector (testing / debugging)

The Inspector is the official MCP debugging UI. Useful before connecting
production clients.

```bash
npx @modelcontextprotocol/inspector
```

In the Inspector UI:

- **Transport:** Streamable HTTP
- **Server URL:** `BASE/mcp`
- **OAuth:** auto (Inspector handles DCR + the OAuth flow)

You can fire each tool individually, inspect the JSON-RPC traffic, and
view the live tool catalogue.

## What the user sees on consent

The OAuth consent screen rendered by FastMCP shows:

| Field | Source |
| --- | --- |
| Server name | `MCP_DISPLAY_NAME` (default `BAUER GROUP Zammad`) |
| Icon | `MCP_ICON_URL` if set, else `${PUBLIC_BASE_URL}/logo.svg` (bundled logo) |
| Linked website | `MCP_WEBSITE_URL` |
| Requested scopes | `read write` (Zammad mode) or your `OIDC_SCOPES` (OIDC mode) |

To customise the look for a specific deployment, set those three env vars
and redeploy — no code changes needed.

## Verifying the connection

Once connected, the AI client should expose ~36 tools whose names start
with verbs like `list_`, `search_`, `get_`, `create_`, `update_`,
`delete_`. Quick smoke test in any client:

> "What's my Zammad profile?"

A correctly-wired client invokes `get_me` and returns the authenticated
user's name, email, and role.
