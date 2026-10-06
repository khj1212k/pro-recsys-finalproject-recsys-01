"""파일 대역 - 운영 코드를 고치지 않고 DB 없이 클러스터링·생성 경로를 돌린다 (ADR 0036, ADR 0009 A8.2 이탈 1·6).

실험(E0)은 `ai_workspace/`의 클러스터링·생성 코드를 **한 줄도 바꾸지 않고** 돌려야 한다. 그 코드는 DB에
네 곳에서 닿는다. 이 모듈은 실험 러너가 실행 중에 그 접촉점만 파일 기반 대역으로 바꿔 끼우게 해 준다.

| 운영 접촉점 | 대역 | 파일 |
|---|---|---|
| `NewsClusterer._load_data_from_db` (기사·임베딩 SELECT) | `FileArticleSource.load` - 동결 반출본 + 임베딩 팩, 의사 시각 창 | (읽기만) |
| `workflow.nodes.get_connection` / `release_connection` / `save_news_letter` | `FileGenerationStore` | `newsletters.jsonl` |
| 저장 노드의 `UPDATE news_letter SET news_letter_embedding` | 연결 대역이 차원과 sha256만 기록 | `newsletter_embeddings.jsonl` |
| `workflow.nodes.get_shared_embedder` (BGE-M3) | `RecordOnlyEmbedder` - 텍스트 sha256만 기록, 벡터 없음 | `newsletter_embeddings.jsonl` |
| `db.batch_manager.create_new_batch` / `update_cluster_log` (Stage5를 통째로 돌릴 때만) | `FileGenerationStore` | `cluster_history.jsonl` |

운영과 달라지는 것(선언된 이탈): `news_raw.news_letter_id`는 갱신되지 않고 요청된 id 목록만 기록된다, 카테고리
매핑은 저장되지 않는다, 창의 상한(의사 시각)을 로더가 건다, `exclude_clustered`는 적용되지 않는다(파일에는
뉴스레터가 없다), 뉴스레터 임베딩은 계산하지 않는다.

**조용히 빗나가지 않게 하는 장치.** 이름이나 인자가 바뀐 운영 함수에 대역을 끼우면 틀린 것이 조용히 돈다.
그래서 `installed()`와 `FileArticleSource.patched_loader()`는 끼우기 전에 `check_production_contract()`를
돌린다: 바꿔 끼우는 대상의 인자 구성과, 대역이 흉내 내는 운영 함수의 본문 sha256이 여기 고정한 값과 같아야
한다(주석 한 줄만 바뀌어도 멈춘다 - 그때 대역을 다시 대조하고 고정값을 갱신한다). 그리고 대역이 끼워져 있는
동안에는 실제 DB 연결을 만드는 길(`db.connection`의 풀과 접속 설정)을 막아, 터널이 열려 있거나 DB 환경변수가
잡혀 있어도 운영 DB에 닿지 않는다. 막힌 시도는 `store.violations`에 남는다.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import textwrap
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from evaluation.llm import embedding_pack, frozen_export

NEWSLETTERS_FILE = "newsletters.jsonl"
EMBEDDINGS_FILE = "newsletter_embeddings.jsonl"
CLUSTER_HISTORY_FILE = "cluster_history.jsonl"
SNAPSHOT_FORMAT = "cluster-membership/1"
# Stage5.execute 안의 process_cluster가 그래프에 넘기는 state의 키(pipeline/stages.py).
STAGE5_STATE_KEYS = ("current_cluster_id", "current_cluster_index", "all_cluster_ids", "all_cluster_groups",
                     "data", "run_id")

_Param = Tuple[str, str, str]  # (이름, 종류, 기본값의 repr)

# 대역을 끼우는 이름과 그 인자 구성. (모듈, 속성 경로) -> 인자 목록.
PATCH_TARGETS: Dict[Tuple[str, str], Tuple[_Param, ...]] = {
    ("core.clustering.hdbscan_clusterer", "NewsClusterer._load_data_from_db"): (
        ("self", "POSITIONAL_OR_KEYWORD", "<없음>"),
        ("exclude_clustered", "POSITIONAL_OR_KEYWORD", "True"),
        ("lookback_hours", "POSITIONAL_OR_KEYWORD", "24"),
    ),
    ("workflow.nodes", "get_connection"): (),
    ("workflow.nodes", "release_connection"): (("conn", "POSITIONAL_OR_KEYWORD", "<없음>"),),
    ("workflow.nodes", "save_news_letter"): (
        ("conn", "POSITIONAL_OR_KEYWORD", "<없음>"),
        ("article_ids", "POSITIONAL_OR_KEYWORD", "<없음>"),
        ("newsletter_result", "POSITIONAL_OR_KEYWORD", "<없음>"),
        ("run_id", "POSITIONAL_OR_KEYWORD", "None"),
        ("generation_history", "POSITIONAL_OR_KEYWORD", "None"),
    ),
    ("workflow.nodes", "get_shared_embedder"): (),
    ("db.batch_manager", "create_new_batch"): (("cluster_log", "POSITIONAL_OR_KEYWORD", "<없음>"),),
    ("db.batch_manager", "update_cluster_log"): (
        ("run_id", "POSITIONAL_OR_KEYWORD", "<없음>"),
        ("cluster_log", "POSITIONAL_OR_KEYWORD", "<없음>"),
    ),
    # 실제 연결을 막는 자리
    ("db.connection", "get_pool"): (),
    ("db.connection", "_build_db_config"): (),
}

# 대역이 흉내 내거나, 대역을 부르는 운영 함수의 본문 sha256(main 1a42b46 = 실행 계획의 기준 b64da14와 같은 내용).
# 값이 달라지면: 운영 함수가 무엇을 하도록 바뀌었는지 읽고, 대역이 여전히 같은 일을 하는지
# tests/evaluation/test_e0_store.py로 확인한 뒤 갱신한다.
SOURCE_PINS: Dict[Tuple[str, str], str] = {
    # FileArticleSource.load가 같은 조건·같은 dict 모양을 구현한다
    ("core.clustering.hdbscan_clusterer", "NewsClusterer._load_data_from_db"):
        "72137bacead90df4f7608b03ed9088166a9ed6ba95bbda2debbf2751c563ff6e",
    # 로더를 부르고 self.data·cluster_meta를 채우는 쪽 (cluster_frozen_window가 기댄다)
    ("core.clustering.hdbscan_clusterer", "NewsClusterer.cluster_news"):
        "35a1c349c64cf4c545101000b6d5d9ddf5f7ba94bb712f441cc586b7603c9ff2",
    # FileGenerationStore.save_news_letter가 같은 값을 남긴다
    ("db.batch_manager", "save_news_letter"):
        "49ea03b06bae0f999d7464c37c8b9fd5245546effff19132aa94788ffeca2296",
    ("db.batch_manager", "create_new_batch"):
        "ae13dd3f117a20f619aec5c7e1812eb969c3b26569b2d60d954657493b141fdc",
    ("db.batch_manager", "update_cluster_log"):
        "e43e3daf2dab78c22a6c04707a07324788ab637f85854f0bc9e36471420fb53d",
    # 연결·저장·임베딩 UPDATE를 부르는 쪽
    ("workflow.nodes", "save_newsletter_to_db"):
        "1187eb50c8d4d953ae08f56f9a928da8b8f7c8e3ef9ce10c2258110d4440d4de",
    # 임베더를 부르고 빈 결과를 다루는 쪽
    ("workflow.nodes", "embed_newsletter_node"):
        "cb8d70bb5a2c0e59d95eb4036b13eb1aa2ac47962793e091d0eac050d5de8226",
    # state의 키와 data dict를 읽는 쪽 (ClusterRun.state_for가 기댄다)
    ("workflow.nodes", "initialize_cluster_processing"):
        "6684f219478ccc82fb573fb7a7d351d8a65c17e62df1a4e1157e7acfdd0a84ef",
}


class ProductionContractError(RuntimeError):
    """운영 코드가 대역이 기대하는 모양과 다르다. 대역을 끼우지 않는다."""


class BodyExpired(RuntimeError):
    """창 안에 본문 보존 기한(ADR 0023, 30일)이 지난 기사가 있다."""


class UnexpectedDatabaseAccess(RuntimeError):
    """대역이 끼워진 동안 운영 코드가 DB에 닿으려 했다."""


# ---------------------------------------------------------------- 계약


_INSTALL_LOCK = threading.Lock()
# 지금 대역이 끼워져 있는 자리의 운영 원본. 계약 검사는 대역이 아니라 이 원본을 본다
# (로더 대역과 저장 대역을 함께 끼울 때 한쪽이 다른 쪽의 대역을 "바뀐 운영 코드"로 읽지 않게).
_ORIGINALS: Dict[Tuple[str, str], Any] = {}


def _resolve(module_name: str, path: str) -> Any:
    if (module_name, path) in _ORIGINALS:
        return _ORIGINALS[(module_name, path)]
    obj: Any = importlib.import_module(module_name)
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _params(obj: Any) -> Tuple[_Param, ...]:
    return tuple(
        (p.name, p.kind.name, "<없음>" if p.default is inspect.Parameter.empty else repr(p.default))
        for p in inspect.signature(obj).parameters.values()
    )


def source_sha256(obj: Any) -> str:
    """함수 본문의 sha256. 들여쓰기와 줄 끝 공백만 정리하고 나머지는 글자 그대로 본다."""
    text = textwrap.dedent(inspect.getsource(obj))
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def check_production_contract() -> List[str]:
    """운영 코드가 고정해 둔 모양과 다른 점의 목록. 비어 있으면 대역을 끼워도 된다."""
    problems: List[str] = []
    for (module_name, path), expected in PATCH_TARGETS.items():
        name = f"{module_name}.{path}"
        try:
            target = _resolve(module_name, path)
        except (ImportError, AttributeError):
            problems.append(f"{name}: 없다(이름이 바뀌었거나 옮겨졌다)")
            continue
        try:
            actual = _params(target)
        except (TypeError, ValueError):
            problems.append(f"{name}: 인자를 읽을 수 없다")
            continue
        if actual != expected:
            problems.append(f"{name}: 인자가 다르다 - 기대 {[p[0] for p in expected]}, 실제 {[p[0] for p in actual]}")
    for (module_name, path), expected_sha in SOURCE_PINS.items():
        name = f"{module_name}.{path}"
        try:
            actual_sha = source_sha256(_resolve(module_name, path))
        except (ImportError, AttributeError):
            problems.append(f"{name}: 없다(이름이 바뀌었거나 옮겨졌다)")
            continue
        except (OSError, TypeError):
            problems.append(f"{name}: 본문을 읽을 수 없다")
            continue
        if actual_sha != expected_sha:
            problems.append(f"{name}: 본문이 고정한 것과 다르다(sha256 {actual_sha[:12]}… ≠ {expected_sha[:12]}…)")
    return problems


def assert_production_contract() -> None:
    problems = check_production_contract()
    if problems:
        raise ProductionContractError(
            "운영 코드가 파일 대역이 기대하는 모양과 다르다 - 대역을 다시 대조하기 전에는 끼우지 않는다:\n  "
            + "\n  ".join(problems)
        )


# ---------------------------------------------------------------- 기사·임베딩: 읽는 쪽


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(frozen_export.canonical_json(obj).encode("utf-8")).hexdigest()


class FileArticleSource:
    """동결 반출본과 임베딩 팩에서 운영 로더(`NewsClusterer._load_data_from_db`)와 같은 dict를 만든다."""

    def __init__(self, export: frozen_export.FrozenExport, pack: embedding_pack.EmbeddingPack):
        if export.ids != pack.ids.tolist() or pack.vectors.shape[0] != len(export.rows):
            raise ValueError("반출본과 임베딩 팩의 id가 같은 순서로 맞지 않는다 - load_embedding_pack으로 검증한 팩을 쓴다")
        self.export = export
        self.pack = pack
        self.calls: List[Dict[str, Any]] = []  # load가 불릴 때마다의 기록(텍스트 없음)
        self._crawled = [frozen_export._parse(r["crawled_at"]) for r in export.rows]

    @classmethod
    def open(cls, export_dir: Path, pack_dir: Path, *, now: Optional[datetime] = None) -> "FileArticleSource":
        """반출본을 열고(만료된 본문은 이때 지워진다) 팩을 대조해 읽는다."""
        export = frozen_export.load_export(Path(export_dir), now=now)
        return cls(export, embedding_pack.load_embedding_pack(Path(pack_dir), export))

    def load(self, *, pseudo_now: datetime, lookback_hours: int = 24, exclude_clustered: bool = True,
             on_expired: str = "raise") -> Dict[str, Any]:
        """창 `pseudo_now − lookback_hours <= crawled_at < pseudo_now`의 기사를 id 순으로.

        운영 쿼리의 조건을 그대로 따른다: 임베딩 있음(팩 검증이 보장), 본문 있음, 수집 시각 하한, id 순서.
        다른 점은 둘이다. 상한을 건다(운영에서는 NOW()가 자연 상한이다). `exclude_clustered`는 받아서 기록만
        한다(파일에는 뉴스레터가 없다).

        창 안에 본문이 지워진(만료된) 기사가 있으면 기본으로 멈춘다 - 그 창의 입력은 더 이상 같은 바이트가
        아니다. `on_expired="drop"`은 운영의 보존 잡이 본문을 비운 뒤의 운영 쿼리와 같은 결과(그 행 제외)를 낸다.
        """
        if not isinstance(pseudo_now, datetime) or pseudo_now.tzinfo is not None:
            raise ValueError("pseudo_now는 시간대 없는 UTC datetime이다(raw_news_crawled_at과 같은 표기)")
        if on_expired not in ("raise", "drop"):
            raise ValueError("on_expired는 'raise' 또는 'drop'이다")
        start = pseudo_now - timedelta(hours=lookback_hours)
        picked = [i for i, at in enumerate(self._crawled) if start <= at < pseudo_now]
        gone = [i for i in picked if not self.export.rows[i]["body"]]
        if gone and on_expired == "raise":
            raise BodyExpired(
                f"창 [{_iso(start)}, {_iso(pseudo_now)}) 안의 기사 {len(gone)}건은 본문 보존 기한이 지나 지워졌다 "
                "- 이 창은 더 이상 재현할 수 없다(ADR 0023)"
            )
        picked = [i for i in picked if self.export.rows[i]["body"]]
        rows = [self.export.rows[i] for i in picked]
        ids = [int(r["id"]) for r in rows]
        vectors = [np.asarray(self.pack.vectors[i], dtype=np.float32) for i in picked]
        self.calls.append({
            "pseudo_now_utc": _iso(pseudo_now), "window_utc": [_iso(start), _iso(pseudo_now)],
            "lookback_hours": lookback_hours, "exclude_clustered": exclude_clustered,
            "rows": len(ids), "dropped_expired": len(gone),
            "ids_sha256": hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest(),
        })
        # 아래 다섯 줄은 운영 함수의 return과 같은 식이다(행이 없을 때의 np.array([])까지).
        return {
            'ids': np.array(ids),
            'titles': [r["title"] for r in rows],
            'embeddings': np.vstack(vectors) if vectors else np.array([]),
            'press_names': [r["press"] for r in rows],
            'contents': [r["body"] for r in rows],
        }

    def loader_for(self, pseudo_now: datetime, *, on_expired: str = "raise") -> Callable[..., Dict[str, Any]]:
        """`NewsClusterer._load_data_from_db` 자리에 끼울 함수(인자 구성이 같다)."""
        source = self

        def _load_data_from_db(self, exclude_clustered: bool = True, lookback_hours: int = 24) -> Dict:
            return source.load(pseudo_now=pseudo_now, lookback_hours=lookback_hours,
                               exclude_clustered=exclude_clustered, on_expired=on_expired)

        return _load_data_from_db

    @contextmanager
    def patched_loader(self, pseudo_now: datetime, *, on_expired: str = "raise") -> Iterator[None]:
        """운영 `NewsClusterer`가 이 파일 소스에서 읽게 한다. 끝나면 운영 함수를 되돌린다."""
        from core.clustering.hdbscan_clusterer import NewsClusterer

        key = ("core.clustering.hdbscan_clusterer", "NewsClusterer._load_data_from_db")
        with _INSTALL_LOCK:
            if key in _ORIGINALS:
                raise ProductionContractError("로더 대역이 이미 끼워져 있다 - 겹쳐 끼우지 않는다")
            assert_production_contract()
            original = NewsClusterer.__dict__["_load_data_from_db"]
            _ORIGINALS[key] = original
            NewsClusterer._load_data_from_db = self.loader_for(pseudo_now, on_expired=on_expired)
        try:
            yield
        finally:
            with _INSTALL_LOCK:
                NewsClusterer._load_data_from_db = original
                _ORIGINALS.pop(key, None)


@dataclass
class ClusterRun:
    """의사 시각 하나에서 운영 `NewsClusterer.cluster_news()`가 낸 결과."""

    pseudo_now: datetime
    params: Dict[str, int]
    clusters: Dict[int, List[int]]
    cluster_meta: Dict[int, Dict[str, Any]]
    data: Dict[str, Any]
    export_identity_sha256: str
    embeddings_sha256: str

    @property
    def n_articles(self) -> int:
        return int(len(self.data["ids"])) if self.data else 0

    def state_for(self, cluster_id: int, *, run_id: Optional[int]) -> Dict[str, Any]:
        """그래프에 넘길 state. Stage5.execute의 process_cluster가 만드는 dict와 같은 키·같은 값이다.

        `current_cluster_index`는 처리 순서가 아니라 `all_cluster_ids`(id 내림차순) 안의 위치다 -
        `initialize_cluster_processing`이 그 위치로 클러스터를 다시 찾는다.
        """
        all_ids = sorted(list(self.clusters.keys()), reverse=True)
        return {
            "current_cluster_id": cluster_id,
            "current_cluster_index": all_ids.index(cluster_id),
            "all_cluster_ids": all_ids,
            "all_cluster_groups": self.clusters,
            "data": self.data,
            "run_id": run_id,
        }

    def membership_snapshot(self) -> Dict[str, Any]:
        """클러스터 멤버십(id만)과 그 sha256. 본문·제목은 없다.

        `membership_sha256`에는 코드 SHA를 넣지 않는다 - 같은 동결 입력에서 뒤의 코드(저장소 계약 리팩터)가
        같은 값을 다시 내는지가 회귀 기준이기 때문이다.
        """
        ids = [int(x) for x in self.data["ids"]] if self.data else []
        start = self.pseudo_now - timedelta(hours=self.params["lookback_hours"])
        body = {
            "format": SNAPSHOT_FORMAT,
            "pseudo_now_utc": _iso(self.pseudo_now),
            "window_utc": [_iso(start), _iso(self.pseudo_now)],
            "params": dict(self.params),
            "n_articles": len(ids),
            "article_ids_sha256": hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest(),
            "clusters": {str(cid): [int(x) for x in members] for cid, members in sorted(self.clusters.items())},
            "cluster_meta": {str(cid): dict(meta) for cid, meta in sorted(self.cluster_meta.items())},
            "export_identity_sha256": self.export_identity_sha256,
            "embeddings_sha256": self.embeddings_sha256,
        }
        return {**body, "membership_sha256": _canonical_sha256(body)}


def cluster_frozen_window(source: FileArticleSource, pseudo_now: datetime, *, min_cluster_size: Optional[int] = None,
                          min_samples: Optional[int] = None, lookback_hours: Optional[int] = None) -> ClusterRun:
    """동결 입력의 한 창을 운영 `NewsClusterer`로 클러스터링한다. LLM은 부르지 않는다.

    파라미터를 주지 않으면 Stage5와 같이 Settings의 값을 쓴다. Mac에서는 `nice -n 19`와 스레드 2개로 돌린다.
    """
    from config.settings import Settings
    from core.clusterer import NewsClusterer  # Stage5가 임포트하는 이름

    params = {
        "min_cluster_size": int(min_cluster_size if min_cluster_size is not None else Settings.HDBSCAN_MIN_CLUSTER_SIZE),
        "min_samples": int(min_samples if min_samples is not None else Settings.HDBSCAN_MIN_SAMPLES),
        "lookback_hours": int(lookback_hours if lookback_hours is not None else Settings.CLUSTER_LOOKBACK_HOURS),
    }
    with source.patched_loader(pseudo_now):
        clusterer = NewsClusterer()
        clusters = clusterer.cluster_news(**params)
        data = clusterer.get_clustered_articles(list(clusters.keys()))
    return ClusterRun(
        pseudo_now=pseudo_now, params=params, clusters=clusters, cluster_meta=dict(clusterer.cluster_meta),
        data=data, export_identity_sha256=source.export.identity_sha256, embeddings_sha256=source.pack.vectors_sha256,
    )


# ---------------------------------------------------------------- 저장: 쓰는 쪽


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RecordOnlyEmbedder:
    """뉴스레터 임베더 자리에 서서 텍스트의 sha256만 남긴다(ADR 0009 A8.2 이탈 6).

    벡터 대신 `None`을 돌려주므로 운영 노드는 `newsletter_embedding=None`으로 계속한다. 이 벡터를 읽는 하류
    노드는 없다. 저장본의 벡터는 뒤에 같은 모델로 따로 계산한다.
    """

    def __init__(self, store: "FileGenerationStore"):
        self._store = store

    def generate_embeddings_batch(self, texts: List[str], batch_size: int = 20) -> Tuple[List[Any], float]:
        for text in texts:
            self._store._append(EMBEDDINGS_FILE, {
                "kind": "record_only", "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "chars": len(text), "at": _now_utc(),
            })
        return [None] * len(texts), 0.0

    def cleanup(self) -> None:
        return None


class _FileCursor:
    _EMBEDDING_UPDATE = "UPDATE news_letter SET news_letter_embedding = %s WHERE news_letter_id = %s"

    def __init__(self, store: "FileGenerationStore"):
        self._store = store

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> None:
        normalized = " ".join(str(sql).split())
        if normalized == self._EMBEDDING_UPDATE and params is not None and len(params) == 2:
            vector = np.asarray(params[0], dtype=np.float32)
            self._store._append(EMBEDDINGS_FILE, {
                "kind": "stored", "news_letter_id": int(params[1]), "dim": int(vector.shape[0]),
                "vector_sha256": hashlib.sha256(vector.tobytes()).hexdigest(), "at": _now_utc(),
            })
            return
        raise self._store._violation(f"대역 연결에 기대하지 않은 SQL: {normalized[:120]}")

    def fetchone(self):
        raise self._store._violation("대역 연결에서 fetchone()")

    def fetchall(self):
        raise self._store._violation("대역 연결에서 fetchall()")

    def close(self) -> None:
        return None


class _FileConnection:
    def __init__(self, store: "FileGenerationStore"):
        self._store = store

    def cursor(self) -> _FileCursor:
        return _FileCursor(self._store)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class FileGenerationStore:
    """생성 경로의 저장 호출을 받아 JSONL로 남긴다. 디렉터리는 git이 추적할 수 없는 곳이어야 한다.

    초안 텍스트(우리가 생성한 뉴스레터)가 들어가므로 공개 저장소에 넣지 않는다(ADR 0023 개정). 한 줄씩 덧붙이고
    줄마다 fsync한다 - 프로세스가 죽어도 저장된 줄은 남고, 다시 열면 번호를 이어 매긴다.
    """

    def __init__(self, directory: Path):
        self.dir = frozen_export.ensure_private_destination(Path(directory))
        old_umask = os.umask(0o077)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)
        self._lock = threading.Lock()
        self.violations: List[str] = []
        self.connections_opened = 0
        self.connections_released = 0
        self._next_newsletter_id = max([r["news_letter_id"] for r in self.newsletters()], default=0) + 1
        self._next_run_id = max([h["run_id"] for h in self.cluster_history()], default=0) + 1

    # -- 파일
    def _append(self, name: str, record: Dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def _read(self, name: str) -> List[Dict[str, Any]]:
        path = self.dir / name
        if not path.exists():
            return []
        with open(path, "rb") as f:
            return [json.loads(line.decode("utf-8")) for line in f if line.strip()]

    def _violation(self, message: str) -> UnexpectedDatabaseAccess:
        with self._lock:
            self.violations.append(message)
        return UnexpectedDatabaseAccess(message)

    def assert_clean(self) -> None:
        """운영 코드가 대역 밖의 DB에 닿으려 한 적이 있으면 실패한다. 운영 노드는 저장 실패를 삼키므로
        (로그만 남긴다) 러너가 끝에서 이 함수로 확인해야 드러난다."""
        if self.violations:
            raise UnexpectedDatabaseAccess(
                f"대역 밖 DB 접근 {len(self.violations)}건: " + "; ".join(self.violations[:5])
            )

    # -- workflow.nodes 자리
    def get_connection(self) -> _FileConnection:
        with self._lock:
            self.connections_opened += 1
        return _FileConnection(self)

    def release_connection(self, conn) -> None:
        with self._lock:
            self.connections_released += 1

    def save_news_letter(self, conn, article_ids: list, newsletter_result: dict,
                         run_id: Optional[int] = None, generation_history: Optional[list] = None) -> int:
        """운영 `db.batch_manager.save_news_letter`가 INSERT에 넣는 값을 같은 정제 함수로 만들어 남긴다.

        하지 않는 것: `news_raw.news_letter_id` 갱신(요청된 id만 `linked_article_ids`로 기록), 카테고리 매핑.
        """
        from core.reconstruction.repository import CATEGORY_MAP
        from core.reconstruction.validator import sanitize_text
        from db.batch_manager import convert_numpy, sanitize_obj, strip_surrogates

        def coerce_text(value) -> str:
            if value is None:
                return ""
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            return sanitize_text(str(value))

        def utf8_safe(text: str) -> str:
            return strip_surrogates(text.encode("utf-8", errors="ignore").decode("utf-8"))

        def stored_json(value: Any) -> Any:
            # 운영은 json.dumps(ensure_ascii=False) 뒤 같은 UTF-8 정리를 거친 문자열을 json 컬럼에 넣는다
            return json.loads(utf8_safe(json.dumps(value, ensure_ascii=False)))

        linked = [int(x) for x in article_ids] if article_ids else []
        history = sanitize_obj(convert_numpy(generation_history)) if generation_history else None
        categories = [sanitize_text(str(c)) for c in (newsletter_result.get("categories") or [])]
        record = {
            "kind": "saved",
            "run_id": run_id,
            "title": utf8_safe(coerce_text(newsletter_result.get("title"))),
            "sentence": utf8_safe(coerce_text(newsletter_result.get("sentence") or newsletter_result.get("summary"))),
            "content": utf8_safe(coerce_text(newsletter_result.get("content"))),
            "keywords": stored_json(sanitize_obj(convert_numpy(newsletter_result.get("keywords", [])))),
            "raw_news_count": len(linked),
            "generation_history": stored_json(history) if history else None,
            "linked_article_ids": linked,
            "categories_requested": [CATEGORY_MAP.get(c, c) for c in categories if c],
            "saved_at": _now_utc(),
        }
        with self._lock:
            record["news_letter_id"] = self._next_newsletter_id
            self._next_newsletter_id += 1
        self._append(NEWSLETTERS_FILE, record)
        return record["news_letter_id"]

    def record_only_embedder(self) -> RecordOnlyEmbedder:
        return RecordOnlyEmbedder(self)

    # -- db.batch_manager 자리 (Stage5.execute를 통째로 돌릴 때)
    def create_new_batch(self, cluster_log: dict) -> int:
        from db.batch_manager import convert_numpy, sanitize_obj

        with self._lock:  # 운영의 COALESCE(MAX(run_id), 0) + 1
            run_id = self._next_run_id
            self._next_run_id += 1
        log = json.loads(json.dumps(sanitize_obj(convert_numpy(cluster_log)), ensure_ascii=False))
        self._append(CLUSTER_HISTORY_FILE, {"kind": "created", "run_id": run_id, "cluster_log": log, "at": _now_utc()})
        return run_id

    def update_cluster_log(self, run_id: int, cluster_log: dict) -> bool:
        from db.batch_manager import convert_numpy, sanitize_obj

        log = json.loads(json.dumps(sanitize_obj(convert_numpy(cluster_log)), ensure_ascii=False))
        self._append(CLUSTER_HISTORY_FILE, {"kind": "updated", "run_id": int(run_id), "cluster_log": log,
                                            "at": _now_utc()})
        return True

    def append_newsletter_record(self, kind: str, record: Dict[str, Any]) -> None:
        """러너가 저장본 말고 남길 텍스트(시도별 형식체 초안, 문체 변환본)를 같은 파일에 덧붙인다(ADR 0009 A8.4).

        초안 텍스트가 있을 수 있는 곳을 이 파일 하나로 묶기 위한 것이다. `saved`는 저장 대역만 쓴다.
        """
        if kind == "saved" or not kind:
            raise ValueError("kind는 'saved'가 아닌 이름이어야 한다")
        self._append(NEWSLETTERS_FILE, {**record, "kind": kind})

    # -- 읽기
    def newsletters(self, kind: str = "saved") -> List[Dict[str, Any]]:
        return [r for r in self._read(NEWSLETTERS_FILE) if r.get("kind") == kind]

    def embedding_records(self) -> List[Dict[str, Any]]:
        return self._read(EMBEDDINGS_FILE)

    def cluster_history(self) -> List[Dict[str, Any]]:
        return self._read(CLUSTER_HISTORY_FILE)


# ---------------------------------------------------------------- 끼우기

_installed = False


@dataclass
class _Patch:
    module: Any
    name: str
    original: Any = field(default=None)


@contextmanager
def installed(store: FileGenerationStore, *, embedder: str = "record_only", cluster_history: bool = False,
              forbid_database: bool = True) -> Iterator[FileGenerationStore]:
    """운영 생성 경로의 저장 접촉점을 `store`로 바꿔 끼운다. 블록을 나가면(오류로 나가도) 전부 되돌린다.

    - 언제나: `workflow.nodes`의 `get_connection`·`release_connection`·`save_news_letter`.
    - `embedder="record_only"`(기본): `workflow.nodes.get_shared_embedder`를 기록 전용 임베더로.
      `"production"`이면 건드리지 않는다(실제 임베더가 있는 환경에서 돌릴 때).
    - `cluster_history=True`: `db.batch_manager`의 `create_new_batch`·`update_cluster_log`도 (Stage5.execute를
      통째로 돌릴 때만 필요하다. E0은 Stage5를 우회하므로 끄고 쓴다).
    - `forbid_database=True`(기본): `db.connection`의 풀과 접속 설정을 막는다 - 대역 밖의 DB 접근은 예외가 되고
      `store.violations`에 남는다.

    패치는 프로세스 전체에 걸린다. 겹쳐 쓰지 않는다.
    """
    global _installed
    if embedder not in ("record_only", "production"):
        raise ValueError("embedder는 'record_only' 또는 'production'이다")
    with _INSTALL_LOCK:
        if _installed:
            raise ProductionContractError("파일 대역이 이미 끼워져 있다 - 겹쳐 끼우지 않는다")
        assert_production_contract()
        import db.batch_manager as batch_manager
        import db.connection as connection
        import workflow.nodes as nodes

        def blocked(what: str) -> Callable[..., Any]:
            def refuse(*args: Any, **kwargs: Any) -> Any:
                raise store._violation(f"대역이 끼워진 동안 실제 DB 연결을 만들려 했다({what})")

            return refuse

        replacements: List[Tuple[Any, str, Any]] = [
            (nodes, "get_connection", store.get_connection),
            (nodes, "release_connection", store.release_connection),
            (nodes, "save_news_letter", store.save_news_letter),
        ]
        if embedder == "record_only":
            shared = store.record_only_embedder()
            replacements.append((nodes, "get_shared_embedder", lambda: shared))
        if cluster_history:
            replacements += [
                (batch_manager, "create_new_batch", store.create_new_batch),
                (batch_manager, "update_cluster_log", store.update_cluster_log),
            ]
        if forbid_database:
            replacements += [
                (connection, "get_pool", blocked("db.connection.get_pool")),
                (connection, "_build_db_config", blocked("db.connection._build_db_config")),
            ]
        applied: List[_Patch] = []
        for module, name, replacement in replacements:
            original = getattr(module, name)
            applied.append(_Patch(module, name, original))
            _ORIGINALS[(module.__name__, name)] = original
            setattr(module, name, replacement)
        _installed = True
    try:
        yield store
    finally:
        with _INSTALL_LOCK:
            for patch in reversed(applied):
                setattr(patch.module, patch.name, patch.original)
                _ORIGINALS.pop((patch.module.__name__, patch.name), None)
            _installed = False
