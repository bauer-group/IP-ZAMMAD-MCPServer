# Operations

Running this in production: what to watch, what to rotate, and what to do when
it breaks. For first-time deployment see [installation.md](installation.md).

---

## The two secrets, and what rotating them costs

| Secret | What it protects | Cost of rotation |
| --- | --- | --- |
| `AUTH_JWT_SIGNING_KEY` | Signs the JWTs the server issues to MCP clients | **Every client must re-authenticate.** Existing JWTs stop validating immediately. |
| `AUTH_STORAGE_ENCRYPTION_KEY` | Encrypts the stored OAuth state (registered clients, upstream Zammad tokens) | **The stored state becomes unreadable.** Every client must re-register and re-authenticate. |

Neither rotation loses Zammad data — only sessions. Both are a deliberate,
announced action rather than something to do casually.

```bash
python scripts/generate-env.py     # generates fresh values in the right format
```

Rotate when: someone who had access leaves, a value was pasted somewhere it
should not have been, or a host was compromised. Rotate them **one at a time**
if you want to keep the blast radius small — they are independent.

`ZAMMAD_OAUTH_CLIENT_SECRET` is rotated in Zammad (Admin → System → API →
Applications), and unlike a Personal Access Token it stays readable afterwards
via the row's **View** action, so a lost copy is recoverable without
re-registering.

---

## The OAuth state store

Two backends, chosen by whether `AUTH_REDIS_URL` is set:

* **Redis** (production). Restart-safe, and the only option if you ever run more
  than one replica — the store is shared state, not a cache.
* **Disk** (single node). Fernet-encrypted files under
  `AUTH_DISK_STORAGE_PATH`. **Mount it as a volume.** Without one, every
  container restart wipes it and every user has to re-authenticate.

### Backup and restore

The store holds registered clients and encrypted upstream tokens. Losing it is
recoverable — everyone re-authenticates — so it does not need the ceremony of a
database backup, but if you want continuity across a host move:

```bash
# Redis
docker compose exec redis redis-cli SAVE
docker cp <redis-container>:/data/dump.rdb ./oauth-state.rdb

# Disk
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar czf /backup/oauth-state.tgz -C /data .
```

A restore is only useful **together with the matching
`AUTH_STORAGE_ENCRYPTION_KEY`** — the data is encrypted with it. Back up the key
separately, or the dump is noise.

---

## What to watch

The server logs structured JSON (`LOG_FORMAT=json`). These events are the ones
worth an alert rather than a dashboard:

| Event | Level | What it means |
| --- | --- | --- |
| `auth.obo_missing_per_user_token_falling_back_to_static` | WARNING | A call ran under the shared token instead of the user's. In `zammad` mode this should be impossible — the server refuses to boot with both configured. If you see it, something is wrong with the claim wiring. |
| `auth.role_denied` | WARNING | Someone outside `MCP_ALLOWED_ROLES` tried to use the server. Expected occasionally; a burst is not. |
| `zammad.userinfo_unreachable` / `zammad.userinfo_server_error` | WARNING | Zammad is not answering token verification. Every request 401s while this persists. |
| `zammad.userinfo_retrying` | INFO | A transient blip that the single retry absorbed. Frequent occurrences mean Zammad is struggling. |
| `audit.write` | INFO | Every non-read tool call, with the caller and the object touched. This is the trail for "who changed ticket 4711". |
| `audit.write_failed` | WARNING | A write raised. |

`/healthz` is unauthenticated and does **not** depend on Zammad — it answers 200
as long as the process is alive. That is deliberate: a liveness probe that fails
when Zammad hiccups would restart a perfectly healthy container. To check
upstream reachability, use the `bg.health` tool.

---

## Verifying a deployment

After any deploy, in order of how much they tell you:

```bash
curl -fsS https://your-host/healthz

# What Claude reads to bootstrap. A 404 here is a total outage for every client.
curl -fsS https://your-host/.well-known/oauth-protected-resource/mcp
curl -fsS https://your-host/.well-known/oauth-authorization-server

# The 401 challenge must carry resource_metadata — this is what Claude follows.
curl -sSi -X POST https://your-host/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  | grep -i 'www-authenticate'
```

Then connect a real client and call `get_me` — it is the cheapest end-to-end
proof that inbound auth, the role gate and the outbound Zammad call all work.

---

## Rollback

Images are tagged per release on GHCR, so a rollback is a tag change:

```bash
ZAMMAD_MCP_VERSION=<previous> docker compose -f docker-compose.traefik.yml up -d
```

Nothing in this server migrates or owns data — Zammad holds all of it, and the
OAuth store is disposable — so rolling back is safe at any point. The one thing
to check is whether the older image predates a settings change: a version that
does not know a variable ignores it (`extra="ignore"`), which can silently
re-enable behaviour you thought you had turned off.

---

## Upgrades

See [upgrading.md](upgrading.md). The short version: read the release notes for
breaking changes, check whether `.env.example` grew any variables, redeploy, and
re-run the verification block above.
