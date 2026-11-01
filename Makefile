# News Intelligence Platform — developer entrypoints.
# `make help` lists targets.

SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f infra/compose/docker-compose.yml
CORE := minio sftpgo nifi postgres rabbitmq
# One-shot, idempotent bootstrap jobs. Run with `compose run` because `up --wait`
# reports exited containers as failures even when they exit 0.
INIT := minio-init sftpgo-init

.PHONY: help env up up-core up-all up-app build down down-volumes ps logs smoke-infra nifi-deploy nifi-redeploy nifi-relist fixtures test test-integration

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
	$(COMPOSE) --profile auth --profile app up -d --wait

build: ## Build the application image (API, worker, MCP, inference gateway)
	docker build -t newsintel-app:$${APP_IMAGE_TAG:-dev} services

up-app: up-core ## Start core + API + worker (+ fake inference if FAKE=1)
	$(COMPOSE) --profile app $(if $(FAKE),--profile fake-inference) up -d --wait api worker $(if $(FAKE),inference-fake)

PY := services/app/.venv/bin/python
nifi-deploy: ## Deploy the NiFi ingestion flow (no-op if already deployed)
	$(PY) -W ignore infra/nifi/deploy_flow.py

nifi-redeploy: ## Recreate the NiFi flow (drops FlowFiles queued inside NiFi)
	$(PY) -W ignore infra/nifi/deploy_flow.py --replace

nifi-relist: ## Re-ingest anything still sitting in landing/uploads/
	$(PY) -W ignore infra/nifi/deploy_flow.py --relist

dev-users: ## DEV ONLY: create Keycloak test users analyst1 / viewer2 / admin1 (password DEV_USER_PASSWORD)
	docker exec -e KEYCLOAK_ADMIN_USER=$$(grep ^KEYCLOAK_ADMIN_USER= .env|cut -d= -f2) \
	  -e KEYCLOAK_ADMIN_PASSWORD=$$(grep ^KEYCLOAK_ADMIN_PASSWORD= .env|cut -d= -f2) \
	  -e DEV_USER_PASSWORD=$$(grep ^DEV_USER_PASSWORD= .env|cut -d= -f2) \
	  -i newsintel-keycloak-1 bash < infra/keycloak/create-dev-users.sh

fixtures: ## Generate deterministic test videos into tests/fixtures/
	bash scripts/make-test-video.sh

test: ## Unit tests (no services needed)
	cd services/app && .venv/bin/pytest -q

test-integration: ## Integration tests against the running stack
	cd services/app && .venv/bin/pytest -m integration -q

up: up-core ## Alias for up-core

down: ## Stop containers (data volumes are kept)
	$(COMPOSE) --profile auth --profile app --profile fake-inference down

down-volumes: ## Stop and DELETE all data volumes (destructive)
	@read -p "Delete ALL platform data volumes? Type 'yes': " a && [ "$$a" = yes ]
	$(COMPOSE) --profile auth --profile app down -v

ps: ## Show service status and health
	$(COMPOSE) --profile auth --profile app ps

logs: ## Tail logs (S=service to filter, e.g. make logs S=nifi)
	$(COMPOSE) --profile auth --profile app logs -f --tail=100 $(S)

smoke-infra: .env ## Verify buckets, policies, SFTP upload path, databases
	cd services/app && .venv/bin/pytest -m integration -q tests/integration/test_infra.py
