# Implementation notes

Choices made where the design is silent or ambiguous, each tied to its design section. Where a
choice would change an expected outcome or an HTTP status code, it is marked **DECISION NEEDED**.

## RESOLVED: sample 12 and rule R6 (Expected outcome per sample, R6)

Sample 12 has `EntryTimeEpoch` 2025-02-15T19:27:26Z and `EntryTimeUtc` 14:27:26Z (5 h apart). The
original R6 made any mismatch over 1 s blocking, which quarantined sample 12 and contradicted the
design table. Owner decision (2026-11-02): R6 is a **warning**; the integer epoch is authoritative.
Sample 12 is `accepted` with warning `timestamp_mismatch`; sample 6 is still quarantined by R5 and
now lists `timestamp_mismatch` as a warning. Result: 8 readings, 5 quarantined.

## Issue codes not listed in the design (Transform pipeline, file 28)

* `number_from_string` (correction): a numeric string converted in a measurement field.
* `not_an_integer` (warning): non-integer value in an integer field (`Battery.Percentage`,
  `Location.WifiAccessPointUsedCount`, `AccountId`).
* `unknown_location_method` (warning): `LocationMethod` not `gps`/`wifi`/`cell`; source becomes null.
* `malformed_json` (blocking, on `rejected` ingestions): the reason the body was rejected.
* R11 `<field>_out_of_range` codes: `humidity_out_of_range`, `lux_out_of_range`,
  `battery_out_of_range`, `dbm_out_of_range`, `accuracy_out_of_range`.

## Parsing (Stage 1, file 29)

* Per-field validators are plain functions, not pydantic `TypeAdapter`s: pydantic's lax mode would
  coerce strings and booleans, which the policy forbids in strict fields.
* `Temperature.Fahrenheit` is parsed as a secondary numeric field, because sample 8 is expected
  to report its string conversion (three corrections: Dbm, Celsius, Fahrenheit).
* `AccountId`, `ShipmentId`, `Shipment.DeviceId`, `Location.FormattedAddress` are secondary fields
  (a bad value becomes null with a warning). `EntityName` is strict.
* `EntryTimeEpoch` accepts an integral float (`1739215646000.0`) but not a string. `EntryTimeUtc`
  must be ISO 8601 with a timezone; naive values are a blocking `type_error`.
* A wrong-typed optional container (for example `"Humidity": "wet"`) is a warning and its fields
  are treated as absent. A wrong-typed `Temperature` or `Location` is reported only by R0.
* R0: when `Location` is missing, `missing_field` is emitted for `Location`, `Location.Latitude`
  and `Location.Longitude` (likewise `Temperature` and `Temperature.Celsius`), so each is
  overridable. A null latitude, longitude or `EntryTimeEpoch` counts as missing.
* R12 is skipped when `Accelerometer.G` is present but invalid (the parser already warned).
* IMEI matching uses `fullmatch`, so `"863257063350583\n"` is malformed.

## Request path (files 20, 23)

* A body that is valid JSON but not an object is accepted (`202`); the worker rejects it with
  `not_an_object` (design: "root is not a JSON object" -> `rejected`).
* Numbers that overflow to infinity (`1e999`) are treated as malformed JSON (jsonb cannot hold them).
* `400 malformed_json` body: the standard error envelope plus top-level `ingestion_id`, `status`
  (`rejected`), `status_url`, `duplicate`, and `error.details.ingestion_id`.
* The `200` duplicate and `202` bodies are `WebhookAccepted`: `ingestion_id`, `status`
  (the current status), `status_url`, `duplicate`.
* `last_used_at` is committed immediately by the auth dependency (a later `4xx` cannot roll it back).
* An operator key on the webhook gets `403` (the route requires the `sender` role).
* Writing routes commit explicitly before responding; the per-request connection otherwise rolls
  back on close.

## API surface (files 19, 21, 22)

* `GET /api/ingestions/batch?ids=` takes ids comma-separated or repeated and returns
  `{"items": [IngestionView, ...]}` in request order. Unknown ids, and ids a sender key did not
  create, are omitted (no error). More than 100 ids, or a non-UUID, is `422`. (The design does not
  specify the response envelope; this matches the Mock Sender client, which also accepts a bare
  list or an id-keyed object.)
* `IngestionView` and `ReadingView` are exactly as in file 19. `last_error` is not exposed.
* `LivezView.worker` also allows `"starting"` (the design's livez text requires it);
  `status` is `"ok"` or `"unavailable"` (503).
* `/livez` after the grace period with no heartbeat row at all returns `503 stale` (the worker never
  started). `/health` reports `worker_heartbeat_age_s: null` then.
* `Overrides`: for every field except `temperature`, an explicit null means "not provided";
  `temperature: null` is kept and nulls the temperature (via `model_fields_set`, instead of a
  sentinel object). Unknown override fields are `422`.
* Bulk reprocess accepts `rejected`, `failed` or `quarantined`; anything else is `422`.
  `limit` defaults to 500 and is capped at 500.
* Errors for `404`/`405`/etc. use the same envelope; unhandled exceptions are `500 internal_error`.

## Persistence (files 11, 24)

* `readings.device_imei` and `timestamp_ms` (generated) are also `NOT NULL`, so the unique key
  cannot be bypassed with nulls. Extra indexes beyond the data model: `readings(ingestion_id)`,
  `readings_history(device_imei, timestamp_ms)`, `readings_history(ingestion_id)`.
* `readings_history` has its own `id` plus `reading_id` (the replaced row's id).
* `write_result` has an extra `owner_ingestion_id` when the result is `unchanged`, so the status
  view can tell whether the ingestion's records are still current. A conflict-quarantined
  ingestion has `write_result = NULL`. Candidate records are stored only for quarantined rows.
* "Different content" compares `px_sensor`, `px_location`, `probe_temperature` and `probe_fault`
  with Postgres JSONB equality (so `10` equals `10.0`). `account_id`, `entity_name` and
  `shipment_id` are written on insert and replace, but do not count as content.
* A reprocessed ingestion that finds a reading it already owns with different content replaces it
  (with history); only a reading owned by *another* ingestion is a conflict.
* The status view picks the latest history version written by the owning ingestion when the
  reading was superseded.
* Worker: a row whose `raw_json` is SQL NULL (malformed body) is re-checked against `raw_body`, so
  reprocessing it is rejected again with `malformed_json`.
* Worker `process_one` re-raises cancellation after releasing the row (the release is shielded).
* `ingestions.device_imei` / `reading_timestamp_ms` / `account_id` are set whenever the pipeline
  produced a sensor record (including quarantined rows), for the `device_imei` filter.

## Promotion (file 25)

* The new pipeline issues (including `operator_override` corrections) replace `issues`; the
  quarantine-time issues are appended to `issues_history` with reason `promotion`. The note,
  overrides and `replace_existing` go in `promotion_overrides`; candidates are cleared.
* `409` for an existing reading is raised only when its content differs; identical content is
  `unchanged` and promotion succeeds. `422` is also returned if the stored root is not an object.
* An override of `timestamp` does not touch `EntryTimeUtc`; if they then disagree R6 still blocks.
* `promoted` ingestions have `attempts` unchanged and `processed_at` set to the promotion time.

## Tooling and layout

* `.python-version` pins 3.12 and `requires-python` is `>=3.12,<3.13` (uv otherwise picked 3.15).
* Web pool 5, worker pool 5, `max_overflow=0`.
* `app/db.py` defines Core `Table` objects for every table (documentation and metadata); most SQL
  is written with `text()` because of generated columns, `ON CONFLICT` and `FOR UPDATE SKIP LOCKED`.
* Integration fixtures live in `tests/conftest.py` as specified; sample helpers are in
  `tests/integration/helpers.py`. The tests use a separate database, `paxafe_test`, so the
  truncate-between-tests fixture never touches the compose `paxafe` database.
* `tests/` runs with coverage `concurrency = ["thread", "greenlet"]` (needed for SQLAlchemy async).
* The compose stack runs web and worker as separate services with `entrypoint: []`; the image's
  default entrypoint (`scripts/entrypoint.sh`: migrate, then honcho) was also checked with
  `docker run`. `scripts/entrypoint.sh` defaults `PORT` to 8000.
* `docs/DESIGN.md` is the design with the schema, sample and exercise links rewritten and a short
  pointer to this file added at the top.
* Not run: any deployment (Render/Neon), and the crash/hang restart scenarios of CUJ 6 against a real
  container (the worker and reclaim logic are covered by integration tests).
