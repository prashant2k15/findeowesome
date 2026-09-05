.PHONY: help install dev up down logs shell stats check export test deploy backup

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## create venv + install deps (local dev)
	python -m venv .venv && .venv/bin/pip install -r requirements.txt

dev: ## run the API locally with reload (sqlite)
	uvicorn app.api.main:app --reload --port 8000

up: ## build + start the whole stack
	docker compose up -d --build

down: ## stop the stack
	docker compose down

logs: ## tail worker logs
	docker compose logs -f worker

shell: ## shell inside the worker container
	docker compose exec worker bash

stats: ## database + worker health
	docker compose exec worker python -m app.cli stats

check: ## run one checker batch now
	docker compose exec worker python -m app.cli check

export: ## regenerate CSV exports
	docker compose exec worker python -m app.cli export

backup: ## dump the database right now
	docker compose exec -T db pg_dump -U $${POSTGRES_USER:-blf} $${POSTGRES_DB:-blf} | gzip > backups/manual-$$(date +%Y%m%d-%H%M).sql.gz

test: ## run the test suite
	.venv/bin/pytest -q || pytest -q
