#!/usr/bin/env bash
# Runs once, on first start of an empty data volume (docker-entrypoint-initdb.d semantics).
# One database + owner role per consumer so credentials and blast radius stay separate.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE ROLE pixeltable LOGIN PASSWORD '${PIXELTABLE_DB_PASSWORD}';
CREATE DATABASE pixeltable OWNER pixeltable;
CREATE ROLE keycloak LOGIN PASSWORD '${KEYCLOAK_DB_PASSWORD}';
CREATE DATABASE keycloak OWNER keycloak;
CREATE ROLE newsintel LOGIN PASSWORD '${APP_DB_PASSWORD}';
CREATE DATABASE newsintel OWNER newsintel;
SQL

# pgvector must be created by a superuser; Pixeltable's embedding indexes depend on it.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" -d pixeltable -c "CREATE EXTENSION IF NOT EXISTS vector;"
