# Operations runbook

Who does the reviewing is still open (see the design's Open Questions). This file describes the
procedure. All operator calls need an `operator` key (`X-API-Key`). Keys are managed from a
laptop with `DATABASE_URL=<url> uv run python -m app.cli ...`.

## Reading `/livez` and `/health`

* `GET /livez` (Render's health check): `200 {"worker": "ok"}` is normal. `starting` is the first
  90 s after the web process started. `unknown` means Postgres could not be reached (a restart
  would not help). `503 stale` means the newest worker heartbeat is older than 60 s, or there is
  none after the grace period: the worker is hung or never started, and Render restarts the
  container.
* `GET /health` always returns `200`: `database`, `worker_heartbeat_age_s` (about 10 s when
  healthy), and counts `pending`, `processing`, `failed`. Growing `pending` with a stale heartbeat
  means a hung worker. `failed > 0` needs the section below.

## Listing the quarantine queue

```bash
curl -s "$API/api/ingestions?status=quarantined&limit=50" -H "X-API-Key: $OP_KEY"
# next page: add &cursor=<next_cursor>; narrow with &device_imei=...
curl -s "$API/api/ingestions/$ID" -H "X-API-Key: $OP_KEY"     # issues + candidate records
```

The view shows `issues` (with `original` values), `candidate_sensor` / `candidate_location`, and
`issues_history` from earlier reprocesses.

## Choosing: promote, reprocess, or leave

| Blocking code | Action |
| --- | --- |
| `device_imei_missing` / `_malformed` | promote with `device_imei` (verify the device out of band first) |
| `device_id_missing` | promote with `device_id` |
| `timestamp_implausible` | promote with `timestamp` (epoch milliseconds) |
| `latitude_out_of_range`, `longitude_out_of_range` | promote with `latitude` / `longitude` |
| `temperature_fault`, `unparseable_number` on temperature | promote with `temperature` (a number, or `null` to store no temperature) |
| `missing_field` on an overridable field | promote with that field |
| `conflicts_existing` | promote with `replace_existing: true` if the new data is right, otherwise leave |
| `type_error` on `EntityName` / `EntryTimeUtc` | not promotable: leave, or fix the code and reprocess |
| `output_schema_violation` | always a code bug: fix, deploy, then reprocess |
| transform bug fixed | reprocess (a resend cannot change the outcome) |

Worked examples (set `API` and `OP_KEY`):

```bash
# latitude 95.0 is a typo for 40.81 (sample 2). 409 if a reading already exists at that
# device and timestamp; add replace_existing only if the new data should win.
curl -s -X POST "$API/api/ingestions/$ID/promote" -H "X-API-Key: $OP_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"overrides":{"latitude":40.81},"note":"typo, confirmed with carrier"}'

# no DeviceId (sample 5): supply the IMEI
-d '{"overrides":{"device_imei":"866088073468499"},"note":"IMEI from device registry"}'

# EntryTimeEpoch sent in seconds (sample 6): supply milliseconds
-d '{"overrides":{"timestamp":1739561246000},"note":"seconds -> ms, matches EntryTimeUtc"}'

# probe/primary fault value 327.67: store no temperature
-d '{"overrides":{"temperature":null},"note":"sensor fault code"}'

# conflicts_existing: replace the other ingestion's reading
-d '{"note":"vendor correction","replace_existing":true}'
```

Results: `200` promoted; `409` not quarantined, or a reading with different content exists and
`replace_existing` was not set (names the owner); `422` blocking issues remain (the response lists
them; nothing changed). Promotion replaces history safely: the old reading is copied to
`readings_history`.

Reprocess one: `POST /api/ingestions/$ID/reprocess` (allowed from `rejected`, `failed`,
`quarantined`). Bulk: `POST /api/ingestions/reprocess?status=quarantined&limit=100` (max 500).
Never reset rows with raw SQL: the endpoint also resets `attempts`, bumps `reprocess_count` and
keeps the old issues in `issues_history`. From a laptop:
`uv run python -m app.cli reprocess --id <uuid>` or `--status quarantined --limit 100`.
A reprocessed ingestion never replaces another ingestion's reading; it is quarantined with
`conflicts_existing` instead.

## Rotating and revoking keys

```bash
DATABASE_URL=<url> uv run python -m app.cli create-key --name mock-sender-2 --role sender
# switch the sender to the new key, then
DATABASE_URL=<url> uv run python -m app.cli list-keys
DATABASE_URL=<url> uv run python -m app.cli revoke-key --id <uuid>
```

A revoked key gets `401` immediately. A resent body under the new key creates a new ingestion;
the reading upsert then finds identical content and changes nothing.

## Handling `failed > 0`

`failed` means the transform or persistence raised an unexpected error `MAX_ATTEMPTS` times (or a
claim expired that many times). The cause is in the structured logs (`ingestion_failed`) and in
`last_error` on the row. Fix the cause, deploy, then reprocess:
`POST /api/ingestions/reprocess?status=failed`.

## Replaying after a transform fix

1. Deploy the fix. 2. List the affected status (`quarantined`, `rejected` or `failed`) and spot
check one. 3. Reprocess (single or bulk). 4. Watch `/health` `pending` drain, then re-list.
Readings that now differ from what another ingestion owns are quarantined as `conflicts_existing`
for a deliberate decision.
