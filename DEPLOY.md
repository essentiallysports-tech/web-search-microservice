# Deploying to EC2

GitHub is the source of truth. One EC2 instance runs Docker Compose; nginx on the
host terminates TLS.

```
you ──push──► GitHub ──CI──► Actions ──ssh──► EC2
                                              │
                          nginx (host, TLS) ──┤
                                  │           │
                        127.0.0.1:8000 ──► api container
                                              │
                                        redis container
                                     (internal network only)
```

About 40 minutes end to end, most of it waiting on package installs and DNS.

---

## Instance size — measured, not guessed

I load-tested this build locally: extracting ten large Wikipedia articles took the API
container from **84 MiB to 352 MiB**, and it **plateaued there across forty pages**.
Python's allocator keeps the arena rather than returning it, so ~352 MiB is the steady
state after any real extraction load — not a leak. The same burst consumed **21.7
CPU-seconds at 1.2 cores average**.

| | t3.small (2 GiB) | t3.medium (4 GiB) |
|---|---|---|
| Cost, us-east-1 | ~$15/mo | ~$30/mo |
| **Works?** | **Yes, with tuning** | Yes, as shipped |
| Overlay to use | `+ docker-compose.t3small.yml` | `docker-compose.prod.yml` |
| api limit | 768 MB | 2 GB |
| redis cache | 384 MB | 1 GB |
| Container total | 1.28 GB of 2 GB | 3.17 GB of 4 GB |

**t3.small is feasible.** CPU is not the constraint — 0.36 credits per ten-page burst
against 24 credits/hour earned means roughly 66 such bursts/hour sustainable, and your
200k/month ceiling works out to ~290 pages/hour. RAM is the binding constraint, and the
default limits don't fit: `2g + 1200m = 3.2 GB` on a 2 GB box.

`docker-compose.t3small.yml` makes it fit by changing three things:

- `LOCAL_CACHE_SIZE` 512 → 128. The L1 cache is bounded by **entry count, not bytes**,
  and holds extracted pages with their full markdown. At ~330 KB/page measured, 512
  entries is a ~165 MB ceiling; 128 caps it near 40 MB. Costs L1 misses that fall
  through to Redis — a ~3 ms round trip, never a provider call.
- `MAX_CONCURRENCY` 10 → 5, `EXTRACT_CONCURRENCY` 5 → 3. Peak memory scales with
  simultaneous lxml parses (a DOM runs 5–10× the source size). At 1.2 cores measured,
  2 vCPU had little headroom for 10 concurrent fetches anyway.
- Redis `maxmemory` 1gb → 384mb. Payloads are zstd-compressed to ~80 KB, so that still
  holds ~4,800 pages against ~12,800.

**The one thing to watch on t3.small:** a smaller Redis may drop the cache hit rate
below the 79% baseline, and a lower hit rate costs provider calls. You could spend the
$15/mo you saved on the instance back in Serper credits. Check the hit rate in week one;
if it has fallen, t3.medium is the cheaper option overall.

> ⚠️ Never use the base `docker-compose.yml` limits alone on a small box. Its 4g API
> limit predates the removal of the Chromium tier. On a 4 GB instance, 4g + Redis's
> 1200m over-commits by 1.2 GB — the container limit never fires and the **host** OOMs,
> so the kernel kills whatever it picks rather than the process at fault. Realistically
> that means losing Redis (your whole cache *and* every issued API token) because of
> something the API did.

**Storage:** 20 GB gp3, encrypted. The image is 366 MB and Redis is memory-only
(`--save ""`), so nothing grows except logs, which rotate at 10 MB × 3 per container.

---

## Other decisions

### DNS and TLS

Get the hostname sorted before you start — Let's Encrypt cannot issue for a bare IP, and
Vercel needs a stable HTTPS URL to reach the admin API.

- Allocate an **Elastic IP** and associate it, or the address changes on stop/start and
  breaks both DNS and your certificate.
- Point an `A` record at it (`search.yourdomain.com`).
- Confirm before bootstrapping: `dig +short search.yourdomain.com` must return it.

### Redis: container or ElastiCache?

**Keep the container.** Persistence is off with LRU eviction, so losing it costs a cold
cache, not data.

Move to ElastiCache when you run a **second instance** — issued API tokens live in Redis,
so two boxes with separate containers means a token minted on one is invisible to the
other.

### How secrets reach the box

By hand, once, over SSH. **`.env` is never in the repo and never in the image.** The
`.gitignore`, the pre-commit hook and a CI check all enforce that, because it holds live
Serper, Brave, Firecrawl and Anthropic credentials.

Upgrade to SSM Parameter Store when you want an audit trail on who read what.

---

## Security group

Create it before launching. This is the real network control; the `ufw` rules
`bootstrap.sh` adds are a second layer for when this gets widened by accident.

**Inbound**

| Port | Source | Why |
|---|---|---|
| 22 | **your IP only** | SSH. Never `0.0.0.0/0`. |
| 80 | `0.0.0.0/0` | ACME challenge + redirect to HTTPS. |
| 443 | `0.0.0.0/0` | The API. |

**Outbound:** all traffic. The service calls Serper, Brave, Firecrawl, Anthropic and
arbitrary websites for extraction; restricting egress breaks it.

**Do not open:**

- **8000** — the API binds `127.0.0.1` only. It also runs with
  `--forwarded-allow-ips "*"`, which is safe *because* only nginx can reach it. Expose it
  and a caller can forge `X-Forwarded-For`.
- **6379** — Redis has no password. Exposing it hands over your cache, your rate-limit
  counters, and every issued API token.

Also set **IMDSv2 to required** in the instance metadata options.

GitHub Actions needs SSH access, and its runners have no fixed IP. Either add
`0.0.0.0/0` on port 22 (bad), or use one of:

- **A self-hosted runner** on the instance — no inbound SSH at all.
- **Tailscale / AWS SSM** in the workflow instead of raw SSH.
- **Manual deploys** over your own SSH (see below) and keep 22 locked to your IP.

Start with manual deploys. Add the Actions path once the rest is proven.

---

## Part 1 — Push to GitHub

### 1. Verify nothing secret is staged

The repo is already initialised with a hardened `.gitignore` and a pre-commit hook.
Enable the hook and check:

```bash
git config core.hooksPath deploy/githooks
```

```bash
git check-ignore -v .env && echo "GOOD: .env is ignored"
```

```bash
git status --short | head -30
```

`.env` must not appear. If it does, **stop** — do not commit until it is ignored.

### 2. Commit

```bash
git add -A
git commit -m "Web search microservice with token auth and EC2 deploy config"
```

The pre-commit hook scans staged content for credential shapes (`esw_`, `fc-`,
`sk-ant-`, `BSA`, `AKIA`, PEM headers) and for a populated `.env.example`. It is tested
against six leak types. If it blocks you, it is probably right.

### 3. Create a **private** GitHub repo and push

```bash
git remote add origin git@github.com:YOUR-ORG/search-service.git
git push -u origin main
```

**Private.** The repo contains your cost model, provider strategy, and admin endpoint
paths. None of it is a credential, but none of it needs to be public either.

If a secret ever does reach GitHub, **rotate the credential**. Rewriting history does not
un-leak it — assume anything pushed to a remote is compromised.

---

## Part 2 — Set up the instance

### 4. Launch

- Ubuntu 24.04 LTS
- `t3.small` (with the tuning overlay) or `t3.medium`
- 20 GB gp3, encrypted
- The security group above, IMDSv2 required
- Elastic IP associated, DNS resolving to it

### 5. Clone from GitHub

```bash
ssh ubuntu@YOUR-HOST
```

Give the instance read access to the private repo — a **deploy key** is the right scope,
since it grants one repo rather than your whole account:

```bash
ssh-keygen -t ed25519 -C "ec2-search-deploy" -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

Add that public key to the repo under **Settings → Deploy keys**, read-only. Then:

```bash
git clone git@github.com:YOUR-ORG/search-service.git ~/search-service
cd ~/search-service
```

### 6. Bootstrap the host

```bash
sudo bash deploy/bootstrap.sh search.yourdomain.com you@yourdomain.com
```

Installs Docker, nginx, certbot, 2 GB swap, `ufw` and unattended security upgrades;
obtains the TLS certificate; installs the production nginx site.

**DNS must already resolve to this box** or the certificate request fails.

Log out and back in so docker group membership applies:

```bash
exit && ssh ubuntu@YOUR-HOST
```

### 7. Write the production environment file

```bash
cd ~/search-service
cp .env.prod.example .env
```

Generate the admin key — this is what the token UI signs in with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `.env` and set at minimum:

```
ENVIRONMENT=prod
AUTH_ENABLED=true
SERVICE_API_KEYS=<the key you just generated>
SERPER_API_KEY=<from your Serper account>
FIRECRAWL_API_KEY=<from your Firecrawl account>
```

```bash
chmod 600 .env
```

> ⚠️ Comments go on their own line. `python-dotenv` reads `KEY=  # note` as the literal
> value `# note` — this silently enabled a paid tier once. `deploy.sh` refuses to start
> if it detects it on a credential field.

### 8. Deploy

On **t3.medium**:

```bash
bash deploy/deploy.sh
```

On **t3.small**, add the tuning overlay:

```bash
bash deploy/deploy.sh docker-compose.t3small.yml
```

The script refuses to start on a misconfiguration, builds, waits for health, and then
**verifies an unauthenticated request returns 401** before declaring success. That last
check is the one worth having: it separates "the service started" from "the service is
not open to the internet".

### 9. Verify from outside

```bash
curl https://search.yourdomain.com/livez
```

```bash
curl -s https://search.yourdomain.com/health | python3 -m json.tool
```

```bash
# Must be 401 — proves auth is enforced through the whole public path
curl -o /dev/null -w '%{http_code}\n' -X POST https://search.yourdomain.com/search \
  -H 'Content-Type: application/json' -d '{"query":"test"}'
```

```bash
# Must be 200 with real results
curl -X POST https://search.yourdomain.com/search \
  -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"what is a cdn","count":3}'
```

Check the TLS grade at <https://ssllabs.com/ssltest/>.

### 10. Issue tokens for your apps

Point the admin UI at the host — in the `admin-ui` Vercel project set:

```
SEARCH_SERVICE_URL=https://search.yourdomain.com
```

Or directly:

```bash
curl -X POST https://search.yourdomain.com/admin/tokens \
  -H "X-API-Key: $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"blog-app","expires_in_days":90}'
```

The secret is returned **once**. One token per app — they get separate rate-limit budgets,
so one app looping cannot starve another.

---

## Part 3 — Deploying updates

### Manual (start here)

```bash
ssh ubuntu@YOUR-HOST 'cd ~/search-service && git pull && bash deploy/deploy.sh'
```

### Automated via GitHub Actions

`.github/workflows/ci.yml` runs on every push. `.github/workflows/deploy.yml` deploys,
gated on CI passing.

> ⚠️ **CI does not verify correctness.** `tests/` is gitignored by choice — the
> 502-test suite lives on the developer's machine and is not pushed, so the runner has
> nothing to run. CI checks that the package installs, the image builds, all six public
> endpoints plus `/admin` are actually mounted, and that no credential was committed.
> A green check means "it will start", not "it works".
>
> **Run the suite locally before every push:**
>
> ```bash
> python -m pytest tests -q      # expect 502 passed
> ```
>
> The CI test step runs the suite *if* it is present, so this starts gating correctness
> automatically the day `tests/` is un-ignored — no workflow edit needed.

It is **manual-trigger by default** (Actions → Deploy to EC2 → type `deploy`), because
deploying costs money when it runs and a bad deploy is downtime for every consuming app.
To deploy on every merge to `main`, uncomment the `push:` trigger — keep the `needs: ci`
gate either way.

Repository secrets required (**Settings → Secrets → Actions**):

| Secret | Value |
|---|---|
| `EC2_HOST` | The instance's public DNS or Elastic IP |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | A **private** key whose public half is in `~/.ssh/authorized_keys` on the box |
| `SERVICE_HOST` | `search.yourdomain.com`, for the post-deploy check |

Generate a dedicated key for CI rather than reusing your personal one:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ./gha_deploy -N ""
```

Put `gha_deploy.pub` in the instance's `authorized_keys`, paste `gha_deploy` into
`EC2_SSH_KEY`, then delete both local files.

Remember the runner-IP problem from the security group section — a self-hosted runner or
SSM avoids opening port 22 to the world.

### Rolling back

Containers are replaced in place, so rollback is deploying the previous commit:

```bash
ssh ubuntu@YOUR-HOST 'cd ~/search-service && git reset --hard HEAD~1 && bash deploy/deploy.sh'
```

Redis keeps running throughout, so neither the cache nor issued tokens are lost. Expect a
few seconds of 502s while the API container restarts; add a second replica if that
matters.

---

## Scaling on one box

At 4 vCPU or more, run multiple API workers. `WEB_CONCURRENCY=1` per container is
deliberate — it keeps the Prometheus counters correct — so scale by replica:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  -f docker-compose.scale.yml up -d --scale api=3
```

The scale overlay removes the API's published port and puts an nginx load balancer
container on `127.0.0.1:8000`, so the host nginx config needs no change. Plain
`--scale api=3` without it fails: three containers cannot bind one host port.

Not on t3.small — 3 × 768 MB does not fit in 2 GB.

---

## What to watch in the first week

Baselines are in `HANDOFF.md`. The four that matter:

```bash
curl -s http://127.0.0.1:8000/metrics | grep -E 'wss_(search_credits|external_calls|cache_events|extract_)'
```

| Signal | Expected | Investigate when |
|---|---|---|
| `wss_cache_events_total` hit ratio | ~79% | Below 60% → callers varying `count`, using `bypass_cache`, or Redis too small |
| `wss_search_credits_total` | ~208 per 1k requests | Rising faster than traffic → cache misses |
| `wss_extract_rescues_total{prior_status="blocked"}` | 33% paid share | **Above 55%** → the datacenter IP is being scored worse |
| `wss_extract_escalations_total{reason="skipped_no_time"}` | **0** | Any sustained count → timeouts crowding the deadline |

**Expect the blocked share to rise after this move.** Development ran from a residential
connection; datacenter ranges are pre-scored worse by anti-bot systems, and blocked pages
fall through to the paid scraper by design. That converts into cost, not failure.
`PROXY_URL` is the lever if it gets bad — and it has never been tested against a real
proxy, so prove it before you need it.

Size the limits from your own traffic rather than my synthetic Wikipedia load:

```bash
docker stats --no-stream
docker compose exec redis valkey-cli info memory | grep used_memory_human
docker compose exec redis valkey-cli info stats | grep evicted_keys
```

`evicted_keys` climbing on t3.small means 384 MB is too small for your working set.

---

## Still open

Carried from `HANDOFF.md`, none of it blocking:

- **No spend ceiling.** The rate limiter bounds request *rate*, not money, and
  `bypass_cache` is caller-controlled — a client looping with it takes the bill from
  $0.41 to $1.97 per 1k. Decide what happens when a monthly budget is hit. The cheapest
  partial fix is refusing `bypass_cache` from ordinary tokens.
- **`/admin/*` is internet-reachable**, because Vercel's egress addresses are not fixed.
  nginx rate-limits it to 20/min and the static admin key is the real control. If you
  later put the UI behind a fixed egress, swap that for an IP allow-list in
  `deploy/nginx-site.conf`.
- **No load testing** beyond 8 concurrent requests and 3 replicas.
- **No off-box backup.** Nothing here needs one — Redis is a cache and the only durable
  state is issued tokens, which can be reissued. If losing tokens becomes unacceptable,
  that is the argument for ElastiCache with snapshots.
