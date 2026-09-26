"""LLM 평가셋(클러스터) 층화 추출 - ADR 0009.

DB의 cluster_history(실행별 클러스터 로그)에서 클러스터를 뽑아
  - 저장소에 커밋할 매니페스트(evaluation/llm/evalsets/<name>.jsonl):
    기사 id / URL / 언론사 / 본문·제목 SHA-256만 담는다.
  - 로컬 전용 본문 파일(data/evalsets/<name>/articles.jsonl, gitignore):
    제목·본문 원문. 기사 저작권 때문에 저장소에는 절대 올리지 않는다.
로 나눠 쓴다. 층: 크기 버킷(3-4 / 5-9 / 10+) × 카테고리 × split_v2 여부. 여기에 더해
ClusterEvaluator가 FAIL을 낸 "어려운 사례"를 n개와 별도로 뽑는다(생성하지 않고 클러스터
라벨·ROC 전용, ADR 0009 A5).

사용 예:
  python -m evaluation.llm.evalset sample --name v1 --n 40 --seed 20260925 --warmup 2
  python -m evaluation.llm.evalset verify --name v1
"""

import argparse
import hashlib
import json
import logging
import random
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "evaluation" / "llm" / "evalsets"
BODIES_DIR = REPO_ROOT / "data" / "evalsets"

UNCATEGORIZED = "미분류"
SIZE_BUCKETS: Tuple[Tuple[int, Optional[int], str], ...] = ((3, 4, "3-4"), (5, 9, "5-9"), (10, None, "10+"))
# cluster_log의 클러스터 키가 아닌 부가 항목 (pipeline/stages.py가 함께 저장)
_NON_CLUSTER_KEYS = {"clustering_stats", "cluster_meta", "cluster_outcomes"}


class EvalsetIntegrityError(RuntimeError):
    """로컬 본문 파일이 매니페스트의 해시와 맞지 않는다(재수집/수정됨)."""


def size_bucket(n: int) -> Optional[str]:
    for lo, hi, name in SIZE_BUCKETS:
        if n >= lo and (hi is None or n <= hi):
            return name
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text or "").encode("utf-8")).hexdigest()


@dataclass
class ArticleRecord:
    raw_news_id: int
    url: str
    press_name: str
    title: str
    body: str


@dataclass
class ClusterCandidate:
    run_id: int
    cluster_id: int
    article_ids: List[int]
    category: str = UNCATEGORIZED
    split_v2: str = "unknown"  # "yes" / "no" / "unknown"(메타가 없던 과거 실행)
    hard_case: bool = False

    @property
    def item_id(self) -> str:
        return f"r{self.run_id}-c{self.cluster_id}"

    def stratum(self) -> Tuple[bool, Optional[str], str, str]:
        return (self.hard_case, size_bucket(len(self.article_ids)), self.category, self.split_v2)


@dataclass
class SampledCluster:
    candidate: ClusterCandidate
    split: str  # "eval" / "warmup"
    sampling_weight: float = 1.0


@dataclass
class EvalItem:
    item_id: str
    split: str
    run_id: int
    cluster_id: int
    category: str
    split_v2: str
    hard_case: bool
    size_bucket: str
    articles: List[ArticleRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# cluster_log -> 후보
# ---------------------------------------------------------------------------

def candidates_from_cluster_log(
    run_id: int,
    cluster_log,
    categories_by_cluster: Optional[Dict[int, str]] = None,
) -> List[ClusterCandidate]:
    if isinstance(cluster_log, str):
        cluster_log = json.loads(cluster_log)
    cluster_log = cluster_log or {}
    categories_by_cluster = categories_by_cluster or {}
    meta = cluster_log.get("cluster_meta") or {}
    outcomes = cluster_log.get("cluster_outcomes") or {}

    out = []
    for key, ids in cluster_log.items():
        if key in _NON_CLUSTER_KEYS or not str(key).lstrip("-").isdigit():
            continue
        cid = int(key)
        m = meta.get(str(cid)) or meta.get(cid)
        split_v2 = "unknown" if m is None else ("yes" if m.get("split_v2") else "no")
        o = outcomes.get(str(cid)) or outcomes.get(cid) or {}
        hard = (o.get("cluster_eval") or {}).get("decision") == "FAIL"
        out.append(ClusterCandidate(
            run_id=int(run_id),
            cluster_id=cid,
            article_ids=[int(x) for x in ids],
            category=categories_by_cluster.get(cid, UNCATEGORIZED),
            split_v2=split_v2,
            hard_case=hard,
        ))
    return out


# ---------------------------------------------------------------------------
# 층화 추출
# ---------------------------------------------------------------------------

def _equal_allocation(k: int, capacity: Dict[str, int], rng: random.Random) -> Dict[str, int]:
    """k개를 버킷에 최대한 균등하게 나누되 각 버킷의 가용 개수를 넘지 않게 한다.

    크기 버킷은 비례 배분하지 않는다: 10+ 클러스터는 드물지만 입력이 길어 생성
    난이도가 가장 높은 층이라 비례 배분하면 거의 뽑히지 않는다. 대신 모집단 대비
    표본 비율을 sampling_weight로 남겨 분석에서 재가중할 수 있게 한다."""
    alloc = {b: 0 for b in capacity}
    remaining = k
    while remaining > 0:
        open_buckets = [b for b in capacity if alloc[b] < capacity[b]]
        if not open_buckets:
            break
        share, extra = divmod(remaining, len(open_buckets))
        order = sorted(open_buckets)
        rng.shuffle(order)
        for i, b in enumerate(order):
            want = share + (1 if i < extra else 0)
            give = min(want, capacity[b] - alloc[b])
            alloc[b] += give
            remaining -= give
    return alloc


def _proportional_allocation(k: int, sizes: Dict[tuple, int]) -> Dict[tuple, int]:
    total = sum(sizes.values())
    if total == 0 or k == 0:
        return {s: 0 for s in sizes}
    raw = {s: k * n / total for s, n in sizes.items()}
    alloc = {s: min(int(v), sizes[s]) for s, v in raw.items()}
    # 최대 나머지(largest remainder) 방식, 동률은 층 키 순서로 결정해 재현 가능하게
    for s in sorted(sizes, key=lambda s: (-(raw[s] - int(raw[s])), s)):
        if sum(alloc.values()) >= k:
            break
        if alloc[s] < sizes[s]:
            alloc[s] += 1
    return alloc


def _draw(pool: List[ClusterCandidate], k: int, rng: random.Random, used: set) -> List[ClusterCandidate]:
    order = list(pool)
    rng.shuffle(order)
    picked = []
    for c in order:
        if len(picked) >= k:
            break
        if used & set(c.article_ids):
            continue
        picked.append(c)
        used.update(c.article_ids)
    return picked


def _draw_stratified(pool: List[ClusterCandidate], k: int, rng: random.Random, used: set) -> List[ClusterCandidate]:
    by_bucket: Dict[str, List[ClusterCandidate]] = defaultdict(list)
    for c in pool:
        by_bucket[size_bucket(len(c.article_ids))].append(c)
    alloc = _equal_allocation(k, {b: len(v) for b, v in by_bucket.items()}, rng)

    picked: List[ClusterCandidate] = []
    for bucket in sorted(by_bucket):
        members = by_bucket[bucket]
        cells: Dict[tuple, List[ClusterCandidate]] = defaultdict(list)
        for c in members:
            cells[(c.category, c.split_v2)].append(c)
        cell_alloc = _proportional_allocation(alloc[bucket], {s: len(v) for s, v in cells.items()})
        got: List[ClusterCandidate] = []
        for cell in sorted(cells):
            got.extend(_draw(cells[cell], cell_alloc[cell], rng, used))
        # 기사 중복으로 어떤 칸이 모자라면 같은 크기 버킷의 다른 칸에서 채운다
        if len(got) < alloc[bucket]:
            rest = [c for c in members if c not in got]
            got.extend(_draw(rest, alloc[bucket] - len(got), rng, used))
        picked.extend(got)

    if len(picked) < k:
        rest = [c for c in pool if c not in picked]
        picked.extend(_draw(rest, k - len(picked), rng, used))
    return picked


def stratified_sample(
    candidates: Sequence[ClusterCandidate],
    *,
    n: int,
    seed: int,
    hard_fraction: float = 0.2,
    warmup: int = 0,
    min_size: int = 3,
) -> List[SampledCluster]:
    rng = random.Random(seed)
    pool = sorted(
        (c for c in candidates if len(c.article_ids) >= min_size),
        key=lambda c: (c.run_id, c.cluster_id),
    )
    hard_pool = [c for c in pool if c.hard_case]
    regular_pool = [c for c in pool if not c.hard_case]
    used: set = set()

    # 어려운 사례(ClusterEvaluator FAIL)는 n개와 **별도로** round(n × hard_fraction)개 더 뽑는다
    # (ADR 0009 A5). 운영에서는 이런 클러스터로 뉴스레터를 만들지 않으므로 생성·발행률 표본에
    # 넣지 않고, 클러스터 라벨 + ClusterEvaluator ROC의 음성 표본으로만 쓴다.
    n_hard = min(int(round(n * hard_fraction)), len(hard_pool))
    hard = _draw_stratified(hard_pool, n_hard, rng, used)
    regular = _draw_stratified(regular_pool, n, rng, used)
    evals = hard + regular
    if len(regular) < n:
        logger.warning("평가셋 일반 클러스터 목표 %d개 중 %d개만 뽑았습니다 (후보 부족 또는 기사 중복)", n, len(regular))

    warm = _draw([c for c in regular_pool if c not in evals], warmup, rng, used)

    population = Counter(c.stratum() for c in pool)
    sampled = Counter(c.stratum() for c in evals)
    out = [SampledCluster(c, "eval", population[c.stratum()] / sampled[c.stratum()]) for c in evals]
    out += [SampledCluster(c, "warmup", 0.0) for c in warm]
    return out


# ---------------------------------------------------------------------------
# 파일 입출력
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def write_evalset(
    sample: Sequence[SampledCluster],
    articles: Dict[int, ArticleRecord],
    *,
    manifest_path: Path,
    bodies_path: Path,
    meta: dict,
) -> None:
    ordered = sorted(sample, key=lambda s: (s.split != "eval", s.candidate.run_id, s.candidate.cluster_id))
    manifest_rows, body_rows, seen = [], [], set()
    for s in ordered:
        c = s.candidate
        art_rows = []
        for aid in c.article_ids:
            a = articles.get(aid)
            if a is None:
                raise KeyError(f"기사 {aid}의 본문을 찾지 못했습니다 ({c.item_id})")
            art_rows.append({
                "raw_news_id": aid,
                "url": a.url,
                "press_name": a.press_name,
                "body_sha256": _sha256(a.body),
                "title_sha256": _sha256(a.title),
                "body_chars": len(a.body or ""),
            })
            if aid not in seen:
                seen.add(aid)
                body_rows.append({"raw_news_id": aid, "url": a.url, "press_name": a.press_name,
                                  "title": a.title, "body": a.body})
        manifest_rows.append({
            "item_id": c.item_id,
            "split": s.split,
            "run_id": c.run_id,
            "cluster_id": c.cluster_id,
            "size": len(c.article_ids),
            "size_bucket": size_bucket(len(c.article_ids)),
            "category": c.category,
            "split_v2": c.split_v2,
            "hard_case": c.hard_case,
            "sampling_weight": round(s.sampling_weight, 6),
            "articles": art_rows,
        })
    _write_jsonl(Path(manifest_path), manifest_rows)
    _write_jsonl(Path(bodies_path), body_rows)
    meta_path = Path(manifest_path).with_suffix(".meta.json")
    meta_path.write_text(json.dumps({**meta, "n_rows": len(manifest_rows)}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")


def _read_jsonl(path: Path) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_evalset(manifest_path: Path, bodies_path: Path, split: Optional[str] = None) -> List[EvalItem]:
    bodies = {int(r["raw_news_id"]): r for r in _read_jsonl(Path(bodies_path))}
    items = []
    for row in _read_jsonl(Path(manifest_path)):
        if split is not None and row["split"] != split:
            continue
        arts = []
        for a in row["articles"]:
            b = bodies.get(int(a["raw_news_id"]))
            if b is None:
                raise EvalsetIntegrityError(f"{row['item_id']}: 기사 {a['raw_news_id']} 본문이 로컬 파일에 없습니다")
            if _sha256(b["body"]) != a["body_sha256"] or _sha256(b["title"]) != a["title_sha256"]:
                raise EvalsetIntegrityError(
                    f"{row['item_id']}: 기사 {a['raw_news_id']}의 본문/제목 해시가 매니페스트와 다릅니다"
                )
            arts.append(ArticleRecord(int(a["raw_news_id"]), b["url"], b["press_name"], b["title"], b["body"]))
        items.append(EvalItem(
            item_id=row["item_id"], split=row["split"], run_id=row["run_id"], cluster_id=row["cluster_id"],
            category=row["category"], split_v2=row["split_v2"], hard_case=row["hard_case"],
            size_bucket=row["size_bucket"], articles=arts,
        ))
    return items


def default_paths(name: str) -> Tuple[Path, Path]:
    return MANIFEST_DIR / f"{name}.jsonl", BODIES_DIR / name / "articles.jsonl"


# ---------------------------------------------------------------------------
# DB 로더
# ---------------------------------------------------------------------------

def load_candidates_from_db(conn, since: Optional[str] = None, until: Optional[str] = None
                            ) -> Tuple[List[ClusterCandidate], Dict[int, ArticleRecord]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, cluster_log FROM cluster_history
            WHERE (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)
              AND (%s::timestamptz IS NULL OR created_at < %s::timestamptz)
            ORDER BY run_id
            """,
            (since, since, until, until),
        )
        runs = cur.fetchall()

        raw_logs = []
        all_ids: set = set()
        for run_id, log in runs:
            log = json.loads(log) if isinstance(log, str) else (log or {})
            raw_logs.append((run_id, log))
            for c in candidates_from_cluster_log(run_id, log):
                all_ids.update(c.article_ids)
        if not all_ids:
            return [], {}

        cur.execute(
            """
            SELECT n.raw_news_id, n.raw_news_url, p.press_name, n.raw_news_title,
                   n.raw_news_content, n.news_letter_id
            FROM news_raw n JOIN press p ON p.press_id = n.press_id
            WHERE n.raw_news_id = ANY(%s)
            """,
            (sorted(all_ids),),
        )
        articles, letter_of = {}, {}
        for rid, url, press, title, body, letter_id in cur.fetchall():
            articles[int(rid)] = ArticleRecord(int(rid), url or "", press or "", title or "", body or "")
            letter_of[int(rid)] = letter_id

        letter_ids = sorted({x for x in letter_of.values() if x is not None})
        category_of_letter: Dict[int, str] = {}
        if letter_ids:
            cur.execute(
                """
                SELECT nlc.news_letter_id, c.category_name
                FROM news_letter_categories nlc JOIN category c ON c.category_id = nlc.category_id
                WHERE nlc.news_letter_id = ANY(%s)
                ORDER BY nlc.id
                """,
                (letter_ids,),
            )
            for lid, name in cur.fetchall():
                category_of_letter.setdefault(int(lid), name)

    candidates = []
    for run_id, log in raw_logs:
        cats = {}
        for c in candidates_from_cluster_log(run_id, log):
            # 클러스터 카테고리 = 소속 기사들이 속한 뉴스레터 카테고리의 최빈값.
            # 뉴스레터가 안 만들어진(평가 실패) 클러스터는 기사가 나중 실행에서 다른
            # 뉴스레터로 묶였을 때만 카테고리가 생긴다 - 없으면 미분류.
            votes = Counter(
                category_of_letter[letter_of[a]]
                for a in c.article_ids
                if letter_of.get(a) is not None and letter_of[a] in category_of_letter
            )
            if votes:
                cats[c.cluster_id] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for c in candidates_from_cluster_log(run_id, log, cats):
            if all(a in articles for a in c.article_ids):
                candidates.append(c)
    return candidates, articles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _stratum_table(sample: Sequence[SampledCluster]) -> Dict[str, int]:
    counts = Counter("|".join(map(str, s.candidate.stratum())) for s in sample if s.split == "eval")
    return dict(sorted(counts.items()))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="DB에서 층화 추출해 매니페스트/본문 파일을 쓴다")
    p_sample.add_argument("--name", required=True)
    p_sample.add_argument("--n", type=int, default=40,
                          help="생성·라벨링할 일반 클러스터 수 (어려운 사례는 이와 별도로 n × hard-fraction개)")
    p_sample.add_argument("--seed", type=int, default=20260925)
    p_sample.add_argument("--hard-fraction", type=float, default=0.2)
    p_sample.add_argument("--warmup", type=int, default=2)
    p_sample.add_argument("--min-size", type=int, default=3)
    p_sample.add_argument("--since", help="cluster_history.created_at 하한 (ISO 날짜)")
    p_sample.add_argument("--until", help="cluster_history.created_at 상한(미포함)")
    p_sample.add_argument("--force", action="store_true", help="같은 이름의 매니페스트를 덮어쓴다")

    p_verify = sub.add_parser("verify", help="로컬 본문이 매니페스트 해시와 일치하는지 확인")
    p_verify.add_argument("--name", required=True)

    args = parser.parse_args(argv)
    manifest_path, bodies_path = default_paths(args.name)

    if args.cmd == "verify":
        items = load_evalset(manifest_path, bodies_path)
        print(f"OK: {len(items)}개 항목, 기사 {sum(len(i.articles) for i in items)}건 해시 일치")
        return 0

    if manifest_path.exists() and not args.force:
        print(f"{manifest_path}가 이미 있습니다. 평가셋은 한 번 고정되면 바꾸지 않습니다 (--force로 덮어쓰기)",
              file=sys.stderr)
        return 2

    from db.connection import get_connection, release_connection

    conn = get_connection()
    try:
        candidates, articles = load_candidates_from_db(conn, since=args.since, until=args.until)
    finally:
        release_connection(conn)

    sample = stratified_sample(candidates, n=args.n, seed=args.seed, hard_fraction=args.hard_fraction,
                               warmup=args.warmup, min_size=args.min_size)
    meta = {
        "name": args.name, "seed": args.seed, "n": args.n, "hard_fraction": args.hard_fraction,
        "warmup": args.warmup, "min_size": args.min_size, "since": args.since, "until": args.until,
        "population": len(candidates), "strata_sampled": _stratum_table(sample),
    }
    write_evalset(sample, articles, manifest_path=manifest_path, bodies_path=bodies_path, meta=meta)
    n_eval = sum(s.split == "eval" for s in sample)
    print(f"평가셋 {args.name}: eval {n_eval}개 / warmup {len(sample) - n_eval}개 (모집단 {len(candidates)}개)")
    print(f"  매니페스트(커밋 대상): {manifest_path}")
    print(f"  본문(로컬 전용, gitignore): {bodies_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
