.PHONY: help run agent cli test coverage test-ui coverage-ui e2e test-all deps deps-dev frontend dev clean build push

# The project virtualenv when present, else the system interpreter.
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)

# Images published to GitHub Container Registry. Official releases are built
# and published by .github/workflows/release.yml (multi-arch); `make push` is
# only for manual pushes (it needs `docker login ghcr.io`).
IMAGE_TAG    ?= latest
# Version stamped on the images (label org.opencontainers.image.version).
VERSION      ?= dev
SERVER_IMAGE ?= ghcr.io/kowan-lab/kowan
AGENT_IMAGE  ?= ghcr.io/kowan-lab/kowan-agent


# The default target prints the help.
help:
	@echo "Kowan — Makefile"
	@echo ""
	@echo "  make deps       install the Python dependencies (requirements.txt)"
	@echo "  make frontend   build the React SPA into web/dist (served by the server)"
	@echo "  make run        start the control plane on http://localhost:5081 (setup token in the log)"
	@echo "  make agent      start the agent (KOWAN_AGENT_MODE=agent|edge, KOWAN_AGENT_TOKEN=...)"
	@echo "  make cli ARGS=\"hosts list\"   run the kowanctl CLI"
	@echo "  make test       run the backend tests (unittest)"
	@echo "  make coverage   backend tests with coverage (threshold in .coveragerc)"
	@echo "  make test-ui    frontend tests (vitest + Testing Library)"
	@echo "  make coverage-ui frontend tests with coverage (thresholds in vitest.config.ts)"
	@echo "  make e2e        build the SPA and run the Playwright tests on a real server"
	@echo "  make test-all   backend + frontend + e2e"
	@echo "  make dev        SPA with hot reload on :5173 (proxy -> :5081)"
	@echo "  make build      build the server + agent images ($(SERVER_IMAGE), $(AGENT_IMAGE))"
	@echo "  make push       build + push the server and agent to ghcr.io"
	@echo "  make clean      remove __pycache__ and build artifacts"

## Start the control plane. Local Docker through /var/run/docker.sock.
run:
	$(PY) -m kowan.server

## Start the agent on this host (mode/token from KOWAN_AGENT_* env).
agent:
	$(PY) -m kowan.agent

## The kowanctl CLI. Usage: make cli ARGS="login -u admin"
cli:
	$(PY) -m kowan.cli $(ARGS)

## Run the test suite (mux/tunnel + REST end to end + docker-over-tunnel).
test:
	$(PY) -m unittest discover -s tests -t .

## Backend tests with coverage: reports in coverage.json + htmlcov/; fails
## below the .coveragerc threshold (fail_under).
coverage:
	$(PY) -m coverage run -m unittest discover -s tests -t .
	$(PY) -m coverage json -o coverage.json -q
	$(PY) -m coverage html -q
	@echo "backend coverage: $$($(PY) -m coverage report --format=total)%"
	$(PY) -m coverage report --format=total >/dev/null

## Frontend tests: components and pages in jsdom against a fake backend.
test-ui:
	cd web/frontend && npm run typecheck:test && npm test

## Frontend tests with coverage (thresholds in web/frontend/vitest.config.ts).
coverage-ui:
	cd web/frontend && npm run coverage

## End to end: the built SPA + a real server on a temporary data dir.
## KOWAN_E2E_DOCKER=1 also deploys a throwaway container.
e2e: frontend
	cd web/frontend && npx playwright test

## Everything: backend with coverage, frontend with coverage, end to end.
test-all: coverage coverage-ui e2e

## Install the Python runtime dependencies.
deps:
	$(PY) -m pip install -r requirements.txt

## Development dependencies (coverage) and Playwright's browser.
deps-dev:
	$(PY) -m pip install -r requirements-dev.txt
	cd web/frontend && npm install && npx playwright install chromium

## Build the React SPA the server serves (output in web/dist).
frontend:
	cd web/frontend && npm install && npm run build

## SPA with hot reload (proxies /api calls to the server on :5081).
dev:
	cd web/frontend && npm run dev

## Build local server + agent images. Every dependency is public.
build:
	docker build --platform linux/amd64 --build-arg VERSION=$(VERSION) -t $(SERVER_IMAGE):$(IMAGE_TAG) -f Dockerfile .
	docker build --platform linux/amd64 --build-arg VERSION=$(VERSION) -t $(AGENT_IMAGE):$(IMAGE_TAG) -f Dockerfile.agent .

## Manual build and push to ghcr.io (first: docker login ghcr.io).
push: build
	docker push $(SERVER_IMAGE):$(IMAGE_TAG)
	docker push $(AGENT_IMAGE):$(IMAGE_TAG)

## Remove artifacts. Does NOT touch ./data (it holds secret.key + the SQLite DB).
clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	rm -rf .coverage coverage.json htmlcov web/frontend/coverage web/frontend/test-results web/frontend/playwright-report
