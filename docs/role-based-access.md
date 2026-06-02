# Role-based MCP access

`MCP_ALLOWED_ROLES` is a **coarse gate** that controls **who can use the
MCP server at all**. It is a defense-in-depth layer on top of Zammad's own
permission system — Zammad still enforces what each tool call may actually
do, but the allowlist lets you say "this MCP endpoint is for Agents only,
never expose it to Customers."

## Why a separate allowlist?

In `AUTH_MODE=zammad` the user's Zammad access token is forwarded to every
API call, so Zammad's own permission system is the primary defence:

- A Customer logged in via the MCP **cannot** read other customers'
  tickets, because Zammad refuses the API call.
- An Agent **cannot** edit roles or delete users, because Zammad refuses.

But a `Customer` token still allows things you may not want exposed via
an AI assistant:

- Creating tickets in their own name.
- Reading their own ticket history.
- Sending replies to ongoing conversations.

If your AI assistant should be **internal only** (agents triaging tickets,
admins running automation), block customers entirely at the MCP layer
instead of relying on per-tool checks. That's what the allowlist does.

## Configuration

`MCP_ALLOWED_ROLES` is a comma-separated list of Zammad role names.
Case-insensitive, whitespace-tolerant.

```env
# Most common: internal MCP for agents and admins
MCP_ALLOWED_ROLES=Admin,Agent

# Admins only - automation-style deployments
MCP_ALLOWED_ROLES=Admin

# Customer-facing self-service MCP
MCP_ALLOWED_ROLES=Customer

# Allow ANY authenticated Zammad user (NOT recommended in production)
MCP_ALLOWED_ROLES=
```

Custom role names are supported — pass whatever name appears in the
Zammad admin's Roles list.

## How the check runs

1. The user logs in via OAuth (Mode 1) and Zammad issues an access token.
2. The MCP server calls `${ZAMMAD_URL}/api/v1/users/me` with that token
   to validate it. The response includes the user's role names.
3. The role names are attached to the FastMCP-issued JWT's claims.
4. On every JSON-RPC request, bg-mcpcore's `access_control` gate (activated by
   the profile's `access_control` block) reads the claims and compares the
   user's roles against `MCP_ALLOWED_ROLES`.
5. Match → request passes through. No match → request is rejected with
   `PermissionError`.

### When role changes take effect

The role set is captured from `/users/me` at login (step 2) and attached to the
FastMCP-issued JWT, so it is fixed for the lifetime of that session. A user whose
Zammad roles change picks up the new set on the next authentication. Revoking the
upstream token in Zammad (User Profile → Token Access → Revoke) ends MCP access
at the next token refresh — the refresh fails and the client must re-authenticate.

## Audit-only mode

Switch to enforcement carefully. Start by logging only:

```env
MCP_ALLOWED_ROLES=Admin,Agent
MCP_ROLE_CHECK_AUDIT_ONLY=true
```

Every denied request is logged as
`auth.role_denied_audit_only_passing_through` but still served. Watch the
logs for a week, confirm the allowlist matches expected usage, then flip
to `false`.

## Mode interaction

| `AUTH_MODE` | Allowlist enforced? | Why |
| --- | --- | --- |
| `zammad` | **Yes** | Zammad token verification populates `roles` in the JWT claims. |
| `oidc` | No (no-op) | External OIDC tokens carry no Zammad role info. The middleware isn't wired in this mode. |
| `none` | No (no-op) | No authenticated user; nothing to check. |

In modes where the allowlist is a no-op, Zammad's own permission system
still enforces fine-grained access on every API call. The allowlist
exists only as a coarse coarse filter, never the sole defence.

## Sample log entries

Successful match (debug-level, not shown at INFO):

```text
2026-05-27T10:32:11Z [debug   ] auth.token_verified sub=user-42 roles=['Admin', 'Agent']
```

Denial (warn-level, INFO and above):

```text
2026-05-27T10:32:12Z [warning ] auth.role_denied sub=user-99 login=customer@example.com user_roles=['customer'] allowed_roles=['admin', 'agent'] method=tools/call
```

Audit-only mode:

```text
2026-05-27T10:32:12Z [warning ] auth.role_denied_audit_only_passing_through sub=user-99 ...
```

## Custom Zammad roles

Zammad supports custom roles (e.g. `Read-Only Agent`, `External
Consultant`). Drop the role name into `MCP_ALLOWED_ROLES` exactly as it
appears in Zammad:

```env
MCP_ALLOWED_ROLES=Admin,Agent,Read-Only Agent
```

Whitespace and casing are normalised, so `read-only agent` would also
match.
