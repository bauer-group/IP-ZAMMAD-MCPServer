# Security Policy

## Reporting a vulnerability

Please report security issues privately to **security@bauer-group.com**, or via
GitHub's [private vulnerability reporting](https://github.com/bauer-group/IP-ZAMMAD-MCPServer/security/advisories/new)
on this repository. Do not open a public issue for a suspected vulnerability.

Include what you did, what you expected, and what happened — a reproduction is
worth more than a severity rating. We aim to acknowledge within two working days.

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Anything older | No — upgrade first, then report if it persists |

This is an internal BAUER GROUP project released under MIT. There is no
long-term-support branch; fixes land on `main` and ship in the next release.

## What this server is trusted with

Worth stating plainly, because it shapes what counts as a vulnerability here:
the MCP server holds **live Zammad access tokens for real users** and makes API
calls on their behalf. A flaw that lets one user's request run under another
user's token, or that leaks a token to an MCP client, is the most serious class
of bug in this codebase.

The trust model is documented in
[docs/ZAMMAD-MCP-SPEC.md](docs/ZAMMAD-MCP-SPEC.md) and
[docs/authentication.md](docs/authentication.md). In short:

- **Inbound** — OAuth 2.1 + PKCE. The server issues its own JWT; clients never
  see a token Zammad would accept.
- **Outbound** — the caller's own Zammad token is forwarded per request, so
  Zammad's permission system does the real authorization. There is no
  ambient service-account identity in the primary mode; the server refuses to
  start if a static token is configured alongside it.
- **At rest** — OAuth state is Fernet-encrypted, whether in Redis or on disk.

## Deployment expectations

These are the operator's responsibility, and getting them wrong is not a
vulnerability in the software:

- `AUTH_MODE=none` is refused outside `ENVIRONMENT=development`.
- `AUTH_JWT_SIGNING_KEY` and `AUTH_STORAGE_ENCRYPTION_KEY` must be real secrets,
  generated per deployment (`python scripts/generate-env.py`), and rotated if
  exposed.
- `MCP_ALLOWED_CLIENT_REDIRECT_URIS` should stay restricted on a public
  deployment — dynamic client registration is unauthenticated by design.
- Terminate TLS in front of the container and keep `ZAMMAD_VERIFY_TLS=true`.

## Known trade-offs

Documented rather than hidden, so you can decide whether they matter to you:

- **Verification caching.** A successful token verification is cached for
  `MCP_ROLE_CACHE_TTL_SECONDS` (default 30), so a revocation or role change can
  take up to that long to take effect on an active session. Failures are never
  cached. Set it to `0` to verify against Zammad on every single request.
- **`MCP_ALLOW_STATIC_FALLBACK`.** Setting this re-enables the shared-identity
  fallback that the boot guard exists to prevent. It is off by default and
  should stay off.
- **Zammad's scope model.** Zammad's OAuth2 server issues a single `full` scope
  (see [docs/zammad-7.md](docs/zammad-7.md)), so the upstream token is not
  narrowed to what the MCP needs. Per-user permissions still apply — they come
  from the user's Zammad roles, not from the token's scope.
