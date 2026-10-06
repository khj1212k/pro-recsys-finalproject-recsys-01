import os
import sys
import uuid

import pytest

# backend/app/database.py는 임포트 시점에 create_engine(DATABASE_URL, ...)을 호출한다.
# SQLAlchemy의 create_engine은 실제 연결을 맺지 않고 URL만 파싱하므로, 이 워크트리에
# 실제 DB가 없어도 문법적으로 유효한 더미 URL만 있으면 backend 모듈 임포트가 가능하다.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")


@pytest.fixture(autouse=True)
def _isolated_llm_spend_ledger(monkeypatch, tmp_path_factory):
    """어떤 테스트도 실제 LLM 지출 원장(<메인 체크아웃>/.ops/llm_spend_ledger.jsonl)에 쓰지 않게 한다.

    어댑터를 페이크 전송 계층으로 부르는 테스트도 지출 가드(core/llm/budget.py, docs/adr/0035)를
    거친다. 테스트마다 빈 임시 원장을 주고, 상한은 닿지 않을 만큼 크게, 단가표에 없는 테스트용
    모델 이름("m" 등)은 명시적 대체 단가로 허용한다. 상한·단가 없는 모델·기본값을 시험하는
    테스트는 이 값을 스스로 덮어쓴다(tests/test_llm_budget_*.py).
    """
    ledger_dir = tmp_path_factory.getbasetemp() / "llm-spend-ledgers"
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(ledger_dir / f"{uuid.uuid4().hex}.jsonl"))
    for name in ("LLM_BUDGET_RUN_USD", "LLM_BUDGET_DAY_USD", "LLM_BUDGET_TOTAL_USD"):
        monkeypatch.setenv(name, "1000000")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "1")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "1")
    monkeypatch.delenv("LLM_RUN_ID", raising=False)

    def reset_run_state():
        # 가드를 임포트한 테스트만 해당된다 - 여기서 임포트하면 LLM을 쓰지 않는 테스트까지 openai를 올린다.
        budget = sys.modules.get("core.llm.budget")
        if budget is not None:
            budget.reset_run_state()

    reset_run_state()  # 앞 테스트가 남긴 중단 래치·연속 실패 수가 새지 않게
    yield
    reset_run_state()
