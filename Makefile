########################
# Dummy

run-dummy-for-prod:
	python realworld_dummy_server.py

run-dummy-for-postman-test:
	BYPASS_ORIGIN_CHECK=True DISABLE_ISOLATION_MODE=True python realworld_dummy_server.py

test-dummy-server-api-with-postman:
	( \
	  [ -f "./realworld/api/run-api-tests.sh" ] || \
	  ( echo '\n\033[0;31m    ENSURE SUBMODULES ARE PRESENT: \033[0m`make submodules-fetch`\n' && exit 1 ) \
	) && \
	( \
	  DELAY_REQUEST=3 APIURL=http://localhost:8000 ./realworld/api/run-api-tests.sh || \
	  ( echo '\n\033[0;31m    ENSURE DEMO SERVER IS RUNNING: \033[0m`make run-dummy-for-postman-test`\n' && exit 1 ) \
	)

test-dummy-server-unittest:
	BYPASS_ORIGIN_CHECK=True python -m unittest realworld_dummy_server.py

########################
# Submodules

submodules-fetch:
	git submodule update --init
