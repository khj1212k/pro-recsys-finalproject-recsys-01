"""
HDBSCAN-based news clusterer
Ported from news/clustering/clusterer.py with modifications for LangGraph workflow
"""
import numpy as np
from collections import Counter
from typing import Dict, List, Optional
import warnings
warnings.filterwarnings('ignore')

try:
    import hdbscan
    HDBSCAN_AVAILABLE = True
except ImportError:
    HDBSCAN_AVAILABLE = False

from sklearn.metrics import silhouette_score


class NewsClusterer:
    """HDBSCAN-based news article clusterer"""

    def __init__(self, min_cluster_size: int = 3, min_samples: int = 2):
        if not HDBSCAN_AVAILABLE:
            raise ImportError("hdbscan library required: pip install hdbscan")

        self.min_cluster_size = min_cluster_size
        self.min_samples = min_samples
        self.clusterer = None
        self.labels_ = None
        self.n_clusters_ = 0
        self.n_noise_ = 0

    def fit_predict(self, embeddings: np.ndarray) -> np.ndarray:
        """
        Perform clustering on embeddings.
        
        Args:
            embeddings: (n_samples, n_features) embedding array
            
        Returns:
            labels: Cluster label array (-1 = noise)
        """
        self.clusterer = hdbscan.HDBSCAN(
            min_cluster_size=self.min_cluster_size,
            min_samples=self.min_samples,
            metric='euclidean',
            cluster_selection_method='eom'
        )

        self.labels_ = self.clusterer.fit_predict(embeddings)
        self.n_clusters_ = len(set(self.labels_)) - (1 if -1 in self.labels_ else 0)
        self.n_noise_ = list(self.labels_).count(-1)

        return self.labels_

    def get_cluster_stats(self) -> Dict:
        """Get clustering statistics"""
        if self.labels_ is None:
            return {}

        cluster_sizes = Counter(self.labels_)
        sizes = [cnt for label, cnt in cluster_sizes.items() if label != -1]

        return {
            'n_clusters': self.n_clusters_,
            'n_noise': self.n_noise_,
            'noise_ratio': self.n_noise_ / len(self.labels_) if len(self.labels_) > 0 else 0,
            'avg_cluster_size': np.mean(sizes) if sizes else 0,
            'cluster_sizes': {int(k): v for k, v in cluster_sizes.items()}
        }

    def evaluate(self, embeddings: np.ndarray) -> Dict:
        """Evaluate clustering quality"""
        if self.labels_ is None:
            return {}

        metrics = self.get_cluster_stats()

        if self.n_clusters_ > 1:
            mask = self.labels_ != -1
            if mask.sum() > self.n_clusters_:
                metrics['silhouette_score'] = silhouette_score(
                    embeddings[mask], self.labels_[mask]
                )
            else:
                metrics['silhouette_score'] = -1
        else:
            metrics['silhouette_score'] = -1

        return metrics


def load_embeddings_from_db(conn, exclude_clustered: bool = True) -> Dict:
    """
    Load embedding data from DB (news_raw table)
    
    Args:
        conn: Database connection
        exclude_clustered: If True, only load articles where news_letter_id IS NULL
    """
    cur = conn.cursor()

    clustered_filter = "AND news_letter_id IS NULL" if exclude_clustered else ""

    cur.execute(f"""
        SELECT N.raw_news_id, N.raw_news_title, N.embedding_result, P.press_name,
               N.raw_news_content
        FROM news_raw N
        JOIN press P ON N.press_id = P.press_id
        WHERE N.embedding_result IS NOT NULL
          AND N.raw_news_content IS NOT NULL
          AND N.raw_news_content != ''
          {clustered_filter}
        ORDER BY N.raw_news_id
    """)

    rows = cur.fetchall()

    ids = []
    titles = []
    embeddings = []
    press_names = []
    contents = []

    def sanitize_text(text):
        """Clean text for UTF-8 compatibility"""
        if not text:
            return ""
        return text.encode('utf-8', errors='replace').decode('utf-8')

    for row in rows:
        raw_news_id, title, emb, press_name, content = row

        if isinstance(emb, str):
            emb = np.array([float(x) for x in emb.strip('[]').split(',')])
        elif isinstance(emb, (list, tuple)):
            emb = np.array(emb)
        else:
            emb = np.array(emb)

        ids.append(raw_news_id)
        titles.append(sanitize_text(title))
        embeddings.append(emb)
        press_names.append(sanitize_text(press_name))
        contents.append(sanitize_text(content) or "")

    return {
        'ids': np.array(ids),
        'titles': titles,
        'embeddings': np.vstack(embeddings) if embeddings else np.array([]),
        'press_names': press_names,
        'contents': contents
    }


def get_cluster_groups(article_ids: np.ndarray, labels: np.ndarray) -> Dict[int, List[int]]:
    """
    Group article IDs by cluster label
    
    Returns:
        clusters: {cluster_id: [article_ids]} dictionary (excludes noise)
    """
    clusters = {}

    for article_id, label in zip(article_ids, labels):
        if label == -1:  # Skip noise
            continue

        if label not in clusters:
            clusters[label] = []

        clusters[label].append(int(article_id))

    return clusters


def get_cluster_articles(cluster_id: int, article_ids: List[int], data: Dict) -> List[Dict]:
    """
    Get article information for a specific cluster
    
    Args:
        cluster_id: Cluster ID
        article_ids: List of article IDs in the cluster
        data: Data dict from load_embeddings_from_db
        
    Returns:
        List of article dicts with id, title, press_name, content
    """
    articles = []
    id_to_idx = {int(aid): idx for idx, aid in enumerate(data['ids'])}
    
    for aid in article_ids:
        if aid in id_to_idx:
            idx = id_to_idx[aid]
            articles.append({
                'raw_news_id': int(data['ids'][idx]),
                'title': data['titles'][idx],
                'press_name': data['press_names'][idx],
                'content': data['contents'][idx] if idx < len(data['contents']) else ""
            })
    
    return articles
