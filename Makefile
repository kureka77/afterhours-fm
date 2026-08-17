.PHONY: dev prod down logs test test-backend test-frontend security db-shell help

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  dev            Start dev container (hot-reload, foreground)"
	@echo "  prod           Build and start prod stack in background (db + app + nginx)"
	@echo "  down           Stop and remove all containers"
	@echo "  logs           Tail prod and nginx logs"
	@echo "  test           Run backend and frontend tests"
	@echo "  test-backend   Run pytest (Python)"
	@echo "  test-frontend  Run Vitest (JS)"
	@echo "  security       Audit JS dependencies for known vulnerabilities (npm audit)"
	@echo "  db-shell       Open psql in the running prod database"

dev:
	docker compose up dev

prod:
	docker compose up db prod nginx --build -d

down:
	docker compose down

logs:
	docker compose logs -f prod nginx

test: test-backend test-frontend

test-backend:
	./venv/bin/pytest

test-frontend:
	npm test

security:
	npm audit

db-shell:
	docker compose exec db psql -U afterhours_fm -d afterhours_fm
