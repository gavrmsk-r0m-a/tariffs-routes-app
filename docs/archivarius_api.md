# Archivarius Data Bridge v1

## Architecture

The bridge is a read-only integration boundary; Archivarius never connects to
PostgreSQL. `app/archivarius.py` owns the canonical representation, exact value
serialization and cursor behavior. Both the WSGI pull API and the separately
invoked outbound script use that same exporter. The push script is not imported
or scheduled by the web process.

API base: `/api/archivarius/v1`.

| Method and endpoint | Purpose | Authentication |
|---|---|---|
| `GET /health` | Minimal network health | None |
| `GET /summary` | Current operational counts | Bearer token |
| `GET /routes` | Routes and active phone links | Bearer token |
| `GET /phones` | Phones and active route memberships | Bearer token |
| `GET /tariffs` | Current tariffs (`is_current = TRUE`) | Bearer token |
| `GET /provider-changes` | Routing events | Bearer token |
| `GET /events` | Incremental business change feed | Bearer token |

There are no write endpoints. Other methods receive JSON `405`; unknown paths
receive JSON `404`. API errors never redirect to the browser login.

## Authentication and token provisioning

Clients send `Authorization: Bearer <TOKEN>`. Tokens are high-entropy values;
only their SHA-256 digest is stored in the existing `api_tokens` table. Active
token digests are checked with constant-time comparison. Successful use updates
`last_used_at`. Browser cookies are ignored.

Create a token (the plaintext is printed once):

```bash
python scripts/create_archivarius_token.py \
  --database-url 'postgresql://USER:PASSWORD@localhost/DB' \
  --name archivarius-home \
  --created-by-user admin
```

Store the displayed token in a secret manager. Deliberate replacement requires
the same name plus `--rotate`; without that flag an existing name is a safe
failure. The creator must be an existing active user.

## Responses and cursors

Responses are UTF-8 JSON with `Cache-Control: no-store`. Dates are ISO dates,
timestamps are UTC ISO-8601, and PostgreSQL `NUMERIC`/Python `Decimal` values are
strings (never binary floating point).

Routes, phones, tariffs and provider changes accept `after_id` and `limit`,
defaulting to `0` and `500`. Events default to `200`. All limits are capped at
`1000`; invalid values return JSON `400`. Results are ordered by primary key
ascending. Follow `next_cursor` while `has_more` is true. An empty page retains
the input cursor. Snapshot cursors enumerate IDs; changes to already enumerated
IDs are recovered through `/events`.

The event source is `change_log`, restricted to the exact entity types:
`route`, `route_phone_number`, `phone_number`, `tariff`, and `routing_event`.
Users, authentication, permissions, API tokens, Telegram/HLR settings, security
events and unrelated dictionaries are excluded. Cursor filtering is `id >
after_id`, ordered by `id ASC`, so acknowledged pages can be polled repeatedly
without gaps or duplicates.

## Direct pull

```bash
curl -H 'Authorization: Bearer <TOKEN>' \
  'https://teleroute.example/api/archivarius/v1/events?after_id=0&limit=200'
```

Archivarius should persist `next_cursor` only after it durably accepts a page.

## Outbound push (disabled by default)

No background scheduler is installed. An administrator can run the one-shot
transport from cron or a systemd timer:

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@localhost/DB'
export ARCHIVARIUS_PUSH_URL='https://relay.example/ingest'
export ARCHIVARIUS_PUSH_TOKEN='<RELAY-TOKEN>'
export ARCHIVARIUS_PUSH_STATE_FILE='/var/lib/teleroute/archivarius-cursor.json'
python scripts/archivarius_push.py --once
```

Add `--snapshot` to send a complete canonical snapshot before the incremental
events page. Payloads contain `schema_version`, `kind`, `generated_at`, and
`data`. Non-local destinations must use HTTPS. The state file advances only
after the relay returns 2xx; network/server failure exits non-zero and preserves
the old cursor. HTTP is accepted only for localhost development.

## Local two-machine Wi-Fi test

On laptop A (TeleRoute/PostgreSQL):

1. Create a service token with the provisioning command above.
2. Start TeleRoute bound to the LAN-capable interface/host setting used by the
   deployment, on port 8000. Do not expose PostgreSQL.
3. Find the laptop's LAN IPv4 address (`ipconfig` on Windows or `ip addr` on
   Linux).
4. If Windows Firewall blocks it, add an inbound TCP 8000 rule for the **Private**
   network profile only. Do not create a Public-profile rule.

From laptop/Lenovo B on the same trusted Wi-Fi:

```bash
curl 'http://<LAPTOP-IP>:8000/api/archivarius/v1/health'
curl -H 'Authorization: Bearer <TOKEN>' \
  'http://<LAPTOP-IP>:8000/api/archivarius/v1/summary'
curl -H 'Authorization: Bearer <TOKEN>' 'http://<LAPTOP-IP>:8000/api/archivarius/v1/routes'
curl -H 'Authorization: Bearer <TOKEN>' 'http://<LAPTOP-IP>:8000/api/archivarius/v1/phones'
curl -H 'Authorization: Bearer <TOKEN>' 'http://<LAPTOP-IP>:8000/api/archivarius/v1/tariffs'
curl -H 'Authorization: Bearer <TOKEN>' 'http://<LAPTOP-IP>:8000/api/archivarius/v1/provider-changes'
curl -H 'Authorization: Bearer <TOKEN>' 'http://<LAPTOP-IP>:8000/api/archivarius/v1/events?after_id=0'
```

`<TOKEN>` is a placeholder—never place a real token in documentation, shell
history, source control, screenshots, or ordinary logs.

## Production security notes

Terminate public traffic with TLS, restrict source networks at the firewall or
reverse proxy, keep PostgreSQL private, use a dedicated token per consumer, and
rotate/deactivate tokens when ownership changes. Protect relay tokens and cursor
files with service-account filesystem permissions. API operational logs contain
token ID/name, endpoint, status and count, but never the bearer value. Monitor
repeated 401s and 5xx responses. The API deliberately exposes no token listing,
user administration, raw table endpoint, credential, or secret setting.
