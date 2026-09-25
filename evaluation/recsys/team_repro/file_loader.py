"""팀 아카이브(CSV/JSON/npy) 기반 파일 DataLoader.

목표: `ai_workspace/recommend_engine/src/data/data_loader.py`의 `DataLoader`가 실제
DB에서 만들어내는 것과 "같은 모양"의 객체(NewsItem 딕셔너리, ctr 로그 DataFrame, 유저
목록, 선호 카테고리 DataFrame)를 파일에서 만들어, FeatureEngineer/LGBMDataset/
LGBMRanker/MMRReranker/Evaluator 등 recommend_engine의 실제 코드를 "고치지 않고"
그대로 실행할 수 있게 한다.

버전 적응 전략
--------------
team-final / fix-snapshot / current(port/july-self-review) 세 버전 모두 DataLoader가
DB에 접근하는 지점은 정확히 다음 다섯 곳뿐이다(세 버전에서 시그니처가 동일함을
직접 diff로 확인했다):

    - load_embedded_news()               -> {news_id: NewsItem}
    - load_ctr_logs(split=None)          -> DataFrame[user_id, news_letter_id, timestamp]
    - load_user_preferred_categories()   -> DataFrame[user_id, category_id]
    - get_all_news_ids() / get_all_user_ids()
    - build_user_profiles() 내부에서 딱 한 번, `self._load_from_db('SELECT user_id
      FROM "user"')`를 직접 호출한다 (세 버전 모두 문자열이 완전히 동일).

`build_user_profiles()` 자체는 오버라이드하지 않는다 - 세 버전이 이 메서드 안에서
실제로 다른 일을 하기 때문이다(team-final/fix-snapshot은 전역 history_embedding을
만들고, current는 `compute_history_embedding()`을 point-in-time으로 호출할 수 있는
형태로 노출한다). 이 메서드를 그대로 상속하게 두고, 그 안에서 호출하는 DB 접근
메서드들만 파일 기반으로 바꾸면 각 버전의 실제 동작 차이가 "코드 그대로" 재현된다.

시간 고정(freeze_datetime_now)
-------------------------------
`build_user_profiles()`는 세 버전 모두 내부에서 `now = datetime.now()`를 직접
호출한다(팀 코드가 실서비스 "지금"을 가정하기 때문). 이 값은 team-final/
fix-snapshot에서는 실제로 history_cosine_similarity에 쓰이는 leakage 버그의 근원이고,
current에서는 (create_features가 항상 명시적 timestamps를 넘기므로) 사실상 죽은
값이지만 그래도 재현성을 위해 고정해야 한다. recommend_engine 소스는 수정하지
않으므로, 각 버전의 `src.data.data_loader` 모듈 객체 안의 `datetime` 이름 자체를
monkeypatch해 `datetime.now()`가 고정된 시각을 반환하게 만든다(freeze_datetime_now).
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

# data/team_archive는 gitignore 대상이며 메인 체크아웃에만 존재한다.
MAIN_CHECKOUT_ROOT = Path("/Users/brownee/Projects/newsletter-recsys")
DATA_ROOT = MAIN_CHECKOUT_ROOT / "data" / "team_archive"
SYNTH_DIR = DATA_ROOT / "synthetic_dataset"
EMB_DIR = DATA_ROOT / "embeddings"
DERIVED_DIR = DATA_ROOT / "derived"
CATEGORIES_CSV = DERIVED_DIR / "newsletter_categories.csv"

EMBEDDING_DIM = 1024


class LabelMode(str, Enum):
    """decomposition (d): 클릭 라벨을 어떻게 해석할지.

    실제 DB 테이블 user_newsletter_ctr_log에는 is_clicked 컬럼이 없고, recsys의
    DataLoader.load_ctr_logs()는 그 테이블의 모든 행을 그냥 '클릭'으로 읽는다(WHERE
    절도 없음). 합성 CSV(synthetic_ctr_logs.csv)에는 is_clicked 컬럼이 남아있지만,
    이게 실제로 DB에 어떻게 적재됐는지(1인 행만 넣었는지, 전부 넣었는지)는 알 수
    없다 - 로더 스크립트가 접근 불가능한 Notion 페이지에만 있다.
    """

    CLICKS_ONLY = "clicks_only"  # is_clicked == 1 인 행만 '클릭 로그'로 사용 (1차 가정)
    ALL_ROWS = "all_rows"  # 모든 행(0/1 불문)을 클릭으로 사용 (실제 DataLoader.load_ctr_logs와 동일한 동작)


class SplitProtocol(str, Enum):
    TEAM = "team_split"  # main_lgbm.py의 동적 시간순 분할 (validation_ratio=0.2)
    GENERATOR = "generator_split"  # 생성기 자체 분할 (ctr_logs_train.csv/valid.csv)


@dataclass
class ArchiveBundle:
    """팀 아카이브 원본 데이터. 버전에 무관하게 한 번만 로드해서 재사용한다."""

    newsletters: pd.DataFrame  # news_letter_id, title, content, created_at, embedding(object=np.ndarray)
    categories: pd.DataFrame  # news_letter_id, category_id, source, confidence
    ctr_logs: pd.DataFrame  # user_id, news_letter_id, timestamp, is_clicked
    users: pd.DataFrame  # user_id
    preferred_categories: pd.DataFrame  # user_id, category_id
    preferred_newsletters: pd.DataFrame  # user_id, news_letter_id (405건, "온보딩 선택 뉴스레터")
    onboarding_log: pd.DataFrame  # user_id, category_id, shown_news_letter_ids(raw str)
    generator_train_keys: pd.DataFrame  # user_id, news_letter_id, timestamp (ctr_logs_train.csv)
    generator_valid_keys: pd.DataFrame  # user_id, news_letter_id, timestamp (ctr_logs_valid.csv)

    @property
    def dataset_end_time(self) -> pd.Timestamp:
        return self.ctr_logs["timestamp"].max()

    @property
    def dataset_start_time(self) -> pd.Timestamp:
        return self.ctr_logs["timestamp"].min()


def _read_csv(path: Path) -> pd.DataFrame:
    csv.field_size_limit(sys.maxsize)
    return pd.read_csv(path)


def load_archive_bundle() -> ArchiveBundle:
    if not CATEGORIES_CSV.exists():
        raise FileNotFoundError(
            f"{CATEGORIES_CSV} 가 없습니다. 먼저 `python evaluation/recsys/team_repro/categories.py`를 실행하세요."
        )

    nl_df = _read_csv(SYNTH_DIR / "newsletters_export.csv")
    nl_df = nl_df.rename(
        columns={
            "news_letter_id": "news_letter_id",
            "news_letter_title": "title",
            "news_letter_content": "content",
            "news_letter_created_at": "created_at",
        }
    )
    nl_df["created_at"] = pd.to_datetime(nl_df["created_at"], format="mixed")
    nl_df = nl_df[["news_letter_id", "title", "content", "created_at"]].drop_duplicates(
        subset=["news_letter_id"]
    )

    meta = json.loads((EMB_DIR / "newsletters_bge_m3.meta.json").read_text(encoding="utf-8"))
    matrix = np.load(EMB_DIR / "newsletters_bge_m3.npy")
    emb_by_id = {int(nid): matrix[i].astype(np.float32) for i, nid in enumerate(meta["ids"])}
    nl_df["embedding"] = nl_df["news_letter_id"].map(emb_by_id)
    missing_emb = nl_df["embedding"].isna().sum()
    if missing_emb:
        raise ValueError(f"{missing_emb}건의 뉴스레터에 임베딩이 없습니다 (categories.py/embed_newsletters.py 확인).")

    cat_df = _read_csv(CATEGORIES_CSV)

    ctr_df = _read_csv(SYNTH_DIR / "synthetic_ctr_logs.csv")
    ctr_df["timestamp"] = pd.to_datetime(ctr_df["created_at"])
    ctr_df = ctr_df[["user_id", "news_letter_id", "timestamp", "is_clicked"]]

    users_df = _read_csv(SYNTH_DIR / "synthetic_user.csv")[["user_id"]].drop_duplicates()

    pref_cat_df = _read_csv(SYNTH_DIR / "synthetic_user_preferred_categories.csv")
    pref_nl_df = _read_csv(SYNTH_DIR / "synthetic_user_preferred_newsletters.csv")
    onboarding_df = _read_csv(SYNTH_DIR / "synthetic_onboarding_presentation_log.csv")

    gen_train = _read_csv(SYNTH_DIR / "ctr_logs_train.csv")
    gen_train["timestamp"] = pd.to_datetime(gen_train["created_at"])
    gen_valid = _read_csv(SYNTH_DIR / "ctr_logs_valid.csv")
    gen_valid["timestamp"] = pd.to_datetime(gen_valid["created_at"])

    return ArchiveBundle(
        newsletters=nl_df.reset_index(drop=True),
        categories=cat_df,
        ctr_logs=ctr_df.reset_index(drop=True),
        users=users_df.reset_index(drop=True),
        preferred_categories=pref_cat_df,
        preferred_newsletters=pref_nl_df,
        onboarding_log=onboarding_df,
        generator_train_keys=gen_train[["user_id", "news_letter_id", "timestamp", "is_clicked"]],
        generator_valid_keys=gen_valid[["user_id", "news_letter_id", "timestamp", "is_clicked"]],
    )


def bundle_before(bundle: ArchiveBundle, cutoff: datetime) -> ArchiveBundle:
    """v2 REQUIRED CHANGE #1: point-in-time 추론용 - ctr_logs를 cutoff '이전'으로만
    엄격히 잘라낸 새 번들을 만든다.

    current/fix-snapshot은 자체 point-in-time cutoff 로직(compute_history_embedding)이
    있어 이 없이도 eval_timestamp만으로 안전하지만, team-final은 그 메서드 자체가
    없어 build_user_profiles()가 로더가 반환하는 로그를 시간 필터 없이 전부 쓴다
    (모듈 docstring/challenge 리뷰 참고) - 즉 team-final의 히스토리 누출은 "지금이
    언제인지"가 아니라 "로더가 무슨 로그를 돌려주는지"에서 생긴다. 그래서 세 버전
    모두에 안전하게 먹히는 유일한 fix는 로더 데이터 자체를 cutoff 이전으로
    잘라내는 것이다.
    """
    from dataclasses import replace

    restricted = bundle.ctr_logs[bundle.ctr_logs["timestamp"] < cutoff].reset_index(drop=True)
    return replace(bundle, ctr_logs=restricted)


def bundle_for_generator_split_training(bundle: ArchiveBundle) -> ArchiveBundle:
    """v2 REQUIRED CHANGE #3 (generator_split): 학습에 쓰이는 로그 자체를
    `ctr_logs_train.csv`(뉴스레터 id 4~154)로 제한한 번들을 만든다.

    v1은 이 프로토콜에서 정답만 valid 파일(155~198)에서 가져오고 실제 학습은
    여전히 synthetic_ctr_logs.csv 전체(4,400개 상호작용, valid 뉴스레터도 포함)로
    했다 - 'cold item 평가'라는 설명과 달리 모델이 정답 아이템을 이미 학습에서
    봤다(BLOCKER). generator_train_keys만 ctr_logs로 넘기면 build_user_profiles/
    create_train_dataset이 구조적으로 valid 뉴스레터의 상호작용을 전혀 보지 못한다.
    """
    from dataclasses import replace

    train_logs = bundle.generator_train_keys[["user_id", "news_letter_id", "timestamp", "is_clicked"]]
    return replace(bundle, ctr_logs=train_logs.reset_index(drop=True))


def category_ids_by_newsletter(bundle: ArchiveBundle) -> Dict[int, List[int]]:
    out: Dict[int, List[int]] = {}
    for row in bundle.categories.itertuples(index=False):
        out[int(row.news_letter_id)] = [int(row.category_id)]
    return out


def freeze_datetime_now(module: ModuleType, pinned_dt: datetime) -> None:
    """`module`의 전역 이름 `datetime`을, `.now()`/`.utcnow()`만 pinned_dt를 반환하고
    나머지는 실제 datetime과 동일하게 동작하는 클래스로 바꿔친다.

    recommend_engine 소스는 건드리지 않는다 - 모듈 네임스페이스의 바인딩만 evaluation/
    쪽에서 교체하는, 표준적인 시간 고정(freeze time) 기법이다. main_lgbm.py 등 다른
    모듈은 건드리지 않고, 실제로 `datetime.now()`가 결과에 영향을 주는
    `src.data.data_loader` 모듈에만 적용한다.
    """

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned_dt if tz is None else pinned_dt.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return pinned_dt

    module.datetime = _FrozenDateTime  # type: ignore[attr-defined]


def _make_news_dict(NewsItemCls, bundle: ArchiveBundle, category_map: Dict[int, List[int]]):
    news_dict = {}
    for row in bundle.newsletters.itertuples(index=False):
        nid = int(row.news_letter_id)
        news_dict[nid] = NewsItemCls(
            news_id=nid,
            title=row.title,
            content=row.content,
            category_ids=category_map.get(nid, []),
            embedding=np.asarray(row.embedding, dtype=np.float32),
            timestamp=row.created_at,
        )
    return news_dict


def build_file_data_loader(
    DataLoaderCls,
    NewsItemCls,
    config: dict,
    bundle: ArchiveBundle,
    pinned_now: datetime,
    label_mode: LabelMode = LabelMode.CLICKS_ONLY,
    candidate_pool_ids: Optional[Sequence[int]] = None,
    history_leakage_mode: str = "fixed",
):
    """`DataLoaderCls`(각 코드 버전의 실제 `DataLoader` 클래스)를 상속하는 파일 기반
    서브클래스를 즉석에서 만들어 인스턴스를 반환한다.

    Args:
        candidate_pool_ids: 지정하면 get_all_news_ids()가 이 부분집합만 반환한다
            (decomposition (b): 추론 후보 풀 실험). None이면 195건 전체.
        history_leakage_mode: "fixed"(기본) 또는 "leaky". current/fix-snapshot 코드에만
            의미가 있다 - compute_history_embedding()이 존재하는 버전에서 "leaky"를
            주면 cutoff_time을 무시하고 항상 pinned_now 기준 전체 로그로 계산해
            team-final의 유출 버그를 현재 파이프라인(lambdarank+point-in-time 후보풀)
            위에서 그대로 재현한다 (decomposition (a): leakage 단일 변수 통제 실험).
            team-final처럼 compute_history_embedding 메서드가 아예 없는 버전에서는
            무시된다(그 버전 자체가 이미 "leaky").
    """
    category_map = category_ids_by_newsletter(bundle)
    news_dict = _make_news_dict(NewsItemCls, bundle, category_map)

    ctr_logs = bundle.ctr_logs
    if label_mode == LabelMode.CLICKS_ONLY:
        ctr_logs = ctr_logs[ctr_logs["is_clicked"] == 1]
    # LabelMode.ALL_ROWS: 실제 DataLoader.load_ctr_logs()와 동일하게 모든 행을 그대로 '클릭'으로 취급
    ctr_logs = ctr_logs[["user_id", "news_letter_id", "timestamp"]].reset_index(drop=True)

    max_history_days = config.get("data", {}).get("max_history_days")

    users_df = bundle.users
    pref_cat_df = bundle.preferred_categories

    all_ids_default = list(news_dict.keys())

    class FileDataLoader(DataLoaderCls):  # type: ignore[misc,valid-type]
        def __init__(self):  # noqa: D401 - intentionally skip DB engine setup
            self.config = config
            self.engine = None
            self._news_dict = None
            self._user_profiles = None
            self._candidate_pool_ids = list(candidate_pool_ids) if candidate_pool_ids is not None else None

        # -- DB 접근 메서드 오버라이드 --------------------------------------
        def _load_from_db(self, query: str, params: dict = None):
            if '"user"' in query:
                return users_df[["user_id"]].copy()
            raise NotImplementedError(
                f"FileDataLoader: 예상치 못한 직접 _load_from_db 호출: {query[:120]!r}"
            )

        def load_embedded_news(self):
            if self._news_dict is not None:
                return self._news_dict
            self._news_dict = dict(news_dict)
            return self._news_dict

        def load_ctr_logs(self, split: str = None):
            df = ctr_logs
            if max_history_days:
                cutoff = pinned_now - timedelta(days=max_history_days)
                df = df[df["timestamp"] >= cutoff]
            return df.copy()

        def load_user_preferred_categories(self):
            return pref_cat_df[["user_id", "category_id"]].copy()

        def get_all_news_ids(self):
            if self._candidate_pool_ids is not None:
                return list(self._candidate_pool_ids)
            return list(self.load_embedded_news().keys())

        def get_all_user_ids(self):
            return users_df["user_id"].tolist()

        def set_candidate_pool(self, ids: Optional[Sequence[int]]):
            self._candidate_pool_ids = list(ids) if ids is not None else None

    # compute_history_embedding()이 있는 버전(current/fix-snapshot)에서만 leaky 모드를 건다.
    if history_leakage_mode == "leaky" and hasattr(DataLoaderCls, "compute_history_embedding"):

        def _leaky_compute_history_embedding(self, user_id, cutoff_time, logs_df, news_dict):
            # cutoff_time을 무시하고 항상 pinned_now를 써서, team-final의 전역(미래 포함)
            # history_embedding 버그를 현재 파이프라인 위에서 재현한다. 원본
            # compute_history_embedding()이 build_user_profiles() 안에서 키워드 인자로
            # 호출되므로(news_dict=...) 파라미터 이름을 그대로 맞춰야 한다.
            return DataLoaderCls.compute_history_embedding(
                self, user_id=user_id, cutoff_time=pinned_now, logs_df=logs_df, news_dict=news_dict
            )

        FileDataLoader.compute_history_embedding = _leaky_compute_history_embedding  # type: ignore[attr-defined]

    freeze_datetime_now(sys.modules[DataLoaderCls.__module__], pinned_now)

    return FileDataLoader()
