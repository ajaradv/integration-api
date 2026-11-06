# Integration API tasks. Run `just` to list them.
#
# Production values live in `.env` (gitignored), which just loads automatically:
#   NEON_DATABASE_URL   Neon DIRECT (non-pooled) URL, pasted as Neon shows it (normalized by app.config)
#   API_URL             public Render URL, e.g. https://paxafe-integration-api.onrender.com
#   RENDER_DEPLOY_HOOK  Render deploy hook URL (Settings -> Deploy Hook), for `just redeploy`

set dotenv-load
set positional-arguments

local_db := "postgresql+asyncpg://postgres:postgres@localhost:55432/paxafe"
test_db := "postgresql+asyncpg://postgres:postgres@localhost:55432/paxafe_test"
local_url := "http://localhost:8000"

default:
    @just --list --unsorted

# --- setup -----------------------------------------------------------------

# Install dependencies into .venv
setup:
    uv sync

# --- local stack (Docker) --------------------------------------------------

# Build and start Postgres, migrations, web and worker in Docker
up:
    docker compose up --build -d
    @just wait-healthy {{local_url}}

# Stop the stack, keep the database volume
down:
    docker compose down

# Stop the stack and delete the database volume
nuke:
    docker compose down -v

# Show container status
ps:
    docker compose ps

# Follow logs (optionally for one service: web, worker, postgres, migrate)
logs service="":
    docker compose logs -f {{service}}

# Start only Postgres (for `just dev` and `just test`)
db:
    docker compose up -d --wait postgres

# Open psql on the local database
psql:
    docker compose exec postgres psql -U postgres -d paxafe

# --- local without Docker for the app (hot reload) -------------------------

# Run migrations against the local database
migrate:
    DATABASE_URL={{local_db}} uv run alembic upgrade head

# Run web (with reload) and worker on the host against the Docker Postgres
dev: db migrate
    DATABASE_URL={{local_db}} uv run honcho start -f Procfile.dev

# --- quality ---------------------------------------------------------------

# Lint, format check, and type check
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy app/transform

# Auto-fix lint and formatting
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Unit tests only (no database needed)
test-unit *args:
    uv run pytest tests/unit "$@"

# Full test suite, including integration tests against the Docker Postgres
test *args: db test-db
    TEST_DATABASE_URL={{test_db}} uv run pytest "$@"

# Create the test database if it is missing
test-db:
    @docker compose exec -T postgres psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='paxafe_test'" | grep -q 1 \
      || docker compose exec -T postgres createdb -U postgres paxafe_test

# Everything CI runs
check: lint test

# --- keys and operations (local) -------------------------------------------

# Create an API key locally: just key mock-sender sender
key name role="sender":
    DATABASE_URL={{local_db}} uv run python -m app.cli create-key --name "{{name}}" --role {{role}}

# List API keys locally
keys:
    DATABASE_URL={{local_db}} uv run python -m app.cli list-keys

# Revoke a local API key by id
revoke id:
    DATABASE_URL={{local_db}} uv run python -m app.cli revoke-key --id {{id}}

# Re-queue ingestions locally: just reprocess --status failed
reprocess *args:
    DATABASE_URL={{local_db}} uv run python -m app.cli reprocess "$@"

# Check /livez and /health (defaults to local)
health url=local_url:
    @curl -fsS {{url}}/livez; echo
    @curl -fsS {{url}}/health; echo

# Poll /health until the database is up and a worker heartbeat is fresh (tries x 2 s)
wait-healthy url=local_url tries="60":
    #!/usr/bin/env bash
    for i in $(seq 1 {{tries}}); do
      body=$(curl -fsS {{url}}/health 2>/dev/null) && echo "$body" | python3 -c '
    import json, sys
    h = json.load(sys.stdin)
    age = h.get("worker_heartbeat_age_s")
    sys.exit(0 if age is not None and age < 60 else 1)' && { echo "$body"; exit 0; }
      sleep 2
    done
    echo "not healthy after $(( {{tries}} * 2 )) s: ${body:-no response}" >&2; exit 1

# --- production (Neon + Render) --------------------------------------------

[private]
need var:
    @[ -n "$(printenv {{var}})" ] || { echo "{{var}} is not set; add it to .env" >&2; exit 1; }

# Run migrations against Neon (Render also runs them on every start)
neon-migrate: (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run alembic upgrade head

# Show the Neon migration revision
neon-status: (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run alembic current

# Create an API key in Neon: just neon-key mock-sender sender
neon-key name role="sender": (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run python -m app.cli create-key --name "{{name}}" --role {{role}}

# List API keys in Neon
neon-keys: (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run python -m app.cli list-keys

# Revoke an API key in Neon by id
neon-revoke id: (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run python -m app.cli revoke-key --id {{id}}

# Re-queue ingestions in Neon: just neon-reprocess --status failed
neon-reprocess *args: (need "NEON_DATABASE_URL")
    DATABASE_URL="$NEON_DATABASE_URL" uv run python -m app.cli reprocess "$@"

# Open psql on Neon
neon-psql: (need "NEON_DATABASE_URL")
    psql "$(echo "$NEON_DATABASE_URL" | sed -e 's/+asyncpg//' -e 's/ssl=require/sslmode=require/')"

# Check the deployed service
prod-health: (need "API_URL")
    @just health "$API_URL"

# Run checks and push main; Render auto-deploys the push. Waits for health if API_URL is set
deploy: check
    @test -z "$(git status --porcelain)" || { echo "working tree is dirty; commit first" >&2; exit 1; }
    git push origin main
    @if [ -n "${API_URL:-}" ]; then echo "waiting for the new deploy (free tier builds take a few minutes)"; sleep 60; just wait-healthy "$API_URL" 150; fi

# Redeploy the current commit without pushing (uses the Render deploy hook)
redeploy: (need "RENDER_DEPLOY_HOOK")
    @curl -fsS -X POST "$RENDER_DEPLOY_HOOK" >/dev/null && echo "deploy triggered"
