# Troubleshooting

A grab-bag of symptoms and the fixes we ran into the most often.

## "AUTH_MODE=none is forbidden in production" at boot

The container refuses to start because `ENVIRONMENT=production` and
`AUTH_MODE=none` are intentionally incompatible.

**Fix:** either pick a real auth mode (`zammad` or `oidc`) or set
`ENVIRONMENT=development`. The latter is appropriate only for local
work — never expose the resulting endpoint on a network.

## "ZAMMAD_OAUTH_CLIENT_ID and ZAMMAD_OAUTH_CLIENT_SECRET required for AUTH_MODE=zammad"

You set `AUTH_MODE=zammad` but didn't fill in the Zammad-side OAuth2
Application credentials.

**Fix:** in Zammad, **Admin → Manage → OAuth2 Applications → Add**.
Copy the generated client ID and secret into `.env`. See
[authentication.md → Mode 1](./authentication.md#mode-1--zammad-as-oauth2-provider-recommended).

## "AUTH_STORAGE_ENCRYPTION_KEY is not a valid Fernet key"

You set `AUTH_REDIS_URL` (Redis-backed OAuth state) but pasted something
that isn't a Fernet key — usually a 64-char hex string (that's the JWT
signing key format) or a raw 32-byte blob.

**Fix:** generate a real Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

A Fernet key is 44 characters, base64-encoded, ends with `=`. The
container fails fast at boot rather than producing a cryptic `InvalidToken`
error on first OAuth login.

## OAuth callback fails with "redirect_uri_mismatch"

Symptom: the user logs into Zammad successfully but Zammad shows an error
page instead of redirecting back.

**Cause:** the redirect URI configured in Zammad does not exactly match
what the MCP server sends. Common offenders:

- Trailing slash differences (`https://x.example.com` vs
  `https://x.example.com/`).
- HTTP vs HTTPS.
- Stale value from before a hostname change.

**Fix:** in Zammad **Admin → Manage → OAuth2 Applications**, open the
app and confirm the Redirect URI is **exactly**
`${PUBLIC_BASE_URL}/auth/callback`, including scheme and no trailing
slash.

## Users get "RoleNotAllowedError" but they should be allowed

The middleware compares lowercased role names. Custom Zammad roles with
unusual casing or spaces sometimes don't match what operators expect.

**Fix:** turn on audit-only mode temporarily:

```env
MCP_ROLE_CHECK_AUDIT_ONLY=true
```

Restart, ask the user to retry, and inspect the log line:

```text
auth.role_denied_audit_only_passing_through sub=... user_roles=['some role'] allowed_roles=['admin', 'agent']
```

Copy the exact role name from `user_roles` into `MCP_ALLOWED_ROLES`, then
turn audit-only back off.

## "MissingUpstreamToken" in tool logs

Symptom: a tool call fails with `MissingUpstreamToken` even though the
user is logged in.

**Cause:** the OAuth state store was wiped (container restart on disk
storage without a mounted volume, or `redis-cli FLUSHDB` followed by a
restart). The MCP-issued JWT still validates but the Zammad token it
referenced is gone.

**Fix #1 (immediate):** the AI client must re-authenticate. Most clients
do this automatically on the next failed call.

**Fix #2 (prevent recurrence):** mount the OAuth state path as a Docker
volume (the bundled compose files already do this). For Redis,
ensure persistence is on (`--appendonly yes`, present by default).

## 504 Gateway Timeout from Traefik (Coolify)

You added a custom `networks:` block to the Coolify compose file. Coolify
attaches services to its own UUID-named network, so adding a second
network creates a routing ambiguity for Traefik.

**Fix:** don't add custom networks on Coolify. The bundled
`docker-compose.coolify.yml` deliberately omits them — service-name DNS
between `zammad-mcp` and `redis` works through Coolify's auto-created
bridge. See the top-of-file comment for details and the (heavily
discouraged) workaround if you really need a named network.

## Container exits with "Address already in use"

Another service is bound to `8000` on the host.

**Fix:** change `ZAMMAD_MCP_PORT` in `.env` (used only in
`docker-compose.development.yml`), or stop the conflicting service.

## "Zammad timed out after 3 retries" on every call

Zammad is unreachable from inside the MCP container.

**Check:**

```bash
docker exec bg-zammad-mcp curl -fsS -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Token token=$ZAMMAD_API_TOKEN" \
    "${ZAMMAD_URL}/api/v1/version"
```

If that returns a connection refused / DNS error, the MCP container
can't reach the Zammad host. Common causes:

- The MCP container is on a different Docker network than Zammad.
  Connect them: `docker network connect <zammad-network> bg-zammad-mcp`.
- `ZAMMAD_URL` uses the host's public hostname but DNS inside the
  container resolves it to a private IP (or vice versa).
- A firewall blocks the outbound connection.

## "401 Unauthorized" from Zammad on every call

The token format is wrong. Zammad has two acceptable formats:

| Token type | Header format |
| --- | --- |
| OAuth2 access token (Mode 1) | `Authorization: Bearer <token>` |
| Personal Access Token (Mode 2/3) | `Authorization: Token token=<token>` |

The MCP server picks the right one automatically — the failure usually
means the configured token is for the wrong slot. Check that:

- In `AUTH_MODE=zammad`, you're testing through the OAuth flow (not
  with a static token).
- In `AUTH_MODE=oidc/none`, `ZAMMAD_API_TOKEN` is a Personal Access
  Token (Profile → Token Access), not an OAuth client secret.

## How to read the structured logs

JSON logs look intimidating but `jq` makes them tractable:

```bash
docker logs bg-zammad-mcp 2>&1 | jq -r 'select(.level == "error")'

docker logs bg-zammad-mcp 2>&1 | jq -r 'select(.event | startswith("auth."))'

docker logs bg-zammad-mcp 2>&1 | jq -r 'select(.event == "auth.role_denied") | "\(.timestamp) \(.login) -> \(.method)"'
```

Switch to `LOG_FORMAT=console` for human-readable Rich output during dev.

## I lost the Zammad client secret

You can't retrieve it — Zammad shows it once and stores only a hash.

**Fix:** in Zammad, **Admin → Manage → OAuth2 Applications**, open the
app, click **Regenerate Secret**. The old secret stops working
immediately. Update `.env` and restart the MCP container.

Existing AI client sessions silently fail on the next token refresh and
prompt the user to re-authenticate.

## Reset everything (nuclear option)

```bash
docker compose -f docker-compose.traefik.yml down -v   # -v wipes named volumes
docker compose -f docker-compose.traefik.yml up -d
```

That regenerates the disk-OAuth volume and (if Redis-backed) wipes
Redis. All AI clients have to re-register via DCR and re-authenticate.
Last resort, not routine maintenance.
