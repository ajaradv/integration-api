"""initial schema

Written by hand: generated columns, partial indexes and CHECK constraints.
Convention: later migrations only add columns or tables.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

STATUSES = (
    "'pending','processing','accepted','accepted_with_corrections',"
    "'quarantined','rejected','promoted','failed'"
)

_READING_PLAIN = """
    px_sensor jsonb NOT NULL,
    px_location jsonb NOT NULL,
    probe_temperature numeric,
    probe_fault boolean NOT NULL DEFAULT false,
    account_id bigint,
    entity_name text,
    shipment_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            key_hash char(64) NOT NULL UNIQUE,
            role text NOT NULL CHECK (role IN ('sender','operator')),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_used_at timestamptz
        )
    """)
    op.execute(f"""
        CREATE TABLE ingestions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            received_at timestamptz NOT NULL DEFAULT now(),
            api_key_id uuid NOT NULL REFERENCES api_keys(id),
            body_hash char(64) NOT NULL,
            idempotency_key varchar(255),
            raw_body bytea NOT NULL,
            raw_json jsonb,
            status text NOT NULL CHECK (status IN ({STATUSES})),
            attempts int NOT NULL DEFAULT 0,
            next_attempt_at timestamptz,
            claimed_at timestamptz,
            processed_at timestamptz,
            last_error text,
            issues jsonb,
            issues_history jsonb NOT NULL DEFAULT '[]'::jsonb,
            candidate_sensor jsonb,
            candidate_location jsonb,
            write_result jsonb,
            account_id bigint,
            device_imei text,
            reading_timestamp_ms bigint,
            promoted_by uuid REFERENCES api_keys(id),
            promoted_at timestamptz,
            promotion_overrides jsonb,
            reprocess_count int NOT NULL DEFAULT 0,
            reprocessed_by uuid REFERENCES api_keys(id),
            reprocessed_at timestamptz,
            CONSTRAINT ingestions_idem_key UNIQUE (api_key_id, idempotency_key)
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX ingestions_body_hash_uq ON ingestions (api_key_id, body_hash) "
        "WHERE idempotency_key IS NULL"
    )
    op.execute("CREATE INDEX ingestions_status_received ON ingestions (status, received_at)")
    op.execute("CREATE INDEX ingestions_device_imei ON ingestions (device_imei)")
    op.execute("CREATE INDEX ingestions_received_id ON ingestions (received_at, id)")
    op.execute(
        "CREATE INDEX ingestions_pending_received ON ingestions (received_at) "
        "WHERE status = 'pending'"
    )
    op.execute("""
        CREATE TABLE worker_heartbeats (
            worker_id text PRIMARY KEY,
            beat_at timestamptz NOT NULL
        )
    """)
    op.execute(f"""
        CREATE TABLE readings (
            id bigserial PRIMARY KEY,
            ingestion_id uuid NOT NULL REFERENCES ingestions(id),
            {_READING_PLAIN},
            device_imei text GENERATED ALWAYS AS (px_sensor->>'device_imei') STORED NOT NULL
                CHECK (device_imei ~ '^[0-9]{{15}}$'),
            timestamp_ms bigint GENERATED ALWAYS AS ((px_sensor->>'timestamp')::bigint) STORED
                NOT NULL CHECK (timestamp_ms > 0),
            temperature numeric GENERATED ALWAYS AS ((px_sensor->>'temperature')::numeric) STORED,
            latitude double precision GENERATED ALWAYS AS ((px_location->>'latitude')::float8)
                STORED CHECK (latitude BETWEEN -90 AND 90),
            longitude double precision GENERATED ALWAYS AS ((px_location->>'longitude')::float8)
                STORED CHECK (longitude BETWEEN -180 AND 180),
            CHECK (px_location->>'device_imei' = px_sensor->>'device_imei'
                   AND px_location->>'timestamp' = px_sensor->>'timestamp'),
            UNIQUE (device_imei, timestamp_ms)
        )
    """)
    op.execute("CREATE INDEX readings_ingestion ON readings (ingestion_id)")
    op.execute(f"""
        CREATE TABLE readings_history (
            id bigserial PRIMARY KEY,
            reading_id bigint NOT NULL,
            ingestion_id uuid NOT NULL,
            {_READING_PLAIN},
            device_imei text NOT NULL,
            timestamp_ms bigint NOT NULL,
            temperature numeric,
            latitude double precision,
            longitude double precision,
            replaced_at timestamptz NOT NULL DEFAULT now(),
            replaced_by_ingestion_id uuid NOT NULL
        )
    """)
    op.execute("CREATE INDEX readings_history_key ON readings_history (device_imei, timestamp_ms)")
    op.execute("CREATE INDEX readings_history_ingestion ON readings_history (ingestion_id)")


def downgrade() -> None:
    for table in ("readings_history", "readings", "worker_heartbeats", "ingestions", "api_keys"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
