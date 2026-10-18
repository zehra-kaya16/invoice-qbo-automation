#!/bin/sh
set -eu
# Migrations have one explicit Compose service; workers never race Alembic.
python manage_deployment.py validate-runtime
exec "$@"
