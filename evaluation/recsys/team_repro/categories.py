"""뉴스레터(newsletters_export.csv, 195건) -> category_id 복원.

실제 DB 스냅샷이 없으므로(팀 아카이브에는 news_letter_categories 조인 결과가 남아있지
않다), 세 가지 소스를 우선순위대로 사용해 카테고리를 복원한다.

    (a) JSON export(data/team_archive/newsletters/*.json)의 "categories" 필드를
        제목(news_letter_title) 정확 일치로 매칭.
    (b) synthetic_onboarding_presentation_log.csv: 각 행이 (user_id, category_id,
        shown_news_letter_ids)를 담고 있고, shown_news_letter_ids는 그 카테고리를
        선택한 유저에게 "그 카테고리의 추천 후보"로 노출된 뉴스레터이므로, 노출된
        newsletter는 해당 category_id에 속한다고 간주한다.
    (c) 위 두 소스 어느 쪽으로도 라벨을 얻지 못한 나머지는 BGE-M3 임베딩 공간에서의
        코사인 유사도 k-NN으로 분류한다. k는 직접 라벨셋에 대한 leave-one-out
        교차검증으로 선택한다.

카테고리 ID 체계 - 중요한 발견
-------------------------------
팀 코드베이스에는 카테고리 id<->이름 매핑이 "두 가지" 존재한다.

  (1) ai_workspace/db/schema.py의 insert_initial_category_data():
      [('정치',1), ('사회',2), ('경제',3), ('IT/과학',4), ('생활/문화',5), ('스포츠',6), ('세계',7)]
      category 테이블을 TRUNCATE ... RESTART IDENTITY 후 이 순서로 삽입하므로, 실제
      라이브 DB에서 자동증가 category_id는 이 순서로 배정된다(batch_manager.py가
      `SELECT category_id FROM category WHERE category_name = %s`로 이름->id 조회).

  (2) ai_workspace/recommend_engine/config/config.yaml의 `categories:` 맵:
      1:정치, 2:경제, 3:IT/과학, 4:사회, 5:생활/문화, 6:스포츠, 7:세계
      (2/3/4번이 (1)과 다르다). 코드베이스를 grep하면 이 config 값 자체는 recsys
      코드 어디서도 읽히지 않는다(config['categories']를 참조하는 코드가 없다) - 즉
      recommend_engine 안에서는 동작에 영향이 없는 "죽은 설정"이다.

  그런데 이 스크립트가 실제로 써야 하는 건 (1)도 (2)도 아니라, 합성 데이터셋
  생성기(이선진)가 synthetic_user_preferred_categories.csv /
  synthetic_onboarding_presentation_log.csv의 category_id 컬럼을 채울 때 실제로
  사용한 체계다 - 이건 라이브 DB 없이 만들어진 별도 산출물이라 (1)과 같다는 보장이
  없다. JSON export(제목 매칭, 카테고리 "이름")와 onboarding 로그(카테고리 "id")가
  겹치는 5건(news_letter_id 155/187/189/190/191)에서 실측 비교한 결과:

    - schema.py 순서로 onboarding category_id를 이름으로 변환 -> 5건 중 5건이
      JSON 이름과 불일치 (0/5 일치, 우연이라기엔 너무 낮다).
    - config.yaml 순서로 변환 -> 5건 중 3건이 정확히 일치, 나머지 2건도
      (정치 vs 경제 - 관세 기사, 사회 vs 생활/문화 - 쇼핑 행사) 라벨링 경계선상의
      합리적인 이견이지 숫자 체계 오류로 보이지 않는다 (evaluation/recsys/team_repro/
      test 스크립트나 아래 direct_labels 비교로 재현 가능).

  결론: 합성 데이터셋의 category_id는 config.yaml 순서를 따른다고 보는 편이 훨씬
  근거가 강하다. 이 스크립트는 (2) config.yaml 순서를 정본으로 채택한다.
  news_letter_categories/user_preferred_categories가 서로 다른 숫자 체계를 쓰면
  category_match_count 피처와 Coverage@k 지표 자체가 의미를 잃으므로, "무엇이
  실제 라이브 DB의 체계였는가"보다 "합성 CSV들 내부에서 서로 정합적인가"가 더
  중요하다. (1) schema.py 체계가 실제 배포판에서 쓰였다면, 합성 데이터를 그
  DB에 그대로 적재했을 때 카테고리 feature가 2/7 확률로 조용히 틀어졌을 것이라는
  뜻이기도 하다 - 이것도 별도의 문서화할 결함이다.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# 경로 / 상수
# ---------------------------------------------------------------------------

# data/team_archive는 .gitignore 대상이며 메인 체크아웃에만 존재한다(워크트리에는 없음).
# 워크트리에서 실행해도 항상 메인 체크아웃의 데이터를 가리키도록 절대경로로 고정한다.
MAIN_CHECKOUT_ROOT = Path("/Users/brownee/Projects/newsletter-recsys")
DATA_ROOT = MAIN_CHECKOUT_ROOT / "data" / "team_archive"
SYNTH_DIR = DATA_ROOT / "synthetic_dataset"
NEWSLETTERS_DIR = DATA_ROOT / "newsletters"
EMB_DIR = DATA_ROOT / "embeddings"
DEFAULT_OUT = DATA_ROOT / "derived" / "newsletter_categories.csv"

# config.yaml `categories:` 순서를 정본으로 채택한다 (모듈 docstring의 "카테고리 ID
# 체계 - 중요한 발견" 참고: 합성 데이터셋 생성기가 실제로 사용한 체계로 보인다).
# schema.py 순서(정치=1,사회=2,경제=3,IT/과학=4,...)는 라이브 DB 자동증가 순서이지만,
# 이 합성 CSV들과는 다른 체계로 보인다 - CATEGORY_ID_TO_NAME_SCHEMA_PY로 남겨둔다.
CATEGORY_ID_TO_NAME: Dict[int, str] = {
    1: "정치",
    2: "경제",
    3: "IT/과학",
    4: "사회",
    5: "생활/문화",
    6: "스포츠",
    7: "세계",
}
CATEGORY_ID_TO_NAME_SCHEMA_PY: Dict[int, str] = {
    1: "정치",
    2: "사회",
    3: "경제",
    4: "IT/과학",
    5: "생활/문화",
    6: "스포츠",
    7: "세계",
}
CATEGORY_NAME_TO_ID: Dict[str, int] = {v: k for k, v in CATEGORY_ID_TO_NAME.items()}
CANONICAL_NAMES = set(CATEGORY_NAME_TO_ID)

# newsletters_0130/0131/0201/22 만 newsletters_export.csv와 제목이 겹친다(직접 확인).
# newsletter_share.json은 2026-01-26 exported_at으로 이 합성 데이터셋(01-30~01-31)과
# 무관한 이전 개발 단계 산출물이라 제목이 하나도 겹치지 않아 제외한다.
JSON_EXPORT_FILES = [
    "newsletters_0130.json",
    "newsletters_0131.json",
    "newsletters_0201.json",
    "newsletters_22.json",
]


@dataclass
class Newsletter:
    news_letter_id: int
    title: str


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------

def load_newsletters(export_csv: Path = SYNTH_DIR / "newsletters_export.csv") -> List[Newsletter]:
    csv.field_size_limit(sys.maxsize)
    with open(export_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = [Newsletter(int(r["news_letter_id"]), r["news_letter_title"]) for r in rows]
    out.sort(key=lambda n: n.news_letter_id)
    return out


def load_embeddings(emb_dir: Path = EMB_DIR) -> Tuple[Dict[int, np.ndarray], dict]:
    meta = json.loads((emb_dir / "newsletters_bge_m3.meta.json").read_text(encoding="utf-8"))
    matrix = np.load(emb_dir / "newsletters_bge_m3.npy")
    ids = meta["ids"]
    if matrix.shape[0] != len(ids):
        raise ValueError(f"임베딩 행 수({matrix.shape[0]})와 meta ids 수({len(ids)})가 다릅니다.")
    return {int(nid): matrix[i] for i, nid in enumerate(ids)}, meta


def _title_to_categories_from_json(json_dir: Path = NEWSLETTERS_DIR) -> Dict[str, List[str]]:
    """title -> 그 JSON row가 가진 categories 리스트(중복 title은 마지막 값으로 덮어씀.
    실제로 195건 대상 직접 매칭에서는 충돌이 없음을 확인했다)."""
    title_cats: Dict[str, List[str]] = {}
    for fn in JSON_EXPORT_FILES:
        path = json_dir / fn
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data:
            title = row.get("news_letter_title") or row.get("title")
            cats = row.get("categories") or []
            if title:
                title_cats[title] = cats
    return title_cats


def direct_labels_from_json(
    newsletters: List[Newsletter], json_dir: Path = NEWSLETTERS_DIR
) -> Dict[int, int]:
    """제목 정확 일치 + categories 리스트 중 정본(7개) 이름이 정확히 1개일 때만 채택.
    (여러 개면 어느 게 대표 카테고리인지 알 수 없어 kNN에 넘긴다.)"""
    title_cats = _title_to_categories_from_json(json_dir)
    labels: Dict[int, int] = {}
    for nl in newsletters:
        cats = title_cats.get(nl.title)
        if cats is None:
            continue
        canonical = [c for c in cats if c in CANONICAL_NAMES]
        # 중복 제거하되 순서 보존
        canonical = list(dict.fromkeys(canonical))
        if len(canonical) == 1:
            labels[nl.news_letter_id] = CATEGORY_NAME_TO_ID[canonical[0]]
    return labels


def direct_labels_from_onboarding(
    onboarding_csv: Path = SYNTH_DIR / "synthetic_onboarding_presentation_log.csv",
) -> Dict[int, int]:
    """(user_id, category_id, shown_news_letter_ids) 로그: 노출된 뉴스레터는 그
    category_id에 속한다. 같은 news_letter_id가 서로 다른 category_id 아래 노출되면
    (충돌) 라벨을 채택하지 않고 kNN로 넘긴다."""
    nid_to_cats: Dict[int, set] = defaultdict(set)
    with open(onboarding_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cat_id = int(row["category_id"])
            ids = [int(x) for x in row["shown_news_letter_ids"].split("|") if x]
            for nid in ids:
                nid_to_cats[nid].add(cat_id)
    labels: Dict[int, int] = {}
    conflicts = 0
    for nid, cats in nid_to_cats.items():
        if len(cats) == 1:
            labels[nid] = next(iter(cats))
        else:
            conflicts += 1
    if conflicts:
        print(f"  [onboarding] 카테고리 충돌로 제외된 뉴스레터: {conflicts}건", file=sys.stderr)
    return labels


def combine_direct_labels(
    json_labels: Dict[int, int], onboarding_labels: Dict[int, int]
) -> Tuple[Dict[int, int], Dict[int, str], int]:
    """두 직접 라벨 소스를 합친다. 온보딩 로그는 DB의 category_id를 그대로 쓰는
    구조적 신호라 제목 매칭(JSON)보다 신뢰도가 높다고 보고, 충돌 시 온보딩을
    우선한다. 합치기 전에 두 소스가 겹치는 뉴스레터에서 실제로 얼마나 일치하는지
    세어 report에 남긴다."""
    overlap_ids = set(json_labels) & set(onboarding_labels)
    agree = sum(1 for nid in overlap_ids if json_labels[nid] == onboarding_labels[nid])
    disagree = len(overlap_ids) - agree
    if disagree:
        print(
            f"  [direct] JSON-제목 라벨과 온보딩 라벨이 겹치는 {len(overlap_ids)}건 중 "
            f"{disagree}건 불일치 (온보딩 우선 채택)",
            file=sys.stderr,
        )

    combined: Dict[int, int] = dict(json_labels)
    source: Dict[int, str] = {nid: "json_title" for nid in json_labels}
    for nid, cat in onboarding_labels.items():
        combined[nid] = cat  # 온보딩이 최종 우선
        source[nid] = "onboarding_log" if nid not in json_labels or json_labels[nid] == cat else "onboarding_log(override)"
    return combined, source, disagree


# ---------------------------------------------------------------------------
# kNN 분류 (코사인 유사도, BGE-M3)
# ---------------------------------------------------------------------------

def _cosine_sim_matrix(query: np.ndarray, bank: np.ndarray) -> np.ndarray:
    """query: (n_q, d), bank: (n_b, d) 모두 L2 정규화되어 있다고 가정
    (meta.json: l2_normalized=true) -> (n_q, n_b) 코사인 유사도."""
    return query @ bank.T


def knn_predict_one(
    query_vec: np.ndarray,
    bank_vecs: np.ndarray,
    bank_labels: List[int],
    k: int,
    exclude_idx: Optional[int] = None,
) -> Tuple[int, float]:
    sims = bank_vecs @ query_vec
    if exclude_idx is not None:
        sims = sims.copy()
        sims[exclude_idx] = -np.inf
    order = np.argsort(-sims)[:k]
    top_labels = [bank_labels[i] for i in order]
    top_sims = sims[order]
    counts = Counter(top_labels)
    best_label, best_count = counts.most_common(1)[0]
    # 신뢰도: k-NN 중 최다 득표 카테고리의 득표 비율
    confidence = best_count / k
    return best_label, float(confidence)


def loo_accuracy_for_k(
    bank_vecs: np.ndarray, bank_labels: List[int], k: int
) -> float:
    n = len(bank_labels)
    if n <= k:
        return float("nan")
    correct = 0
    for i in range(n):
        pred, _ = knn_predict_one(bank_vecs[i], bank_vecs, bank_labels, k, exclude_idx=i)
        if pred == bank_labels[i]:
            correct += 1
    return correct / n


def choose_k(
    bank_vecs: np.ndarray, bank_labels: List[int], k_grid: List[int]
) -> Tuple[int, Dict[int, float]]:
    scores = {}
    for k in k_grid:
        if k >= len(bank_labels):
            continue
        scores[k] = loo_accuracy_for_k(bank_vecs, bank_labels, k)
    if not scores:
        raise ValueError("직접 라벨 수가 너무 적어 k-NN 후보 k를 하나도 평가할 수 없습니다.")
    best_k = max(scores, key=lambda k: (scores[k], -k))  # 동점이면 작은 k
    return best_k, scores


# ---------------------------------------------------------------------------
# 메인 파이프라인
# ---------------------------------------------------------------------------

@dataclass
class CategoryRecoveryResult:
    rows: List[dict]  # news_letter_id, category_id, category_name, source, confidence
    loo_scores: Dict[int, float]
    chosen_k: int
    source_counts: Counter


def recover_categories(
    export_csv: Path = SYNTH_DIR / "newsletters_export.csv",
    json_dir: Path = NEWSLETTERS_DIR,
    onboarding_csv: Path = SYNTH_DIR / "synthetic_onboarding_presentation_log.csv",
    emb_dir: Path = EMB_DIR,
    k_grid: Optional[List[int]] = None,
) -> CategoryRecoveryResult:
    if k_grid is None:
        k_grid = [1, 3, 5, 7, 9, 11, 15, 21, 31]

    newsletters = load_newsletters(export_csv)
    embeddings, _meta = load_embeddings(emb_dir)

    json_labels = direct_labels_from_json(newsletters, json_dir)
    onboarding_labels = direct_labels_from_onboarding(onboarding_csv)
    direct, source, _disagree = combine_direct_labels(json_labels, onboarding_labels)

    # 임베딩이 없는 뉴스레터는 kNN을 못 돌리므로 별도 처리(0 벡터 재임베딩 실패 등).
    missing_emb = [nl.news_letter_id for nl in newsletters if nl.news_letter_id not in embeddings]
    if missing_emb:
        print(f"  [경고] 임베딩이 없는 뉴스레터 {len(missing_emb)}건: {missing_emb[:10]}...", file=sys.stderr)

    direct_ids = [nid for nid in direct if nid in embeddings]
    bank_vecs = np.stack([embeddings[nid] for nid in direct_ids])
    bank_labels = [direct[nid] for nid in direct_ids]

    chosen_k, loo_scores = choose_k(bank_vecs, bank_labels, k_grid)

    rows = []
    source_counts = Counter()
    for nl in newsletters:
        nid = nl.news_letter_id
        if nid in direct:
            cat_id = direct[nid]
            rows.append(
                {
                    "news_letter_id": nid,
                    "category_id": cat_id,
                    "category_name": CATEGORY_ID_TO_NAME[cat_id],
                    "source": source[nid],
                    "confidence": 1.0,
                }
            )
            source_counts[source[nid].split("(")[0]] += 1
        elif nid in embeddings:
            pred, conf = knn_predict_one(embeddings[nid], bank_vecs, bank_labels, chosen_k)
            rows.append(
                {
                    "news_letter_id": nid,
                    "category_id": pred,
                    "category_name": CATEGORY_ID_TO_NAME[pred],
                    "source": "knn",
                    "confidence": conf,
                }
            )
            source_counts["knn"] += 1
        else:
            rows.append(
                {
                    "news_letter_id": nid,
                    "category_id": None,
                    "category_name": None,
                    "source": "unlabeled_no_embedding",
                    "confidence": 0.0,
                }
            )
            source_counts["unlabeled_no_embedding"] += 1

    return CategoryRecoveryResult(rows=rows, loo_scores=loo_scores, chosen_k=chosen_k, source_counts=source_counts)


def save_csv(result: CategoryRecoveryResult, out_path: Path = DEFAULT_OUT) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["news_letter_id", "category_id", "category_name", "source", "confidence"]
        )
        writer.writeheader()
        for row in sorted(result.rows, key=lambda r: r["news_letter_id"]):
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    result = recover_categories()
    save_csv(result, args.out)

    print("=" * 60)
    print("카테고리 복원 결과")
    print("=" * 60)
    print(f"전체 뉴스레터: {len(result.rows)}건")
    for src, cnt in result.source_counts.most_common():
        print(f"  - {src}: {cnt}건")
    print(f"\nk-NN k 후보별 LOO 정확도 (직접 라벨 {sum(1 for r in result.rows if r['source'] != 'knn' and r['source'] != 'unlabeled_no_embedding')}건 대상):")
    for k, acc in sorted(result.loo_scores.items()):
        marker = "  <- 선택" if k == result.chosen_k else ""
        print(f"  k={k}: {acc:.4f}{marker}")
    print(f"\n저장 위치: {args.out}")


if __name__ == "__main__":
    main()
