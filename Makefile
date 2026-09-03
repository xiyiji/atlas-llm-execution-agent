.PHONY: install test lint run migrate worker compose-up compose-down

install:
	python3 -m pip install -r requirements-dev.txt

test:
	pytest

lint:
	ruff check app tests

run:
	./run.sh

migrate:
	alembic upgrade head

worker:
	celery -A app.task_queue:celery_app worker --beat --loglevel=INFO

compose-up:
	docker compose up --build

compose-down:
	docker compose down
