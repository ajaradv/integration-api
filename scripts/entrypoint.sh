#!/bin/sh
# Migrations run once, before either process starts. If the service ever runs more
# than one instance, move this step to a release/pre-deploy command.
set -e
alembic upgrade head
export PORT="${PORT:-8000}"
exec honcho start -f Procfile
