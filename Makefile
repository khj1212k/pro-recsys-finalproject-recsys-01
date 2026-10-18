# 저장소 루트에서 쓰는 명령 모음. 인자 없이 `make`를 치면 목록이 나온다.
# CI(.github/workflows/ci.yml)와 같은 명령을 둔다. 예외는 report-charts·report-charts-check 하나다 - 리포트 JSON에서
# docs/assets/*.svg를 다시 만드는 명령이고, CI에서는 tests/test_portfolio_charts.py가 같은 대조를 한다.
# 파이썬 환경은 `make venv`가 만드는 .venv다 (다른 환경을 쓰려면 PY=경로를 준다).

PY ?= .venv/bin/python
PYTEST ?= $(PY) -m pytest -q
# 테스트 범위를 좁히려면 TESTS=tests/경로 (기본: 전체)
TESTS ?= tests/

.DEFAULT_GOAL := help
.PHONY: help venv ci-lock test itest lint report-charts report-charts-check

help: ## 이 목록
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

venv: ## .venv를 만들고 잠금 파일(requirements-ci.txt)로 설치한다 - CI와 같은 버전
	uv venv --python 3.11 .venv
	uv pip install --python $(PY) -r requirements-ci.txt
	uv pip install --python $(PY) --no-deps -e ai_workspace -e ai_workspace/recommend_engine

# 기존 잠금 파일의 버전은 유지된다(uv가 -o 파일을 선호 버전으로 읽는다). 전부 올리려면 `make ci-lock UPGRADE=1`.
ci-lock: ## requirements-ci.in에서 requirements-ci.txt를 다시 만든다 (uv 필요)
	uv pip compile requirements-ci.in --universal --python-version 3.11 $(if $(UPGRADE),--upgrade) -o requirements-ci.txt

test: ## 단위 테스트 - DB 없이 도는 것만, CI의 test 잡과 같다
	$(PYTEST) -m "not integration and not benchmark" $(TESTS)

itest: ## 통합 테스트 - 전용 테스트 DB의 TEST_DATABASE_URL이 필요하다 (docs/runbook.md 13절)
	@test -n "$$TEST_DATABASE_URL" || { echo "TEST_DATABASE_URL이 없다. 운영 DB 말고 일회용 테스트 DB를 줘야 한다 (docs/runbook.md 13절)"; exit 2; }
	DATABASE_URL="$$TEST_DATABASE_URL" $(PYTEST) -m integration $(TESTS)

lint: ## ruff - 돌지 못하는 코드만 잡는 보수적 규칙 (pyproject.toml), uv 필요
	uvx --from "ruff==0.16.9" ruff check .

report-charts: ## 커밋된 리포트 JSON에서 docs/assets/*.svg를 다시 쓴다 (표준 라이브러리만, 설치할 것 없음)
	$(PY) scripts/portfolio_charts.py

report-charts-check: ## 쓰지 않고, 커밋된 SVG가 리포트 JSON에서 만든 것과 다르면 실패한다
	$(PY) scripts/portfolio_charts.py --check
