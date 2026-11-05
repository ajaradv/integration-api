# PAXAFE Tive Integration API

Receives Tive webhook calls, stores the raw payload, and converts each one in the background into
one PAXAFE ("PX") sensor record and one PX location record. Bad data is never dropped silently
and never stored as-is: every payload ends as `accepted`, `accepted_with_corrections`,
`quarantined`, `rejected` (or `failed`), and the original body is always kept.

```text
Mock Sender --POST /api/webhook/tive--> web (uvicorn): size, key, header, JSON check, INSERT -> 202
                                         |
                      Postgres: ingestions (also the work queue) · readings · *_history
                                         ^
                                  worker: claim (SKIP LOCKED) -> transform -> persist -> heartbeat
```

The full design is in [`docs/DESIGN.md`](docs/DESIGN.md). Choices the implementation had to make
are in [`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md). The review procedure for quarantined
payloads is in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Live URLs and test key

Not deployed from this checkout. After deploying (see Deployment), record here: API URL,
`/docs` URL, and the test sender key created with the CLI.

## Quickstart

```bash
docker compose up --build -d                       # postgres:55432, migrate, web:8000, worker
export DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/paxafe
uv run python -m app.cli create-key --name demo --role sender     # prints the key once
export KEY=pxk_...

curl -s localhost:8000/livez
curl -s -X POST localhost:8000/api/webhook/tive -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps(json.load(open("tests/fixtures/sample-tive-payloads.json"))["payloads"][0]["payload"]))')"
# -> 202 {"ingestion_id": "...", "status": "pending", "status_url": "/api/ingestions/...", ...}
curl -s localhost:8000/api/ingestions/<ingestion_id> -H "X-API-Key: $KEY"
```

An operator key (`--role operator`) is needed for promote, reprocess and listing.

## Local setup

* Python 3.12 and [uv](https://docs.astral.sh/uv/); Docker for Postgres.
* `uv sync`, copy `.env.example` to `.env` if you run web and worker outside Docker:
  `uv run alembic upgrade head`, `uv run uvicorn app.main:app`, `uv run python -m app.worker`.
* Compose publishes Postgres on host port **55432** and web on **8000** (project name `paxafe`).

## Tests

```bash
docker compose up -d postgres
docker compose exec postgres psql -U postgres -c "CREATE DATABASE paxafe_test"   # once
export TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/paxafe_test

uv run pytest -m unit                 # no database; transform, hashing, vendored-file pins
uv run pytest -m integration          # needs TEST_DATABASE_URL (it truncates that database!)
uv run pytest --cov=app --cov-fail-under=85
uv run ruff check . && uv run ruff format --check . && uv run mypy app/transform
```

Integration tests are skipped when `TEST_DATABASE_URL` is unset. Use a database nobody else needs:
the fixtures migrate it (downgrade to base, upgrade to head) and truncate tables between tests.

## API reference

Interactive docs at `/docs`. Errors are `{"error": {"code", "message", "details?"}}`.

| Method and path | Role | Purpose |
| --- | --- | --- |
| `POST /api/webhook/tive` | sender | Ingest a payload |
| `GET /api/ingestions/{id}` | sender, operator | Status, issues, PX records, candidates |
| `GET /api/ingestions/batch?ids=a,b,...` | sender, operator | Up to 100 views as `{"items": [...]}`; a sender sees only its own |
| `GET /api/ingestions?status=&device_imei=&limit=&cursor=` | operator | Keyset-paginated list |
| `POST /api/ingestions/{id}/promote` | operator | `{overrides, note, replace_existing?}` |
| `POST /api/ingestions/{id}/reprocess` | operator | Re-run the transform on the stored body |
| `POST /api/ingestions/reprocess?status=&limit=` | operator | Bulk reprocess (max 500) -> `{"queued": n}` |
| `GET /livez`, `GET /health`, `GET /docs` | none | Liveness (503 only for a hung worker), diagnostics, OpenAPI |

Webhook status codes:

| Code | Meaning | Stored? |
| --- | --- | --- |
| 202 | Stored and queued (not "valid") | yes, `pending` |
| 200 | Duplicate delivery; same `ingestion_id` (`duplicate: true`) | no new row |
| 400 `invalid_idempotency_key` | `Idempotency-Key` not 1-255 printable ASCII | no |
| 400 `malformed_json` | Not JSON (also `NaN`, `\u0000`, invalid UTF-8); body has `ingestion_id` | yes, `rejected` |
| 401 | Missing, unknown or revoked key | no |
| 403 | Key has the wrong role | no |
| 409 `idempotency_key_reused` | Same key, different body | no |
| 413 | Body over 256 KB (checked first, before auth) | no |

## Processing model and statuses

The request path does no Tive-specific validation; the worker (a Postgres-table queue with
`FOR UPDATE SKIP LOCKED`) parses every field on its own, runs rules R0-R12, maps to PX, and checks
the output against the vendored PX JSON schemas.

| Status | Meaning |
| --- | --- |
| `pending` / `processing` | queued / claimed by a worker |
| `accepted` | no blocking issues, no corrections (warnings allowed) |
| `accepted_with_corrections` | no blocking issues, at least one correction |
| `quarantined` | at least one blocking issue; no PX records written, candidates kept |
| `promoted` | quarantined, then promoted by an operator with overrides |
| `rejected` | body is not JSON, or its root is not an object |
| `failed` | unexpected errors after `MAX_ATTEMPTS` (5) attempts |

## Edge-case policy (R0-R12)

| # | Rule | Outcome |
| --- | --- | --- |
| R0 | `Temperature` object with `Celsius` key; `Location` object; latitude, longitude, `EntryTimeEpoch` present | blocking `missing_field` |
| R1 | `DeviceId` is 15 digits | blocking `device_imei_missing` / `_malformed` |
| R2 | `DeviceName` non-empty (becomes `device_id`) | blocking `device_id_missing` |
| R3 | `EntityName` differs from `DeviceName` | warning `entity_name_differs` |
| R4 | `Shipment.DeviceId` differs from `DeviceId` | warning `shipment_device_mismatch` |
| R5 | `EntryTimeEpoch` in [2015-01-01, now + 24 h] ms; never converted | blocking `timestamp_implausible` |
| R6 | `EntryTimeUtc` within 1 s of `EntryTimeEpoch` | warning `timestamp_mismatch` (the epoch is authoritative) |
| R7 | latitude in [-90, 90], longitude in [-180, 180] | blocking `latitude_/longitude_out_of_range` |
| R8 | `Temperature.Celsius` in [-100, 100] (covers 327.67, -327.68, 32767, -32768) | blocking `temperature_fault` |
| R9 | `Temperature.Celsius` is null | warning `temperature_null` |
| R10 | probe temperature outside [-100, 100] | warning `probe_fault`; stored null with `probe_fault = true` |
| R11 | humidity [0,100], lux >= 0, battery [0,100], dBm [-150,-50], accuracy >= 0 | warning, PX field null |
| R12 | `Accelerometer.G` null but X, Y, Z present | correction `magnitude_computed` |

Numeric strings are converted (correction `number_from_string`) in measurement fields only;
identity, time and coordinate fields are strict (`type_error`).

Per-sample outcomes (tested in `tests/unit/test_pipeline_samples.py` and
`tests/integration/test_e2e_samples.py`):

| # | Sample | Status | Main issues |
| --- | --- | --- | --- |
| 1 | Standard Temperature Shipment | accepted | warning `shipment_device_mismatch` |
| 2 | Device A571992 Reading | quarantined | `latitude_out_of_range` |
| 3 | GPS Location Shipment | accepted | none |
| 4 | Vaccine Shipment Checkpoint | accepted | warning `probe_fault` |
| 5 | Device Name Only | quarantined | `device_imei_missing` |
| 6 | Biologics Replenishment Shipment | quarantined | `timestamp_implausible`; warning `timestamp_mismatch` |
| 7 | Cellular Location Only | accepted | warning `probe_fault`; accuracy 500 m -> Medium |
| 8 | Standard Temperature Shipment Update | accepted_with_corrections | strings converted; warning `shipment_device_mismatch` |
| 9 | Device A571992 Follow-up Reading | quarantined | `longitude_out_of_range` |
| 10 | Clinical Supply Shipment | accepted | none (`account_id` 999 stored) |
| 11 | Minimal Data Payload | accepted | none |
| 12 | Vaccine Shipment Southbound | accepted | warning `timestamp_mismatch` (`EntryTimeUtc` is 5 h off the epoch) |
| 13 | Entity and Device Name | quarantined | `device_imei_missing`; warning `entity_name_differs` |

**Sample 12** carries an `EntryTimeUtc` (`14:27:26Z`) exactly 5 hours earlier than its
`EntryTimeEpoch` (`19:27:26Z`), which looks like local time labelled as UTC. R6 trusts the integer
epoch and only warns, so the reading is stored. Result: 8 readings, 5 quarantined.

## Idempotency

* No header: a canonical hash of the body (sorted keys, no whitespace) is unique per API key.
* `Idempotency-Key` header: unique per API key; the same key with another body is `409`, and
  identical bodies under different keys are separate ingestions.
* Worker claim: `SKIP LOCKED`. Readings: insert-first upsert on `(device_imei, timestamp_ms)`;
  a changed reading moves the old version to `readings_history` (last write wins for first-pass
  ingestions; a reprocessed ingestion never replaces another ingestion's reading).
* Operator actions are guarded by status preconditions.

## Data model

`api_keys` (SHA-256 hashes only), `ingestions` (raw bytes, parsed JSON, status, issues, candidates,
also the queue), `readings` (one row per reading holding both PX records as JSONB; key columns are
generated from them and CHECK-constrained), `readings_history`, `worker_heartbeats`. See
`migrations/versions/0001_initial.py`.

## Deployment (Render + Neon)

1. Create a free Neon project; copy the **direct** (non-pooled) connection string and change the
   scheme to `postgresql+asyncpg://` (asyncpg does not take `sslmode=`; use `?ssl=require`).
2. Create a Render Blueprint from `render.yaml` and set the `DATABASE_URL` secret.
   Migrations run at container start; honcho runs web and worker in one container. Move
   migrations to a release step if the service ever runs more than one instance.
3. Create keys from a laptop (Render's free instances have no shell):
   `DATABASE_URL=<neon-url> uv run python -m app.cli create-key --name mock-sender --role sender`.
4. Migrations only add columns or tables (convention, not enforced), so redeploying the previous
   image is a safe rollback.

Not deployed or run against Render/Neon from this checkout.

## Assumptions

* Quarantine review has **no owner**; the promote and reprocess endpoints and the runbook exist,
  but nobody is assigned.
* `AccountId` is stored only (not checked against the API key).
* Last write wins for duplicate `(device, timestamp)`; an out-of-order retry can replace newer
  data, recoverable from history.
* Fault values (`327.67`, `-327.68`, `32767`, `-32768`) are inferred from two samples; the range
  rule [-100, 100] covers them. Accuracy thresholds (50 m / 500 m) are inferred from the PX example.
* `street` is always null, as in the PX example.

## Risks and future improvements

No rate limiting; a sender key can read any ingestion by id; `/docs` and `/health` are
unauthenticated; no retention policy; Tive's real retry and ordering behaviour is unknown. Future:
tenant enforcement, rate limiting, retention, route plausibility, `LISTEN/NOTIFY` wake-ups, an
operator UI. See the design for the full list.
