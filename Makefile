# News Intelligence Platform — developer entrypoints.
# `make help` lists targets.

SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f infra/compose/docker-compose.yml
CORE := minio sftpgo nifi postgres rabbitmq
# One-shot, idempotent bootstrap jobs. Run with `compose run` because `up --wait`
# reports exited containers as failures even when they exit 0.
INIT := minio-init sftpgo-init

.PHONY: help env up up-core up-all down down-volumes ps logs smoke-infra

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

env: ## Generate .env with random secrets (refuses to overwrite)
	@bash scripts/gen-dev-env.sh

.env:
	@echo "No .env — run 'make env' first." >&2; exit 1

up-core: .env ## Start ingestion + storage + queue + database (no auth/app)
	$(COMPOSE) up -d --wait $(CORE)
	@for j in $(INIT); do $(COMPOSE) run --rm --no-deps $$j || exit 1; done

up-all: up-core ## Start everything including Keycloak and the application
	$(COMPOSE) --profile auth --profile app up -d --wait keycloak

up: up-core ## Alias for up-core

down: ## Stop containers (data volumes are kept)
	$(COMPOSE) --profile auth --profile app down

down-volumes: ## Stop and DELETE all data volumes (destructive)
	@read -p "Delete ALL platform data volumes? Type 'yes': " a && [ "$$a" = yes ]
	$(COMPOSE) --profile auth --profile app down -v

ps: ## Show service status and health
	$(COMPOSE) --profile auth --profile app ps

logs: ## Tail logs (S=service to filter, e.g. make logs S=nifi)
	$(COMPOSE) --profile auth --profile app logs -f --tail=100 $(S)

smoke-infra: .env ## Verify buckets, policies, SFTP upload path
	@bash scripts/smoke-infra.sh
