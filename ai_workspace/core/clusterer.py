"""
HDBSCAN-based news clusterer with split_v2 support
Ported from news/clustering/clusterer.py with modifications for LangGraph workflow
"""
import re
import numpy as np
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
import warnings
warnings.filterwarnings('ignore')

try:
    import hdbscan
    HDBSCAN_AVAILABLE = True
except ImportError:
    HDBSCAN_AVAILABLE = False

from sklearn.metrics import silhouette_score
from sklearn.cluster import KMeans


# ========================================
# Split V2 Constants & Helpers
# ========================================
STOPWORDS = {
    "있다","없다","했다","한다","됐다","된다","이번","오늘","내일","어제","관련","대한","통해","위해","기준",
    "가능","필요","전망","확대","강화","추진","논란","갈등","문제","발표","공개","확인","진행","조치","대응","검토","주목",
    "정부","국회","대통령","위원회","당국","업계","시장","증가","감소","상승","하락",
}

# size=3 outlier 기준
OUTLIER_RATIO_TH = 1.35
OUTLIER_DELTA_TH = 0.20
SIZE3_JACCARD_TH = 0.18
SIZE3_DIST_RATIO_TH = 1.20

# VETO / FORCE 게이트
VETO_JACCARD_TH = 0.55
VETO_DUP_TITLE_RATIO = 0.50
FORCE_JACCARD_TH = 0.12
FORCE_SIL_TH = 0.18
FORCE_MIN_N = 5

# 스코어 기준
SPLIT_SCORE_TH = 1.80
SIL_MIN = 0.10
DIST_RATIO_GOOD = 1.20


@dataclass
class SplitDecision:
    """Split decision result"""
    should_split: bool
    reason: str
    debug: Dict[str, Any] = field(default_factory=dict)


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

    def cluster_with_split(self, data: Dict) -> List[Tuple[List[int], List[str]]]:
        """
        HDBSCAN 클러스터링 + split_v2 자동 적용

        Args:
            data: {'ids': np.ndarray, 'titles': List[str], 'embeddings': np.ndarray, ...}

        Returns:
            final_groups: [(article_ids, titles), ...] - 최종 그룹 리스트
        """
        embeddings = data['embeddings']
        if len(embeddings) == 0:
            return []

        # 1단계: HDBSCAN 클러스터링
        labels = self.fit_predict(embeddings)

        # 클러스터별로 인덱스 그룹화
        cluster_idx_groups: Dict[int, List[int]] = {}
        for idx, lab in enumerate(labels):
            if lab == -1:
                continue
            cluster_idx_groups.setdefault(int(lab), []).append(idx)

        if not cluster_idx_groups:
            return []

        # 2단계: 각 클러스터에 split_v2 자동 적용
        final_groups = []

        for cid, idxs in cluster_idx_groups.items():
            cluster_titles = [data['titles'][i] for i in idxs]
            cluster_ids = [int(data['ids'][i]) for i in idxs]
            cluster_X = embeddings[idxs]

            dec = decide_split_v2(cluster_X, cluster_titles)

            if dec.should_split:
                dbg = dec.debug or {}
                idx0 = dbg.get('idx0', [])
                idx1 = dbg.get('idx1', [])

                if idx0:
                    group0_ids = [cluster_ids[j] for j in idx0]
                    group0_titles = [cluster_titles[j] for j in idx0]
                    final_groups.append((group0_ids, group0_titles))
                if idx1:
                    group1_ids = [cluster_ids[j] for j in idx1]
                    group1_titles = [cluster_titles[j] for j in idx1]
                    final_groups.append((group1_ids, group1_titles))
            else:
                final_groups.append((cluster_ids, cluster_titles))

        return final_groups


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


# ========================================
# Split V2 Implementation
# ========================================
def _tokenize_title(title: str) -> List[str]:
    """Extract meaningful tokens from title"""
    title = (title or "").strip()
    if not title:
        return []

    # Try kiwipiepy if available
    try:
        from kiwipiepy import Kiwi
        kiwi = Kiwi()
        analyzed = kiwi.analyze(title)
        tokens = analyzed[0][0] if analyzed else []
        toks = []
        for t in tokens:
            if t.tag in ("NNP", "NNG", "SL", "SH"):
                w = t.form.strip()
                if len(w) >= 2 and w not in STOPWORDS:
                    toks.append(w)
        return toks
    except Exception:
        pass

    # Fallback: regex-based
    raw = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    return [w for w in raw if w not in STOPWORDS]


def _extract_entities_from_titles(titles: List[str]) -> List[List[str]]:
    """Extract entities from list of titles"""
    return [_tokenize_title(t) for t in titles]


def _top_entities_by_coverage(tokens_per_doc: List[List[str]], top_k: int = 8):
    """Get top entities by document coverage"""
    n = len(tokens_per_doc)
    if n == 0:
        return [], {}

    doc_has = defaultdict(int)
    for toks in tokens_per_doc:
        for w in set(toks):
            doc_has[w] += 1

    cov = {w: doc_has[w] / n for w in doc_has}
    ranked = sorted(cov.items(), key=lambda x: (x[1], len(x[0])), reverse=True)
    top = [w for w, _ in ranked[:top_k]]
    return top, cov


def _char_ngrams(s: str, n: int = 2) -> set:
    """Generate character n-grams"""
    s = (s or "").strip()
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i+n] for i in range(len(s) - n + 1)}


def _entity_jaccard_soft(top0: List[str], top1: List[str], ngram: int = 2) -> float:
    """Soft Jaccard similarity using character n-grams"""
    def to_ngram_set(words: List[str]) -> set:
        out = set()
        for w in words:
            out |= _char_ngrams(w, n=ngram)
        return out

    s0 = to_ngram_set(top0)
    s1 = to_ngram_set(top1)
    if not s0 and not s1:
        return 1.0
    if not s0 or not s1:
        return 0.0
    return len(s0 & s1) / len(s0 | s1)


def _token_sim_char_jaccard(a: str, b: str, ngram: int = 2) -> float:
    """Character-level Jaccard similarity between two tokens"""
    A, B = _char_ngrams(a, ngram), _char_ngrams(b, ngram)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def _entity_jaccard_fuzzy(top0: List[str], top1: List[str], ngram: int = 2, match_th: float = 0.55) -> float:
    """Fuzzy Jaccard with token-level matching"""
    A = [w for w in top0 if w]
    B = [w for w in top1 if w]
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0

    used_b = [False] * len(B)
    inter = 0

    for a in A:
        best_j = -1
        best_sim = 0.0
        for j, b in enumerate(B):
            if used_b[j]:
                continue
            sim = _token_sim_char_jaccard(a, b, ngram=ngram)
            if sim > best_sim:
                best_sim = sim
                best_j = j
        if best_j >= 0 and best_sim >= match_th:
            used_b[best_j] = True
            inter += 1

    union = len(A) + len(B) - inter
    return inter / union if union > 0 else 1.0


def _avg_intra_dist(X: np.ndarray) -> float:
    """Average pairwise distance within cluster"""
    n = X.shape[0]
    if n <= 1:
        return 0.0
    s = 0.0
    c = 0
    for i in range(n):
        for j in range(i + 1, n):
            s += float(np.linalg.norm(X[i] - X[j]))
            c += 1
    return s / max(c, 1)


def _normalize_title(t: str) -> str:
    """Normalize title for duplicate detection"""
    t = (t or "").strip()
    if not t:
        return ""
    t = re.sub(r"\[[^\]]+\]", " ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = t.lower()
    t = re.sub(r"[\s\-_·—–,.:;!?\"'""''`~]+", "", t)
    return t


def _duplicate_title_ratio(titles: List[str]) -> float:
    """Calculate duplicate title ratio"""
    norms = [_normalize_title(t) for t in titles]
    norms = [x for x in norms if x]
    n = len(norms)
    if n == 0:
        return 0.0
    c = Counter(norms)
    mx = max(c.values()) if c else 0
    return float(mx / n)


def _split_size3_outlier(X: np.ndarray) -> Tuple[List[int], List[int], Dict]:
    """Handle size=3 cluster outlier detection"""
    dbg: Dict[str, Any] = {}
    centroid = X.mean(axis=0)
    dists = [float(np.linalg.norm(X[i] - centroid)) for i in range(3)]
    order = np.argsort(dists)
    d0, d1, d2 = dists[order[0]], dists[order[1]], dists[order[2]]

    ratio = (d2 / (d1 + 1e-9)) if d1 > 1e-9 else 999.0
    delta = d2 - d1

    outlier_idx = int(order[2])
    inliers = [int(order[0]), int(order[1])]

    dbg["size3_dists"] = [round(x, 4) for x in dists]
    dbg["size3_ratio"] = float(ratio)
    dbg["size3_delta"] = float(delta)
    dbg["outlier_idx"] = outlier_idx
    dbg["inliers"] = inliers

    strong = (ratio >= OUTLIER_RATIO_TH) and (delta >= OUTLIER_DELTA_TH)
    dbg["size3_strong_outlier"] = bool(strong)

    return inliers, [outlier_idx], dbg


def decide_split_v2(X: np.ndarray, titles: List[str]) -> SplitDecision:
    """
    Decide whether to split a cluster into two sub-clusters.

    Uses multi-signal approach:
    - Entity Jaccard similarity
    - Silhouette score
    - Centroid distance ratio
    - Title duplicate ratio

    Args:
        X: Embeddings array (n_samples, n_features)
        titles: List of article titles

    Returns:
        SplitDecision with should_split, reason, and debug info
    """
    n = int(X.shape[0])
    if n <= 2:
        return SplitDecision(False, "n<=2: split 의미 없음", {"n": n})

    debug: Dict[str, Any] = {"n": n}

    # Size=3: special handling
    if n == 3:
        idx0, idx1, dbg3 = _split_size3_outlier(X)
        debug.update(dbg3)
        debug["sizes"] = (len(idx0), len(idx1))

        X0, X1 = X[idx0], X[idx1]
        intra_all = _avg_intra_dist(X)

        c0 = X0.mean(axis=0)
        c1 = X1.mean(axis=0)
        centroid_dist = float(np.linalg.norm(c0 - c1))
        dist_ratio = centroid_dist / (intra_all + 1e-6)

        debug["all_intra"] = float(intra_all)
        debug["centroid_dist"] = float(centroid_dist)
        debug["dist_ratio"] = float(dist_ratio)

        toks_all = _extract_entities_from_titles(titles)
        toks0 = [toks_all[i] for i in idx0]
        toks1 = [toks_all[i] for i in idx1]
        top0, _ = _top_entities_by_coverage(toks0, top_k=10)
        top1, _ = _top_entities_by_coverage(toks1, top_k=10)
        jac = _entity_jaccard_soft(top0[:8], top1[:8], ngram=2)

        debug["top_entities_0"] = top0[:10]
        debug["top_entities_1"] = top1[:10]
        debug["entity_jaccard"] = float(jac)
        debug["idx0"] = idx0
        debug["idx1"] = idx1

        if debug.get("size3_strong_outlier") and (jac <= SIZE3_JACCARD_TH or dist_ratio >= SIZE3_DIST_RATIO_TH):
            reason = f"size3 split(outlier + jaccard={jac:.2f} or dist_ratio={dist_ratio:.2f})"
            return SplitDecision(True, reason, debug)

        reason = f"size3 보류(outlier 부족 또는 분리 시그널 약함)"
        return SplitDecision(False, reason, debug)

    # n>=4: 2-way KMeans + multi-signal
    km = KMeans(n_clusters=2, n_init=10, random_state=42)
    y = km.fit_predict(X)
    idx0 = np.where(y == 0)[0].tolist()
    idx1 = np.where(y == 1)[0].tolist()

    if not idx0 or not idx1:
        return SplitDecision(False, "KMeans split 실패", {"n": n})

    debug["sizes"] = (len(idx0), len(idx1))
    debug["idx0"] = idx0
    debug["idx1"] = idx1

    X0, X1 = X[idx0], X[idx1]

    intra_all = _avg_intra_dist(X)
    intra0 = _avg_intra_dist(X0)
    intra1 = _avg_intra_dist(X1)

    debug["all_intra"] = float(intra_all)
    debug["intra0"] = float(intra0)
    debug["intra1"] = float(intra1)

    centroid_dist = float(np.linalg.norm(km.cluster_centers_[0] - km.cluster_centers_[1]))
    debug["centroid_dist"] = float(centroid_dist)

    dist_ratio = centroid_dist / (intra_all + 1e-6)
    debug["dist_ratio"] = float(dist_ratio)

    # Silhouette
    sil = -1.0
    if len(idx0) >= 2 and len(idx1) >= 2:
        try:
            y2 = np.zeros(n, dtype=np.int32)
            y2[idx1] = 1
            sil = float(silhouette_score(X, y2))
        except Exception:
            sil = -1.0
    debug["silhouette"] = float(sil)

    s0, s1 = len(idx0), len(idx1)
    size_ratio = min(s0, s1) / max(s0, s1)
    debug["size_ratio"] = float(size_ratio)

    # Entity Jaccard
    toks_all = _extract_entities_from_titles(titles)
    toks0 = [toks_all[i] for i in idx0]
    toks1 = [toks_all[i] for i in idx1]

    top0, _ = _top_entities_by_coverage(toks0, top_k=10)
    top1, _ = _top_entities_by_coverage(toks1, top_k=10)

    debug["top_entities_0"] = top0[:10]
    debug["top_entities_1"] = top1[:10]

    jac = _entity_jaccard_fuzzy(top0[:8], top1[:8], ngram=2, match_th=0.55)
    debug["entity_jaccard"] = float(jac)

    # Duplicate title ratio
    dup_ratio = _duplicate_title_ratio(titles)
    debug["dup_title_ratio"] = float(dup_ratio)

    # VETO gates
    if jac >= VETO_JACCARD_TH:
        return SplitDecision(False, f"VETO: entity_jaccard={jac:.2f} >= {VETO_JACCARD_TH}", debug)

    if dup_ratio >= VETO_DUP_TITLE_RATIO:
        return SplitDecision(False, f"VETO: dup_title_ratio={dup_ratio:.2f} >= {VETO_DUP_TITLE_RATIO}", debug)

    # FORCE gate
    if (n >= FORCE_MIN_N) and (jac <= FORCE_JACCARD_TH) and (sil >= FORCE_SIL_TH):
        return SplitDecision(True, f"FORCE: jaccard={jac:.2f}, sil={sil:.2f}", debug)

    # Scoring
    score = 0.0
    reasons: List[str] = []

    if dist_ratio >= DIST_RATIO_GOOD:
        score += 0.9
        reasons.append(f"dist_ratio={dist_ratio:.2f}↑")
    elif dist_ratio >= 1.08:
        score += 0.45

    if sil >= SIL_MIN:
        score += 0.9
        reasons.append(f"sil={sil:.2f}↑")
    elif sil >= 0.07:
        score += 0.35

    if jac <= 0.15:
        score += 0.8
        reasons.append(f"jaccard={jac:.2f}↓")
    elif jac <= 0.35:
        score += 0.35

    if intra_all > 1e-6:
        improve0 = intra0 / intra_all
        improve1 = intra1 / intra_all
        if improve0 <= 0.75 and improve1 <= 0.75:
            score += 0.7
            reasons.append("intra_improve↑")
        elif improve0 <= 0.9 and improve1 <= 0.9:
            score += 0.25

    # Size imbalance penalty
    if n >= 6 and size_ratio < 0.2:
        score -= 0.4
        reasons.append("size_imbalance_penalty")

    debug["score"] = float(score)

    if score >= SPLIT_SCORE_TH:
        return SplitDecision(True, f"split 권장(score={score:.2f}): " + ", ".join(reasons), debug)

    return SplitDecision(False, f"split 보류(score={score:.2f} < {SPLIT_SCORE_TH})", debug)


def apply_split_v2_to_clusters(
    cluster_idx_groups: Dict[int, List[int]],
    data: Dict
) -> List[Tuple[List[int], List[str]]]:
    """
    Apply split_v2 to all clusters and return final groups.

    Args:
        cluster_idx_groups: {cluster_id: [indices]} from HDBSCAN
        data: Data dict with 'ids', 'titles', 'embeddings'

    Returns:
        List of (article_ids, titles) tuples for each final group
    """
    final_groups = []

    for cid, idxs in cluster_idx_groups.items():
        cluster_titles = [data['titles'][i] for i in idxs]
        cluster_ids = [int(data['ids'][i]) for i in idxs]
        cluster_X = data['embeddings'][idxs]

        dec = decide_split_v2(cluster_X, cluster_titles)

        if dec.should_split:
            dbg = dec.debug or {}
            idx0 = dbg.get('idx0', [])
            idx1 = dbg.get('idx1', [])

            if idx0:
                group0_ids = [cluster_ids[j] for j in idx0]
                group0_titles = [cluster_titles[j] for j in idx0]
                final_groups.append((group0_ids, group0_titles))
            if idx1:
                group1_ids = [cluster_ids[j] for j in idx1]
                group1_titles = [cluster_titles[j] for j in idx1]
                final_groups.append((group1_ids, group1_titles))
        else:
            final_groups.append((cluster_ids, cluster_titles))

    return final_groups

def run_clustering(
    min_cluster_size: int = 3, 
    min_samples: int = 2, 
    min_target: int = 0
) -> Tuple[Dict[int, List[int]], List[int], Dict[str, Any]]:
    """
    Execute the clustering process using DB embeddings.
    
    Args:
        min_cluster_size: HDBSCAN parameter
        min_samples: HDBSCAN parameter
        min_target: Minimum target (unused in this function but kept for compat)
        
    Returns:
        Tuple of (cluster_groups, cluster_ids, data_dict)
    """
    from db.connection import get_connection
    from core.clusterer import NewsClusterer, load_embeddings_from_db, get_cluster_groups
    
    conn = get_connection()
    try:
        data = load_embeddings_from_db(conn, exclude_clustered=True)
    finally:
        conn.close()
        
    ids = data['ids']
    embeddings = data['embeddings']
    
    if len(ids) == 0:
        return {}, [], data
        
    clusterer = NewsClusterer(
        min_cluster_size=min_cluster_size, 
        min_samples=min_samples
    )
    labels = clusterer.fit_predict(embeddings)
    
    # Simple grouping by label
    cluster_groups = get_cluster_groups(ids, labels)
    sorted_cluster_ids = sorted(cluster_groups.keys())
    
    return cluster_groups, sorted_cluster_ids, data
