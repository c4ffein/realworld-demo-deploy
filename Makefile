.PHONY: run-dummy-for-prod run-dummy-for-hurl run-dummy-for-bruno
.PHONY: test-dummy-server-api-with-hurl-and-already-launched-server test-dummy-server-api-with-hurl
.PHONY: test-dummy-server-api-with-bruno-and-already-launched-server test-dummy-server-api-with-bruno
.PHONY: test-dummy-server-unittest
.PHONY: submodules-fetch
.PHONY: lint lint-check
.PHONY: compare-openapi compare-openapi-json compare-openapi-markdown

PYTHON = uvx --with fastapi --with uvicorn python
PORT ?= 8000
SERVER_STARTUP_WAIT ?= 1

########################
# Help

help:
	@echo "Available commands:"
	@echo "  run-dummy-for-prod"
	@echo "  run-dummy-for-hurl"
	@echo "  run-dummy-for-bruno"
	@echo "  test-dummy-server-api-with-hurl-and-already-launched-server"
	@echo "  test-dummy-server-api-with-hurl"
	@echo "  test-dummy-server-api-with-bruno-and-already-launched-server"
	@echo "  test-dummy-server-api-with-bruno"
	@echo "  test-dummy-server-unittest"
	@echo "  submodules-fetch"
	@echo "  lint"
	@echo "  lint-check"
	@echo "  compare-openapi"
	@echo "  compare-openapi-json"
	@echo "  compare-openapi-markdown"

########################
# Run

run-dummy-for-prod:
	PATH_PREFIX=/api $(PYTHON) realworld_dummy_server.py $(PORT)

run-dummy-for-hurl:
	PATH_PREFIX=/api DISABLE_ISOLATION_MODE=True $(PYTHON) realworld_dummy_server.py $(PORT)

run-dummy-for-bruno:
	PATH_PREFIX=/api DISABLE_ISOLATION_MODE=True $(PYTHON) realworld_dummy_server.py $(PORT)

########################
# Tests

test-dummy-server-api-with-hurl-and-already-launched-server:
	( \
	  [ -f "./realworld/api/hurl/run-hurl-tests.sh" ] || \
	  ( echo '\n\033[0;31m    ENSURE SUBMODULES ARE PRESENT: \033[0m`make submodules-fetch`\n' && exit 1 ) \
	) && \
	( \
	  HOST=http://localhost:$(PORT) ./realworld/api/run-api-tests-hurl.sh || \
	  ( echo '\n\033[0;31m    ENSURE DEMO SERVER IS RUNNING: \033[0m`make run-dummy-for-hurl`\n' && exit 1 ) \
	)

test-dummy-server-api-with-hurl:
	@set -e; \
	$(PYTHON) -c "print('deps ready')"; \
	PATH_PREFIX=/api DISABLE_ISOLATION_MODE=True \
	MAX_USERS_PER_SESSION=100 MAX_ARTICLES_PER_SESSION=100 MAX_COMMENTS_PER_SESSION=100 \
	$(PYTHON) realworld_dummy_server.py $(PORT) & \
	SERVER_PID=$$!; \
	trap "kill $$SERVER_PID 2>/dev/null || true" EXIT; \
	while ! curl -s http://localhost:$(PORT)/api/tags > /dev/null 2>&1; do sleep 0.2; done; \
	make test-dummy-server-api-with-hurl-and-already-launched-server; \
	kill $$SERVER_PID 2>/dev/null || true

test-dummy-server-api-with-bruno-and-already-launched-server:
	( \
	  [ -d "./realworld/api/bruno" ] || \
	  ( echo '\n\033[0;31m    ENSURE SUBMODULES ARE PRESENT: \033[0m`make submodules-fetch`\n' && exit 1 ) \
	) && \
	( \
	  mkdir -p .tmp/bin && printf '#!/bin/sh\ncd $(CURDIR)/realworld/api/bruno && exec bun x @usebruno/cli "$$@"\n' > .tmp/bin/bru && chmod +x .tmp/bin/bru && \
	  PATH="$(CURDIR)/.tmp/bin:$$PATH" HOST=http://localhost:$(PORT) ./realworld/api/run-api-tests-bruno.sh || \
	  ( echo '\n\033[0;31m    ENSURE DEMO SERVER IS RUNNING: \033[0m`make run-dummy-for-bruno`\n' && exit 1 ) \
	)

test-dummy-server-api-with-bruno:
	@set -e; \
	$(PYTHON) -c "print('deps ready')"; \
	PATH_PREFIX=/api DISABLE_ISOLATION_MODE=True \
	MAX_USERS_PER_SESSION=100 MAX_ARTICLES_PER_SESSION=100 MAX_COMMENTS_PER_SESSION=100 \
	$(PYTHON) realworld_dummy_server.py $(PORT) & \
	SERVER_PID=$$!; \
	trap "kill $$SERVER_PID 2>/dev/null || true" EXIT; \
	while ! curl -s http://localhost:$(PORT)/api/tags > /dev/null 2>&1; do sleep 0.2; done; \
	make test-dummy-server-api-with-bruno-and-already-launched-server; \
	kill $$SERVER_PID 2>/dev/null || true

test-dummy-server-unittest:
	uvx --with fastapi --with uvicorn --with pytest python -m pytest realworld_dummy_server.py

########################
# Submodules

submodules-fetch:
	git submodule update --init

########################
# Lint

lint:
	uvx ruff check --fix; uvx ruff format --line-length 120

lint-check:
	uvx ruff check && uvx ruff format --line-length 120 --check

########################
# OpenAPI Comparison

compare-openapi:
	uvx --with fastapi --with uvicorn --with pyyaml --with httpx python compare_openapi.py

compare-openapi-json:
	uvx --with fastapi --with uvicorn --with pyyaml --with httpx python compare_openapi.py --format json

compare-openapi-markdown:
	uvx --with fastapi --with uvicorn --with pyyaml --with httpx python compare_openapi.py --format markdown
