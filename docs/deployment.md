# Deployment

How to put `compose.prod.yaml` on the internet safely: what sits in front of it,
how the real client address reaches the API, how the data is backed up and
restored, and what the stack can carry (see [Capacity](#capacity)).

```text
browser ──https──▶ [Cloudflare] ──https──▶ TLS proxy (Caddy) ──http──▶ web (nginx :8080) ──▶ api ──▶ postgres
                    optional                 on this host                 SPA + /api proxy                └─▶ redis
```

## 1. TLS in front of the stack

Only `web` publishes a port (`WEB_HOST_PORT`, default 8080). Terminate TLS in
front of it and forward plain HTTP with `X-Forwarded-Proto: https`; nginx then
adds HSTS. A minimal Caddyfile:

```caddyfile
chat.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Caddy obtains and renews the certificate, and sets `X-Forwarded-Proto` and
`X-Forwarded-For` itself. Bind `WEB_HOST_PORT` to localhost only if nothing but
the proxy should reach it (`127.0.0.1:8080:8080` in an override file), and close
it in the host firewall either way.

## 2. The real client address (`TRUSTED_PROXY_CIDRS`)

Login, registration, and password reset are rate-limited per client IP, and
the API learns that IP from the `X-Forwarded-For` header nginx sends. That
header is only as honest as the hop that wrote it, so nginx decides who may
write it:

- `X-Forwarded-For` is believed **only** when the connection comes from an
  address in `TRUSTED_PROXY_CIDRS`. Walking the header from the right, the
  first address that is not a trusted proxy is the client.
- nginx then **replaces** the header with that single address before
  proxying. A client that sends its own `X-Forwarded-For` gains nothing.

Set it to the addresses your proxies connect from:

| Setup | `TRUSTED_PROXY_CIDRS` |
| --- | --- |
| Caddy/Traefik on the same host | the Docker bridge, `172.16.0.0/12` |
| A cloud load balancer | its subnet |
| Cloudflare → Caddy on the host | the bridge **plus** Cloudflare's ranges (below) |

Compose refuses to start without it. Get it wrong in one direction and every
visitor shares one address and one rate-limit bucket (an empty value behind a
proxy). Get it wrong in the other (trusting `0.0.0.0/0`) and clients can choose
their own address again. Check after deploying: sign in with a wrong password
11 times from one machine and confirm the 11th is refused, then try once from
another network and confirm it is not.

## 3. Cloudflare in front (recommended for a public launch)

The app limits abuse per IP and per account, but a single server cannot absorb
a volumetric flood. Cloudflare's free plan absorbs that at its edge. None of
this needs a code change; it needs your Cloudflare account and domain.

1. **Proxy the DNS record** (orange cloud) for `chat.example.com`.
2. **SSL/TLS mode: Full (strict).** Caddy's certificate is valid, so
   Cloudflare can verify the origin. Never use Flexible: it sends traffic to
   the origin unencrypted.
3. **Trust Cloudflare in nginx.** Add its ranges to the bridge:

   ```bash
   CF=$( (curl -s https://www.cloudflare.com/ips-v4; echo; curl -s https://www.cloudflare.com/ips-v6) | grep . | paste -sd, -)
   echo "TRUSTED_PROXY_CIDRS=172.16.0.0/12,$CF"   # into .env.production
   ```

   and tell Caddy to accept Cloudflare's `X-Forwarded-For` (otherwise Caddy
   overwrites it with the Cloudflare edge address):

   ```caddyfile
   {
       servers {
           trusted_proxies static <the same Cloudflare ranges>
       }
   }
   ```

   The ranges change rarely; re-run this when Cloudflare announces a change.
4. **Lock the origin to Cloudflare.** In the host firewall, allow 443 only from
   Cloudflare's ranges. Otherwise an attacker who finds the server's IP simply
   goes around the edge.
5. **Rate-limiting rule** (Security → WAF → Rate limiting rules): for example,
   requests to `/api/v1/auth/` above 20 per minute per IP → block for 10
   minutes. It stops floods before they reach the server; the app's own limits
   still apply behind it.
6. **Leave `/api/` uncached.** Cloudflare does not cache it by default (no
   cacheable extension, `no-store` on the entry HTML). Answer streams are
   server-sent events; the API sends a heartbeat every 15 s, well inside
   Cloudflare's 100 s idle timeout.
7. **Bot Fight Mode** (Security → Bots) is a reasonable default for the
   public pages.

## 4. Backups

The `backup` service runs `pg_dump` every `BACKUP_INTERVAL_SECONDS` (default
daily) into `BACKUP_HOST_DIR` (default `./backups`) and deletes dumps older
than `BACKUP_KEEP_DAYS` (14). Dumps are written under a temporary name and
renamed only when complete, are readable by their owner only (`600`), and
contain the whole database, LangGraph checkpoints included.

```bash
make prod-backup                                            # a dump right now
make prod-restore file=app-20261011T020000Z.dump confirm=yes   # stops the API, restores, starts it
docker compose --env-file .env.production -f compose.prod.yaml ps backup   # "unhealthy" = no recent dump
```

**Copy the dumps off the machine.** A dump on the same disk survives a bad
migration or a dropped table, not a lost server. For example, with
[rclone](https://rclone.org) configured for any object store, from the host's
crontab:

```cron
30 3 * * * rclone copy /srv/chat/backups remote:chat-backups --max-age 48h
```

Turn on the bucket's versioning or object lock, so ransomware on the server
cannot delete the off-site copies too.

**Restore drill.** The procedure was tested on 2026-10-11 against a database
with users, conversations, runs, citations, and 309 checkpoints. Every table
matched after restore, a second restore over existing data succeeded, and a
truncated dump was refused before any data was touched. Repeat the drill
whenever the schema changes and at least quarterly: restore the latest dump
into a scratch database and compare row counts.

```bash
docker compose --env-file .env.production -f compose.prod.yaml exec postgres \
  psql -U app -d postgres -c 'CREATE DATABASE restore_drill'
docker compose --env-file .env.production -f compose.prod.yaml run --rm \
  -e PGDATABASE=restore_drill backup sh /deploy/restore.sh /backups/<file>
```

`restore.sh` runs `pg_restore` in a single transaction: if it fails part-way,
the database is left as it was.

## 5. Moving to the pgvector image

Document retrieval ([ADR-0015](adr/0015-chat-scoped-pdf-retrieval.md))
replaced `postgres:16-alpine` with `pgvector/pgvector:0.8.7-pg16-bookworm`.
Both are PostgreSQL 16, but Alpine uses musl and the pgvector image uses
glibc, and the two sort `en_US.utf8` text differently. **Do not start the new
image on the old data volume.** PostgreSQL would start without any warning,
and text indexes, including the unique index on `users.email`, could then
return wrong results. The documents migration detects this and refuses to
run, but a refused migration still means downtime, so move the data this way
instead.

Restore into the new, empty volume **before** the migration job runs. If the
migrations ran first, the restored `alembic_version` would point back at the
old revision, and the next upgrade would fail on tables that already exist.

```bash
PROD="docker compose --env-file .env.production -f compose.prod.yaml"

# 1. On the old release: a fresh dump, checked, then stop everything.
make prod-backup
ls -t backups/app-*.dump | head -1                     # note the file name
$PROD run --rm backup pg_restore --list /backups/<file> > /dev/null && echo dump ok
make prod-down

# 2. Remove the old volume. The dump is now the only copy: copy it off the
#    machine first (section 4).
docker volume rm web-search-chatbot-prod_postgres-data

# 3. On the new release: start only PostgreSQL, restore, then migrate and start.
$PROD up -d --wait postgres
$PROD run --rm backup sh /deploy/restore.sh /backups/<file>
make prod-up                                            # migration job, then the API
```

Compare row counts (`users`, `conversations`, `messages`, `runs`,
`citations`, `checkpoints`) with the old database before reopening traffic. To
roll back, check out the previous release, remove the new volume, and restore
the same dump with the old image.

The procedure was rehearsed on 2026-10-19 with a development database:
identical row counts after the restore, a clean upgrade to the documents
revision, and all 37 B-tree indexes verified with PostgreSQL's `amcheck`
under the new collation.

**Development** uses the same steps with the development compose file. Dump
while the old container is still running, before the next `docker compose up`:

```bash
docker compose exec -T postgres pg_dump -U app -d app -Fc > dev.dump
docker compose down
docker volume rm web-search-chatbot_postgres-data
docker compose up -d --wait postgres
docker compose exec -T postgres pg_restore -U app -d app --no-owner --single-transaction < dev.dump
make dev
```

If the development data is disposable, skip the dump and the restore.

## Capacity

See [load-testing.md](load-testing.md) for measured numbers, how they were
measured, and how to rerun the test on your own hardware.
