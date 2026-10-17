"""활성 휴리스틱의 가중치가 "클릭 한 번"에 얼마나 민감한지 보는 합성 점검.

실제 사용자 데이터로 측정한 추천 품질이 아니다. 주제 4개 x 12개(1024차원, 주제 내 코사인 ~0.7) 합성 코퍼스에서,
장기 프로필이 주제 0(+2)인 사용자가 주제 1을 클릭한 뒤 상위 10개 중 주제 1 비율이 어떻게 바뀌는지를 낸다.
여러 시드로 반복해 평균과 범위를 낸다.

두 표를 낸다.
1. 고정 장기 벡터(ADR 0015 증거 4의 표): 장기 벡터를 클릭과 무관하게 고정하고 heuristic-v1의 장기:단기 가중치만 바꾼다.
   ADR 0015가 0.45:0.35를 고른 근거다. 그때의 서빙은 장기 벡터를 하루 한 번 만들었다.
2. 클릭으로 갱신되는 장기 벡터(ADR 0033 이후의 서빙): 장기 벡터가 클릭 이력의 반감기 7일 감쇠 합이라 방금 한 클릭이
   장기 벡터에도 들어간다. heuristic-v1(사전값)과 heuristic-v2의 (b)·(a) 세트(ADR 0014)를 같은 이력에서 비교한다.

    .venv/bin/python -m evaluation.serving.click_shift_sensitivity
"""
import os
import sys
from datetime import timedelta

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:5432/unused")
from app.recsys.config import RecsysConfig  # noqa: E402
from app.recsys.pipeline import Deadline, RealtimeRecommender  # noqa: E402
from app.recsys.scoring import HeuristicScorer, PriorHeuristicScorer, PriorHeuristicWeights  # noqa: E402
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser  # noqa: E402

DIM, TOPICS, PER_TOPIC = 1024, 4, 12
WEIGHTS = [(0.6, 0.2), (0.5, 0.3), (0.45, 0.35), (0.4, 0.4), (0.35, 0.45), (0.3, 0.5)]
CLICKS = [1, 2, 3]
SEEDS = range(10)
# 표 2의 클릭 이력: 주제 0을 5번(1~8일 전), 주제 2를 3번(2~6일 전). (뉴스레터 ID, 며칠 전)
HISTORY = [(2, 1), (3, 2), (4, 3), (5, 5), (6, 8), (26, 2), (27, 4), (28, 6)]


def corpus(seed):
    rng = np.random.default_rng(seed)
    items, topic_of = [], {}
    for t in range(TOPICS):
        for j in range(PER_TOPIC):
            v = np.zeros(DIM, dtype=np.float32)
            v[t] = 1.0
            v += rng.normal(0, 0.02, DIM).astype(np.float32)
            nid = t * PER_TOPIC + j + 1
            items.append(FakeNewsletter(nid, v / np.linalg.norm(v), NOW - timedelta(hours=1 + j), 1 + j % 3, (t + 1,)))
            topic_of[nid] = t
    return items, topic_of


def _share(rec, repo, topic_of):
    top = rec.recommend(repo, 1, NOW, Deadline(10.0)).news_letter_ids[:10]
    return sum(topic_of[n] == 1 for n in top) / len(top)


def share_after(w_long, w_short, n_clicks, seed):
    """표 1: 고정 장기 벡터 + heuristic-v1."""
    items, topic_of = corpus(seed)
    vec = {n.id: n.embedding for n in items}
    lt = vec[1] + 0.6 * vec[2 * PER_TOPIC + 1]
    repo = FakeRepo(items, [FakeUser(1, long_term=lt / np.linalg.norm(lt))])
    weights = PriorHeuristicWeights(long_term=w_long, short_term=w_short)
    rec = RealtimeRecommender(RecsysConfig(), scorer=PriorHeuristicScorer(weights))
    for k in range(n_clicks):
        repo.click(1, PER_TOPIC + 1 + k, NOW - timedelta(minutes=5 + k))
    return _share(rec, repo, topic_of)


def shares_with_live_profile(scorer, seed):
    """표 2: 클릭으로 갱신되는 장기 벡터. 반환: 클릭 0~3번 뒤의 주제 1 비율."""
    items, topic_of = corpus(seed)
    repo = FakeRepo(items, [FakeUser(1)])
    for nid, days in HISTORY:
        repo.click(1, nid, NOW - timedelta(days=days))
    rec = RealtimeRecommender(RecsysConfig(), scorer=scorer)
    out = [_share(rec, repo, topic_of)]
    for k in range(max(CLICKS)):
        repo.click(1, PER_TOPIC + 1 + k, NOW - timedelta(minutes=5 + k))
        out.append(_share(rec, repo, topic_of))
    return out


def main():
    print(f"[1] fixed long-term vector, heuristic-v1: topic-1 share@10 after N clicks on topic 1 "
          f"(mean [min,max] over {len(SEEDS)} seeds)")
    print("w_long w_short | " + " | ".join(f"{c} click(s)" for c in CLICKS))
    for w_long, w_short in WEIGHTS:
        cells = []
        for c in CLICKS:
            vals = [share_after(w_long, w_short, c, s) for s in SEEDS]
            cells.append(f"{np.mean(vals):.2f} [{min(vals):.1f},{max(vals):.1f}]")
        print(f"{w_long:5.2f} {w_short:6.2f}  | " + " | ".join(cells))

    print(f"\n[2] long-term vector updated by every click (ADR 0033), history {len(HISTORY)} clicks on topics 0/2: "
          f"topic-1 share@10 after N clicks (mean [min,max] over {len(SEEDS)} seeds)")
    print("scorer          | 0 clicks | " + " | ".join(f"{c} click(s)" for c in CLICKS))
    for name, scorer in (("heuristic-v1", PriorHeuristicScorer()), ("heuristic-v2-b", HeuristicScorer(set_name="b")),
                         ("heuristic-v2-a", HeuristicScorer(set_name="a"))):
        runs = np.asarray([shares_with_live_profile(scorer, s) for s in SEEDS])
        cells = [f"{runs[:, j].mean():.2f} [{runs[:, j].min():.1f},{runs[:, j].max():.1f}]" for j in range(runs.shape[1])]
        print(f"{name:15s} | " + " | ".join(cells))


if __name__ == "__main__":
    main()
