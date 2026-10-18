# C2 (docs/adr/0010 Addendum 2026-10-18): 클러스터 평가가 아웃라이어를 빼거나(outlier_indices)
# 서브그룹 하나만 남기면(sub_groups) current_articles는 줄어들지만 current_article_ids는 원래 클러스터
# 전체로 남는다. 저장 노드가 current_article_ids로 저장하던 때는
# - 생성에 쓰이지 않은(판정이 "다른 사건"이라고 뺀) 기사까지 news_raw.news_letter_id가 채워져
#   hdbscan_clusterer의 `news_letter_id IS NULL` 필터 때문에 다음 실행에서 영구 제외됐고
# - raw_news_count가 실제 출처 수보다 크게 기록됐다.
# 실제 그래프와 실제 role 클라이언트에 페이크 전송 계층만 끼워 저장 호출의 article_ids를 본다.
# 고치기 전 코드로는 아래 두 시나리오가 [10, 11, 12, 13]으로 실패한다(exp/generation-warmup eab2b89의 같은 시나리오).
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests import llm_graph_fakes as fx  # noqa: E402

from workflow.graph import compile_workflow  # noqa: E402
from workflow.nodes import _used_article_ids  # noqa: E402

IDS4 = [10, 11, 12, 13]
TITLES4 = fx.TITLES + ["반도체 수출 호조"]
BODIES4 = fx.BODIES + ["반도체 수출이 두 달째 늘었다고 산업부가 밝혔다."]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, llm_ledger_init):
    fx.configure_env(monkeypatch, tmp_path, llm_ledger_init)
    yield
    fx.teardown_env()


def _run_with_refinement(monkeypatch, first_cluster_eval):
    """클러스터 평가가 FAIL(이상치 제거 또는 분할)을 내고, 줄어든 기사로 다시 평가해 PASS한 뒤 저장까지 간다."""
    saved = fx.no_db(monkeypatch)
    # judge 순서: 클러스터 평가(FAIL) -> 클러스터 재평가(PASS) -> 뉴스레터 평가
    gen_fake, judge_fake = fx.role_transports(
        [fx.content(fx.CLEAN_WITHOUT_DRAM), fx.meta(), fx.tone(body=fx.CASUAL_WITHOUT_DRAM)],
        [first_cluster_eval, fx.cluster_pass(), fx.judge_pass()],
    )
    final = compile_workflow().invoke(fx.initial_state(IDS4, TITLES4, BODIES4))
    return final, saved, gen_fake, judge_fake


def test_outlier_removed_by_cluster_eval_is_not_linked_to_the_newsletter(monkeypatch):
    final, saved, _gen, judge = _run_with_refinement(monkeypatch, fx.cluster_fail(outliers=[2]))

    assert final["completed_newsletters"] == [901]
    assert saved[0]["article_ids"] == [10, 11, 13]
    assert judge.chat.completions.call_count == 3  # 평가 FAIL, 재평가 PASS, 뉴스레터 평가
    # 상태에는 원래 클러스터 전체가 그대로 남는다 - 저장 노드가 쓰는 값만 달라졌다
    assert final["current_article_ids"] == IDS4


def test_only_the_kept_sub_group_is_linked_when_cluster_eval_splits(monkeypatch):
    final, saved, _gen, _judge = _run_with_refinement(monkeypatch, fx.cluster_fail(sub_groups=[[0, 1, 3], [2]]))

    assert final["completed_newsletters"] == [901]
    assert saved[0]["article_ids"] == [10, 11, 13]


def test_every_article_is_linked_when_nothing_was_excluded(monkeypatch):
    saved = fx.no_db(monkeypatch)
    fx.role_transports([fx.content(), fx.meta(), fx.tone()], [fx.cluster_pass(), fx.judge_pass()])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["completed_newsletters"] == [901]
    assert saved[0]["article_ids"] == [10, 11, 12]


def test_used_article_ids_keeps_order_drops_duplicates_and_ignores_missing_ids():
    state = {
        "current_article_ids": [1, 2, 3, 4],
        "current_articles": [{"id": 3}, {"id": 1}, {"id": 3}, {"title": "id 없음"}],
    }

    assert _used_article_ids(state) == [3, 1]


def test_used_article_ids_falls_back_to_cluster_ids_only_when_no_articles_were_loaded():
    assert _used_article_ids({"current_article_ids": [1, 2], "current_articles": []}) == [1, 2]
    assert _used_article_ids({"current_article_ids": [1, 2]}) == [1, 2]
