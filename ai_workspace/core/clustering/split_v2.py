# 클러스터 분할 판단 로직
# - 1차 클러스터가 서로 다른 주제를 포함하는지 검사
# - Jaccard 유사도, Silhouette 점수 등으로 분할 여부 결정

import re
import numpy as np
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from sklearn.metrics import silhouette_score
from sklearn.cluster import KMeans

# 제목에서 키워드 추출 시 제외할 불용어
STOPWORDS = {
    "있다","없다","했다","한다","됐다","된다","이번","오늘","내일","어제","관련","대한","통해","위해","기준",
    "가능","필요","전망","확대","강화","추진","논란","갈등","문제","발표","공개","확인","진행","조치","대응","검토","주목",
    "정부","국회","대통령","위원회","당국","업계","시장","증가","감소","상승","하락",
}

# 3개짜리 클러스터 아웃라이어 판단 임계값
OUTLIER_RATIO_TH = 1.35      
OUTLIER_DELTA_TH = 0.20      
SIZE3_JACCARD_TH = 0.18      
SIZE3_DIST_RATIO_TH = 1.20   

# 분할 거부/강제 조건 =
VETO_JACCARD_TH = 0.55     
VETO_DUP_TITLE_RATIO = 0.50  
FORCE_JACCARD_TH = 0.12     
FORCE_SIL_TH = 0.18      
FORCE_MIN_N = 5           

# 점수 기반 분할 판단 기준
SPLIT_SCORE_TH = 1.80        
SIL_MIN = 0.10              
DIST_RATIO_GOOD = 1.20       

@dataclass
class SplitDecision:
    should_split: bool                           
    reason: str                                 
    debug: Dict[str, Any] = field(default_factory=dict)  

# 한글 형태소 분석기 (Kiwi)
_KIWI = None

def _get_kiwi():
    global _KIWI
    if _KIWI is None:
        try:
            from kiwipiepy import Kiwi
            _KIWI = Kiwi()
        except ImportError:
            _KIWI = False
    return _KIWI

def _tokenize_title(title: str) -> List[str]:
    title = (title or "").strip()
    if not title: return []
    
    kiwi = _get_kiwi()
    if kiwi:
        try:
            analyzed = kiwi.analyze(title)
            tokens = analyzed[0][0] if analyzed else []
            return [t.form.strip() for t in tokens 
                    if t.tag in ("NNP", "NNG", "SL", "SH") 
                    and len(t.form.strip()) >= 2 and t.form.strip() not in STOPWORDS]
        except Exception:
            pass
            
    # Fallback / ImportError handling
    raw = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    return [w for w in raw if w not in STOPWORDS]

def _top_entities_by_coverage(tokens_per_doc: List[List[str]], top_k: int = 8):
    n = len(tokens_per_doc)
    if n == 0: return [], {}
    doc_has = defaultdict(int)
    for toks in tokens_per_doc:
        for w in set(toks): doc_has[w] += 1
    cov = {w: doc_has[w] / n for w in doc_has}
    ranked = sorted(cov.items(), key=lambda x: (x[1], len(x[0])), reverse=True)
    return [w for w, _ in ranked[:top_k]], cov

def _char_ngrams(s: str, n: int = 2) -> set:
    s = (s or "").strip()
    if len(s) < n: return {s} if s else set()
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
    if not s0 and not s1: return 1.0
    if not s0 or not s1: return 0.0
    return len(s0 & s1) / len(s0 | s1)

def _entity_jaccard_fuzzy(top0: List[str], top1: List[str], ngram: int = 2, match_th: float = 0.55) -> float:
    A, B = [w for w in top0 if w], [w for w in top1 if w]
    if not A or not B: return 1.0 if not A and not B else 0.0
    used_b, inter = [False] * len(B), 0
    for a in A:
        best_j, best_sim = -1, 0.0
        for j, b in enumerate(B):
            if used_b[j]: continue
            set_a, set_b = _char_ngrams(a, ngram), _char_ngrams(b, ngram)
            sim = len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0
            if sim > best_sim: best_sim, best_j = sim, j
        if best_j >= 0 and best_sim >= match_th:
            used_b[best_j], inter = True, inter + 1
    union = len(A) + len(B) - inter
    return inter / union if union > 0 else 1.0

def _avg_intra_dist(X: np.ndarray) -> float:
    n = X.shape[0]
    if n <= 1: return 0.0
    dists = [np.linalg.norm(X[i] - X[j]) for i in range(n) for j in range(i + 1, n)]
    return float(np.mean(dists)) if dists else 0.0

def _duplicate_title_ratio(titles: List[str]) -> float:
    def norm(t):
        t = re.sub(r"\[[^\]]+\]|\([^)]*\)", " ", (t or "").lower())
        return re.sub(r"[\s\-_·—–,.:;!?\"'""''`~]+", "", t)
    norms = [x for x in [norm(t) for t in titles] if x]
    if not norms: return 0.0
    return max(Counter(norms).values()) / len(norms)

def _split_size3_outlier(X: np.ndarray) -> Tuple[List[int], List[int], Dict]:
    dbg = {}
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
    strong = (ratio >= OUTLIER_RATIO_TH) and (delta >= OUTLIER_DELTA_TH)
    dbg["size3_strong_outlier"] = bool(strong)

    return inliers, [outlier_idx], dbg

# ========================================
# Core Decision Logic
# ========================================
def decide_split_v2(X: np.ndarray, titles: List[str]) -> SplitDecision:
    n = int(X.shape[0])
    if n <= 2: return SplitDecision(False, "n<=2: split 의미 없음", {"n": n})

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

        toks_all = [_tokenize_title(t) for t in titles]
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
            return SplitDecision(True, f"size3 split(outlier + jaccard={jac:.2f})", debug)

        return SplitDecision(False, "size3 보류", debug)

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
    intra0, intra1 = _avg_intra_dist(X0), _avg_intra_dist(X1)

    debug["all_intra"], debug["intra0"], debug["intra1"] = float(intra_all), float(intra0), float(intra1)

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
        except Exception: pass
    debug["silhouette"] = float(sil)

    # Entity Jaccard
    toks_all = [_tokenize_title(t) for t in titles]
    toks0, toks1 = [toks_all[i] for i in idx0], [toks_all[i] for i in idx1]
    top0, _ = _top_entities_by_coverage(toks0, top_k=10)
    top1, _ = _top_entities_by_coverage(toks1, top_k=10)
    debug["top_entities_0"], debug["top_entities_1"] = top0[:10], top1[:10]

    jac = _entity_jaccard_fuzzy(top0[:8], top1[:8], ngram=2, match_th=0.55)
    debug["entity_jaccard"] = float(jac)

    # Duplicate title
    dup_ratio = _duplicate_title_ratio(titles)
    debug["dup_title_ratio"] = float(dup_ratio)

    # VETO
    if jac >= VETO_JACCARD_TH:
        return SplitDecision(False, f"VETO: jaccard={jac:.2f}", debug)
    if dup_ratio >= VETO_DUP_TITLE_RATIO:
        return SplitDecision(False, f"VETO: dup_title={dup_ratio:.2f}", debug)

    # FORCE
    if (n >= FORCE_MIN_N) and (jac <= FORCE_JACCARD_TH) and (sil >= FORCE_SIL_TH):
        return SplitDecision(True, f"FORCE: jac={jac:.2f}, sil={sil:.2f}", debug)

    # Scoring
    score, reasons = 0.0, []
    if dist_ratio >= DIST_RATIO_GOOD: score += 0.9; reasons.append(f"dist={dist_ratio:.2f}")
    elif dist_ratio >= 1.08: score += 0.45

    if sil >= SIL_MIN: score += 0.9; reasons.append(f"sil={sil:.2f}")
    elif sil >= 0.07: score += 0.35

    if jac <= 0.15: score += 0.8; reasons.append(f"jac={jac:.2f}")
    elif jac <= 0.35: score += 0.35
    
    if intra_all > 1e-6:
        imp0, imp1 = intra0/intra_all, intra1/intra_all
        if imp0 <= 0.75 and imp1 <= 0.75: score += 0.7; reasons.append("intra_imp")
        elif imp0 <= 0.9 and imp1 <= 0.9: score += 0.25

    size_ratio = min(len(idx0), len(idx1)) / max(len(idx0), len(idx1))
    if n >= 6 and size_ratio < 0.2: score -= 0.4; reasons.append("imbalance")

    debug["score"] = float(score)
    if score >= SPLIT_SCORE_TH:
        return SplitDecision(True, f"Score={score:.2f} ({', '.join(reasons)})", debug)

    return SplitDecision(False, f"Score={score:.2f} < TH", debug)