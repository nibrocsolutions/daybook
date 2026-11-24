.PHONY: test dev up

test:
	python3 -m pytest

dev:
	ENV=development SEED_DEMO=true python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000

up:
	docker compose up --build
