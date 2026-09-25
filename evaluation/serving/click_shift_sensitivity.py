"""HeuristicScorer의 장기:단기 가중치가 "클릭 한 번"에 얼마나 민감한지 보는 합성 점검.

실제 사용자 데이터로 측정한 추천 품질이 아니다. 주제 4개 x 12개(1024차원, 주제 내
코사인 ~0.7) 합성 코퍼스에서, 장기 프로필이 주제 0(+2)인 사용자가 주제 1을 클릭한
뒤 상위 10개 중 주제 1 비율이 어떻게 바뀌는지를 가중치/클릭 수별로 출력한다
(ADR 0015의 가중치 선택 근거). 여러 시드로 반복해 평균과 범위를 낸다.

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
# pipeline -> scheduler.calculate_ranking -> app.database가 임포트 시점에 create_engine을
# 부른다. 이 점검은 DB에 접속하지 않으므로 URL 파싱만 통과하면 된다.
os.environ.setdefault("DATABASE_URL", "postgresql://unused:unused@localhost:5432/unused")

from app.recsys.config import RecsysConfig  # noqa: E402
from app.recsys.pipeline import Deadline, RealtimeRecommender  # noqa: E402
from app.recsys.scoring import HeuristicScorer, HeuristicWeights  # noqa: E402
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser  # noqa: E402

DIM, TOPICS, PER_TOPIC = 1024, 4, 12
WEIGHTS = [(0.6, 0.2), (0.5, 0.3), (0.45, 0.35), (0.4, 0.4), (0.35, 0.45), (0.3, 0.5)]
CLICKS = [1, 2, 3]
SEEDS = range(10)


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


def share_after(w_long, w_short, n_clicks, seed):
    items, topic_of = corpus(seed)
    vec = {n.id: n.embedding for n in items}
    lt = vec[1] + 0.6 * vec[2 * PER_TOPIC + 1]
    repo = FakeRepo(items, [FakeUser(1, long_term=lt / np.linalg.norm(lt))])
    weights = HeuristicWeights(long_term=w_long, short_term=w_short)
    rec = RealtimeRecommender(RecsysConfig(), scorer=HeuristicScorer(weights))
    for k in range(n_clicks):
        repo.click(1, PER_TOPIC + 1 + k, NOW - timedelta(minutes=5 + k))
    top = rec.recommend(repo, 1, NOW, Deadline(10.0)).news_letter_ids[:10]
    return sum(topic_of[n] == 1 for n in top) / len(top)


def main():
    print(f"topic-1 share@10 after N clicks on topic 1 (mean [min,max] over {len(SEEDS)} seeds)")
    print("w_long w_short | " + " | ".join(f"{c} click(s)" for c in CLICKS))
    for w_long, w_short in WEIGHTS:
        cells = []
        for c in CLICKS:
            vals = [share_after(w_long, w_short, c, s) for s in SEEDS]
            cells.append(f"{np.mean(vals):.2f} [{min(vals):.1f},{max(vals):.1f}]")
        print(f"{w_long:5.2f} {w_short:6.2f}  | " + " | ".join(cells))


if __name__ == "__main__":
    main()
