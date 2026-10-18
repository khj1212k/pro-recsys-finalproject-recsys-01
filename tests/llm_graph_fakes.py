# 실제 그래프 + 실제 role 클라이언트(어댑터·지출 가드) + 페이크 전송 계층 테스트용 공용 도구.
# pytest는 test_*.py만 수집하므로 이 파일은 테스트로 수집되지 않는다.
#
# 쓰는 곳: tests/test_llm_failure_injection.py, tests/test_save_links_only_used_articles.py,
# tests/test_tone_drift_covers_stored_fields.py (docs/adr/0010 Addendum 2026-10-18).
# 아래 기사·초안 문장은 전부 이 테스트를 위해 지어낸 것이다(실제 기사 텍스트가 아니다 - ADR 0023).
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response  # noqa: E402

import core.llm.adapters as adapters_module  # noqa: E402
import core.llm.registry as registry  # noqa: E402
import core.reconstruction.generator as generator_module  # noqa: E402
import core.tone_converter as tone_module  # noqa: E402
import workflow.evaluators as evaluators_module  # noqa: E402
import workflow.nodes as nodes_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.llm import budget  # noqa: E402
from core.llm.client import LLMResult, LLMUsage  # noqa: E402
from core.llm.schemas import (  # noqa: E402
    ClusterEval, CriterionScores, NewsletterContent, NewsletterEvalV2, NewsletterMeta, ToneResult,
)
from core.llm_metrics import LLMMetricsCollector  # noqa: E402

IDS = [10, 11, 12]
TITLES = ["삼성전자 HBM 증설", "SK하이닉스 투자 확대", "메모리 업황 개선"]
BODIES = [
    "삼성전자는 평택 공장에 HBM 라인을 증설한다고 밝혔다. 투자 규모는 2조 원이다.",
    "SK하이닉스도 청주 공장 투자를 늘린다. 회사는 투자 규모를 1조5000억 원으로 잡았다.",
    "메모리 업황이 개선되면서 D램 가격이 10% 올랐다.",
]
CLEAN = (
    "삼성전자와 SK하이닉스가 HBM 투자를 늘리고 있다. 삼성전자는 평택 공장에 2조 원을 투입하고, "
    "SK하이닉스는 1조5000억 원을 투자한다. 메모리 업황 개선으로 D램 가격은 10% 올랐다."
)
# 세 번째 기사(D램 가격 10%)를 뺀 클러스터용 본문: 원문에 없는 수치가 들어 있으면 사실성 게이트가 막는다
CLEAN_WITHOUT_DRAM = (
    "삼성전자와 SK하이닉스가 HBM 투자를 늘리고 있다. 삼성전자는 평택 공장에 2조 원을 투입하고, "
    "SK하이닉스는 1조5000억 원을 투자한다."
)
CASUAL_WITHOUT_DRAM = "📰 삼성전자와 SK하이닉스가 HBM 투자를 늘려요. 삼성전자는 2조 원, SK하이닉스는 1조5000억 원이에요."
SENTENCE = "삼성전자는 2조 원을 투자한다"
CASUAL_BODY = "📰 삼성전자와 SK하이닉스가 HBM 투자를 늘려요. 삼성전자는 2조 원, SK하이닉스는 1조5000억 원이에요. D램 가격은 10% 올랐어요."
CASUAL_SENTENCE = "삼성전자가 2조 원을 투자해요 ✅"


def initial_state(ids=None, titles=None, bodies=None, cluster_id=1):
    ids = list(ids or IDS)
    return {
        "run_id": 7,
        "all_cluster_groups": {cluster_id: list(ids)},
        "all_cluster_ids": [cluster_id],
        "current_cluster_index": 0,
        "data": {
            "ids": list(ids),
            "titles": list(titles or TITLES),
            "contents": list(bodies or BODIES),
            "press_names": ["동아일보", "경향신문", "한국경제", "매일경제", "조선일보", "한겨레"][: len(ids)],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }


# ---------------------------------------------------------------- 환경

def configure_env(monkeypatch, tmp_path, llm_ledger_init):
    """role 환경변수를 비우고 임시 원장·킬 스위치 파일·무대기 재시도로 맞춘다(autouse 픽스처에서 부른다)."""
    for name in ("GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL",
                 "GEN_REASONING_EFFORT", "JUDGE_REASONING_EFFORT", "TONE_REASONING_EFFORT",
                 "LLM_RUN_ID", "LLM_CIRCUIT_BREAKER_THRESHOLD", "LLM_KILL_SWITCH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", llm_ledger_init(str(tmp_path / "ledger.jsonl")))
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(nodes_module.Settings, "FAITHFULNESS_GATE_MODE", "enforce")
    monkeypatch.setattr(nodes_module.Settings, "TONE_DRIFT_GATE_MODE", "enforce")
    monkeypatch.setattr(nodes_module.Settings, "JUDGE_GATE_MODE", "shadow")
    registry.reset_registry()
    budget.reset_run_state()
    LLMMetricsCollector().reset()


def teardown_env():
    registry.reset_registry()
    budget.reset_run_state()


def no_db(monkeypatch):
    """DB 저장을 페이크로 바꾸고, 저장 호출 기록(article_ids, newsletter)을 돌려준다."""
    saved = []

    class _Conn:
        def cursor(self):
            raise AssertionError("임베딩이 없으므로 cursor()는 불리면 안 된다")

        def commit(self):
            pass

    def save(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved.append({"article_ids": list(article_ids), "newsletter": dict(newsletter)})
        return 900 + len(saved)

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _Conn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", save)
    return saved


def role_transports(gen_responses, judge_responses, on_gen_call=None, on_judge_call=None):
    """레지스트리의 실제 role 클라이언트에 페이크 전송 계층만 끼운다(generator와 tone은 같은 인스턴스).

    그래프의 호출 순서: judge(클러스터 평가) -> generator(본문) -> generator(메타) -> judge(뉴스레터 평가)
    -> tone(문체 변환). 큐가 빈 뒤의 요청은 AssertionError가 되어 어댑터의 일반 오류 분기로 들어가므로,
    "추가 호출 0"은 call_count로 확인한다.
    """
    gen_fake = FakeOpenAIClient(responses=gen_responses, on_call=on_gen_call)
    judge_fake = FakeOpenAIClient(responses=judge_responses, on_call=on_judge_call)
    registry.get_client("generator")._client = gen_fake
    registry.get_client("judge")._client = judge_fake
    assert registry.get_client("tone")._client is gen_fake
    return gen_fake, judge_fake


# ---------------------------------------------------------------- 응답 조립

def cluster_pass():
    return make_response("{}", parsed=ClusterEval(decision="PASS", confidence=0.9, summary="HBM 투자"))


def cluster_fail(outliers=(), sub_groups=()):
    return make_response("{}", parsed=ClusterEval(
        decision="FAIL", confidence=0.4, feedback="다른 사건",
        outlier_indices=list(outliers), sub_groups=[list(g) for g in sub_groups]))


def judge_pass():
    return make_response("{}", parsed=NewsletterEvalV2(
        scores=CriterionScores(faithfulness=5, coverage=4, coherence=4, style=4), unsupported_claims=[], feedback=""))


def content(text=CLEAN):
    return make_response("{}", parsed=NewsletterContent(content=text))


def meta(sentence=SENTENCE):
    return make_response("{}", parsed=NewsletterMeta(
        title="HBM 투자 확대", sentence=sentence,
        keywords=["삼성전자", "SK하이닉스", "HBM", "D램", "투자"], categories=["IT/과학"]))


def tone(summary=CASUAL_SENTENCE, body=CASUAL_BODY):
    return make_response("{}", parsed=ToneResult(
        title="📰 HBM 투자 확대", summary=summary, content=body, keywords=["HBM"]))



# ---------------------------------------------------------------- 클라이언트 계약 수준 페이크

def wire_clients(monkeypatch, judge, generator, tone_client):
    """세 모듈의 get_client를 주어진 LLMClient 페이크로 바꾼다(전송 계층 없이 결과를 직접 주입한다)."""
    monkeypatch.setattr(evaluators_module, "get_client", lambda role: judge)
    monkeypatch.setattr(generator_module, "get_client", lambda role: generator)
    monkeypatch.setattr(tone_module, "get_client", lambda role: tone_client)


def blocked_result(error, http_status=None, attempts=1):
    """재호출해도 소용없는 실패로 끝난 LLMResult: 킬 스위치(attempts=0), HTTP 오류, length, content_filter."""
    return LLMResult(
        text=None, parsed=None, usage=LLMUsage(), latency_s=0.0, attempts=attempts,
        provider="fake", model="fake-model", error=error, http_status=http_status,
    )


def cluster_pass_result():
    """FakeLLMClient용: 클러스터 평가 PASS 결과."""
    return {"parsed": ClusterEval(decision="PASS", confidence=0.9, summary="HBM 투자")}
