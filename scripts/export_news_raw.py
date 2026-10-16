#!/usr/bin/env python3
"""news_raw 동결 반출(읽기 전용)·검증·30일 정리. 구현과 설명은 evaluation/llm/frozen_export.py에 있다.

    python scripts/export_news_raw.py plan-window --t0-kst 2026-10-08T14:00 --days 5
    python scripts/export_news_raw.py export --start-utc <UTC> --end-utc <UTC> \
        --psql-command "ssh -F .ops/micro/ssh_config micro 'cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -U newsletter -d newsletter'"
    python scripts/export_news_raw.py verify --dir data/exports/<T0>
    python scripts/export_news_raw.py purge  --dir data/exports/<T0> [--everything]

실행 절차(누가, 언제, 확인, 정리)는 docs/runbook-hosting.md의 반출 절에 있다.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from evaluation.llm.frozen_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
