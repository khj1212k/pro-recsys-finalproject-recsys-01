"""요청 시점 추천의 SQL 경로와 단기 상태 저장소 후보를 운영 규모로 시드한 일회용 DB에서 잰다.

ADR 0015(요청 시점 추천 구조)와 0017(단기 상태 저장소)의 증거를 만드는 스크립트다.

- --server-url 의 Postgres 서버에 recsys_bench_<hex> 데이터베이스를 새로 만들고
  CREATE EXTENSION vector -> alembic upgrade head -> 시드 -> 측정 -> DROP DATABASE 한다.
  기존 데이터베이스에는 쓰지 않는다.
- 임베딩은 합성(주제 중심 + 잡음, 1024차원)이다. 지연/쿼리 계획을 재는 용도이지 추천
  품질 측정이 아니다.
- 규모 기본값은 팀 합성 데이터의 한 번 실행분(뉴스레터 195개, 대부분 하루 151개)을
  기준으로 하루 약 165개 x 180일 = 뉴스레터 3만 개(72시간 신선도 창 안 약 500개),
  사용자 2만 명, 클릭 로그 100만 건(90일)이다.
- --redis-url 을 주면 Redis 리스트(LPUSH/LTRIM/EXPIRE)를 단기 상태 저장소로 쓰는 대안도
  같은 사용자/같은 아이템 캐시 조건으로 잰다(redis 파이썬 패키지 필요, 프로젝트 의존성 아님).

    .venv/bin/python -m evaluation.serving.request_path_bench \\
        --server-url postgresql://postgres:postgres@127.0.0.1:55433/postgres \\
        --redis-url redis://127.0.0.1:56379/0 --out /tmp/bench.json
"""
import argparse
import io
import json
import os
import platform
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "backend")
DIM = 1024
TOPICS = 8


# --------------------------------------------------------------------------- utils
def with_db(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + dbname, parts.query, parts.fragment))


def stats(samples_ms):
    s = np.sort(np.asarray(samples_ms, dtype=np.float64))
    if len(s) == 0:
        return {"n": 0}
    return {
        "n": int(len(s)),
        "p50_ms": round(float(np.percentile(s, 50)), 3),
        "p95_ms": round(float(np.percentile(s, 95)), 3),
        "p99_ms": round(float(np.percentile(s, 99)), 3),
        "mean_ms": round(float(s.mean()), 3),
    }


def timed(fn, args_iter):
    out = []
    for a in args_iter:
        t0 = time.perf_counter()
        fn(a)
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


def vec_text(m: np.ndarray):
    buf = io.StringIO()
    np.savetxt(buf, m, fmt="%.5f", delimiter=",")
    return ["[" + line + "]" for line in buf.getvalue().splitlines()]


def copy_rows(cur, table_cols: str, rows):
    buf = io.StringIO()
    for r in rows:
        buf.write("\t".join("\\N" if v is None else str(v) for v in r))
        buf.write("\n")
    buf.seek(0)
    cur.copy_expert(f"COPY {table_cols} FROM STDIN", buf)


# ---------------------------------------------------------------------- lifecycle
def create_database(server_url: str, name: str) -> str:
    import psycopg2

    conn = psycopg2.connect(server_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    conn.close()
    url = with_db(server_url, name)
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.close()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url


def drop_database(server_url: str, name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(server_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    conn.close()


# --------------------------------------------------------------------------- seed
def seed(url: str, args, rng, now: datetime) -> dict:
    import psycopg2

    conn = psycopg2.connect(url)
    t_seed = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute("SHOW TimeZone")
        # timestamp without time zone 컬럼은 앱이 쓰는 방식(aware 값/NOW())과 같게
        # 서버 세션 TimeZone 벽시계로 넣는다.
        server_tz = ZoneInfo(cur.fetchone()[0])

        def wall(ts: datetime) -> str:
            return ts.astimezone(server_tz).replace(tzinfo=None).isoformat(sep=" ")

        centers = rng.normal(size=(TOPICS, DIM)).astype(np.float32)
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)

        copy_rows(
            cur,
            "category (category_id, category_name, category_code)",
            [(t + 1, f"topic{t}", 100 * (t + 1)) for t in range(TOPICS)],
        )

        n = args.newsletters
        ages_h = np.sort(rng.uniform(5 / 60, args.days * 24, n))[::-1]
        nl_created = [now - timedelta(hours=float(a)) for a in ages_h]
        nl_topic = rng.integers(0, TOPICS, n)
        emb = centers[nl_topic] + rng.normal(0, 0.03, size=(n, DIM)).astype(np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        nl_ids = np.arange(1, n + 1)
        chunk = 5000
        for s in range(0, n, chunk):
            texts = vec_text(emb[s : s + chunk])
            copy_rows(
                cur,
                "news_letter (news_letter_id, news_letter_title, news_letter_sentence, "
                "news_letter_content, news_letter_created_at, news_letter_embedding, "
                "news_letter_keywords, raw_news_count)",
                (
                    (int(nl_ids[i]), f"title {i}", "sentence", "content " * 50,
                     wall(nl_created[i]), texts[i - s], "[]", int(1 + rng.integers(0, 10)))
                    for i in range(s, min(n, s + chunk))
                ),
            )
        copy_rows(
            cur,
            "news_letter_categories (news_letter_id, category_id)",
            ((int(nl_ids[i]), int(nl_topic[i]) + 1) for i in range(n)),
        )

        u = args.users
        cold_users = max(1, int(u * 0.05))
        ua = rng.integers(0, TOPICS, u)
        ub = rng.integers(0, TOPICS, u)
        uemb = centers[ua] + 0.6 * centers[ub] + rng.normal(0, 0.03, size=(u, DIM)).astype(np.float32)
        uemb /= np.linalg.norm(uemb, axis=1, keepdims=True)
        utexts = vec_text(uemb)
        user_ids = np.arange(1, u + 1)
        copy_rows(
            cur,
            '"user" (user_id, user_email, user_password_hash, user_nickname, user_created_at, user_embedding)',
            (
                (int(user_ids[i]), f"bench{i}@example.com", "h", "n", wall(now - timedelta(days=200)),
                 None if i < cold_users else utexts[i])
                for i in range(u)
            ),
        )
        pref_rows = []
        for i in range(u):
            for c in rng.choice(TOPICS, size=int(rng.integers(1, 4)), replace=False):
                pref_rows.append((int(user_ids[i]), int(c) + 1))
        copy_rows(cur, "user_preferred_categories (user_id, category_id)", pref_rows)

        # 클릭: 90일에 고르게 + "헤비" 사용자는 최근 24시간에 30회씩(단기 벡터 LIMIT 20이
        # 꽉 차는 최악 조건). 클릭한 뉴스레터는 클릭 시각 직전 72시간 안에서 고른다.
        c = args.clicks
        click_age_h = rng.uniform(0, args.click_days * 24, c)
        click_users = user_ids[cold_users:][rng.integers(0, u - cold_users, c)]
        heavy = user_ids[cold_users : cold_users + args.heavy_users]
        heavy_age_h = rng.uniform(0, 23.5, len(heavy) * 30)
        click_age_h = np.concatenate([click_age_h, heavy_age_h])
        click_users = np.concatenate([click_users, np.repeat(heavy, 30)])
        click_ts = np.asarray([now - timedelta(hours=float(a)) for a in click_age_h])
        order = np.argsort(click_age_h)[::-1]  # 오래된 클릭부터 -> log_id가 시간 순
        hi = np.searchsorted(-ages_h, -click_age_h, side="right")
        lo = np.searchsorted(-ages_h, -(click_age_h + 72), side="left")
        hi = np.maximum(hi, lo + 1)
        pick = np.minimum(lo + (rng.random(len(lo)) * (hi - lo)).astype(int), n - 1)
        copy_rows(
            cur,
            "user_newsletter_ctr_log (user_id, news_letter_id, created_at)",
            ((int(click_users[k]), int(nl_ids[pick[k]]), wall(click_ts[k])) for k in order),
        )

        batch_rows = []
        recent_ids = nl_ids[-200:]
        for i in range(u):
            ids = rng.choice(recent_ids, size=40, replace=False).tolist()
            batch_rows.append((int(user_ids[i]), json.dumps(ids), wall(now - timedelta(hours=10))))
        copy_rows(cur, "news_letter_today_batch (user_id, news_letter_ids, created_at)", batch_rows)

        for table, col in (("news_letter", "news_letter_id"), ('"user"', "user_id"), ("category", "category_id")):
            cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), (SELECT MAX({col}) FROM {table}))")
    conn.commit()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("VACUUM ANALYZE")
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM news_letter), (SELECT COUNT(*) FROM user_newsletter_ctr_log), "
            "(SELECT COUNT(*) FROM news_letter WHERE news_letter_created_at >= "
            "      (now() - interval '72 hours')::timestamp), "
            "pg_size_pretty(pg_total_relation_size('news_letter')), "
            "pg_size_pretty(pg_total_relation_size('user_newsletter_ctr_log'))"
        )
        n_nl, n_click, n_window, nl_size, log_size = cur.fetchone()
        cur.execute(
            "SELECT user_id FROM user_newsletter_ctr_log WHERE created_at >= (now() - interval '24 hours')::timestamp "
            "GROUP BY user_id"
        )
        active = [r[0] for r in cur.fetchall()]
    conn.close()
    return {
        "seed_seconds": round(time.perf_counter() - t_seed, 1),
        "newsletters": n_nl,
        "newsletters_in_72h_window": n_window,
        "clicks": n_click,
        "users": u,
        "cold_users_without_embedding": cold_users,
        "users_active_24h": len(active),
        "heavy_users_30_clicks_24h": int(len(heavy)),
        "news_letter_size": nl_size,
        "ctr_log_size": log_size,
        "_active": active,
        "_heavy": [int(x) for x in heavy],
        "_cold": [int(x) for x in user_ids[:cold_users]],
        "_nl_ids": nl_ids,
    }


# ------------------------------------------------------------------------ explain
def explain(engine, now: datetime, sample_user: int, sample_vec: np.ndarray) -> dict:
    from sqlalchemy import text

    since72 = now - timedelta(hours=72)
    since24 = now - timedelta(hours=24)
    queries = {
        "knn_window": (
            "SELECT n.news_letter_id FROM news_letter n WHERE n.news_letter_created_at >= :since "
            "AND n.news_letter_embedding IS NOT NULL ORDER BY n.news_letter_embedding <=> :q LIMIT 100",
            {"since": since72, "q": np.ascontiguousarray(sample_vec, dtype=np.float32)},
        ),
        "short_term_avg": (
            "SELECT vector_send(AVG(t.e)) FROM (SELECT n.news_letter_embedding AS e "
            "FROM user_newsletter_ctr_log l JOIN news_letter n ON n.news_letter_id = l.news_letter_id "
            "WHERE l.user_id = :uid AND l.created_at >= :since AND n.news_letter_embedding IS NOT NULL "
            "ORDER BY l.created_at DESC LIMIT 20) t",
            {"uid": sample_user, "since": since24},
        ),
        "last_click_id": (
            "SELECT log_id FROM user_newsletter_ctr_log WHERE user_id = :uid "
            "ORDER BY created_at DESC, log_id DESC LIMIT 1",
            {"uid": sample_user},
        ),
        "window_meta": (
            "SELECT news_letter_id, news_letter_created_at::timestamptz, raw_news_count "
            "FROM news_letter WHERE news_letter_created_at >= :since",
            {"since": since72},
        ),
    }
    out = {}
    with engine.connect() as conn:
        for name, (sql, params) in queries.items():
            plan = conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), params).scalar()
            root = plan[0]
            nodes = []

            def walk(node):
                label = node["Node Type"]
                if node.get("Index Name"):
                    label += f"[{node['Index Name']}]"
                nodes.append(label)
                for child in node.get("Plans", []):
                    walk(child)

            walk(root["Plan"])
            out[name] = {"execution_ms": round(root["Execution Time"], 3), "nodes": nodes}
    return out


# --------------------------------------------------------------------------- main
def run(args) -> dict:
    rng = np.random.default_rng(args.seed)
    now = datetime.now(timezone.utc)
    dbname = f"recsys_bench_{uuid.uuid4().hex[:8]}"
    url = create_database(args.server_url, dbname)
    os.environ["DATABASE_URL"] = url
    for p in (ROOT, BACKEND):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        return _measure(args, rng, now, url)
    finally:
        if not args.keep:
            drop_database(args.server_url, dbname)


def _measure(args, rng, now, url) -> dict:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.database import register_pgvector_on_connect
    from app.recsys.config import RecsysConfig
    from app.recsys.pipeline import popular_ids
    from app.recsys.service import build_service
    from app.recsys.sql_repository import SqlRecsysRepository, sql_repo_scope, vector_from_send

    info = seed(url, args, rng, now)
    active, heavy, cold = info.pop("_active"), info.pop("_heavy"), info.pop("_cold")
    nl_ids = info.pop("_nl_ids")
    engine = create_engine(url)
    register_pgvector_on_connect(engine)
    R = args.reps
    since72, since24 = now - timedelta(hours=72), now - timedelta(hours=24)
    pick = lambda pool, k=R: [int(x) for x in rng.choice(pool, size=k)]  # noqa: E731
    result = {"seed": info, "env": {
        "machine": platform.machine(), "system": platform.system(),
        "loadavg1_start": round(os.getloadavg()[0], 2), "reps": R,
    }}

    # 1) 쿼리 단위(한 커넥션, 순차)
    q = {}
    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        repo.recent_ids(1)  # 커넥션 워밍
        profile = repo.long_term_and_categories(active[0])[0]
        window = [r[0] for r in s.execute(
            text("SELECT news_letter_id FROM news_letter WHERE news_letter_created_at >= :s"), {"s": since72}
        ).fetchall()]
        cands = window[:300]
        q["last_click_id"] = stats(timed(repo.last_click_id, pick(active)))
        q["long_term_and_categories"] = stats(timed(repo.long_term_and_categories, pick(active)))
        q["short_term_avg_active"] = stats(timed(lambda u: repo.short_term_vector(u, since24, 20), pick(active)))
        q["short_term_avg_heavy"] = stats(timed(lambda u: repo.short_term_vector(u, since24, 20), pick(heavy)))
        q["knn_100_in_window"] = stats(timed(lambda _: repo.knn_ids(profile, since72, 100), range(R)))
        q["recent_100"] = stats(timed(lambda _: repo.recent_ids(100), range(R)))
        q["popular_100(window_meta+compute_scores)"] = stats(
            timed(lambda _: popular_ids(repo, since72, now, 100), range(R))
        )
        q["category_recent_50"] = stats(timed(lambda _: repo.category_recent_ids([1, 2], since72, 50), range(R)))
        q["clicked_among_300"] = stats(timed(lambda u: repo.clicked_among(u, cands), pick(heavy)))
        q["items_300_uncached"] = stats(timed(lambda _: repo.items(cands), range(max(20, R // 10))))
        q["latest_batch"] = stats(timed(repo.latest_batch, pick(active)))
    result["query"] = q
    result["explain"] = explain(engine, now, heavy[0], profile)

    # 2) 전체 경로(service.recommend). API처럼 요청마다 앱 풀 세션 하나를 먼저 쥐고
    #    (인증 조회) 추천을 기다린다. dedicated_pool = 운영 배선(build_sql_service, 실시간
    #    경로 전용 풀), shared_pool = 실시간 경로도 앱 풀을 쓰던 이전 배선.
    def one_request(service, app_engine, uid):
        with Session(app_engine) as s:
            s.execute(text('SELECT user_id FROM "user" WHERE user_id = :u'), {"u": uid})
            return service.recommend(uid, fallback_repo=SqlRecsysRepository(s))

    def load(service, app_engine, clients, per_client):
        lat, lock, srcs, errors = [], threading.Lock(), {}, {}

        def worker(uids):
            for uid in uids:
                t0 = time.perf_counter()
                try:
                    rec = one_request(service, app_engine, uid)
                except Exception as e:  # 풀 타임아웃 등은 세고 계속 간다
                    with lock:
                        errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
                    continue
                ms = (time.perf_counter() - t0) * 1000
                with lock:
                    lat.append(ms)
                    srcs[rec.source] = srcs.get(rec.source, 0) + 1

        batches = [pick(active + cold, per_client) for _ in range(clients)]
        wall0 = time.perf_counter()
        with ThreadPoolExecutor(clients) as ex:
            list(ex.map(worker, batches))
        wall = time.perf_counter() - wall0
        return {**stats(lat), "rps": round(len(lat) / wall, 1), "sources": srcs, "errors": errors}

    from app.recsys.runtime import build_sql_service

    cfg = RecsysConfig(cache_ttl_s=0)  # 운영 기본 예산(300ms), 결과 캐시만 끈다
    levels = (1, 4, 8, 16, 32)  # FastAPI 동기 엔드포인트 스레드풀 기본값은 40
    e2e = {"time_budget_ms": cfg.time_budget_ms, "rounds": []}
    for rnd in range(args.e2e_rounds):
        # 순서 효과(캐시 온도, 러너 상태)를 줄이려고 라운드마다 배선 순서를 바꾼다.
        order = ("dedicated_pool", "shared_pool") if rnd % 2 == 0 else ("shared_pool", "dedicated_pool")
        round_rows = {}
        for wiring in order:
            # 앱 엔진은 backend/app/database.py와 같은 기본 풀(5+10). 대기 한도만 10초로 줄인다.
            app_engine = create_engine(url, pool_timeout=10)
            register_pgvector_on_connect(app_engine)
            if wiring == "dedicated_pool":
                service = build_sql_service(cfg, app_engine, url)
            else:
                service = build_service(
                    cfg, repo_factory=partial(sql_repo_scope, app_engine, cfg.time_budget_ms)
                )
            row = {}
            t0 = time.perf_counter()
            first = one_request(service, app_engine, active[1])
            row["cold_first_request_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            row["cold_first_source"] = first.source
            for clients in levels:
                per_client = R if clients == 1 else max(25, R // clients)
                row[f"{clients}_clients"] = load(service, app_engine, clients, per_client)
            row["counters"] = service.counters.snapshot()
            service.shutdown()
            app_engine.dispose()
            round_rows[wiring] = row
        e2e["rounds"].append(round_rows)
    result["end_to_end"] = e2e

    # 3) 단기 상태 저장소 비교: 같은 사용자, 같은(워밍된) 아이템 임베딩 캐시 조건.
    #    Redis/프로세스 내는 "최근 클릭 ID 목록"만 들고, 벡터는 아이템 캐시에서 평균낸다.
    with Session(engine) as s:
        rows = s.execute(text(
            "SELECT n.news_letter_id, vector_send(n.news_letter_embedding) FROM news_letter n "
            "WHERE n.news_letter_created_at >= :s"), {"s": now - timedelta(hours=24 + 72 + 1)}
        ).fetchall()
        item_vec = {r[0]: vector_from_send(r[1]) for r in rows}
        recent_clicks = s.execute(text(
            "SELECT user_id, news_letter_id, extract(epoch FROM created_at::timestamptz) FROM user_newsletter_ctr_log "
            "WHERE created_at >= :s ORDER BY created_at"), {"s": since24}
        ).fetchall()
    since_epoch = since24.timestamp()

    def mean_of(ids):
        vecs = [item_vec[i] for i in ids if i in item_vec]
        return np.mean(vecs, axis=0) if vecs else None

    st = {}
    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        st["postgres_avg_in_db(heavy)"] = stats(timed(lambda u: repo.short_term_vector(u, since24, 20), pick(heavy)))
        st["postgres_avg_in_db(active)"] = stats(timed(lambda u: repo.short_term_vector(u, since24, 20), pick(active)))

        def pg_ids(uid):
            ids = [r[0] for r in s.execute(text(
                "SELECT news_letter_id FROM user_newsletter_ctr_log WHERE user_id = :u AND created_at >= :s "
                "ORDER BY created_at DESC LIMIT 20"), {"u": uid, "s": since24}).fetchall()]
            return mean_of(ids)

        st["postgres_ids+item_cache(heavy)"] = stats(timed(pg_ids, pick(heavy)))

        def pg_click_write(uid):
            s.execute(text(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (:u, :n, :t)"),
                {"u": uid, "n": int(nl_ids[-1]), "t": datetime.now(timezone.utc)})
            s.commit()

        st["postgres_click_insert_commit(write)"] = stats(timed(pg_click_write, pick(active, max(50, R // 5))))

    local = {}
    for uid, nid, ts in recent_clicks:
        local.setdefault(uid, deque(maxlen=20)).appendleft((nid, float(ts)))

    def local_read(uid):
        return mean_of([n for n, t in local.get(uid, ()) if t >= since_epoch])

    def local_write(uid):
        local.setdefault(uid, deque(maxlen=20)).appendleft((int(nl_ids[-1]), time.time()))

    st["in_process_deque(heavy)"] = stats(timed(local_read, pick(heavy)))
    st["in_process_deque(write)"] = stats(timed(local_write, pick(active)))

    if args.redis_url:
        import redis

        r = redis.Redis.from_url(args.redis_url)
        prefix = f"recsys_bench:{uuid.uuid4().hex[:6]}:"
        pipe = r.pipeline(transaction=False)
        for uid, nid, ts in recent_clicks:
            pipe.lpush(prefix + str(uid), f"{nid}:{float(ts)}")
            pipe.ltrim(prefix + str(uid), 0, 19)
        pipe.execute()
        r.ping()

        def redis_read(uid):
            ids = []
            for raw in r.lrange(prefix + str(uid), 0, 19):
                nid, ts = raw.decode().split(":")
                if float(ts) >= since_epoch:
                    ids.append(int(nid))
            return mean_of(ids)

        def redis_write(uid):
            p = r.pipeline(transaction=True)
            p.lpush(prefix + str(uid), f"{int(nl_ids[-1])}:{time.time()}")
            p.ltrim(prefix + str(uid), 0, 19)
            p.expire(prefix + str(uid), 86400)
            p.execute()

        st["redis_list+item_cache(heavy)"] = stats(timed(redis_read, pick(heavy)))
        st["redis_lpush_ltrim_expire(write)"] = stats(timed(redis_write, pick(active, max(50, R // 5))))
        keys = list(r.scan_iter(prefix + "*"))
        for k in range(0, len(keys), 1000):
            r.delete(*keys[k : k + 1000])
    result["short_term_store"] = st
    result["env"]["loadavg1_end"] = round(os.getloadavg()[0], 2)
    engine.dispose()
    return result


def print_report(res: dict) -> None:
    print(json.dumps(res["seed"], ensure_ascii=False))
    print(json.dumps(res["env"], ensure_ascii=False))
    for section in ("query", "short_term_store"):
        print(f"\n## {section}")
        for name, s in res[section].items():
            print(f"{name:<42} p50={s['p50_ms']:8.3f}ms p95={s['p95_ms']:8.3f}ms (n={s['n']})")
    print("\n## explain")
    for name, e in res["explain"].items():
        print(f"{name:<18} exec={e['execution_ms']:.3f}ms plan={' > '.join(e['nodes'])}")
    e2e = res["end_to_end"]
    print("\n## end_to_end (budget %sms)" % e2e["time_budget_ms"])
    for i, rnd in enumerate(e2e["rounds"]):
        for wiring in ("dedicated_pool", "shared_pool"):
            e = rnd[wiring]
            print(f"[round {i} {wiring}] cold_first={e['cold_first_request_ms']}ms ({e['cold_first_source']})")
            for k, c in e.items():
                if not k.endswith("_clients"):
                    continue
                fb = sum(v for src, v in c["sources"].items() if src in ("batch", "popular", "recent", "empty"))
                print(f"   {k:<11} p50={c.get('p50_ms')} p95={c.get('p95_ms')} rps={c['rps']} "
                      f"fallback={fb}/{c['n']} errors={c['errors']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server-url", required=True, help="CREATE DATABASE 권한이 있는 Postgres(+pgvector) 서버 URL")
    ap.add_argument("--redis-url")
    ap.add_argument("--newsletters", type=int, default=30_000)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--users", type=int, default=20_000)
    ap.add_argument("--clicks", type=int, default=1_000_000)
    ap.add_argument("--click-days", type=int, default=90)
    ap.add_argument("--heavy-users", type=int, default=500)
    ap.add_argument("--reps", type=int, default=400)
    ap.add_argument("--e2e-rounds", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep", action="store_true", help="측정 후 벤치 DB를 지우지 않는다")
    ap.add_argument("--out", help="결과 JSON 경로")
    args = ap.parse_args(argv)
    res = run(args)
    print_report(res)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
