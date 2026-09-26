# HDBSCAN 기반 뉴스 클러스터링
# - 임베딩 벡터 유사도로 기사 그룹화
# - 1차 클러스터링 후 필요시 2차 분할

import numpy as np
import ast
import warnings
from collections import Counter
from typing import Dict, List, Tuple
from .split_v2 import decide_split_v2

try:
    import hdbscan
    HDBSCAN_AVAILABLE = True
except ImportError:
    HDBSCAN_AVAILABLE = False


def parse_embedding(raw_value) -> np.ndarray:
    """DB에서 읽은 임베딩 값을 1차원 float32 numpy array로 변환.

    - pgvector.Vector: db.connection의 연결은 register_vector가 걸려 있어 vector 컬럼이
      이 객체로 온다. np.array(Vector)는 값이 아니라 객체를 감싼 0차원 object 배열이 돼
      HDBSCAN에서 TypeError로 죽으므로 to_numpy()로 꺼낸다.
    - 문자열 '[0.1, 0.2, ...]': 어댑터가 없는 연결(레거시 경로). ast.literal_eval은
      리터럴만 평가하므로 eval()과 달리 임의 코드 실행 위험이 없다.
    - list/tuple/ndarray: 그대로 변환.
    """
    if isinstance(raw_value, str):
        values = ast.literal_eval(raw_value)
    elif hasattr(raw_value, "to_numpy"):
        values = raw_value.to_numpy()
    else:
        values = raw_value
    arr = np.asarray(values, dtype=np.float32)
    if arr.ndim != 1:
        raise ValueError(f"임베딩은 1차원이어야 합니다 (shape={arr.shape}, type={type(raw_value).__name__})")
    return arr


class NewsClusterer:
    def __init__(self, min_cluster_size: int = 3, min_samples: int = 2, lookback_hours: int = 24):
        if not HDBSCAN_AVAILABLE:
            raise ImportError("hdbscan library required: pip install hdbscan")
        self.min_cluster_size = min_cluster_size
        self.min_samples = min_samples
        self.lookback_hours = lookback_hours
        self.labels_ = None
        self.data = None
        # cluster_with_split이 만든 최종 그룹마다 split_v2로 쪼개졌는지 기록한다.
        # 평가셋 층화(evaluation/llm/evalset.py, ADR 0009)가 cluster_history에서 읽는다.
        self.group_meta_: List[Dict] = []
        self.cluster_meta: Dict[int, Dict] = {}

    def fit_predict(self, embeddings: np.ndarray) -> np.ndarray:
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=self.min_cluster_size,
            min_samples=self.min_samples,
            metric='euclidean',
            cluster_selection_method='eom'
        )
        self.labels_ = clusterer.fit_predict(embeddings)
        return self.labels_

    def cluster_with_split(self, data: Dict) -> List[Tuple[List[int], List[str]]]:
        embeddings = data['embeddings']
        self.group_meta_ = []
        if len(embeddings) == 0: return []

        # 1차 cluster
        labels = self.fit_predict(embeddings)
        cluster_idx_groups = {}
        for news_idx, cluster_label in enumerate(labels):
            if cluster_label != -1:
                cluster_idx_groups.setdefault(int(cluster_label), []).append(news_idx)

        # 2차 cluster
        final_groups = []
        from tqdm import tqdm
        for idxs in tqdm(cluster_idx_groups.values(), desc="Splitting clusters"):
            c_titles = [data['titles'][i] for i in idxs]
            c_ids = [int(data['ids'][i]) for i in idxs]
            c_X = embeddings[idxs]
            
            # 1차 클러스터들 중에 또 split이 필요한지 판단
            dec = decide_split_v2(c_X, c_titles)
            if dec.should_split and dec.debug.get('idx0'):
                for split_idx in ['idx0', 'idx1']:
                    sub_idxs = dec.debug[split_idx]
                    final_groups.append(([c_ids[j] for j in sub_idxs], [c_titles[j] for j in sub_idxs]))
                    self.group_meta_.append({"split_v2": True})
            else:
                final_groups.append((c_ids, c_titles))
                self.group_meta_.append({"split_v2": False})
        return final_groups

    def _load_data_from_db(self, exclude_clustered: bool = True, lookback_hours: int = 24) -> Dict:
        from db.connection import get_connection, release_connection
        print("📥 DB에서 뉴스 데이터 로딩 중...", end="", flush=True)
        conn = get_connection()
        try:
            cur = conn.cursor()
            clustered_filter = "AND news_letter_id IS NULL" if exclude_clustered else ""

            # 쿼리: 임베딩과 본문이 있는 기사만 조회
            # lookback_hours는 SQL 파라미터(%s)로 바인딩한다 (문자열 포매팅으로 쿼리문에
            # 직접 끼워넣지 않음 - 값 자체는 내부 설정값이라도 파라미터화된 쿼리를 유지).
            cur.execute(f"""
                SELECT N.raw_news_id, N.raw_news_title, N.embedding_result, P.press_name, N.raw_news_content
                FROM news_raw N
                JOIN press P ON N.press_id = P.press_id
                WHERE N.embedding_result IS NOT NULL
                  AND N.raw_news_content IS NOT NULL
                  AND N.raw_news_content != ''
                  AND N.raw_news_crawled_at >= NOW() - (%s * INTERVAL '1 hour')
                  {clustered_filter}
                ORDER BY N.raw_news_id
            """, (lookback_hours,))

            rows = cur.fetchall()
            ids, titles, embeddings, press_names, contents = [], [], [], [], []
            
            for r in rows:
                if r[2] is None: continue
                # 임베딩 파싱
                emb = parse_embedding(r[2])
                
                ids.append(r[0])
                titles.append(r[1])
                embeddings.append(emb)
                press_names.append(r[3])
                contents.append(r[4])
                
            print(f" 완료 ({len(ids)}건)")
            return {
                'ids': np.array(ids),
                'titles': titles,
                'embeddings': np.vstack(embeddings) if embeddings else np.array([]),
                'press_names': press_names,
                'contents': contents
            }
        finally:
            release_connection(conn)

    def cluster_news(self, min_cluster_size=None, min_samples=None, lookback_hours=None) -> Dict[int, List[int]]:
        # Pipeline 연동용
        # 이전에는 이 인자들이 무시되고 __init__ 시점의 기본값(3, 2)이 항상 쓰였다.
        # CLI/Settings에서 내려온 값이 실제로 hdbscan.HDBSCAN까지 도달하도록 인스턴스
        # 상태를 갱신한다.
        if min_cluster_size is not None:
            self.min_cluster_size = min_cluster_size
        if min_samples is not None:
            self.min_samples = min_samples
        if lookback_hours is not None:
            self.lookback_hours = lookback_hours

        self.cluster_meta = {}
        self.data = self._load_data_from_db(lookback_hours=self.lookback_hours)
        if not self.data or len(self.data['ids']) == 0:
            return {}

        groups = self.cluster_with_split(self.data)
        self.cluster_meta = dict(enumerate(self.group_meta_))
        return {idx: g_ids for idx, (g_ids, _) in enumerate(groups)}

    def get_clustered_articles(self, cluster_ids: List[int] = None) -> Dict:
        """
        클러스터링된 기사 데이터 반환
        pipeline/stages.py에서 사용됨
        """
        return self.data
