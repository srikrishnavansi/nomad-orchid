PY := .venv/bin/python

.PHONY: up seed test demo ui reset venv

venv:
	test -x $(PY) || python3 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt

up:
	docker compose up -d
	until curl -sf -o /dev/null http://127.0.0.1:8069/web/login; do sleep 3; done

seed: up venv
	test -f ground_truth.json || $(PY) seed.py

test: seed
	$(PY) test_orchid.py

demo: seed
	$(PY) agent.py --user aoife "Which orders this month were priced off the list price or off the agreed rebate?"
	$(PY) agent.py --user niamh "Which orders this month were priced off the list price or off the agreed rebate?"

ui: seed
	$(PY) server.py

reset:
	docker compose down -v
	rm -f ground_truth.json
