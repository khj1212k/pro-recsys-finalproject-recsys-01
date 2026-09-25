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
    """DB에서 읽은 임베딩 값(문자열 또는 리스트)을 numpy array로 안전하게 변환.

    pgvector 컬럼은 드라이버에 따라 '[0.1, 0.2, ...]' 형태의 문자열로 반환될 수 있다.
    ast.literal_eval은 eval()과 달리 리터럴(숫자/리스트/튜플 등)만 평가하므로
    임의 코드 실행 위험이 없다.
    """
    if isinstance(raw_value, str):
        return np.array(ast.literal_eval(raw_value))
    return np.array(raw_value)


class NewsClusterer:
    def __init__(self, min_cluster_size: int = 3, min_samples: int = 2):
        if not HDBSCAN_AVAILABLE:
            raise ImportError("hdbscan library required: pip install hdbscan")
        self.min_cluster_size = min_cluster_size
        self.min_samples = min_samples
        self.labels_ = None
        self.data = None

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
            else:
                final_groups.append((c_ids, c_titles))
        return final_groups

    def _load_data_from_db(self, exclude_clustered: bool = True) -> Dict:
        from db.connection import get_connection
        print("📥 DB에서 뉴스 데이터 로딩 중...", end="", flush=True)
        conn = get_connection()
        try:
            cur = conn.cursor()
            clustered_filter = "AND news_letter_id IS NULL" if exclude_clustered else ""
            
            # 쿼리: 임베딩과 본문이 있는 기사만 조회
            cur.execute(f"""
                SELECT N.raw_news_id, N.raw_news_title, N.embedding_result, P.press_name, N.raw_news_content
                FROM news_raw N 
                JOIN press P ON N.press_id = P.press_id
                WHERE N.embedding_result IS NOT NULL
                  AND N.raw_news_content IS NOT NULL
                  AND N.raw_news_content != ''
                  AND N.raw_news_crawled_at >= NOW() - INTERVAL '24 hours'
                  {clustered_filter}
                ORDER BY N.raw_news_id
            """)
            
            rows = cur.fetchall()
            ids, titles, embeddings, press_names, contents = [], [], [], [], []
            
            for r in rows:
                if not r[2]: continue
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
            conn.close()

    def cluster_news(self, min_cluster_size=None, min_samples=None) -> Dict[int, List[int]]:
        # Pipeline 연동용
        self.data = self._load_data_from_db()
        if not self.data or len(self.data['ids']) == 0:
            return {}
            
        groups = self.cluster_with_split(self.data)
        
        
        return {idx: g_ids for idx, (g_ids, _) in enumerate(groups)}

    def get_clustered_articles(self, cluster_ids: List[int] = None) -> Dict:
        """
        클러스터링된 기사 데이터 반환
        pipeline/stages.py에서 사용됨
        """
        return self.data
