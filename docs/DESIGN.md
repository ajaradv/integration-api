# Tive → PAXAFE Integration Pipeline

> Copy of the approved design. Where the implementation had to choose or deviate, see
> [`../IMPLEMENTATION_NOTES.md`](../IMPLEMENTATION_NOTES.md) (notably sample 12).

*   **Author:** avega
*   **Status:** In Review
*   **Date:** 2026-11-01

--------------------------------------------------------------------------------

## Overview

PAXAFE monitors pharmaceutical cold chain shipments using data from tracking
devices made by several vendors. Each vendor sends data in its own shape.
PAXAFE needs every reading in one shape: its normalized sensor and location
formats. In this document they are called "PX". Each PX JSON schema includes
one reference record, `examples[0]`, referred to here as "the PX example".
With one shape, later systems (alerting, shipment timelines, compliance
reports) can treat every vendor the same way.

This design covers two applications:

1.  **Integration API**: accepts Tive webhook calls at
    `POST /api/webhook/tive`. It checks the caller's API key and stores the
    raw payload. It then validates the payload in the background, converts it
    into one PX sensor record and one PX location record, and stores both.
2.  **Mock Sender**: produces Tive-shaped payloads (the provided samples,
    generated valid payloads, and deliberately broken ones), sends them to the
    Integration API, and shows how each one was handled.

This is a take-home exercise (the take-home exercise (`EXERCISE.md`, not included in this repository)) with a
4–5 hour limit and free hosting. Its scoring weights integration accuracy
highest (35%). These limits drive several scope decisions below. Both
applications are deployed publicly and run end to end. The goal is to show
how a production integration handles bad data, not only good data.

## The Problem

A Tive device reports temperature, humidity, light, motion, battery, and
position in one nested JSON document. PAXAFE stores this as two flat records
with different field names, units, precision, and enums. The field mapping
itself is direct. The work lies in the cases where the incoming data is
wrong, incomplete, or ambiguous, and the provided samples contain many of
them:

*   **Values that break the schema**: a latitude of `95.0` and a longitude of
    `-200.0`.
*   **Missing identity**: payloads with no `DeviceId`. The Tive schema
    requires this field, and it is the only source for PAXAFE's required
    `device_imei`.
*   **Conflicting identity**: `EntityName: A571992` sent together with
    `DeviceName: Ship33CABOL`, and a `Shipment.DeviceId` that differs from the
    top-level `DeviceId`.
*   **Wrong units**: an `EntryTimeEpoch` of `1739561246`, which is in seconds.
    Read as milliseconds, it places the reading in January 1970.
*   **Wrong types**: numbers sent as strings (`"Dbm": "-100"`,
    `"Celsius": "10.5"`).
*   **Sensor fault values**: probe temperatures of `327.67` and `-32768`.
    These are inferred to be "no reading" codes, not real temperatures.
*   **Duplicate and conflicting readings**: several payloads for device
    A571992 share one timestamp but report different coordinates.
*   **More than one account**: one payload has `AccountId: 999` where every
    other payload has `478`.

In cold chain monitoring, each of these failures has a cost:

*   **Dropped silently**: a reading lost this way leaves a gap in the
    temperature record of a regulated shipment.
*   **Stored as-is**: a stored fault value such as `327.67 °C` can raise a
    false excursion alert (an alert that the temperature left its allowed
    range) or end up in an audit report.
*   **Corrected by guessing**: a timestamp or device identity that the system
    guessed and then stored attaches data to the wrong shipment, and no one can
    trace it back.

The system therefore needs one explicit, written policy for every class of bad
input. Each payload ends in one of four outcomes:

1.  **Accepted as-is.**
2.  **Accepted with corrections**, each correction recorded.
3.  **Quarantined for review.** No PX records are written. The candidate
    records and the reasons are kept.
4.  **Rejected**, when the body is not a JSON object.

The original payload is always kept, so any decision can be audited or
replayed later.

## Goals

*   **Correct mapping**: every field the PX sensor and location schemas
    define is filled from the matching Tive field, with the units, rounding,
    and enum values the PX schemas specify. Each output record is checked
    against its PX JSON schema before it is stored.
*   **A decision for every sample**: each of the 13 provided sample payloads
    gets a documented, tested outcome and a stated reason (see
    [Expected outcome per sample](#expected-outcome-per-sample)).
*   **Nothing lost without a trace**: every authenticated request with a
    well-formed envelope is recorded with its raw body, its outcome, and the
    reasons, including requests that end up rejected or quarantined. A
    well-formed envelope means a body within the size limit and a valid
    `Idempotency-Key` header, if one is sent.
*   **Safe to retry**: sending the same payload more than once creates no
    duplicate records. The API returns the same ingestion each time.
*   **Clear errors for the sender**: the API returns distinct HTTP status codes
    for bad credentials, oversized bodies, malformed headers, and malformed
    JSON. Schema and business-rule failures are reported per field through the
    ingestion status endpoint.
*   **Authentication**: requests without a valid API key are rejected before
    the body is parsed. Keys are stored as hashes, not plain text, and can be
    revoked.
*   **Tested transform**: unit tests cover the field mapping and every
    edge-case rule listed under The Problem. Each sample payload has an
    end-to-end test.
*   **Deployed and reproducible**: both applications are live on a free-tier
    host, use the same database engine locally and in production, and can be
    set up from the README alone.
*   **Visible results in the Mock Sender**: for each payload it sends, the
    Mock Sender shows the HTTP status, the final ingestion status, the stored
    PX records or the quarantine/rejection reasons, and any corrections
    applied.

## Non-Goals

*   **Other vendors**: only Tive is handled. The design keeps the
    vendor-specific code separate so another vendor could be added, but none is
    built.
*   **Alerting and excursion detection**: the system flags implausible
    values. It does not decide whether a shipment went out of its temperature
    range, and it sends no notifications.
*   **Route plausibility**: a position far from the shipment lane is not
    checked. For example, the "GPS Location Shipment" sample reports San
    Francisco for a Boston-to-Atlanta lane.
*   **A shipment data model**: the reading stores `ShipmentId` and
    `AccountId` in their own columns. All other shipment fields stay in the
    raw payload. Shipment entities, lanes, and timelines are out of scope.
*   **A general query API for PX data**: reads are limited to ingestion status
    and the quarantine review endpoints.
*   **Tenant enforcement**: `AccountId` is stored but not checked against the
    API key.
*   **Reverse geocoding**: address parts are split out of `FormattedAddress`
    only where that can be done reliably. No external geocoding service is
    called.
*   **Production-scale infrastructure**: no autoscaling or multi-region setup,
    and no paid services.
*   **Matching Tive's real webhook security**: Tive's actual webhook signing
    method, if any, is not reproduced. Authentication uses an API key in a
    header, as the exercise specifies.
*   **User accounts or UI login for the Mock Sender**: it is a test tool. Its
    API key is set in its configuration.

## Open Questions

*   **Who reviews quarantined payloads?** With the chosen policies, 5 of the
    13 samples are quarantined. The design provides the review and promotion
    endpoints, and a runbook that describes the review procedure. No team or
    person is assigned to do that work. The README states this as an
    assumption.
*   **Tive's retry and ordering behavior**: the duplicate policy is "last
    write wins". If Tive retries out of order, an older retry can replace a
    newer reading. The history table makes this recoverable but not
    preventable. Tive's real delivery guarantees are unknown.
*   **Fault-value list**: `327.67`, `-327.68`, `32767`, and `-32768` are
    inferred from two samples, not from Tive documentation.
*   **Accuracy thresholds**: ≤ 50 m `High`, ≤ 500 m `Medium`, > 500 m `Low`
    reproduce the PX example (WiFi, 23 m → `High`), but PAXAFE has not
    confirmed the numbers.
*   **Address parsing**: the PX example sets `street` to null even though
    `FormattedAddress` contains a street. The design follows the example and
    leaves `street` null. PAXAFE should confirm this is intended.

## Critical User Journeys (CUJs)

### CUJ 1: A valid reading is ingested

*   **Given**: sample 1 ("Standard Temperature Shipment") has not been sent
    yet. The Mock Sender has a valid sender API key. The API, the worker, and
    Postgres are running and warm.
*   **When**: the Mock Sender posts sample 1.
*   **Then**: the API responds `202` with an `ingestion_id` in under 500 ms.
    The request does one key lookup and one `INSERT`. Within 5 s,
    `GET /api/ingestions/{id}` reports three things:
    *   status `accepted`;
    *   a warning for the `Shipment.DeviceId` mismatch;
    *   the two stored PX records, each marked `current: true`.

    Both records validate against the PX JSON schemas.

### CUJ 2: A retry does not create duplicates

*   **Given**: CUJ 1 has completed.
*   **When**: the Mock Sender posts the identical body again, without an
    `Idempotency-Key`.
*   **Then**: the API responds `200` with the original `ingestion_id` and its
    current status. No new ingestion row or PX record is created.
*   **When (variant)**: the Mock Sender sends two requests with the same
    `Idempotency-Key`. The first has body A and the second has body B.
*   **Then**: the first request gets `202`. The second gets `409
    idempotency_key_reused`, and nothing is stored for it. Two requests with
    byte-identical bodies but *different* keys each get their own ingestion.

### CUJ 3: A payload is reprocessed after a transform fix

*   **Given**: a payload was quarantined because of a bug in the transform,
    and the fix has been deployed.
*   **When**: an operator calls `POST /api/ingestions/{id}/reprocess`.
*   **Then**: the ingestion returns to `pending`, and its earlier issues move
    to `issues_history`. The worker processes it again with the fixed code,
    and it reaches its correct final status. Resending the body would not do
    this; it would only return the old outcome. If the reprocessed result
    conflicts with a reading that another ingestion owns, the ingestion is
    quarantined with `conflicts_existing` instead of overwriting it.

### CUJ 4: A payload with an invalid location is quarantined, reviewed, and promoted

*   **Given**: sample 1 has been accepted (CUJ 1). Then sample 2 ("Device
    A571992 Reading", latitude `95.0`, same IMEI and timestamp as sample 1)
    is sent.
*   **When**: the worker processes sample 2.
*   **Then**: the ingestion status is `quarantined`, with blocking issue
    `latitude_out_of_range` (95.0). No PX records are written. The candidate
    sensor and location records are stored on the ingestion.
*   **When (continued)**: an operator calls the promote endpoint with a
    corrected latitude.
*   **Then**: an accepted record already exists for
    `(863257063350583, 1739215646000)`, so the endpoint responds `409` unless
    the operator passes `replace_existing: true`. With that flag, three things
    happen:
    *   the existing records move to history;
    *   the new records are written;
    *   the ingestion status becomes `promoted`, with the operator's key and
        correction recorded.

    Sample 1's ingestion then shows its records as `current: false`,
    `superseded_by` sample 2's ingestion.

### CUJ 5: Authentication and transport errors are reported synchronously

*   **Given**: the Integration API is running.
*   **When**: the Mock Sender sends each demo case.
*   **Then**: each case gets its own status code:

    | Demo case | Response | Stored? |
    | --- | --- | --- |
    | Oversized body | `413` | no |
    | No key | `401` | no |
    | Revoked or unknown key | `401` | no |
    | Malformed `Idempotency-Key` | `400` | no |
    | Body is not valid JSON (including `NaN`, `\u0000`, or invalid UTF-8) | `400` | yes, as a `rejected` ingestion, so it can be inspected |

### CUJ 6: The worker dies or hangs

*   **Given**: the worker process crashes or hangs.
*   **When**: payloads keep arriving.
*   **Then**: the outcome depends on the failure:
    *   **Crash**: honcho exits the container and Render restarts it. Payloads
        sent during the restart get connection errors, which the sender
        retries.
    *   **Hang**: the web process keeps accepting and storing `pending` rows.
        Once the worker heartbeat is more than 60 s old, `/livez` returns
        `503` and Render restarts the container.

    In both cases, after the restart the worker reclaims stale `processing`
    rows and processes every `pending` row, and each reaches a final status.
    A payload that crashes the worker every time is marked `failed` after
    `MAX_ATTEMPTS` (5) attempts. It cannot restart the container more than 5
    times.

## Technical Plan (Architecture)

### Key Decisions

| Topic | Decision |
| --- | --- |
| Stack | Python, FastAPI, Pydantic v2, PostgreSQL |
| Processing | Store the raw payload, respond `202`, transform in the background |
| Queue | The `ingestions` table is the queue. A worker polls it with `FOR UPDATE SKIP LOCKED`. There is no Redis. (Changed from Redis + arq during review.) |
| Request-path checks | Body size, API key, header format, and "body is JSON" only. All other validation runs in the worker |
| Partial validity | Any blocking rule failure quarantines the whole ingestion. Candidate records are kept for promotion |
| Missing or malformed `DeviceId` | Quarantine. The operator supplies the IMEI on promotion |
| Implausible timestamp | Never converted. Quarantine |
| Probe temperature | Stored in extra columns next to the PX sensor record. A probe fault value becomes null, with `probe_fault = true` |
| Primary temperature fault value | Quarantine |
| Numbers sent as strings | Converted, with a recorded correction, in measurement fields. Strict in identity, time, and coordinate fields |
| Unparseable or out-of-range secondary measurement | Set to null, with a warning |
| Same device and timestamp, different content | Last write wins for first-pass ingestions. The replaced version goes to a history table. Reprocessed ingestions never replace another ingestion's reading |
| `AccountId` | Stored only |
| Worker hosting | API and worker as two processes in one container, under honcho |
| Mock Sender | Server-rendered FastAPI + Jinja + HTMX app |
| PX record storage | One `readings` row per `(device_imei, timestamp_ms)`, holding the exact PX sensor and location JSON in `JSONB` columns. Key columns are generated from them and constrained. (Changed from two tables during review.) |
| Accuracy category | ≤ 50 m `High`, ≤ 500 m `Medium`, > 500 m `Low`, null meters → null |
| Idempotency | Optional `Idempotency-Key` header, otherwise a body hash. Both are unique per API key. PX writes insert first, then lock. An operator reprocess endpoint |

### System diagram

```text
 ┌──────────────────────────┐        ┌────────────────────────────────────────┐
 │ Mock Sender              │        │ Integration API container              │
 │ (Render web, free)       │        │ (Render web, free; honcho supervises)  │
 │                          │ POST   │                                        │
 │ FastAPI + Jinja + HTMX   │ /api/  │  ┌──────────────┐    ┌──────────────┐  │
 │  - samples               ├webhook▶│  │ web (uvicorn)│    │ worker       │  │
 │  - generator + mutations │ /tive  │  │ size, auth,  │    │ poll + claim │  │
 │  - demo cases            │◀202+id─┤  │ JSON, insert │    │ transform    │  │
 │  - results table         │        │  │ status reads │    │ persist      │  │
 │                          │ GET    │  │ /livez       │    │ reclaim      │  │
 │ API key in env, never in ├/api/──▶│  └──────┬───────┘    └──────┬───────┘  │
 │ the browser              │ingest- │         │                   │          │
 └──────────────────────────┘ions/   └─────────┼───────────────────┼──────────┘
                              batch            │ asyncpg (direct)  │
                                               ▼                   ▼
                              ┌────────────────────────────────────────────────┐
                              │ External: Neon Postgres (free tier)            │
                              │ ingestions (also the work queue) · readings ·  │
                              │ *_history · api_keys · worker_heartbeats       │
                              └────────────────────────────────────────────────┘
```

**Hosting**:

*   **Postgres** runs on Neon's free tier. Render's free Postgres expires
    after a fixed period, which would break the deployed demo during the
    review window.
*   **Postgres is the only stateful service.** It stores data, acts as the
    queue, and holds the worker heartbeat.
*   **Locally**, `docker compose` runs Postgres 16, `web`, and `worker` from
    one image.

### Request path: `POST /api/webhook/tive`

```text
request
  │
  ├─ 1. body > 256 KB (Content-Length or streamed count) ─▶ 413  (not stored)
  ├─ 2. X-API-Key missing / unknown / revoked ────────────▶ 401  (not stored)
  ├─ 3. Idempotency-Key present but not 1–255 printable
  │      ASCII characters ───────────────────────────────▶ 400  (not stored)
  ├─ 4. read body bytes; compute body_hash
  │      parse JSON (strict: no NaN/Infinity, no \u0000)
  │      invalid → raw_json = null, status = rejected
  ├─ 5. INSERT ingestion … ON CONFLICT DO NOTHING RETURNING id
  │      no row returned → look up the existing row:
  │        header present → by (api_key_id, idempotency_key)
  │            stored body_hash differs ─────────────────▶ 409 idempotency_key_reused
  │        header absent  → by (api_key_id, body_hash)
  │      existing row ───────────────────────────────────▶ 200 {ingestion_id, status}
  │                                                         (400 if it is rejected)
  └─ 6. COMMIT ────────────────────────────────────────────▶ 202 {ingestion_id,
                                                              status: "pending",
                                                              status_url}
                                                            (400 + ingestion_id if
                                                             rejected as malformed)
```

1.  **Order of checks**: the size check runs first, as ASGI middleware, so an
    oversized body is never read into memory, even from an unauthenticated
    caller. Authentication runs next. Then the header format is checked, then
    the JSON is parsed. Requests that fail steps 1–3 are not stored.
2.  **Authentication**: the server hashes the `X-API-Key` header with SHA-256
    and looks it up in `api_keys` where `revoked_at IS NULL`. Keys are 32
    random bytes. A fast hash is enough for high-entropy keys, so a password
    hash is not used. `last_used_at` is updated at most once a minute per key.
3.  **Body hash and JSON parsing**:
    *   The body is SHA-256 hashed in canonical form: the parsed JSON is
        re-serialized with sorted keys and no whitespace. Two bodies that
        differ only in key order or formatting count as the same resend.
        Number formats are kept as parsed, so `10` and `10.0` hash
        differently. This assumes a real resend sends identical bytes.
    *   A body that is not valid JSON is hashed as raw bytes.
    *   The parser rejects `NaN`, `Infinity`, and `-Infinity`. Python's
        `json.loads` accepts them by default, but Postgres `jsonb` cannot
        store them.
    *   The parser also rejects any string containing `\u0000`, which `jsonb`
        cannot store either.
    *   `raw_body` is stored as `bytea`, so NUL bytes and invalid UTF-8 are
        kept exactly as received.
4.  **Dedup**: an optional `Idempotency-Key` header (any string of 1–255
    printable characters, such as a Tive delivery ID) takes priority over the
    body hash.
    *   The body-hash uniqueness is a *partial* unique index that applies only
        when no header was sent. So two deliveries with byte-identical bodies
        and different keys stay separate.
    *   Reusing a key with a different body returns `409` and stores nothing.
        This follows Stripe's `Idempotency-Key` semantics.
    *   The check happens after a single untargeted `ON CONFLICT DO NOTHING`.
        Two requests that race on the same key with different bodies
        therefore get one `202` and one `409`.
5.  **The request path does no Tive-specific validation.** The `202` means
    "stored and queued", not "valid", and the response body says so. Because
    the table is the queue, a committed row is already queued. No separate
    enqueue step can fail.

### Worker: the Postgres-table queue

The worker is a plain asyncio process (`python -m app.worker`). Its loop has
four duties.

1.  **Claim a batch**:

    ```text
    UPDATE ingestions SET status='processing', attempts=attempts+1, claimed_at=now()
    WHERE id IN (
      SELECT id FROM ingestions
      WHERE status='pending' AND attempts < MAX_ATTEMPTS
        AND (next_attempt_at IS NULL OR next_attempt_at <= now())
      ORDER BY received_at
      FOR UPDATE SKIP LOCKED
      LIMIT :batch)
    RETURNING id, raw_json, attempts, reprocess_count
    ```

    `SKIP LOCKED` means two worker instances never claim the same row, so the
    worker can later scale out without code changes. The batch size is
    `WORKER_MAX_JOBS` (4). This limits memory use within the free tier's
    512 MB.
2.  **Process each claimed row** concurrently, wrapped in
    `asyncio.wait_for(..., timeout=30 s)`:
    1.  Run `transform(raw_json, overrides=None, now_ms)`.
    2.  Persist the result in one transaction (see
        [Persisting and duplicates](#persisting-and-duplicates)).
    3.  On an unexpected exception, timeout, or cancellation, set the row back
        to `pending` with `last_error` and `next_attempt_at = now() +
        2^attempts s`. If `attempts` has reached `MAX_ATTEMPTS` (5), mark it
        `failed` instead. `failed` is the dead-letter state; `/health` counts
        these rows and the Mock Sender shows them.
3.  **Reclaim abandoned rows** every 60 s and on startup. A row can be left
    in `processing` with `claimed_at` older than 5 min, for example after an
    OOM kill. Such a row returns to `pending` if `attempts < MAX_ATTEMPTS`,
    and is marked `failed` with `last_error = 'attempts exhausted'` otherwise.
    Because `attempts` is incremented when the row is claimed, a payload that
    crashes the process every time is retried at most 5 times.
4.  **Write a heartbeat**: upsert `worker_heartbeats (worker_id, beat_at)`
    once at startup, before the first claim, and then every 10 s.

**Polling interval**: the worker polls every 0.5 s while it is finding work.
When idle, it backs off to 5 s. A sent payload therefore usually waits 0.5–5 s
before it is claimed, and the database is not queried twice a second while
nothing happens.

**Health endpoints**: health is split into liveness and diagnostics.

*   **`GET /livez`** is the endpoint Render health-checks. It returns `503`
    only when it can read the heartbeat and the newest `beat_at` is more than
    60 s old, meaning the worker is hung. The heartbeat row survives in
    Postgres while the container sleeps, so on every wake or deploy it starts
    out stale. To avoid failing then, `/livez` returns `200` with
    `worker: "starting"` until 90 s after the web process started. If
    Postgres cannot be reached, it returns `200` with `worker: "unknown"`,
    because a restart cannot fix a database outage.
*   **`GET /health`** is diagnostic only and always returns `200`. It reports
    the database state, the heartbeat age, the queue depth (`pending` count),
    the `processing` count, and the `failed` count.
*   **Crashed worker**: honcho exits the container when either process exits,
    so Render restarts it.

### Transform pipeline

The pipeline produces a list of **issues**. Each issue has `code`, `field`,
`severity`, `message`, `original`, `corrected`, and `source` (`pipeline` or
`operator`). The three severities are:

*   **`correction`**: a value was converted or derived into a different,
    non-null value, such as a string turned into a number or a computed
    magnitude. The original value is recorded.
*   **`warning`**: the record is stored, but a value is suspect or was set to
    null. The original value is still in the raw payload and in the issue.
*   **`blocking`**: the ingestion is quarantined.

The final status follows from the issues:

| Status | Condition |
| --- | --- |
| `rejected` | The body is not valid JSON (as defined above), or its root is not a JSON object |
| `quarantined` | At least one `blocking` issue. Candidate records are saved on the ingestion |
| `accepted_with_corrections` | No blocking issues and at least one `correction` |
| `accepted` | No blocking issues and no corrections. Warnings may be present |
| `promoted` | Was `quarantined`, and an operator promoted it with overrides |
| `failed` | Unexpected processing error after `MAX_ATTEMPTS` attempts |

**Stage 1: Parse each field on its own.** Each field in the Tive schema,
including fields inside nested objects, is validated by its own small
validator. The payload is not parsed as one model. A field that fails becomes
`None`, is marked invalid, and gets an issue. The parsed view is always built
when the root is an object. This keeps one bad field from hiding the others,
and it lets candidate records be built even when an issue is blocking. The
rules skip fields marked invalid, so a problem is never reported twice.
Fields fall into three groups:

*   **Strict** (identity, time, coordinates): `DeviceId`, `DeviceName`,
    `EntityName`, `EntryTimeEpoch`, `EntryTimeUtc`, `Location.Latitude`,
    `Location.Longitude`. A wrong type is a `blocking` `type_error`.
*   **Primary measurement**: `Temperature.Celsius`. A numeric string that
    parses fully as a finite decimal is converted, with a `correction`. Any
    other string is `blocking` `unparseable_number`.
*   **Secondary measurements**: probe temperature, humidity, lux,
    accelerometer, battery, cellular dBm, accuracy meters, and the counts.
    *   A numeric string is converted, with a `correction`.
    *   An unparseable value becomes null, with a `warning`
        `unparseable_number`.
    *   A non-integer value for an integer field (`battery_level`,
        `wifi_access_points`) also becomes null, with a warning.
    *   Booleans are always rejected as numbers.

**Stage 2: Rules.** The rules run in order, and every rule runs, so one
response lists every problem.

| # | Rule | Outcome |
| --- | --- | --- |
| R0 | Required structure: `Temperature` is an object containing the key `Celsius`; `Location` is an object; `Location.Latitude`, `Location.Longitude`, and `EntryTimeEpoch` are present | Missing → blocking `missing_field` (one issue per field) |
| R1 | `DeviceId` is present and matches `^[0-9]{15}$` | Missing or malformed → blocking `device_imei_missing` / `device_imei_malformed` |
| R2 | `DeviceName` is present and non-empty; it becomes `device_id` | Missing → blocking `device_id_missing` |
| R3 | `EntityName` is present and differs from `DeviceName` | Warning `entity_name_differs`. `EntityName` is stored in a metadata column only |
| R4 | `Shipment.DeviceId` is present and differs from `DeviceId` | Warning `shipment_device_mismatch` |
| R5 | `EntryTimeEpoch` is between 2015-01-01 and now + 24 h (milliseconds) | Outside → blocking `timestamp_implausible`. Never converted |
| R6 | `EntryTimeUtc` is present and differs from `EntryTimeEpoch` by more than 1 s | Warning `timestamp_mismatch`. The integer epoch is authoritative (sample 12 has a 5 h offset in `EntryTimeUtc`, which looks like local time labeled UTC) |
| R7 | Latitude is in [−90, 90] and longitude is in [−180, 180] | Outside → blocking `latitude_out_of_range` / `longitude_out_of_range` |
| R8 | `Temperature.Celsius` is outside [−100, 100] °C. This includes the observed fault values `327.67`, `-327.68`, `32767`, and `-32768` | Blocking `temperature_fault` |
| R9 | `Temperature.Celsius` is null | Warning `temperature_null`. PX `temperature` is null, which the PX schema allows |
| R10 | `ProbeTemperature.Celsius` is outside [−100, 100] °C | Warning `probe_fault`. `probe_temperature` becomes null and `probe_fault` becomes true |
| R11 | Secondary range checks fail: humidity outside [0, 100], lux < 0, battery outside [0, 100], dBm outside [−150, −50], or negative accuracy meters | Warning `<field>_out_of_range`. The PX field becomes null |
| R12 | `Accelerometer.G` is null but X, Y, and Z are present | Correction `magnitude_computed`, where magnitude = √(x² + y² + z²) |

Notes on the ranges:

*   **R8 and R10** use the same range: [−100, 100] °C. It is wider than any
    cold chain range, but it excludes values that cannot be physical
    temperatures. A separate fault-value list would never change an outcome,
    because every observed fault value is already outside this range.
*   **R10 and R11** set secondary values to null instead of quarantining the
    payload. A bad secondary value should not block the primary temperature.

**Stage 3: Map to PX.** The provider is always `"Tive"` and the type is
always `"Active"`.

| Record | PX field | Source | Rule |
| --- | --- | --- | --- |
| both | `device_id` | `DeviceName` | as-is |
| both | `device_imei` | `DeviceId` | as-is (or the operator override) |
| both | `timestamp` | `EntryTimeEpoch` | as-is |
| sensor | `temperature` | `Temperature.Celsius` | round to 2 decimals, half-up |
| sensor | `humidity` | `Humidity.Percentage` | round to 1 decimal (`38.70000076` → `38.7`) |
| sensor | `light_level` | `Light.Lux` | round to 1 decimal |
| sensor | `accelerometer` | `Accelerometer.{X,Y,Z,G}` | x, y, z, and magnitude rounded to 3 decimals; null if `Accelerometer` is absent |
| sensor | `tilt`, `box_open` | — | null |
| location | `latitude`, `longitude` | `Location.*` | as-is |
| location | `altitude` | — | null |
| location | `location_accuracy` | `Accuracy.Meters` | round to an integer |
| location | `location_accuracy_category` | `Accuracy.Meters` | ≤ 50 `High`, ≤ 500 `Medium`, > 500 `Low`; null → null |
| location | `location_source` | `LocationMethod` | `gps` → `GPS`, `wifi` → `WiFi`, `cell` → `Cellular`, null → null |
| location | `address.full_address` | `FormattedAddress` | as-is; `address` is null if `FormattedAddress` is null |
| location | `address.locality/state/postal_code/country` | `FormattedAddress` | filled only when the last parts match `<locality>, <ST> <ZIP>, <country>`; otherwise null. `street` is always null, as in the PX example |
| location | `battery_level` | `Battery.Percentage` | integer |
| location | `cellular_dbm` | `Cellular.Dbm` | round to 2 decimals |
| location | `cellular_network_type`, `cellular_operator` | — | null |
| location | `wifi_access_points` | `WifiAccessPointUsedCount` | as-is |

Rounding goes through `Decimal` with `ROUND_HALF_UP`, then converts to
`float` for JSON. Rounding the binary float directly is wrong for some
inputs: `round(2.675, 2)` returns `2.67`.

**Stage 4: Check the output against the schemas.** If there are no blocking
issues, each PX record is checked against the vendored PX schemas with
`jsonschema`. A failure here means the transform has a bug; the input is not
at fault. The failure is recorded as blocking `output_schema_violation`, so
the record is quarantined instead of stored, and it is logged at error level.

### Persisting and duplicates

Both PX records for a reading are stored in **one `readings` row**, keyed by
`(device_imei, timestamp_ms)`. The PX sensor and location records always
share that key and are always written together (partial validity is
all-or-nothing). A single row therefore has one owner and one outcome, and
the two records can never end up with different results.

A reading's **content** is `px_sensor`, `px_location`, `probe_temperature`,
and `probe_fault`. Accepted results are written in one transaction:

1.  Run `INSERT … ON CONFLICT (device_imei, timestamp_ms) DO NOTHING
    RETURNING id` into `readings`. If a row comes back, the result is
    `inserted`.
2.  Otherwise a row already exists. Lock it with `SELECT … FOR UPDATE`, then:
    *   **Identical content**: the result is `unchanged`. The row keeps its
        current `ingestion_id`.
    *   **Different content, ingestion processed for the first time**: copy
        the existing row to `readings_history`, with `replaced_at` and
        `replaced_by_ingestion_id`. Update the row in place and set its
        `ingestion_id` to this ingestion. The result is `replaced`.
    *   **Different content, reprocessed ingestion** (`reprocess_count > 0`)
        **where the existing row belongs to another ingestion**: do not
        replace it. Roll back the reading write, then record the ingestion
        as `quarantined` with blocking `conflicts_existing`, which references
        the owner. Replacing that reading requires promotion with
        `replace_existing`.

The insert runs first because `SELECT … FOR UPDATE` on a row that does not
exist yet locks nothing. Without that order, two workers could both insert,
and the second would fail on the unique constraint.

The ingestion's final status and issues are written in the same transaction,
along with `write_result`. That field records one result for the reading
(`inserted`, `unchanged`, or `replaced`) and the reading key
`(device_imei, timestamp_ms)`.

**Record ownership**: `readings.ingestion_id` always names the ingestion that
last wrote the reading's content. The status endpoint does not look readings
up by `ingestion_id`; it finds them by the key stored in `write_result`. Each
reading is returned with `current: true` when its content matches this
ingestion's output. If a later ingestion replaced it, the reading is returned
with `current: false`, `superseded_by`, and the history copy. So an
ingestion whose write was `unchanged` still shows its records. The status
view still returns two PX records, sensor and location, taken from the one
row.

"Last" means last to be *committed* by a worker. That is usually, but not
always, the order of arrival.

### Idempotency summary

| Layer | Mechanism | Prevents |
| --- | --- | --- |
| HTTP request | `UNIQUE (api_key_id, idempotency_key)`, plus a partial `UNIQUE (api_key_id, body_hash) WHERE idempotency_key IS NULL` | Retries creating new ingestions, including two copies arriving at once |
| Worker claim | `FOR UPDATE SKIP LOCKED` and the `status` / `attempts` conditions | Two workers transforming the same ingestion |
| PX-record upsert | Insert-first upsert on `(device_imei, timestamp_ms)`, with history on change | Two payloads describing the same reading creating two readings |
| Operator actions | Status preconditions on promote and reprocess; the `conflicts_existing` guard | Double-clicks, and reprocess silently overwriting another ingestion's reading |

If an API key is rotated, a resent body creates a new ingestion. The
PX-record upsert then finds identical content and changes nothing.

### Data model (PostgreSQL)

```text
api_keys
  id uuid PK · name text · key_hash char(64) UNIQUE · role text CHECK IN ('sender','operator')
  created_at · revoked_at NULL · last_used_at NULL

ingestions                                  -- also the work queue
  id uuid PK · received_at timestamptz · api_key_id FK
  body_hash char(64) · idempotency_key varchar(255) NULL
  raw_body bytea · raw_json jsonb NULL
  status text CHECK IN (pending, processing, accepted, accepted_with_corrections,
                        quarantined, rejected, promoted, failed)
  attempts int DEFAULT 0 · next_attempt_at timestamptz NULL · claimed_at · processed_at
  last_error text
  issues jsonb  -- [{code, field, severity, message, original, corrected, source}]
  issues_history jsonb DEFAULT '[]'
  candidate_sensor jsonb NULL · candidate_location jsonb NULL
  write_result jsonb NULL   -- {result: inserted|unchanged|replaced, device_imei, timestamp_ms}
  account_id bigint NULL · device_imei text NULL · reading_timestamp_ms bigint NULL
  promoted_by FK api_keys NULL · promoted_at · promotion_overrides jsonb NULL
  reprocess_count int DEFAULT 0 · reprocessed_by FK api_keys NULL · reprocessed_at
  UNIQUE (api_key_id, idempotency_key)                         -- NULLs never conflict
  UNIQUE (api_key_id, body_hash) WHERE idempotency_key IS NULL -- partial
  INDEX (status, received_at) · INDEX (device_imei) · INDEX (received_at, id)
  INDEX (received_at) WHERE status = 'pending'                 -- claim query

worker_heartbeats
  worker_id text PK · beat_at timestamptz

readings                                    -- one row per reading; both PX records
  id bigserial PK · ingestion_id FK
  px_sensor jsonb NOT NULL · px_location jsonb NOT NULL
  device_imei   text   GENERATED ALWAYS AS (px_sensor->>'device_imei') STORED
                       CHECK (device_imei ~ '^[0-9]{15}$')
  timestamp_ms  bigint GENERATED ALWAYS AS ((px_sensor->>'timestamp')::bigint) STORED
                       CHECK (timestamp_ms > 0)
  temperature   numeric GENERATED ALWAYS AS ((px_sensor->>'temperature')::numeric) STORED
  latitude      double precision GENERATED ALWAYS AS ((px_location->>'latitude')::float8) STORED
                       CHECK (latitude BETWEEN -90 AND 90)
  longitude     double precision GENERATED ALWAYS AS ((px_location->>'longitude')::float8) STORED
                       CHECK (longitude BETWEEN -180 AND 180)
  CHECK (px_location->>'device_imei' = px_sensor->>'device_imei'
         AND px_location->>'timestamp' = px_sensor->>'timestamp')
  probe_temperature numeric NULL · probe_fault boolean NOT NULL DEFAULT false
  account_id bigint NULL · entity_name text NULL · shipment_id text NULL  -- from ShipmentId
  created_at · updated_at
  UNIQUE (device_imei, timestamp_ms)

readings_history
  all columns of the replaced row, with the generated columns declared as plain
  columns, + replaced_at + replaced_by_ingestion_id
```

The key columns are **generated from `px_payload`**, not written separately,
so the JSON and the columns cannot drift apart. The `CHECK` constraints on
the generated columns make the database refuse an invalid IMEI or coordinate,
even if the application has a bug. `raw_body` keeps the exact bytes for
audit. `raw_json` is the parsed form the worker uses, and it is null when the
body is not valid JSON.

### API surface

| Method & path | Key role | Purpose |
| --- | --- | --- |
| `POST /api/webhook/tive` | sender | Ingest a payload (described above) |
| `GET /api/ingestions/{id}` | sender, operator | Status, issues, current or superseded PX records, candidate records, and timestamps |
| `GET /api/ingestions/batch?ids=<≤100>` | sender, operator | The same view for many ingestions at once. A sender key sees only ingestions it created |
| `GET /api/ingestions?status=&device_imei=&limit=&cursor=` | operator | List ingestions (for example, the quarantine queue), with keyset pagination on `(received_at, id)` |
| `POST /api/ingestions/{id}/promote` | operator | Body: `{overrides: {device_imei?, device_id?, timestamp?, latitude?, longitude?, temperature?}, note, replace_existing?: bool}` |
| `POST /api/ingestions/{id}/reprocess` | operator | Re-run the current transform on the stored raw payload, for example after a bug fix. Allowed from `rejected`, `failed`, or `quarantined`; otherwise `409` |
| `POST /api/ingestions/reprocess?status=failed\|rejected\|quarantined&limit=` | operator | Bulk reprocess of up to 500 rows; returns the count queued |
| `GET /livez` | none | Liveness, used as Render's health check |
| `GET /health` | none | Diagnostics, always `200` |
| `GET /docs` | none | FastAPI OpenAPI UI |

A sender key on an operator endpoint gets `403`. A sender key can still read
any single ingestion by its ID. This is a known gap: tenancy is a non-goal,
and IDs are random UUIDs. It is listed under Risks.

**Promotion** runs synchronously, because it is an operator action that needs
an immediate answer.

1.  Lock the ingestion. It must be `quarantined`; any other status returns
    `409`.
2.  Apply the overrides to `raw_json` and re-run the full pipeline. Each
    override is recorded as a `correction` with `source: operator`.
    `temperature: null` is allowed and records
    `temperature_nulled_by_operator`.
3.  If any blocking issue remains, return `422` with the issues; nothing
    changes.
4.  If an accepted reading at the key belongs to a different ingestion and
    `replace_existing` is not true, return `409` and name that ingestion.
5.  Otherwise persist through the same upsert and history path, and set the
    status to `promoted`.

Whether a blocking code can be promoted:

| Blocking code | Promotable by override | Otherwise |
| --- | --- | --- |
| `device_imei_missing` / `_malformed` | yes (`device_imei`) | — |
| `device_id_missing` | yes (`device_id`) | — |
| `timestamp_implausible` | yes (`timestamp`) | — |
| `latitude_out_of_range`, `longitude_out_of_range` | yes (`latitude`, `longitude`) | — |
| `temperature_fault`, `unparseable_number` on temperature | yes (`temperature`, including null) | — |
| `missing_field` on an overridable field | yes (that field) | — |
| `conflicts_existing` | yes (`replace_existing: true`) | — |
| `type_error` on `EntityName`, `EntryTimeUtc` | no | fix the code, then reprocess; or leave in quarantine |
| `output_schema_violation` | no | always a code bug: fix it, then reprocess |

**Reprocessing** is the way an identical payload gets a new outcome. A resend
cannot do this, because request-level dedup returns the original ingestion.

*   In one statement, reprocessing checks the status and then:
    *   resets the row to `pending`, with `attempts = 0` and
        `next_attempt_at = null`;
    *   appends the current issues, with a timestamp, to `issues_history`;
    *   clears `issues`, the candidate records, and `write_result`;
    *   increments `reprocess_count` and records who reprocessed it and when.
*   No enqueue is needed: the worker claims the row on its next poll.
*   A reprocessed row never replaces another ingestion's reading. It is
    quarantined with `conflicts_existing` instead (see Persisting).
*   A `rejected` ingestion whose body was malformed JSON is rejected again,
    because `raw_body` cannot change.

### Expected outcome per sample

| # | Sample | Expected status | Main issues |
| --- | --- | --- | --- |
| 1 | Standard Temperature Shipment | `accepted` | warning `shipment_device_mismatch` |
| 2 | Device A571992 Reading | `quarantined` | `latitude_out_of_range` (95.0) |
| 3 | GPS Location Shipment | `accepted` | none (lane implausibility is not checked) |
| 4 | Vaccine Shipment Checkpoint | `accepted` | warning `probe_fault` (327.67) |
| 5 | Device Name Only | `quarantined` | `device_imei_missing` |
| 6 | Biologics Replenishment Shipment | `quarantined` | `timestamp_implausible`; warning `timestamp_mismatch` |
| 7 | Cellular Location Only | `accepted` | warning `probe_fault` (−32768); accuracy 500 m → `Medium`; address can't be split → `full_address` only |
| 8 | Standard Temperature Shipment Update | `accepted_with_corrections` | Dbm, Celsius, and Fahrenheit converted from strings; warning `shipment_device_mismatch` |
| 9 | Device A571992 Follow-up Reading | `quarantined` | `longitude_out_of_range` (−200.0) |
| 10 | Clinical Supply Shipment | `accepted` | none (`account_id` 999 stored) |
| 11 | Minimal Data Payload | `accepted` | none; probe is null without a fault; accuracy, source, and address are null |
| 12 | Vaccine Shipment Southbound | `accepted` | warning `timestamp_mismatch` (`EntryTimeUtc` is 5 h behind the epoch) |
| 13 | Entity and Device Name | `quarantined` | `device_imei_missing`; warning `entity_name_differs` |

These expected outcomes are the end-to-end test fixtures.

### Mock Sender

*   **One page** (`/`) with three panels and a results table:
    1.  **Samples**: the 13 provided payloads, with "send one" and "send all".
    2.  **Generator**: count, number of devices, and a seed (the same seed
        gives the same payloads). Checkboxes select edge-case mutations to mix
        in:
        *   out-of-range coordinates
        *   missing `DeviceId`
        *   timestamp in seconds
        *   numbers as strings
        *   probe fault value
        *   primary temperature fault value
        *   exact resend
        *   same key, different content

        Valid payloads are checked against the vendored
        `tive-incoming-schema.json` before mutations are applied, so a bug in
        the generator shows up as a generator error.
    3.  **Demo cases**: one button per CUJ 5 case (oversized, no key, bad key,
        malformed header, malformed JSON), plus an "Idempotency" group:
        *   *resend*: the same body twice → `202`, then `200` with the same
            ID;
        *   *key reuse*: the same `Idempotency-Key` with two different bodies
            → `202`, then `409`.
*   **Results table**: one row per send, showing:
    *   time, payload label, and HTTP status;
    *   `ingestion_id`, final status, and issues;
    *   an expandable JSON view of the stored or candidate records.
*   **Polling**: the whole table refreshes with one request (`GET /results`).
    That request calls the API's batch endpoint with every ID that is not yet
    final. It polls every 1 s, backs off to 5 s after 10 s, and stops when
    every row is final or 60 s have passed.
*   **Idempotency-Key toggle**: when on, each send gets a generated UUID
    header, so the demo can show dedup with and without the header.
*   **Warm-up**: on page load, the Mock Sender calls the API's `/livez` in the
    background, so the API starts waking from free-tier sleep before the first
    send. The banner shows "API waking up…" and stops polling once `/livez`
    returns `200`.
*   **State**: send history lives in memory in the Mock Sender process,
    capped at 500 rows, and is lost when it restarts. The Integration API is
    the system of record, so losing it affects only the display.
*   **Configuration**: `INTEGRATION_API_URL` and `INTEGRATION_API_KEY` are set
    as environment variables. Neither is ever sent to the browser.

### Non-Functional Requirements

*   **Security**:
    *   Keys are stored only as SHA-256 hashes. Keys and raw bodies are never
        logged; the body hash is logged instead.
    *   Request size is limited to 256 KB.
    *   CORS is disabled, because only the Mock Sender's server calls the API.
*   **Latency**:
    *   The webhook response takes one key lookup plus one `INSERT`, in under
        500 ms on a warm instance.
    *   The time to a final status is the poll interval (0.5–5 s) plus the
        transform time.
*   **Free-tier limits**:
    *   Render's free services sleep after 15 minutes without inbound traffic.
        The first request after that waits for a cold start, typically under
        a minute.
    *   Web and worker share 512 MB of memory. The worker claims at most 4
        rows at once.
    *   Neon's free tier provides 0.5 GB of storage. The 13 samples plus a few
        thousand generated payloads use a few MB.
*   **Rollout**:
    *   Alembic migrations run at container start, before honcho starts
        either process. When the schema is already current this is a no-op.
    *   Rolling back means redeploying the previous image. Migrations are
        written to only add columns or tables, so the previous image keeps
        working against the newer schema. This is a convention, not an
        enforced rule (see Risks).

## Alternatives Considered

Each decision in the Technical Plan was chosen from two or three options.
The rejected options are listed below. For each one, the reason given is
either the condition under which that option fails here, or, where it does
not fail, the trade-off that was accepted. The constraints in play are a 4–5
hour limit, free-tier hosting, and scoring weighted toward integration
accuracy. Where a rejected option is a reasonable alternative, it is marked
**Strongest alternative**, followed by the trade-off accepted.

### A. Stack and hosting

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| TypeScript on Vercel functions + Neon | Least deploy effort; no idle sleep; Zod or Ajv validate the JSON schemas directly | No long-running process, so background work needs a hosted queue; every function instance opens its own database connection, so a pooler is required | The async design needs a persistent worker, which serverless functions do not provide on free tiers |
| TypeScript long-running server (Fastify/Hono) + Postgres job table | Async works on the free tier; one language for both apps and shared payload types | Same idle-sleep problem as Python; more to build | No advantage over Python for this design. Field-level coercion and error reporting fit the edge-case rules with less code in Pydantic |

### B. When processing happens

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Synchronous, one transaction | Simplest; the sender gets the final answer in the response | A rollback loses the raw payload; a slow database means slow webhook responses | Breaks the goal "nothing lost without a trace" |
| Synchronous, raw payload committed first | Immediate answer; raw payload survives a transform failure; no worker | A crash between the two transactions leaves stuck rows; webhook latency includes transform and database cost | **Strongest alternative.** Trade-off accepted: a slow transform or database would delay the response to Tive, and Tive's retry behavior on slow responses is unknown. The async design keeps webhook latency at one `INSERT`. The cost is that schema errors show only in the status endpoint |

### C. What runs the background transform

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| FastAPI `BackgroundTasks` + recovery sweep | No extra services; results within milliseconds | Tasks are lost on process restart; two code paths (task and sweep) need atomic claiming; no backpressure | Free hosts restart and sleep processes routinely, so the sweep would become the main path, not the fallback |
| Redis + arq worker (the original choice during drafting) | Retries, cron, and job tooling built in; a familiar pattern | A second hosted service. It still needs a Postgres sweep, because the row and the job can diverge. arq keeps result keys for 1 h by default, so re-enqueuing the same job ID (reprocess, sweep) is silently dropped unless configured otherwise. A Redis outage becomes a health-check concern | Replaced during review. The `ingestions` table is already the source of truth and already needs atomic claiming, so using it as the queue removes a service, the sweep-versus-queue drift, and two arq-specific failure modes. The cost is 0.5–5 s of poll latency |

### D. What is checked before `202`

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Tive schema check on the request path (`422` for structural failures) | The sender sees clear violations immediately | Validation lives in two places; the line between structural and business rules blurs (for example, `"10.5"`); a stricter request-path schema rejects payloads the worker could save | With all validation in one place, the request path can never lose a payload because of a validation bug, and replay always applies the current rules |
| Full dry-run transform on the request path | Complete verdict in the response | The transform runs twice; the two runs can disagree, because duplicate checks depend on stored state | Carries most of the cost of synchronous processing, plus the complexity of async |

### E. Payload with a valid sensor reading but an invalid location

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| All or nothing, no quarantine | Simplest rule | Throws away a valid temperature reading, leaving a gap in the regulated record | Incurs the dropped-reading cost described in The Problem |
| Split: store whichever record passes | Keeps every valid measurement | A bad coordinate may mean the whole payload is corrupt | Stores a temperature from a payload known to contain a corrupt field, so a bad reading can enter the record as if it were valid |

### F. Missing or malformed `DeviceId`

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Strict reject | Deterministic; never guesses identity | Those readings are lost | Same reading-loss cost as E |
| Fill in the IMEI from a `DeviceName` → IMEI registry learned from earlier payloads | Recovers readings automatically | The outcome depends on arrival order, so replays and tests do too; a reused device name attaches readings to the wrong IMEI | Attaching data to the wrong device is the most damaging failure named in The Problem |

### G. Timestamp in the wrong unit

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Magnitude rule (`< 10¹¹` → seconds) | Recovers clear unit mix-ups; the rule is unambiguous | Doesn't handle microseconds or device clock resets; there is nothing to check against when `EntryTimeUtc` is absent | **Strongest alternative.** Trade-off accepted: an automatic conversion stores a timestamp the device never sent. If the heuristic is wrong, readings move to the wrong time with nothing to detect it. Quarantine costs one manual promotion per affected payload |
| `EntryTimeUtc` takes priority when present | Corrects only when two fields agree | `EntryTimeUtc` is often absent; a wrongly labeled string time would override the integer | Same stored-but-never-sent risk as the magnitude rule, and it applies to fewer payloads |

### H. Probe temperature and fault values

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Ignore the probe (keep it only in the raw payload) | Follows the PX schema exactly; least code | The probe is often the in-product temperature, and it would be invisible in normalized data | Hides the reading most relevant to compliance |
| Any fault value, probe or primary, quarantines | Most conservative | An unplugged accessory probe blocks a valid primary reading; 7 of 13 samples would be quarantined | Sends too much normal traffic to a manual queue that nobody owns |

### I. Numbers sent as strings

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Convert in all fields | Simplest rule; lossless | Also converts identity, time, and coordinates | A string coordinate or ID more likely signals an upstream mapping bug than a formatting quirk. Converting it would hide that bug in the fields that decide which device a reading belongs to, and when it was taken |
| Strict everywhere | Never converts anything | Quarantines unambiguous values like `"10.5"`, which the operator would just retype | Adds manual work without reducing risk |

### J. Same device and timestamp, different content

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| First write wins; conflicts quarantined | Stored records never change automatically | Every legitimate vendor correction waits in the manual queue | **Strongest alternative.** Trade-off accepted: "last write wins" lets an out-of-order retry replace newer data. The history table makes every replacement recoverable, and vendor backfills are expected to be more common than out-of-order retries. That expectation is an Open Question |
| Store every version, no uniqueness | Simplest writes; nothing is ever replaced | Every reader must deduplicate, and a reader that forgets double-counts | Pushes correctness onto every consumer, and double-counting skews averages and duplicates excursions |

### K. `AccountId`

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Bind each API key to allowed accounts | Tenant separation at write time; a leaked key can affect only its own account | The check runs in the worker, so violations still get `202`; needs ongoing configuration | PAXAFE's real tenancy rules are unknown, so this would be built against guessed requirements |
| Full tenant scoping on all tables and reads | The model a multi-customer system needs | The most scope; one missed filter leaks data | Exceeds the time limit, and a single missed filter is a data leak |

### L. Where the worker runs

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Inside the API process (FastAPI lifespan task) | One process; free | Transform work shares the event loop with requests; a consumer crash can take down the API | Two processes cost nothing extra and keep the worker isolated |
| Separate paid worker service | No sleep; independent health checks and scaling | Recurring monthly cost; the demo depends on a card staying active | Adds a recurring cost the exercise does not require |

### M. Mock Sender form

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Static browser app on Vercel or Netlify | No cold start; responsive UI | The API key ships to every visitor's browser; the API needs CORS | Exposes a write-capable API key to every visitor |
| Streamlit | Little UI code | The script reruns on every interaction, which makes polling and per-session history awkward | Harder to implement table-level polling and send history |

### N. How PX records are stored

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| One typed column per PX field | The database enforces every type and range; simple queries | A migration for every PX schema change; JSON rebuilt from columns can drift from what the transform produced | Generated key columns with `CHECK` constraints keep the most important database checks without the drift |
| Typed columns plus the PX JSON | Constraints and the exact record | Every reading stored twice; two copies to keep in sync | Generated columns give the same consistency with one copy |
| Separate `sensor_readings` and `location_readings` tables (each with history) | Mirrors PX's two-record split | Two upserts per ingestion, which can return different results (one `unchanged`, the other `replaced` or `conflict`). Ownership and rollback across the two tables are then undefined | Replaced during review. Both records share one key and one lifecycle, so one `readings` row with `px_sensor` and `px_location` gives a single owner and a single outcome |

### O. Accuracy category

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Category from location method | Simple | Contradicts the PX example (WiFi, 23 m → `High`) | An integration accuracy error |
| Meter thresholds capped by method (cell ≤ `Medium`) | Prevents overconfident cell fixes | Two invented rules; the provided data doesn't support the cap | Adds an assumption that no sample tests |

### P. Idempotency

| Alternative | Pros | Cons | Reason for rejection |
| --- | --- | --- | --- |
| Body hash only, no header | One constraint, no `409` path | Cannot keep apart two real deliveries with byte-identical bodies | **Strongest alternative.** Trade-off accepted: the header costs one extra unique index and one conflict branch, and gives senders that have a delivery ID an exact dedup key. Without a header, the behavior is the same as body-hash-only |
| Natural key `(imei, timestamp)` only, no request-level dedup | Least code | Every retry creates a new ingestion; malformed retries are never deduplicated | The ingestion log fills with duplicates, and "same ingestion on retry" is lost |

## Risks & Mitigations

| Risk | Impact | Mitigation | Residual |
| --- | --- | --- | --- |
| Nobody owns quarantine review | 5 of 13 samples (and similar real traffic) wait indefinitely | Promote and reprocess endpoints; `docs/OPERATIONS.md` describes the procedure; the README states the assumption | Ownership is still open |
| Out-of-order retry under last-write-wins | An older reading replaces a newer one | Every replacement is copied to history with the replacing ingestion | Recoverable, not preventable |
| Poison payload crashes the worker | The container restarts; webhooks fail during the restart | `attempts` incremented on claim; capped at 5; marked `failed` | Up to 5 restarts per poison payload |
| Hung worker | Ingestions pile up as `pending` | Heartbeat; `/livez` returns `503` after 60 s; Render restarts the container | Up to about 60 s of delay |
| Database outage | No ingestion is stored; webhooks return `500` | `/livez` stays `200`, so there is no restart loop; `pool_pre_ping` recovers after Neon wakes | Tive must retry; no local buffer |
| Worker polling keeps Neon compute awake | Uses the free compute allowance | Polling backs off to 5 s when idle; the container sleeps after 15 min without traffic, which stops polling | Compute is used while the demo is active |
| Throughput ceiling (one free container, a pool of 5, 4 jobs at a time) | Bursts queue up | The request path is one `INSERT`; the Mock Sender uses one batched poll; Verification runs a 50-payload check | Not load-tested beyond that |
| Unbounded growth of `ingestions` and `raw_body` | Reaches Neon's 0.5 GB limit over time | Body size limited to 256 KB | Retention is future work |
| No rate limiting; each `401` costs a database lookup | Abuse can load the database | Size limit; hashed key lookup on a unique index | Rate limiting is future work |
| A sender key can read any ingestion by ID; `/docs` and `/health` are unauthenticated | Status and queue counts are visible to anyone with an ID or the URL | IDs are random UUIDs; the batch endpoint is scoped to the caller's key | A known gap; tenancy is a non-goal |
| Fault-value list and accuracy thresholds are inferred | Wrong classification for unseen codes | The range rule covers all observed codes; thresholds are in one constants file | Needs PAXAFE confirmation |
| Free-tier cold starts | The first request after idle takes up to about a minute | The Mock Sender warms the API on page load | Visible to the first reviewer request |
| Migration rollback compatibility relies on the add-only convention | A destructive migration would break a rollback | The convention is documented in the README; there is only one migration | Not enforced |

## Additional Resources

*   `EXERCISE.md` (not included in this repository): requirements and scoring.
*   [`tive-incoming-schema.json`](../app/schemas/tive-incoming-schema.json): the input
    contract.
*   [`px-sensor-schema.json`](../app/schemas/px-sensor-schema.json) and
    [`px-location-schema.json`](../app/schemas/px-location-schema.json): the output
    contracts.
*   [`sample-tive-payloads.json`](../tests/fixtures/sample-tive-payloads.json): the 13 test
    payloads behind the expected-outcome table.

## Future Improvements

*   **Tenant enforcement**: bind API keys to `AccountId`s, then scope reads by
    tenant (Alternatives K).
*   **Rate limiting**: a per-key token bucket on the webhook route.
*   **Retention**: expire idempotency keys after about 24 h, and partition or
    archive `ingestions` by month.
*   **Route plausibility**: flag positions far from the line between
    `ShipFrom` and `ShipTo`.
*   **Paid tiers**: a separate worker service and no idle sleep (Alternatives
    L).
*   **Lower poll latency**: `LISTEN`/`NOTIFY` on insert to wake the worker
    immediately, keeping polling as the fallback.
*   **Second vendor**: relax the IMEI `CHECK` to "non-empty" when a vendor
    without IMEIs is added.
*   **Operator UI**: a quarantine review page instead of raw API calls.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Repository layout and mechanical choices

*   **Two GitHub repos**, as the exercise requires, developed side by side in
    this workspace:
    *   `./integration-api/` becomes the repo `paxafe-integration-api`.
    *   `./mock-tive-sender/` becomes the repo `paxafe-mock-tive-sender`.
*   **Python and tooling**: Python 3.12. `uv` for dependencies and the lock
    file. `ruff` for lint and format, `mypy` in strict mode on
    `app/transform/`, and `pytest`.
*   **Integration API dependencies**: `fastapi`, `uvicorn[standard]`,
    `pydantic` v2, `pydantic-settings`, `sqlalchemy[asyncio]` 2.x (Core, not
    the ORM), `asyncpg`, `alembic`, `jsonschema`, `honcho`, and `structlog`.
    Dev only: `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx`, `ruff`, and
    `mypy`.
*   **Mock Sender dependencies**: `fastapi`, `uvicorn[standard]`, `jinja2`,
    `httpx`, `pydantic-settings`, and `jsonschema`. HTMX 2.0.x is vendored as
    a static file.
*   **Database driver**: asyncpg connects to Neon's **direct** (non-pooled)
    endpoint. Neon's pooled endpoint uses PgBouncer in transaction mode, which
    breaks asyncpg's prepared statements. Pool sizes are 5 for web and 5 for
    the worker.
*   **Schema files**: the JSON schemas are copied into each repo. The files in
    this workspace are the source. A test in each repo checks its copies
    against a pinned SHA-256, so an edit to a copy without updating the pin is
    caught.

### Build order and cut line

| Order | Work | Est. | Cut if time runs out |
| --- | --- | --- | --- |
| 1 | Transform package, unit tests, and the 13-sample outcome test (no database) | 1.5 h | Never cut: this covers integration accuracy, the most heavily weighted criterion |
| 2 | Migration, webhook route, auth, CLI key creation | 0.75 h | Never cut |
| 3 | Worker loop: claim, retry, reclaim, persist and upsert with history, heartbeat, `/livez`, `/health` | 0.75 h | Reclaim only at startup; `/health` reports only the database state |
| 4 | Deploy the API (Render + Neon) and create the test key from a laptop | 0.5 h | Never cut |
| 5 | Mock Sender: samples, batched results polling, demo cases | 0.75 h | Generator limited to samples, without mutations |
| 6 | Promote and reprocess endpoints, `Idempotency-Key` header | 0.5 h | Leave the HTTP endpoints as documented future work. Quarantined rows are still stored, and an operator reprocesses them with `python -m app.cli reprocess --id` (file 39). That command applies the same guarded reset as the endpoint, so the `attempts` reset, `reprocess_count`, and `issues_history` all still apply. Never reprocess with a raw SQL `UPDATE` |
| 7 | README, assumptions, operations notes | 0.25 h | Never cut |

The full design is about 5 hours of work. Orders 3, 5, and 6 are where it is
most likely to overrun. Each cut keeps the system consistent with the Goals.
The cut for Order 6 leaves reprocessing as a manual SQL step.

### Integration API: target files (`./integration-api/`)

#### 1. `pyproject.toml` (Create)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**:
    *   Project metadata and the dependencies listed above.
    *   `[tool.ruff]`: line length 100, rules `E,F,I,B,UP,SIM`.
    *   `[tool.mypy]`: strict for `app.transform.*`.
    *   `[tool.pytest.ini_options]`: `asyncio_mode = "auto"` and markers
        `unit` and `integration`.

#### 2. `uv.lock` (Create)

*   **Type / Scope**: Lock file.
*   **Proposed Changes**: generated by `uv lock`, so local, CI, and Docker
    builds install the same versions.

#### 3. `.env.example` (Create)

*   **Type / Scope**: Configuration template.
*   **Proposed Changes**: `DATABASE_URL` (an asyncpg URL), `MAX_BODY_BYTES=262144`,
    `LOG_LEVEL`, `CLAIM_STALE_S=300`, `MAX_ATTEMPTS=5`, `WORKER_MAX_JOBS=4`,
    `WORKER_POLL_MIN_S=0.5`, `WORKER_POLL_MAX_S=5`, `JOB_TIMEOUT_S=30`,
    `HEARTBEAT_INTERVAL_S=10`, `HEARTBEAT_STALE_S=60`, and `LIVEZ_GRACE_S=90`. The real `.env` is
    git-ignored.

#### 4. `.gitignore`, `.dockerignore` (Create)

*   **Type / Scope**: Repository hygiene.
*   **Proposed Changes**: ignore `.env`, `.venv`, `__pycache__`, and
    `.pytest_cache`. Keep the tests and `.git` out of the Docker build
    context.

#### 5. `Dockerfile` (Create)

*   **Type / Scope**: Container image.
*   **Proposed Changes**: based on `python:3.12-slim`. Installs with
    `uv sync --frozen --no-dev`, runs as a non-root user, and uses
    `ENTRYPOINT ["scripts/entrypoint.sh"]`. The same image serves local
    compose, CI, and Render.

#### 6. `scripts/entrypoint.sh` (Create)

*   **Type / Scope**: Container start script.
*   **Proposed Changes**: runs `alembic upgrade head`, then
    `exec honcho start -f Procfile`. Migrations run once, before either
    process starts. The README notes that migrations should move to a release
    step if the service ever runs more than one instance.

#### 7. `Procfile` (Create)

*   **Type / Scope**: Process definitions for honcho.
*   **Proposed Changes**:
    *   `web: uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers`
    *   `worker: python -m app.worker`

    When either process exits, honcho stops the other and exits, and Render
    restarts the container.

#### 8. `docker-compose.yml` (Create)

*   **Type / Scope**: Local development environment.
*   **Proposed Changes**:
    *   Service `postgres` (version 16, with a health check).
    *   A one-shot `migrate` service.
    *   Services `web` and `worker`, built from the same image and depending
        on `migrate`.

    Locally, web and worker run as separate services for clearer logs. They
    run the same commands as the Procfile.

#### 9. `render.yaml` (Create)

*   **Type / Scope**: Render blueprint.
*   **Proposed Changes**: one free `web` service (Docker,
    `healthCheckPath: /livez`). `DATABASE_URL` is a `sync: false` secret
    copied from Neon.

#### 10. `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako` (Create)

*   **Type / Scope**: Migration configuration.
*   **Proposed Changes**: a standard async Alembic setup that reads
    `DATABASE_URL` from `app.config`. Migrations are written by hand, not
    autogenerated, because they use generated columns, partial indexes, and
    `CHECK` constraints.

#### 11. `migrations/versions/0001_initial.py` (Create)

*   **Type / Scope**: Database schema.
*   **Proposed Changes**:
    *   Create every table in the [data model](#data-model-postgresql),
        including `worker_heartbeats` and the partial unique index on
        `body_hash`.
    *   `status` uses a `CHECK` constraint, not a Postgres enum.
    *   The history tables declare the generated columns as plain columns.
    *   The downgrade drops everything.

#### 12. Package markers (Create)

*   **Type / Scope**: Python packages.
*   **Proposed Changes**: empty `__init__.py` files in `app/`, `app/api/`,
    `app/ingest/`, and `app/worker/`.

#### 13. `app/config.py` (Create)

*   **Type / Scope**: Settings.
*   **Proposed Changes**: `Settings(BaseSettings)` with every variable from
    `.env.example`, and a cached `get_settings()`.

#### 14. `app/logging.py` (Create)

*   **Type / Scope**: Logging setup.
*   **Proposed Changes**: `structlog` JSON lines with `request_id`,
    `ingestion_id`, `api_key_id`, and `body_hash` bound. A processor drops
    `x-api-key` and `raw_body` if they ever appear in a log event.

#### 15. `app/db.py` (Create)

*   **Type / Scope**: Database access.
*   **Proposed Changes**:
    *   `create_engine(settings, pool_size)` returns an `AsyncEngine` with
        `pool_pre_ping=True`. Neon suspends idle compute, and pre-ping
        recovers the pool after it wakes.
    *   A `get_conn()` dependency yields a connection inside a transaction.
    *   SQLAlchemy Core `Table` objects for every table.

#### 16. `app/auth.py` (Create)

*   **Type / Scope**: API key authentication.
*   **Proposed Changes**:
    *   `generate_key()` returns the plaintext key and its SHA-256 hex. It
        uses `secrets.token_urlsafe(32)` with a `pxk_` prefix, so leaked keys
        are easy to spot.
    *   `hash_key(plaintext)`.
    *   `require_role(*roles)` is a dependency factory. It returns `401` when
        the key is missing, unknown, or revoked, `403` when the role is
        wrong, and an `ApiKey(id, name, role)` otherwise.
    *   `last_used_at` is written only when it is more than 60 s old.

#### 17. `app/main.py` (Create)

*   **Type / Scope**: FastAPI app factory.
*   **Proposed Changes**: `create_app()`:
    *   **Lifespan**: creates the engine (pool 5) and disposes of it on
        shutdown.
    *   **Routers**: `webhook`, `ingestions`, and `health`.
    *   **Middleware**: `BodySizeLimitMiddleware` (outermost) and request-ID
        middleware.
    *   **Errors**: exception handlers return
        `{error: {code, message, details?}}` for `400`, `401`, `403`, `404`,
        `409`, `413`, `422`, and `500`.
    *   **CORS**: not enabled.

#### 18. `app/middleware.py` (Create)

*   **Type / Scope**: ASGI middleware.
*   **Proposed Changes**:
    *   `BodySizeLimitMiddleware` returns `413` when `Content-Length` is
        greater than `MAX_BODY_BYTES`. It also wraps `receive` to count
        streamed bytes and stops with `413` once the limit is passed. It runs
        before authentication, so oversized bodies are never buffered.
    *   `RequestIdMiddleware` reads `X-Request-ID` or generates a UUID, and
        echoes it in the response.

#### 19. `app/api/models.py` (Create)

*   **Type / Scope**: Request and response models.
*   **Proposed Changes**:
    *   `WebhookAccepted(ingestion_id, status, status_url, duplicate: bool)`.
    *   `ReadingView(kind, px_payload, current: bool, superseded_by: UUID | None)`.
    *   `IngestionView(id, status, received_at, processed_at, attempts, issues, issues_history, readings: list[ReadingView], candidate_sensor, candidate_location, write_result, probe_temperature, probe_fault, promoted_at, reprocess_count)`.
    *   `IngestionPage(items, next_cursor)`.
    *   `PromoteRequest(overrides, note: str (1..500), replace_existing: bool = False)`.
    *   `Overrides(device_imei matching ^[0-9]{15}$, device_id: str (1..64), timestamp: int, latitude: float, longitude: float, temperature: float | None)`.
        Each field is optional. `temperature` uses a sentinel, so that an
        explicit null can be told apart from a field that was not provided.
    *   `HealthView(database, worker_heartbeat_age_s, pending, processing, failed)`.
    *   `LivezView(status, worker: "ok" | "stale" | "unknown")`.

#### 20. `app/api/webhook.py` (Create)

*   **Type / Scope**: Webhook route.
*   **Proposed Changes**: `POST /api/webhook/tive` with
    `require_role("sender")`. It follows the
    [request path](#request-path-post-apiwebhooktive) steps 3–6:
    1.  Validate the `Idempotency-Key` format (`400`).
    2.  Read the bytes.
    3.  Strict-parse with `parse_constant` raising, then walk the result for
        `\u0000`.
    4.  Hash the body (`ingest.hashing`).
    5.  Call `repository.insert_ingestion`. It returns `(row, created)` or
        raises `IdempotencyKeyReused`, which maps to `409`.
    6.  Respond `202` when created, `200` with `duplicate: true` when it is
        an existing row, or `400` with the ingestion ID when the row is
        `rejected`.

#### 21. `app/api/ingestions.py` (Create)

*   **Type / Scope**: Status and operator routes.
*   **Proposed Changes**:
    *   `GET /api/ingestions/{id}` (sender or operator) returns `404` if not
        found. It finds readings by the key in `write_result` and computes
        `current` and `superseded_by` (see Record ownership).
    *   `GET /api/ingestions/batch?ids=` (sender or operator) accepts at most
        100 IDs. For a sender key it filters to `api_key_id = caller`.
    *   `GET /api/ingestions` (operator) filters on `status` and
        `device_imei`, with `limit` 1–200 (default 50) and an opaque base64
        `cursor` over `(received_at, id)`.
    *   `POST …/{id}/promote`, `POST …/{id}/reprocess`, and
        `POST …/reprocess?status=&limit=` (operator) delegate to
        `ingest.operator`.

#### 22. `app/api/health.py` (Create)

*   **Type / Scope**: Health routes.
*   **Proposed Changes**:
    *   `GET /livez` reads `max(beat_at)` from `worker_heartbeats`, with a
        1 s statement timeout.
        *   Within `LIVEZ_GRACE_S` (90) of the web process starting, it
            returns `200` with `worker: "starting"`.
        *   After that grace period, it returns `503` with `worker: "stale"`
            if the heartbeat is older than `HEARTBEAT_STALE_S`.
        *   If the query fails, it returns `200` with `worker: "unknown"`.
        *   Otherwise it returns `200` with `worker: "ok"`.
    *   `GET /health` always returns `200` with a `HealthView`: the database
        state, the heartbeat age, and counts of `pending`, `processing`, and
        `failed`.

#### 23. `app/ingest/hashing.py` (Create)

*   **Type / Scope**: Body parsing and hashing.
*   **Proposed Changes**:
    *   `strict_parse(body: bytes) -> Any | MalformedBody`. It decodes UTF-8
        strictly, calls `json.loads` with a `parse_constant` that raises, and
        rejects any string containing `\u0000`.
    *   `canonical_hash(parsed)` is the SHA-256 of
        `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
    *   `raw_hash(body)` hashes the raw bytes.

#### 24. `app/ingest/repository.py` (Create)

*   **Type / Scope**: All SQL for ingestion, the queue, and readings.
*   **Proposed Changes**:
    *   **Webhook**: `insert_ingestion(...) -> (row, created)` does a single
        `INSERT … ON CONFLICT DO NOTHING RETURNING`. When no row comes back,
        it re-selects by `(api_key_id, idempotency_key)` if the header was
        sent, otherwise by `(api_key_id, body_hash)`. If the header was sent
        and the stored hash differs, it raises `IdempotencyKeyReused`.
    *   **Queue**:
        *   `claim_batch(conn, n, max_attempts)` runs the
            [claim query](#worker-the-postgres-table-queue).
        *   `release_for_retry(conn, id, error, attempts)` sets `pending`
            and the backoff `next_attempt_at`.
        *   `mark_failed(conn, id, error)` sets `failed`.
        *   `reclaim_stale(conn, stale_s, max_attempts)` returns the counts
            released and failed.
        *   `beat(conn, worker_id)` writes the heartbeat.
    *   **Persist**:
        *   `persist_result(conn, ingestion, result, final_status)` upserts
            the single `readings` row and writes the status, issues,
            candidates, `write_result`, `processed_at`, `device_imei`,
            `reading_timestamp_ms`, and `account_id`.
        *   `upsert_reading(conn, px_sensor, px_location, extras,
            ingestion_id, allow_replace_foreign: bool)` returns `"inserted"`,
            `"unchanged"`, `"replaced"`, or `"conflict"`.
            *   It runs inside a savepoint. On `"conflict"`, the savepoint is
                rolled back and the caller persists the ingestion as
                `quarantined` with `conflicts_existing`.
    *   **Reads**: `get_ingestion_view`, `get_ingestion_views(ids,
        api_key_scope)`, and `list_ingestions(filters, cursor, limit)`.

#### 25. `app/ingest/operator.py` (Create)

*   **Type / Scope**: Promote and reprocess logic.
*   **Proposed Changes**:
    *   `promote(conn, id, req, operator_key)` implements Promotion steps 1–5.
        It calls `pipeline.transform(raw_json, req.overrides, now_ms)` and
        then `upsert_reading` with `allow_replace_foreign =
        req.replace_existing`. It returns `409`, `422`, or the updated view.
    *   `reprocess(conn, id, operator_key)` is a single conditional `UPDATE`
        implementing the Reprocessing reset. It returns `409` if no row was
        updated.
    *   `reprocess_bulk(conn, status, limit, operator_key)` does the same in
        bulk, for at most 500 rows.

#### 26. `app/transform/__init__.py` (Create)

*   **Type / Scope**: Package API.
*   **Proposed Changes**: re-exports `transform` and `PipelineResult`.

#### 27. `app/transform/constants.py` (Create)

*   **Type / Scope**: Policy constants.
*   **Proposed Changes**: every policy number in one file:
    *   `PROVIDER` and `DEVICE_TYPE`.
    *   `TEMP_RANGE_C = (-100, 100)`.
    *   `OBSERVED_FAULT_VALUES`, for documentation and tests only.
    *   `MIN_TIMESTAMP_MS = 1420070400000` and `MAX_FUTURE_MS = 86_400_000`.
    *   `UTC_TOLERANCE_MS = 1000`.
    *   `ACCURACY_THRESHOLDS = ((50, "High"), (500, "Medium"))`, with `"Low"`
        as the fallback.
    *   `LOCATION_SOURCE_MAP`.
    *   `IMEI_PATTERN`.
    *   Secondary ranges: `HUMIDITY`, `BATTERY`, `DBM = (-150, -50)`, and
        `LUX_MIN = 0`.

#### 28. `app/transform/issues.py` (Create)

*   **Type / Scope**: Issue model.
*   **Proposed Changes**:
    *   `Severity` (`correction`, `warning`, `blocking`).
    *   `IssueCode` (`StrEnum`) holds every code in the rules table, plus
        `type_error`, `unparseable_number`, `missing_field`,
        `output_schema_violation`, `not_an_object`, `conflicts_existing`,
        `operator_override`, and `temperature_nulled_by_operator`.
    *   `Issue`, a frozen dataclass.

#### 29. `app/transform/tive_fields.py` (Create)

*   **Type / Scope**: Parsing each field on its own.
*   **Proposed Changes**:
    *   **Field table**: `FIELDS` maps each dotted Tive path to a group
        (`strict`, `primary`, or `secondary`) and a `TypeAdapter`.
    *   **Parser**: `parse(raw: dict) -> (ParsedView, list[Issue])` walks
        `FIELDS` and validates each path on its own.
        *   A failure gives an issue whose severity depends on the group, and
            the value is stored as `INVALID`.
        *   A numeric string is converted with a `correction`. It must match
            `^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$` and be finite.
            Booleans are rejected.
    *   **`ParsedView`** gives read access by path. `is_valid(path)` and
        `present(path)` let the rules tell "absent" apart from "invalid".

#### 30. `app/transform/rounding.py` (Create)

*   **Type / Scope**: Numeric helpers.
*   **Proposed Changes**: `round_half_up(value, places)` goes through
    `Decimal(str(value))`. `to_int(value)` rounds half-up to an integer.

#### 31. `app/transform/address.py` (Create)

*   **Type / Scope**: Splitting the address.
*   **Proposed Changes**: `split_address(formatted) -> dict | None`.
    *   Returns `None` when the input is null or blank.
    *   Otherwise sets `full_address`, with `street = None`.
    *   Fills `locality`, `state`, `postal_code`, and `country` only when the
        last three comma-separated parts match `<locality>`,
        `^[A-Z]{2} \d{5}(-\d{4})?$`, and `<country>`.

#### 32. `app/transform/rules.py` (Create)

*   **Type / Scope**: Business rules R0–R12.
*   **Proposed Changes**:
    *   Each rule is a function `(view, now_ms) -> list[Issue | Nullify | Computed]`.
        `RULES` runs them in order.
    *   Rules skip paths that are `INVALID`.
    *   `now_ms` is a parameter, so tests are deterministic.

#### 33. `app/transform/mapping.py` (Create)

*   **Type / Scope**: Tive → PX mapping.
*   **Proposed Changes**: implements the mapping table exactly.
    *   `to_px_sensor(view, nullify, computed)`.
    *   `to_px_location(view, nullify)`.
    *   `extras(view, nullify)` gives `SensorExtras(probe_temperature,
        probe_fault, account_id, entity_name, shipment_id from ShipmentId)`.

#### 34. `app/transform/output_validation.py` (Create)

*   **Type / Scope**: Output contract check.
*   **Proposed Changes**: loads the vendored PX schemas once with
    `Draft7Validator`. `validate_px(kind, record) -> list[Issue]` returns a
    blocking `output_schema_violation` issue, with a JSON pointer, for each
    schema error.

#### 35. `app/transform/pipeline.py` (Create)

*   **Type / Scope**: Pipeline orchestration.
*   **Proposed Changes**: `transform(raw, overrides, now_ms) ->
    PipelineResult(status_hint, issues, px_sensor, px_location, extras)`.
    The steps are:
    1.  A root that is not a `dict` → `rejected` with `not_an_object`.
    2.  Apply overrides to a deep copy of the payload, each one recorded as
        an `operator_override` correction.
    3.  `parse`.
    4.  Run the rules (R0–R12).
    5.  Map, even when there are blocking issues, so the candidates can be
        stored.
    6.  Validate the output only when there are no blocking issues.
    7.  Derive `status_hint` from the status table.

    The pipeline does no I/O.

#### 36. `app/schemas/tive-incoming-schema.json`, `px-sensor-schema.json`, `px-location-schema.json` (Create)

*   **Type / Scope**: Vendored contracts.
*   **Proposed Changes**: byte-for-byte copies of the workspace files.

#### 37. `app/worker/loop.py` (Create)

*   **Type / Scope**: The worker.
*   **Proposed Changes**: `run(settings)` loops until it receives SIGTERM.
    At startup, before the first claim, it writes the heartbeat and runs
    `reclaim_stale`. On each pass:
    1.  `claim_batch`.
    2.  For each claimed row, run `process_one` under
        `asyncio.wait_for(JOB_TIMEOUT_S)`, with up to `WORKER_MAX_JOBS` rows
        processed at once.
    3.  Every `HEARTBEAT_INTERVAL_S`, write the heartbeat.
    4.  Every 60 s, and once at startup, run `reclaim_stale`.
    5.  Sleep `WORKER_POLL_MIN_S` if work was found. Otherwise double the
        sleep, up to `WORKER_POLL_MAX_S`.

    `process_one` runs the transform and then `persist_result`. On an
    exception, a timeout, or cancellation, it calls `release_for_retry`, or
    `mark_failed` once the attempts are exhausted. It logs every outcome.
    `allow_replace_foreign` is `reprocess_count == 0`.

#### 38. `app/worker/__main__.py` (Create)

*   **Type / Scope**: Worker entrypoint.
*   **Proposed Changes**: sets up logging and the engine (pool 5), installs
    SIGTERM handling, and runs `loop.run`.

#### 39. `app/cli.py` (Create)

*   **Type / Scope**: Admin CLI.
*   **Proposed Changes**: `python -m app.cli` with the subcommands
    `create-key --name --role`, `revoke-key --id`, `list-keys`, and
    `reprocess --id <uuid> | --status <failed|quarantined> [--limit N]`.
    *   `create-key` prints the plaintext key once.
    *   `reprocess` calls the same `operator.reprocess` /
        `reprocess_bulk` functions as the HTTP endpoints, with
        `reprocessed_by = NULL`. It records `{"source": "cli"}` in the
        appended `issues_history` entry.
    *   In production it runs from a laptop against the Neon URL:
        `DATABASE_URL=<neon-url> uv run python -m app.cli create-key …`.
        Render's free instances have no shell.

#### 40. `tests/conftest.py` (Create)

*   **Type / Scope**: Test fixtures.
*   **Proposed Changes**:
    *   `samples`.
    *   `now_ms`, fixed at 2025-03-01T00:00:00Z.
    *   `db`, which migrates `TEST_DATABASE_URL` and truncates tables between
        tests.
    *   `client`, an httpx `AsyncClient` over `create_app()`.
    *   `sender_key` and `operator_key`.
    *   `run_worker_once`, which calls `claim_batch` and `process_one`
        directly.

    Integration tests are skipped when `TEST_DATABASE_URL` is unset.

#### 41. `tests/fixtures/sample-tive-payloads.json` (Create)

*   **Type / Scope**: Test data.
*   **Proposed Changes**: a vendored copy of the 13 samples.

#### 42. `tests/fixtures/expected_outcomes.py` (Create)

*   **Type / Scope**: Expected results.
*   **Proposed Changes**: for each sample: the expected status, the issue
    codes grouped by severity, and selected PX field values. Examples:
    sample 1 has `humidity == 38.7`, `accelerometer.magnitude == 0.990`,
    `location_source == "WiFi"`, and `location_accuracy_category ==
    "High"`; sample 7 has `location_accuracy_category == "Medium"`.

#### 43. `tests/unit/test_rounding.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: half-up cases (`2.675` → `2.68`, `-0.5625` →
    `-0.563`, `38.70000076293945` → `38.7`), plus null and integer cases.

#### 44. `tests/unit/test_address.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: every `FormattedAddress` in the samples, plus ZIP+4
    and a blank string.

#### 45. `tests/unit/test_tive_fields.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   Numeric strings convert in measurement fields and are recorded as
        corrections.
    *   An unparseable secondary value gives a warning and null. An
        unparseable primary temperature is blocking.
    *   Booleans are rejected.
    *   Strict fields reject strings.
    *   One invalid strict field does not stop the other fields from being
        parsed.
    *   Non-integer values for integer fields give a warning and null.

#### 46. `tests/unit/test_rules.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: one class per rule, R0–R12, with a pass case and
    every failure branch. This includes these boundaries: ±90 / ±90.000001
    latitude; `MIN_TIMESTAMP_MS` and +24 h; each observed fault value for
    both primary and probe temperature; dBm at −150 and −151; R3 with
    `EntityName` absent (no warning); and R0 for each missing field.

#### 47. `tests/unit/test_mapping.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   Sample 1 maps field for field to each PX schema's `examples[0]`,
        including rounding, the address split, enums, and nulls.
    *   Every `location_source` value.
    *   Accuracy thresholds at 50, 51, 500, and 501 m, and with null.
    *   A missing `Accelerometer`.
    *   Nullify markers are honored.

#### 48. `tests/unit/test_output_validation.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**: a valid record passes. A missing required field, a
    bad enum value, and `battery_level` 101 each produce
    `output_schema_violation` with the right pointer.

#### 49. `tests/unit/test_pipeline_samples.py` (Create)

*   **Type / Scope**: Unit tests (the main accuracy test).
*   **Proposed Changes**:
    *   Parametrized over all 13 samples: expected status, issue codes, field
        values, and schema validity of every record that is not quarantined.
    *   Roots that are not objects are rejected.
    *   A string latitude still produces the other issues and a candidate
        sensor record.
    *   Sample 2 with a latitude override has no blocking issues.

#### 50. `tests/unit/test_hashing.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   Key order and whitespace do not change the hash.
    *   A changed value changes the hash.
    *   `strict_parse` rejects `NaN`, `Infinity`, `\u0000`, and invalid
        UTF-8.

#### 51. `tests/unit/test_vendored_schemas.py` (Create)

*   **Type / Scope**: Drift guard.
*   **Proposed Changes**: compares the SHA-256 of each vendored file against
    a pinned constant.

#### 52. `tests/integration/test_webhook.py` (Create)

*   **Type / Scope**: Integration tests.
*   **Proposed Changes**:
    *   Each CUJ 5 case returns its stated code, and the stored/not-stored
        behavior matches.
    *   An oversized body without a key returns `413`, not `401`.
    *   A valid request returns `202` and creates a `pending` row.
    *   The same body again returns `200` with the same ID. The same body
        under a different key gets a new ID.
    *   A header with the same body returns `200`. A header reused with a
        different body returns `409` and stores no row.
    *   Identical bodies with different headers create two rows.
    *   Race: two concurrent requests that reuse one key with different
        bodies get one `202` and one `409`.
    *   A resend of a malformed body returns `400` with the original ID.

#### 53. `tests/integration/test_worker.py` (Create)

*   **Type / Scope**: Integration tests.
*   **Proposed Changes**:
    *   A claimed and processed row gets the expected status.
    *   Two concurrent `claim_batch` calls never return the same row.
    *   A failing job retries with backoff, then is marked `failed` after
        `MAX_ATTEMPTS`.
    *   A timeout releases the row.
    *   A row stuck in `processing` with `attempts` below the cap returns to
        `pending`. At the cap, it becomes `failed`.
    *   A reprocessed row is claimed again (CUJ 3).
    *   The heartbeat is written.

#### 54. `tests/integration/test_persistence.py` (Create)

*   **Type / Scope**: Integration tests.
*   **Proposed Changes**:
    *   Race: two connections persisting different payloads for the same key
        at the same time end with one current `readings` row and one
        `readings_history` row.
    *   A reprocessed ingestion that hits `conflict` leaves the existing
        reading unchanged, with no partial write.
    *   `unchanged` keeps the original `ingestion_id`. `replaced` updates it
        and writes history.
    *   A reprocessed ingestion that meets a reading owned by another
        ingestion gets `conflict` and `conflicts_existing`.
    *   An invalid IMEI or latitude inserted directly raises a `CHECK`
        violation.
    *   The status view shows `current` and `superseded_by` correctly.

#### 55. `tests/integration/test_operator.py` (Create)

*   **Type / Scope**: Integration tests.
*   **Proposed Changes**:
    *   Promoting sample 2 with a corrected latitude returns `409`. With
        `replace_existing` it becomes `promoted`, and history is written.
    *   Promoting a row that is not quarantined returns `409`.
    *   Overrides that still fail return `422`.
    *   Sample 5 with `device_imei = 866088073468499` (an IMEI no sample
        uses) becomes `promoted`. With `863257063350583` it returns `409`.
    *   A sample with a `temperature_fault`, promoted with
        `temperature: null`, becomes `promoted`.
    *   Reprocess moves the issues to history and resets `attempts`. Bulk
        reprocess returns its count.
    *   A sender key on operator endpoints gets `403`.

#### 56. `tests/integration/test_health.py` (Create)

*   **Type / Scope**: Integration tests.
*   **Proposed Changes**:
    *   `/livez` returns `200` `starting` within the grace period even with a
        stale heartbeat, and after the grace period: `200` with a fresh
        heartbeat, `503` with a stale
        one, and `200` `unknown` when the database query fails (simulated
        with an unreachable engine).
    *   `/health` always returns `200` with counts.

#### 57. `tests/integration/test_e2e_samples.py` (Create)

*   **Type / Scope**: End-to-end tests.
*   **Proposed Changes**:
    *   Post all 13 samples in order and run the worker until nothing is
        pending.
    *   Every final status matches `expected_outcomes`.
    *   Sample 8 replaced nothing, because its timestamp differs from
        sample 1.
    *   Exactly 8 `readings` rows exist. Each has both `px_sensor` and
        `px_location`, and each validates against its PX schema.

#### 58. `.github/workflows/ci.yml` (Create)

*   **Type / Scope**: CI.
*   **Proposed Changes**: on push and PR, with a `postgres:16` service:
    1.  `uv sync --frozen`
    2.  `ruff check`
    3.  `ruff format --check`
    4.  `mypy app/transform`
    5.  `pytest --cov=app --cov-fail-under=85` with `TEST_DATABASE_URL`
    6.  `docker build`

    Branch coverage for `app/transform/` is reported against a 95% target but
    does not fail the build.

#### 59. `README.md` (Create)

*   **Type / Scope**: Documentation.
*   **Proposed Changes**, in this order:
    1.  What it does, and the diagram.
    2.  Live URLs and the test key.
    3.  A curl quickstart.
    4.  Local setup (`docker compose up`, `python -m app.cli create-key`).
    5.  Tests.
    6.  API reference (`/docs` and a status-code table).
    7.  The processing model and statuses.
    8.  The edge-case policy (R0–R12) and the per-sample outcomes.
    9.  Idempotency.
    10. The data model.
    11. Deployment: Render and Neon, `render.yaml`, and key creation from a
        laptop.
    12. Assumptions: every Open Question stated as an assumption, plus
        "AccountId stored only" and "quarantine review has no owner".
    13. Risks and future improvements.

#### 60. `docs/DESIGN.md` (Create)

*   **Type / Scope**: Documentation.
*   **Proposed Changes**: a copy of this design document. Links are rewritten
    for the repository: schema links point to `app/schemas/`, sample links
    point to `tests/fixtures/`, and `EXERCISE.md` is described in the text
    instead of linked.

#### 61. `docs/OPERATIONS.md` (Create)

*   **Type / Scope**: Runbook.
*   **Proposed Changes**:
    *   Reading `/livez` and `/health`.
    *   Listing the quarantine queue.
    *   Choosing promote, reprocess, or leave, using the promotable-codes
        table and one worked example per quarantine reason.
    *   Rotating and revoking keys from a laptop.
    *   Handling `failed > 0`.
    *   Replaying after a transform fix.

    The runbook describes the review procedure. Who does the reviewing is
    still open (see Open Questions).

### Mock Sender: target files (`./mock-tive-sender/`)

#### 62. `pyproject.toml`, `uv.lock`, `.env.example`, `.gitignore`, `.dockerignore` (Create)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**:
    *   The dependencies listed above, with the same ruff and pytest
        configuration as the Integration API.
    *   `.env.example` holds `INTEGRATION_API_URL`, `INTEGRATION_API_KEY`,
        `REQUEST_TIMEOUT_S=60` (long enough to cover an API cold start),
        `POLL_TIMEOUT_S=60`, and `HISTORY_LIMIT=500`.

#### 63. `Dockerfile`, `render.yaml` (Create)

*   **Type / Scope**: Deployment.
*   **Proposed Changes**:
    *   The image uses `python:3.12-slim`, runs as a non-root user, and
        starts `uvicorn sender.main:app`.
    *   The blueprint defines one free web service with
        `healthCheckPath: /healthz` and two `sync: false` secrets.

#### 64. `sender/__init__.py`, `sender/config.py` (Create)

*   **Type / Scope**: Package and settings.
*   **Proposed Changes**: a `Settings(BaseSettings)` with the variables from
    `.env.example`.

#### 65. `sender/samples.py` (Create)

*   **Type / Scope**: Sample loader.
*   **Proposed Changes**: `load_samples() -> list[Sample(index, name, description, payload)]`.

#### 66. `sender/generator.py` (Create)

*   **Type / Scope**: Payload generator.
*   **Proposed Changes**:
    *   `generate(count, device_count, seed, start_ms, interval_ms)` uses a
        seeded `random.Random`.
    *   Each device gets a 15-digit IMEI, a `G######` name, and a shipment.
        Readings get random values within schema ranges, and the temperature
        varies around 2–8 °C.
    *   Each payload is validated against the vendored Tive schema. Any
        failure raises `GeneratorError`.

#### 67. `sender/mutations.py` (Create)

*   **Type / Scope**: Edge-case mutators.
*   **Proposed Changes**:
    *   **Single-payload mutations** are pure functions that each return a
        new payload: `out_of_range_latitude`, `out_of_range_longitude`,
        `drop_device_id`, `seconds_timestamp`, `stringify_numbers`,
        `probe_fault`, and `primary_temp_fault`.
    *   **Pair mutations**: `exact_resend` and `conflicting_duplicate`.
    *   **Registry**: `MUTATIONS` maps each name to a label and its function.
    *   **Mixing**: `apply_mix(payloads, selected, rate, rng)` applies the
        selected mutations at the given rate.

#### 68. `sender/client.py` (Create)

*   **Type / Scope**: Integration API client.
*   **Proposed Changes**: `ApiClient` wraps `httpx.AsyncClient`.
    *   `send(payload | raw_bytes, key_override, idempotency_key) -> SendResult`
        sends one payload.
    *   `get_statuses(ids) -> dict` calls the batch endpoint.
    *   `livez()` checks liveness.
    *   The client never retries, so the demo shows real responses. Errors
        are recorded in the result and are not raised.

#### 69. `sender/store.py` (Create)

*   **Type / Scope**: In-memory send history.
*   **Proposed Changes**: `HistoryStore` is a bounded `deque` guarded by an
    `asyncio.Lock`. Each `SendRecord` holds `local_id`, `label`, `sent_at`,
    `http_status`, `ingestion_id`, `final_status`, `issues`, `readings`,
    `error`, and `first_polled_at`.

#### 70. `sender/main.py` (Create)

*   **Type / Scope**: Routes.
*   **Proposed Changes**:
    *   `GET /` renders the page.
    *   Send routes:
        *   `POST /send/sample/{index}` sends one sample.
        *   `POST /send/samples` sends all 13.
        *   `POST /send/generated` takes a form with count (at most 200),
            devices, seed, mutations, rate, and the idempotency toggle.
        *   `POST /send/demo/{case}`, where the case is `oversized`,
            `no_key`, `bad_key`, `bad_header`, `malformed_json`, `resend`,
            or `idempotency_conflict`.

        Each send route returns the results-table partial.
    *   `GET /results` calls `get_statuses` once for all non-final IDs and
        returns the table. The response includes the `hx-trigger` interval
        for the current backoff stage (1 s, then 5 s after 10 s), or no
        trigger when every row is final or 60 s have passed.
    *   `GET /api-status` calls `livez()` and returns the banner partial. The
        partial has no polling trigger once the API is up.
    *   `GET /healthz` is the Mock Sender's own liveness check.

#### 71. `sender/templates/base.html`, `index.html`, `partials/results_table.html`, `partials/api_banner.html` (Create)

*   **Type / Scope**: Jinja templates.
*   **Proposed Changes**: three panels plus the results table. Each row shows
    a status chip, the issue codes, and a `<details>` block with the JSON.

#### 72. `sender/static/htmx.min.js`, `sender/static/style.css` (Create)

*   **Type / Scope**: Static assets.
*   **Proposed Changes**: vendored HTMX 2.0.x (the pinned version is noted
    in the README) and a small stylesheet.

#### 73. `sender/data/sample-tive-payloads.json`, `sender/schemas/tive-incoming-schema.json` (Create)

*   **Type / Scope**: Vendored data.
*   **Proposed Changes**: byte-for-byte copies of the workspace files.

#### 74. `tests/test_generator.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   100 generated payloads all pass the Tive schema.
    *   The same seed produces identical output.
    *   Timestamps increase per device.
    *   Every IMEI has 15 digits.

#### 75. `tests/test_mutations.py` (Create)

*   **Type / Scope**: Unit tests.
*   **Proposed Changes**:
    *   Each mutation produces exactly its intended defect and leaves its
        input unchanged.
    *   A seeded `apply_mix` is deterministic.

#### 76. `tests/test_routes.py` (Create)

*   **Type / Scope**: Route tests.
*   **Proposed Changes**: run against an `httpx.MockTransport` fake of the
    API.
    *   Each send route records its result.
    *   `/results` makes one batch call and stops polling once every row is
        final or the timeout passes.
    *   Each demo case sends what it claims to.
    *   The API key never appears in the rendered HTML.

#### 77. `tests/test_vendored_files.py` (Create)

*   **Type / Scope**: Drift guard.
*   **Proposed Changes**: compares the vendored files against pinned SHA-256
    values.

#### 78. `.github/workflows/ci.yml` (Create)

*   **Type / Scope**: CI.
*   **Proposed Changes**: runs `uv sync --frozen`, `ruff check`,
    `ruff format --check`, `pytest`, and `docker build`.

#### 79. `README.md` (Create)

*   **Type / Scope**: Documentation.
*   **Proposed Changes**:
    *   What the Mock Sender does and its live URL.
    *   Local setup against a local or deployed API.
    *   Reference for the generator and mutations.
    *   Each demo case and the API response it expects.
    *   A note that history is kept in memory.

### Workspace files (`./`)

#### 80. `docs/designs/tive-paxafe-integration.md` (this document)

*   **Type / Scope**: Design document.
*   **Proposed Changes**: kept as the source of truth. It is copied to
    `integration-api/docs/DESIGN.md` (file 60), with links rewritten.

`EXERCISE.md` and the four JSON files in the workspace stay unchanged. They
are the source for the vendored copies.

### Verification Plan

*   **Build**: `docker build .` succeeds in both repos. In
    `integration-api`, `docker compose up --build` starts Postgres, migrate,
    web, and worker, and `/livez` and `/health` both return `200`.
*   **Unit tests**: `uv run pytest -m unit` in `integration-api`, and
    `uv run pytest` in `mock-tive-sender`.
*   **Integration tests**: `uv run pytest -m integration`, with
    `TEST_DATABASE_URL` pointing at the compose Postgres.
*   **Static analysis**: `uv run ruff check .`, `uv run ruff format --check .`,
    and `uv run mypy app/transform`.
*   **Coverage**:
    *   At least 85% line coverage for `app/`, enforced in CI.
    *   A 95% branch-coverage target for `app/transform/`, reported but not
        enforced.
*   **Deployed end to end**, run manually after each deploy and recorded in
    the README:
    1.  Open the Mock Sender and wait for the banner to turn green.
    2.  Send all 13 samples. The final statuses match the expected-outcome
        table exactly: 8 accepted or accepted with corrections, and 5
        quarantined.
    3.  Run every demo case and check each status code.
    4.  Send 50 generated payloads with every mutation at a rate of 0.3.
        None end in `failed`, and webhook responses stay under 500 ms on a
        warm instance.
    5.  Restart the API service on Render while rows are `pending`. They
        reach a final status after the restart.
*   **Operator path**, run manually with curl:
    1.  Promote sample 5 with IMEI `866088073468499`.
    2.  Reprocess one quarantined row, and confirm that `issues_history` holds
        the earlier issues and the row is processed again.
