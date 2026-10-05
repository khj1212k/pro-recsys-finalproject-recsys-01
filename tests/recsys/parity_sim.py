"""메모리 저장소 위에서 서비스를 돌려 로그를 쌓고, 그 로그를 parity 게이트의 입력으로 바꾼다.

시간은 가상 시계다: 요청 -> (응답 뒤) 노출 로그 기록 -> (가끔) 클릭 순으로 흐르고, 요청 사이에 시계가 나아간다.
그래서 "요청 직후의 클릭", "방금 쓴 노출 로그" 같은 경계가 실제 순서대로 생긴다.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import lightgbm as lgb
import numpy as np

from app.recsys.config import RecsysConfig
from app.recsys.lgbm_scorer import RegisteredModel
from app.recsys.metrics import RecsysCounters
from app.recsys.runtime import build_scorer_stack
from app.recsys.scoring import ADAPTER_FEATURE_SCHEMA, decode_features
from app.recsys.service import build_service
from evaluation.recsys.service_logs import ServiceLogs, epoch_us
from evaluation.recsys.serving_parity import LoggedRequest
from recsys_core import serving
from recsys_core.profile import NO_CATEGORY
from tests.recsys.fakes import FakeNewsletter, FakeRepo, FakeUser, LogRecorder
from tests.recsys.test_lgbm_scorer import FakeSource

START = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
MODEL_VERSION = "lgbm:ranker@gate"


def train_model_text(seed: int = 0) -> str:
    """어댑터의 22열을 받는 작은 LightGBM 모델. 무작위 데이터로 만든다: 순위의 품질이 아니라, 같은 입력에
    같은 점수가 나오는지를 보는 데 쓴다. 히스토리·단기·인기도·신선도 열을 모두 쓰게 목표를 섞었다."""
    rng = np.random.default_rng(seed)
    n = 4000
    X = rng.normal(size=(n, len(serving.FEATURE_NAMES))).astype(np.float32)
    col = {name: i for i, name in enumerate(serving.FEATURE_NAMES)}
    X[:, [col["user_age"], col["user_gender"]]] = np.nan
    X[rng.random(n) < 0.3, col["hours_since_last_event"]] = np.nan
    for name in ("pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h", "pop_inviews_24h", "hist_len", "short_len"):
        X[:, col[name]] = np.floor(np.abs(X[:, col[name]]) * 4)
    X[:, col["hours_since_pub"]] = np.abs(X[:, col["hours_since_pub"]]) * 30
    y = (1.5 * X[:, col["hist_cos"]] + X[:, col["short_cos"]] + 0.4 * np.log1p(X[:, col["pop_clicks_6h"]])
         - 0.02 * X[:, col["hours_since_pub"]] + 0.5 * X[:, col["cat_share"]] + rng.normal(0, 0.2, n))
    booster = lgb.train(
        {"objective": "regression", "verbose": -1, "num_leaves": 15, "seed": seed, "min_data_in_leaf": 10},
        lgb.Dataset(X, y, feature_name=list(serving.FEATURE_NAMES)),
        num_boost_round=60,
    )
    return booster.model_to_string()


@dataclass
class SimResult:
    repo: FakeRepo
    log: LogRecorder
    requests: List[LoggedRequest]
    logs: ServiceLogs
    counters: Dict[str, int]
    model_text: str
    serving_spec: object
    n_requests: int


def simulate(n_items: int = 120, n_users: int = 8, n_requests: int = 200, dim: int = 24, seed: int = 0,
             model_text: Optional[str] = None, click_rate: float = 0.5, fast_requests: int = 60,
             **cfg_kw) -> SimResult:
    """fast_requests: 처음 그만큼의 요청은 밀리초 간격으로 이어진다(요청·클릭이 같은 초에 여러 건). 자동화된
    클라이언트와 통합 테스트의 재생이 이런 모양이고, 같은 초의 클릭이 20건 상한의 경계에 걸리는 경우가 여기서 나온다."""
    rng = np.random.default_rng(seed)
    topics = rng.standard_normal((6, dim)).astype(np.float32)
    newsletters = []
    for i in range(1, n_items + 1):
        t = int(rng.integers(0, 6))
        emb = topics[t] + 0.6 * rng.standard_normal(dim).astype(np.float32)
        newsletters.append(FakeNewsletter(
            id=i, embedding=emb * float(rng.uniform(0.5, 2.0)),
            created_at=START - timedelta(hours=float(rng.uniform(0.2, 80))),  # 일부는 72시간 창 밖
            raw_news_count=int(rng.integers(1, 9)), category_ids=(1 + t % 5,),
        ))
    users = [FakeUser(u, category_ids=sorted(set(rng.integers(1, 6, int(rng.integers(0, 3))).tolist())))
             for u in range(1, n_users + 1)]
    repo = FakeRepo(newsletters, users)
    ids = np.array([n.id for n in newsletters])
    # 요청을 재생하기 전의 이력: 사용자 절반은 며칠에 걸친 클릭이 있고, 나머지는 재생 중에 처음 클릭한다.
    for u in range(1, n_users // 2 + 1):
        for _ in range(int(rng.integers(3, 25))):
            repo.click(u, int(rng.choice(ids)), START - timedelta(hours=float(rng.uniform(0.1, 200)),
                                                                   microseconds=int(rng.integers(0, 10**6))))

    model_text = model_text or train_model_text()
    source = FakeSource()
    source.models["gate"] = RegisteredModel("ranker", "gate", model_text, list(serving.FEATURE_NAMES),
                                            serving.SCHEMA_HASH)
    source.shadows.insert(0, "gate")
    clock = {"now": START}

    @contextmanager
    def factory():
        yield repo

    cfg = RecsysConfig(model_reload_s=0.0, shadow_max=1, **cfg_kw)
    counters = RecsysCounters()
    stack = build_scorer_stack(cfg, source, serving.features, counters)
    for scorer in (stack.active, *stack.shadows):
        scorer.reload_in_background = False
    log = LogRecorder()
    draw_rng = np.random.default_rng(seed + 1)
    service = build_service(
        cfg, repo_factory=factory, scorer=stack, impression_writer=log, now_fn=lambda: clock["now"],
        counters=counters, rng_factory=lambda request_id: draw_rng, feature_repo_factory=factory,
        clock=lambda: (clock["now"] - START).total_seconds(),
    )
    as_of_by_request = {}
    try:
        for step in range(n_requests):
            fast = step < fast_requests
            if fast:
                clock["now"] += timedelta(milliseconds=float(rng.uniform(15, 90)))
            else:
                clock["now"] += timedelta(seconds=float(rng.uniform(0.2, 900)), microseconds=int(rng.integers(0, 10**6)))
            user = int(rng.integers(1, n_users + 1)) if not fast else 1 + step % 2
            rec = service.recommend(user, fallback_repo=repo)
            service.log_impressions(user, rec, rec.news_letter_ids)
            as_of_by_request[rec.request_id] = rec.features_as_of
            # 노출 로그는 응답 뒤에 쓰인다(요청보다 조금 늦은 시각), 클릭은 그 뒤에 온다
            repo.impress(user, rec.news_letter_ids,
                         clock["now"] + timedelta(milliseconds=int(rng.integers(1, 4) if fast else rng.integers(5, 400))))
            if rec.news_letter_ids and rng.random() < (0.9 if fast else click_rate):
                for nid in rng.choice(rec.news_letter_ids, size=int(rng.integers(1, 3)), replace=False):
                    if fast:
                        clock["now"] += timedelta(milliseconds=float(rng.uniform(4, 30)))
                    else:
                        clock["now"] += timedelta(seconds=float(rng.uniform(0.5, 40)),
                                                  microseconds=int(rng.integers(0, 10**6)))
                    repo.click(user, int(nid), clock["now"])
    finally:
        service.shutdown()

    return SimResult(
        repo=repo, log=log, requests=logged_requests(log), logs=service_logs(repo), counters=counters.snapshot(),
        model_text=model_text, serving_spec=cfg.candidate_spec(), n_requests=n_requests,
    )


def logged_requests(log: LogRecorder) -> List[LoggedRequest]:
    """LogRecorder에 쌓인 요청·칸 행 -> 게이트 입력. 어댑터 피처(스키마 2)가 남은 요청만."""
    out = []
    for row in log.requests:
        if row["feature_schema_version"] != ADAPTER_FEATURE_SCHEMA:
            continue
        slots = log.slots_of(row["request_id"])
        shadow = None
        if slots and all(s["scores_shadow"] and MODEL_VERSION in s["scores_shadow"] for s in slots):
            shadow = np.array([s["scores_shadow"][MODEL_VERSION] for s in slots], dtype=np.float64)
        out.append(LoggedRequest(
            request_id=row["request_id"], user_id=row["user_id"], as_of=row["features_as_of"],
            candidate_ids=list(row["candidate_ids"]), slot_ids=[s["news_letter_id"] for s in slots],
            slot_features=np.stack([decode_features(s["features"]) for s in slots]),
            slot_shadow=shadow, meta={"cache_hit": row["cache_hit"], "source": row["source"]},
        ))
    return out


def service_logs(repo: FakeRepo) -> ServiceLogs:
    """메모리 저장소의 로그를 DB에서 읽은 것과 같은 모양으로."""
    items = sorted(repo.newsletters.values(), key=lambda n: n.id)
    return ServiceLogs(
        item_ids=np.array([n.id for n in items]),
        item_emb=np.stack([n.embedding for n in items]),
        item_created_us=np.array([epoch_us(n.created_at) for n in items]),
        item_category=np.array([min(n.category_ids) if n.category_ids else NO_CATEGORY for n in items]),
        click_user=np.array([c[1] for c in repo.clicks], dtype=np.int64),
        click_item=np.array([c[2] for c in repo.clicks], dtype=np.int64),
        click_us=np.array([epoch_us(c[3]) for c in repo.clicks], dtype=np.int64),
        inview_item=np.array([i[1] for i in repo.impressions], dtype=np.int64),
        inview_us=np.array([epoch_us(i[2]) for i in repo.impressions], dtype=np.int64),
        user_categories={u.id: list(u.category_ids) for u in repo.users.values()},
    )
