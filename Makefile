.PHONY: run-dummy-for-prod run-dummy-for-postman
.PHONY: test-dummy-server-api-with-postman-and-already-launched-server test-dummy-server-api-with-postman
.PHONY: test-dummy-server-unittest
.PHONY: submodules-fetch
.PHONY: lint lint-check
.PHONY: patch-check patch-apply

########################
# Help

help:
	@echo "Available commands:"
	@echo "  run-dummy-for-prod"
	@echo "  run-dummy-for-postman"
	@echo "  test-dummy-server-api-with-postman-and-already-launched-server"
	@echo "  test-dummy-server-api-with-postman"
	@echo "  test-dummy-server-unittest"
	@echo "  submodules-fetch"
	@echo "  lint"
	@echo "  lint-check"
	@echo "  patch-check"
	@echo "  patch-apply"

########################
# Run

run-dummy-for-prod:
	PATH_PREFIX=api python realworld_dummy_server.py

run-dummy-for-postman:
	PATH_PREFIX=api DISABLE_ISOLATION_MODE=True python realworld_dummy_server.py

########################
# Tests

test-dummy-server-api-with-postman-and-already-launched-server:
	( \
	  [ -f "./realworld/api/run-api-tests.sh" ] || \
	  ( echo '\n\033[0;31m    ENSURE SUBMODULES ARE PRESENT: \033[0m`make submodules-fetch`\n' && exit 1 ) \
	) && \
	( \
	  DELAY_REQUEST=3 APIURL=http://localhost:8000/api ./realworld/api/run-api-tests.sh || \
	  ( echo '\n\033[0;31m    ENSURE DEMO SERVER IS RUNNING: \033[0m`make run-dummy-for-postman-test`\n' && exit 1 ) \
	)

test-dummy-server-api-with-postman:
	@set -e; \
	PATH_PREFIX=api DISABLE_ISOLATION_MODE=True python realworld_dummy_server.py & \
	SERVER_PID=$$!; \
	trap "kill $$SERVER_PID 2>/dev/null || true" EXIT; \
	sleep 0.4; \
	kill -0 "$$SERVER_PID" 2>/dev/null || exit 4; \
	make test-dummy-server-api-with-postman-and-already-launched-server; \
	kill $$SERVER_PID 2>/dev/null || true

test-dummy-server-unittest:
	python -m unittest realworld_dummy_server.py

########################
# Submodules

submodules-fetch:
	git submodule update --init

########################
# Lint

lint:
	uvx ruff check --fix --exclude waitress; uvx ruff format --line-length 120 --exclude waitress

lint-check:
	uvx ruff check --exclude waitress && uvx ruff format --line-length 120 --check --exclude waitress

########################
# Patch

patch-check:
	git -C angular-realworld-example-app apply --check ../patch-frontend-api-url-and-cookies.patch

patch-apply:
	git -C angular-realworld-example-app apply ../patch-frontend-api-url-and-cookies.patch
