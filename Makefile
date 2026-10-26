.PHONY: help run agent cli test coverage test-ui coverage-ui e2e test-all deps deps-dev frontend dev clean build push

# The project virtualenv when present, else the system interpreter.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

# Immagini pubblicate su GitHub Container Registry. Le release ufficiali le
# builda e pubblica il workflow .github/workflows/release.yml (multi-arch);
# `make push` serve solo per push manuali (richiede `docker login ghcr.io`).
IMAGE_TAG    ?= latest
# Versione stampata nelle immagini (label org.opencontainers.image.version).
VERSION      ?= dev
SERVER_IMAGE ?= ghcr.io/kowan-lab/kowan
AGENT_IMAGE  ?= ghcr.io/kowan-lab/kowan-agent


# Il target di default mostra l'aiuto.
help:
	@echo "Kowan — Makefile"
	@echo ""
	@echo "  make deps       installa le dipendenze Python (requirements.txt)"
	@echo "  make frontend   builda la SPA React in web/dist (servita dal server)"
	@echo "  make run        avvia il control-plane su http://localhost:5081 (admin/admin)"
	@echo "  make agent      avvia l'agent (KOWAN_AGENT_MODE=agent|edge, KOWAN_AGENT_TOKEN=...)"
	@echo "  make cli ARGS=\"hosts list\"   esegue la CLI kowanctl"
	@echo "  make test       esegue i test backend (unittest)"
	@echo "  make coverage   test backend con coverage (soglia in .coveragerc)"
	@echo "  make test-ui    test frontend (vitest + Testing Library)"
	@echo "  make coverage-ui test frontend con coverage (soglie in vitest.config.ts)"
	@echo "  make e2e        builda la SPA e lancia i test Playwright su un server reale"
	@echo "  make test-all   backend + frontend + e2e"
	@echo "  make dev        SPA con hot-reload su :5173 (proxy -> :5081)"
	@echo "  make build      builda le immagini server + agent ($(SERVER_IMAGE), $(AGENT_IMAGE))"
	@echo "  make push       build + push di server e agent su ghcr.io"
	@echo "  make clean      rimuove __pycache__ e artefatti di build"

## Avvia il control-plane. Docker locale via /var/run/docker.sock.
run:
	$(PY) -m kowan.server

## Avvia l'agent su questo host (mode/token da env KOWAN_AGENT_*).
agent:
	$(PY) -m kowan.agent

## CLI kowanctl. Uso: make cli ARGS="login -u admin"
cli:
	$(PY) -m kowan.cli $(ARGS)

## Esegue la suite di test (mux/tunnel + REST end-to-end + docker-over-tunnel).
test:
	$(PY) -m unittest discover -s tests -t .

## Test backend con coverage: report in coverage.json + htmlcov/, fallisce
## sotto la soglia di .coveragerc (fail_under).
coverage:
	$(PY) -m coverage run -m unittest discover -s tests -t .
	$(PY) -m coverage json -o coverage.json -q
	$(PY) -m coverage html -q
	@echo "backend coverage: $$($(PY) -m coverage report --format=total)%"
	$(PY) -m coverage report --format=total >/dev/null

## Test frontend: componenti e pagine in jsdom contro un backend finto.
test-ui:
	cd web/frontend && npm run typecheck:test && npm test

## Test frontend con coverage (soglie in web/frontend/vitest.config.ts).
coverage-ui:
	cd web/frontend && npm run coverage

## End-to-end: SPA buildata + server reale su un data dir temporaneo.
## KOWAN_E2E_DOCKER=1 abilita anche il deploy di un container usa-e-getta.
e2e: frontend
	cd web/frontend && npx playwright test

## Tutto: backend con coverage, frontend con coverage, end-to-end.
test-all: coverage coverage-ui e2e

## Installa le dipendenze runtime Python.
deps:
	$(PY) -m pip install -r requirements.txt

## Dipendenze di sviluppo (coverage) e browser per Playwright.
deps-dev:
	$(PY) -m pip install -r requirements-dev.txt
	cd web/frontend && npm install && npx playwright install chromium

## Builda la SPA React servita dal server (output in web/dist).
frontend:
	cd web/frontend && npm install && npm run build

## SPA con hot-reload (proxy delle chiamate /api verso il server su :5081).
dev:
	cd web/frontend && npm run dev

## Builda le immagini locali server + agent. Tutte le dipendenze sono pubbliche.
build:
	docker build --platform linux/amd64 --build-arg VERSION=$(VERSION) -t $(SERVER_IMAGE):$(IMAGE_TAG) -f Dockerfile .
	docker build --platform linux/amd64 --build-arg VERSION=$(VERSION) -t $(AGENT_IMAGE):$(IMAGE_TAG) -f Dockerfile.agent .

## Build e push manuale su ghcr.io (prima: docker login ghcr.io).
push: build
	docker push $(SERVER_IMAGE):$(IMAGE_TAG)
	docker push $(AGENT_IMAGE):$(IMAGE_TAG)

## Pulisce gli artefatti. NON tocca ./data (contiene secret.key + il DB SQLite).
clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	rm -rf .coverage coverage.json htmlcov web/frontend/coverage web/frontend/test-results web/frontend/playwright-report
