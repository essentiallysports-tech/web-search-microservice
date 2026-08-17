# Search Service — Token Admin

Internal panel for issuing and revoking API tokens for the web search microservice.
Deploys to Vercel independently; the microservice is deployed separately.

## What it does

- Sign in with a **static admin key** (a value from the service's `SERVICE_API_KEYS`)
- Issue a token per consuming app, with an optional expiry
- Show the secret **once** at creation
- List live tokens with created / expires / last-used
- Revoke a token, effective immediately

## Why it needs a server side

The admin key must never reach the browser, so every call to the microservice goes
through a Next.js route handler (`app/api/*`). The browser talks only to this app; this
app talks to the service.

Two decisions worth keeping:

**The admin key is not a Vercel environment variable.** It is entered at sign-in and
stored in an httpOnly, sameSite cookie for 8 hours. That means this deployment holds no
long-lived credential capable of minting API tokens — a compromised Vercel project, or a
build log that echoed its env, leaks nothing. The cost is that you paste the key once per
session.

**Issued tokens cannot administer tokens.** The service gates `/admin/*` on static keys
only and returns `403` for a valid-but-non-admin credential. So a leaked consumer token
cannot escalate into issuing more.

## Setup

```bash
npm install
```

```bash
cp .env.example .env.local
```

Set one variable:

```
SEARCH_SERVICE_URL=https://search.internal.example.com
```

Then:

```bash
npm run dev
```

## Deploying to Vercel

```bash
npx vercel --cwd admin-ui
```

Set `SEARCH_SERVICE_URL` in the Vercel project settings for Production and Preview.

**The service must be publicly reachable over HTTPS** for Vercel's servers to call it.
`docker-compose.yml` publishes to `127.0.0.1:8000`, so it is not reachable from the
internet as shipped — put it behind a reverse proxy with TLS first, and restrict it to
the callers that need it.

Because this panel can mint credentials that spend money, also gate the deployment
itself — Vercel Authentication (Project → Settings → Deployment Protection) restricts it
to your team with no code change. The admin key is the second factor, not the only one.

## Prerequisites on the service

The service needs auth enabled and at least one static key, or `/admin/*` is open:

```
AUTH_ENABLED=true
SERVICE_API_KEYS=<generated-admin-key>
```

Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Tokens are stored in the service's Redis. If Redis is down, issuing and listing return
`503` while static keys keep working — which is exactly why static keys still exist.

## Layout

```
app/page.tsx              the whole UI (one client component)
app/api/session/route.ts  sign in / out; verifies the key before storing it
app/api/tokens/route.ts   list + create
app/api/tokens/[id]/      revoke
lib/service.ts            server-only client for the admin API
lib/session.ts            httpOnly cookie handling
```
