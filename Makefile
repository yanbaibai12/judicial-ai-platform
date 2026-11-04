.PHONY: help install init-db migrate seed run worker test coverage lint typecheck format check \
	docker-build up down logs backup restore load-test security-scan

help:
	@echo "install       安装开发依赖"
	@echo "migrate       执行 Alembic 迁移"
	@echo "seed          写入演示数据"
	@echo "run           启动 API（8080）"
	@echo "worker        启动异步任务 Worker"
	@echo "test          全量测试"
	@echo "coverage      生成覆盖率报告"
	@echo "lint/typecheck/check  代码质量"
	@echo "load-test     健康检查压测"
	@echo "security-scan 本地安全扫描（需安装工具）"

install:
	uv sync --dev --extra production

init-db:
	uv run python scripts/init_db.py

migrate:
	uv run python -m alembic upgrade head

seed:
	uv run python -m judicial_ai.seed

run:
	uv run uvicorn judicial_ai.main:app --reload --host 0.0.0.0 --port 8080

worker:
	JOB_MODE=worker uv run python -m judicial_ai.worker

test:
	uv run python -m pytest

coverage:
	uv run python -m pytest --cov=judicial_ai --cov-report=term-missing --cov-report=html

lint:
	uv run ruff check src services tests migrations scripts

typecheck:
	uv run mypy src

format:
	uv run ruff format src services tests migrations scripts

check: lint typecheck test

docker-build:
	docker build -t judicial-ai-platform:0.2.0 .
	docker build -f deploy/backup/Dockerfile -t judicial-ai-backup:0.2.0 .

up:
	docker compose -f deploy/compose/docker-compose.yml up -d --build

down:
	docker compose -f deploy/compose/docker-compose.yml down

logs:
	docker compose -f deploy/compose/docker-compose.yml logs -f app worker

backup:
	bash deploy/scripts/backup.sh

restore:
	bash deploy/scripts/restore.sh

load-test:
	uv run python scripts/loadtest.py --scenario health --concurrency 8 --duration 15

security-scan:
	uvx pip-audit || true
	trivy fs --exit-code 1 --severity HIGH,CRITICAL . || true
	gitleaks detect --no-git --redact || true

