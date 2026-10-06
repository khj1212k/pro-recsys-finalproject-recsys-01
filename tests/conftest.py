import importlib.util
import os
import sys
import uuid
from pathlib import Path

import pytest

# backend/app/database.py는 임포트 시점에 create_engine(DATABASE_URL, ...)을 호출한다.
# SQLAlchemy의 create_engine은 실제 연결을 맺지 않고 URL만 파싱하므로, 이 워크트리에
# 실제 DB가 없어도 문법적으로 유효한 더미 URL만 있으면 backend 모듈 임포트가 가능하다.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")


def _load_spend_ledger_module():
    """원장 모듈을 파일 경로로 직접 올린다(표준 라이브러리만 쓴다).

    `core.llm` 패키지로 임포트하면 __init__이 어댑터와 openai SDK까지 올린다 - LLM을 쓰지 않는
    테스트에 그 비용을 지우지 않으려고 패키지를 거치지 않는다.
    """
    path = Path(__file__).resolve().parents[1] / "ai_workspace" / "core" / "llm" / "spend_ledger.py"
    spec = importlib.util.spec_from_file_location("_conftest_spend_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_spend_ledger = _load_spend_ledger_module()


@pytest.fixture
def llm_ledger_init():
    """원장 파일을 `spend_cli init`과 같은 방식으로 만든다. 가드는 원장을 만들지 않으므로(docs/adr/0035)
    원장 경로를 스스로 바꾸는 테스트는 이 함수로 먼저 만들어야 호출이 허용된다."""

    def init(path):
        _spend_ledger.SpendLedger(path).init(note="pytest")
        return path

    return init


@pytest.fixture(autouse=True)
def _isolated_llm_spend_ledger(monkeypatch, tmp_path_factory):
    """어떤 테스트도 실제 LLM 지출 원장(<메인 체크아웃>/.ops/llm_spend_ledger.jsonl)·그 누계 기록
    (~/.local/state/...)·킬 스위치 파일(<메인 체크아웃>/.ops/LLM_KILL_SWITCH)에 쓰지 않게 한다.

    어댑터를 페이크 전송 계층으로 부르는 테스트도 지출 가드(core/llm/budget.py, docs/adr/0035)를
    거친다. 테스트마다 init된 빈 임시 원장과 임시 상태 디렉터리를 주고, 상한은 닿지 않을 만큼 크게,
    단가표에 없는 테스트용 모델 이름("m" 등)은 명시적 대체 단가로 허용한다. 상한·단가 없는 모델·
    기본값을 시험하는 테스트는 이 값을 스스로 덮어쓴다(tests/test_llm_budget_*.py).

    킬 스위치 파일 경로도 임시 경로로 돌린다: 일·전체 상한에 닿으면 가드가 그 파일을 만든다. 상한을
    낮추는 테스트가 경로 바꾸기를 빠뜨려도 개발자 체크아웃의 실제 킬 스위치가 켜지지 않는다.
    """
    base = tmp_path_factory.getbasetemp()
    token = uuid.uuid4().hex
    ledger_path = base / "llm-spend-ledgers" / f"{token}.jsonl"
    monkeypatch.setenv("LLM_SPEND_STATE_DIR", str(base / "llm-spend-state" / token))
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(ledger_path))
    _spend_ledger.SpendLedger(ledger_path).init(note="pytest")
    for name in ("LLM_BUDGET_RUN_USD", "LLM_BUDGET_DAY_USD", "LLM_BUDGET_TOTAL_USD"):
        monkeypatch.setenv(name, "1000000")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "1")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "1")
    monkeypatch.delenv("LLM_RUN_ID", raising=False)

    kill_switch = str(base / "llm-kill-switches" / token / "LLM_KILL_SWITCH")
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    # 아직 임포트되지 않았으면 환경변수가 Settings의 기본값이 되고, 이미 임포트됐으면 속성을 바꾼다.
    monkeypatch.setenv("LLM_KILL_SWITCH_FILE", kill_switch)
    settings_module = sys.modules.get("config.settings")
    if settings_module is not None:
        monkeypatch.setattr(settings_module.Settings, "LLM_KILL_SWITCH_FILE", kill_switch)

    def reset_run_state():
        # 가드를 임포트한 테스트만 해당된다 - 여기서 임포트하면 LLM을 쓰지 않는 테스트까지 openai를 올린다.
        budget = sys.modules.get("core.llm.budget")
        if budget is not None:
            budget.reset_run_state()

    reset_run_state()  # 앞 테스트가 남긴 중단 래치·연속 실패 수가 새지 않게
    yield
    reset_run_state()
