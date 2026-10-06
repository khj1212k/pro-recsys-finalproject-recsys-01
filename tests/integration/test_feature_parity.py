"""train/serve parity 게이트(ADR 0033, 설계 문서 §3.3 · E11)를 실제 PostgreSQL + 실제 라우터 + 운영 배선으로 돈다.

흐름: 뉴스레터·사용자·과거 클릭/노출을 시드 -> 재구축 잡으로 장기 프로필 상태를 채움 -> 22열 LightGBM 모델을
등록 스크립트로 shadow 등록 -> GET /newsletters/today 200건을 재생하며 일부를 POST /logs/newsletter/click으로
클릭 -> DB의 로그만 읽어 게이트 네 항목을 계산.

reports/recsys/parity_v1.json의 수치는 이 테스트가 CI에서 쓴 파일이다(RECSYS_PARITY_REPORT가 가리키는 경로,
.github/workflows/ci.yml이 아티팩트 recsys-parity로 올린다). 로컬에서는 DB를 띄우지 않는다.
"""
import importlib.util
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import psycopg2

from tests.integration.test_realtime_recsys_seeded_db import DIM, api_client, engine, seeded  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
N_EXTRA_NEWSLETTERS = 300   # 시드 48개와 합쳐 신선도 창 안의 후보가 cap(300)과 출처별 k(100)를 넘게
N_REQUEST_USERS = 6
N_CROWD_USERS = 5
N_REQUESTS = 200


def _load_register_script():
    spec = importlib.util.spec_from_file_location("register_model", REPO / "scripts" / "register_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_extra_newsletters(pg_conn, seeded, rng, now):  # noqa: F811
    """주제 4개 주변에 흩어진 뉴스레터. 임베딩의 크기는 일부러 1이 아니게, 생성 시각은 최근 80시간에 걸치게
    (일부는 72시간 창 밖), 일부는 카테고리를 둘 갖게 한다."""
    ids = []
    with pg_conn.cursor() as cur:
        for i in range(N_EXTRA_NEWSLETTERS):
            topic = int(rng.integers(0, 4))
            v = np.zeros(DIM, dtype=np.float32)
            v[topic] = 1.0
            v[4 + int(rng.integers(0, 12))] = float(rng.uniform(0.2, 0.9))
            v += rng.normal(0, 0.03, DIM).astype(np.float32)
            v *= float(rng.uniform(0.5, 2.0))
            created = now - timedelta(hours=float(rng.uniform(0.3, 80)), microseconds=int(rng.integers(0, 10**6)))
            cur.execute(
                "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                "news_letter_embedding, news_letter_keywords, raw_news_count, news_letter_created_at) "
                "VALUES (%s, '요약', '내용', %s::vector, '[]', %s, %s) RETURNING news_letter_id",
                (f"parity-{i}", str(v.tolist()), int(rng.integers(1, 9)), created),
            )
            nid = cur.fetchone()[0]
            cats = {seeded.cat_ids[topic]} | ({seeded.cat_ids[(topic + 1) % 4]} if i % 9 == 0 else set())
            for cid in cats:
                cur.execute("INSERT INTO news_letter_categories (news_letter_id, category_id) VALUES (%s, %s)",
                            (nid, cid))
            ids.append(nid)
    return ids


def _cleanup_newsletters(pg_conn, ids):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM user_newsletter_ctr_log WHERE news_letter_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM recommendation_impression_log WHERE news_letter_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM news_letter_categories WHERE news_letter_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM news_letter WHERE news_letter_id = ANY(%s)", (ids,))


def test_serving_features_scores_and_lists_match_the_offline_harness_over_200_replayed_requests(
    api_client, engine, seeded, pg_conn, database_url  # noqa: F811
):
    from app.recsys.config import RecsysConfig
    from app.recsys.runtime import build_sql_service
    from evaluation.recsys.serving_parity import (
        GATE_ITEMS,
        THRESHOLDS,
        load_from_db,
        load_model,
        run_gate,
        write_report,
    )
    from jobs.tasks.rebuild_user_state import rebuild_all
    from recsys_core import serving
    from tests.recsys.parity_sim import train_model_text

    rng = np.random.default_rng(20261006)
    now = datetime.now(timezone.utc)
    model_name = f"parity-{uuid.uuid4().hex[:8]}"
    extra = _seed_extra_newsletters(pg_conn, seeded, rng, now)
    all_ids = np.array(sorted(seeded.topic_of) + extra)
    service = None
    try:
        users = [seeded.add_user(categories=[seeded.cat_ids[u % 4]] if u % 3 else []) for u in range(N_REQUEST_USERS)]
        crowd = [seeded.add_user() for _ in range(N_CROWD_USERS)]

        # --- 재생 전의 이력. 클릭 API를 거치지 않은 행들이다(시드·이관과 같은 경로).
        for uid in users[: N_REQUEST_USERS // 2]:
            for _ in range(int(rng.integers(8, 35))):
                at = now - timedelta(hours=float(rng.uniform(0.2, 240)), microseconds=int(rng.integers(0, 10**6)))
                seeded.click(uid, int(rng.choice(all_ids)), at=at)
        for _ in range(400):
            at = now - timedelta(hours=float(rng.uniform(0.05, 50)), microseconds=int(rng.integers(0, 10**6)))
            seeded.click(int(rng.choice(crowd)), int(rng.choice(all_ids)), at=at)
        with pg_conn.cursor() as cur:
            for _ in range(2500):
                at = now - timedelta(hours=float(rng.uniform(0.05, 30)), microseconds=int(rng.integers(0, 10**6)))
                cur.execute(
                    "INSERT INTO recommendation_impression_log "
                    "(request_id, user_id, news_letter_id, position, source, model_version, created_at) "
                    "VALUES (%s, %s, %s, 0, 'realtime', 'heuristic-v1', %s)",
                    (str(uuid.uuid4()), int(rng.choice(crowd)), int(rng.choice(all_ids)), at),
                )
        # 채우기: 클릭 API를 거치지 않은 이력을 장기 프로필 상태에 반영한다(재구축 잡의 경로)
        backfill = rebuild_all(engine, user_ids=users)
        assert backfill["rebuilt"] == N_REQUEST_USERS // 2

        # --- 모델 등록(스크립트의 함수로): 22열, 스키마 지문 포함, shadow
        register_model = _load_register_script()
        tx = psycopg2.connect(database_url)
        try:
            register_model.register(tx, register_model.build_row(train_model_text(), "gate", name=model_name))
        finally:
            tx.close()

        # --- 운영 배선의 서비스 하나를 모든 요청이 같이 쓴다(결과 캐시·아이템 캐시·shadow 전용 스레드 포함)
        # 게이트는 피처와 shadow 점수가 남은 요청을 비교한다: 러너가 느려도 요청이 폴백으로 가지 않고, 전용
        # 스레드의 결과가 로그 쓰기의 대기 상한(기본 100ms)에 걸려 버려지지 않게 세 한도를 넉넉히 둔다.
        cfg = RecsysConfig(time_budget_ms=5000, shadow_budget_ms=5000, shadow_log_wait_ms=5000,
                           model_name=model_name, shadow_max=1)
        service = build_sql_service(cfg, database_url)
        shadow = service.recommender.stack.shadows[0]
        deadline = time.monotonic() + 30
        while shadow._current is None and time.monotonic() < deadline:  # 레지스트리 로드는 백그라운드 스레드다
            time.sleep(0.05)
            shadow.maybe_reload()
        assert shadow._current is not None, "shadow model was not loaded from the registry"

        sources, n_clicks = {}, 0
        for _ in range(N_REQUESTS):
            uid = int(rng.choice(users))
            client = api_client(uid, service=service)
            resp = client.get("/newsletters/today")
            assert resp.status_code == 200
            shown = [item["news_letter_id"] for item in resp.json()]
            sources[resp.headers["X-Rec-Source"]] = sources.get(resp.headers["X-Rec-Source"], 0) + 1
            if shown and rng.random() < 0.45:
                for position in rng.choice(len(shown), size=int(rng.integers(1, 3)), replace=False):
                    body = {"news_letter_id": shown[int(position)], "request_id": resp.headers["X-Request-Id"],
                            "position": int(position)}
                    assert client.post("/logs/newsletter/click", json=body).status_code == 200
                    n_clicks += 1
        counters = service.counters.snapshot()

        # --- 게이트: DB의 로그만 읽는다
        version = f"lgbm:{model_name}@gate"
        logs, requests = load_from_db(pg_conn, model_version=version, user_ids=users)
        predict, loaded_version = load_model(pg_conn, model_name, "gate")
        assert loaded_version == version
        report = run_gate(
            logs, requests, predict, cfg.candidate_spec(),
            meta={
                "source": "CI integration test: PostgreSQL 16 + pgvector, real routers, production wiring "
                          "(tests/integration/test_feature_parity.py)",
                "commit": os.environ.get("GITHUB_SHA"),
                "ci_run_id": os.environ.get("GITHUB_RUN_ID"),
                "replayed_requests": N_REQUESTS,
                "clicks_through_api": n_clicks,
                "response_sources": sources,
                "model_version": version,
                "service_counters": {k: v for k, v in sorted(counters.items())
                                     if k.split(".")[0] in ("features", "shadow", "fallback", "cache", "scorer")},
            },
        )
        out = os.environ.get("RECSYS_PARITY_REPORT")
        if out:
            write_report(report, out)
        summary = {k: report[k] for k in GATE_ITEMS}
        overlap = summary["candidate_generator_top20_overlap"]
        print("\n[parity gate] " + json.dumps(
            {"requests": len(requests), "pass": report["pass"],
             "features_max_abs_diff": summary["features"]["max_abs_diff"],
             "min_kendall_tau": summary["scores"]["min_kendall_tau"],
             "mean_top20_overlap": overlap["mean_overlap"],
             "candidate_config_equal": summary["candidate_config"]["equal"], "sources": sources}))

        # 재생이 게이트가 보려는 경로를 실제로 지났는가
        assert sum(sources.values()) == N_REQUESTS and sources.get("realtime", 0) >= 120
        assert counters.get("fallback.timeout", 0) == 0 and counters.get("fallback.error", 0) == 0
        assert counters.get("features.error", 0) == 0 and counters.get("shadow.error", 0) == 0
        assert len(requests) >= 150, f"only {len(requests)} requests carry adapter features: {counters}"
        assert max(len(r.candidate_ids) for r in requests) > 150  # 출처별 k를 넘는 후보 수

        # 1. 피처: 전 열 max|Δ| < 1e-6, NaN 위치 동일
        assert summary["features"]["nan_position_mismatches"] == 0, summary["features"]
        assert summary["features"]["max_abs_diff"] < THRESHOLDS["features_max_abs_diff"], summary["features"]
        # 2. 점수 순서: 칸 로그의 shadow 점수 vs 다시 계산한 피처로 낸 점수, Kendall τ = 1
        assert summary["scores"]["requests_with_shadow_scores"] >= 150, summary["scores"]
        assert summary["scores"]["min_kendall_tau"] == 1.0, summary["scores"]
        # 3. 랭커 상위 20개 겹침: 서빙의 후보 집합 위 vs 하네스의 후보 구성 위(둘 다 오프라인 재계산)
        assert overlap["mean_overlap"] >= THRESHOLDS["candidate_generator_top20_mean_overlap"], overlap
        # 4. 후보 생성기 구성
        assert summary["candidate_config"]["equal"] is True, summary["candidate_config"]
        assert report["pass"] is True
        assert report["meta"]["feature_schema_hash"] == serving.SCHEMA_HASH
    finally:
        if service is not None:
            service.shutdown()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (model_name,))
        _cleanup_newsletters(pg_conn, extra)
