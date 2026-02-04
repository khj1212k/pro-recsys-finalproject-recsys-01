# Stage0: 사용자 임베딩 생성
# - 선호 뉴스레터 + 클릭 이력 기반 가중 평균
# - 시간 감쇠 적용하여 최신 관심사 반영

import math
import logging
import numpy as np
from datetime import datetime, timedelta
from typing import Dict
from db.connection import get_connection

logger = logging.getLogger(__name__)

class UserEmbedder:
    def __init__(self, pref_weight=0.4, decay_rate=0.05, lookback=90, min_inte=1):
        self.P_W = pref_weight
        self.DECAY = decay_rate
        self.LOOKBACK = lookback
        self.MIN_INTE = min_inte

    def batch_update_all_users(self) -> Dict[str, int]:
        stats = {'success': 0, 'failed': 0, 'skipped': 0}
        updates = []
        
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # 1. 대상 사용자 조회
                cur.execute('SELECT user_id FROM "user" WHERE user_embedding IS NULL')
                targets = [r[0] for r in cur.fetchall()]
                if not targets: return stats

                # 2. 데이터 일괄 조회
                logger.info(f"Processing {len(targets)} users...")
                t_tuple = tuple(targets) 
                
                # (1) 선호도
                cur.execute(f"SELECT user_id, news_letter_id \
                              FROM user_preferred_newsletter \
                              WHERE user_id IN %s", (t_tuple,)) 
                prefs_map = {}
                for uid, nid in cur.fetchall():
                    prefs_map.setdefault(uid, []).append(nid)

                # (2) 이력
                cutoff = datetime.now() - timedelta(days=self.LOOKBACK)
                cur.execute(f"SELECT user_id, news_letter_id, created_at \
                              FROM user_newsletter_ctr_log \
                              WHERE user_id IN %s AND created_at >= %s", (t_tuple, cutoff))
                hist_map = {} 
                all_nids = set() 
                for uid, nid, dt in cur.fetchall():
                    hist_map.setdefault(uid, []).append((nid, dt))
                    all_nids.add(nid)
                    
                for p in prefs_map.values(): 
                    all_nids.update(p) # 선호 뉴스레터, 이력 뉴스레터 모두 포함

                # (3) 벡터
                vec_map = {} 
                if all_nids:
                    cur.execute("SELECT news_letter_id, news_letter_embedding \
                                FROM news_letter \
                                WHERE news_letter_id = ANY(%s)", (list(all_nids),))
                    vec_map = {nid: np.array(v) for nid, v in cur.fetchall() if v} 

                # 3. 계산
                now = datetime.now()
                for uid in targets: 
                    ps = prefs_map.get(uid, []) 
                    hs = hist_map.get(uid, []) 

                    if len(ps) + len(hs) < self.MIN_INTE:
                        stats['skipped'] += 1; continue

                    # 가중치 계산
                    weights = {nid: self.P_W for nid in ps} # 선호 뉴스레터 가중치(0.4)
                    for nid, dt in hs: # 이력 뉴스레터 가중치
                        days = (now - dt).total_seconds() / 86400 # 날짜 차이 계산
                        # 최종 가중치 = (1 - 선호 가중치) * e^(-감쇠율 * 날짜 차이)
                        weights[nid] = weights.get(nid, 0.0) + (1 - self.P_W) * math.exp(-self.DECAY * days)

                    # 벡터가 있는 뉴스레터만
                    valid_ids = [n for n in weights if n in vec_map]
                    if not valid_ids:
                        stats['skipped'] += 1; continue
                        
                    # 벡터 가중 합
                    vs = np.stack([vec_map[n] for n in valid_ids])
                    ws = np.array([weights[n] for n in valid_ids]).reshape(-1, 1)
                    emb = np.sum(vs * ws, axis=0) / np.sum(ws)
                    
                    # 정규화, :=는 계산 및 변수저장 
                    if (norm := np.linalg.norm(emb)) > 0: # 0으로 나누는거 방지
                        updates.append(((emb / norm).tolist(), uid))
                        stats['success'] += 1
                    else:
                        stats['failed'] += 1

                # 4. 저장
                if updates:
                    cur.executemany('UPDATE "user" \
                                     SET user_embedding=%s \
                                     WHERE user_id=%s', updates)
            
            conn.commit() 

        except Exception as e:
            conn.rollback() 
            logger.error(f"Error: {e}")
            stats = {'success': 0, 
                     'failed': len(targets) if 'targets' in locals() else 0, 
                     'skipped': 0}
        finally:
            conn.close() 
            
        return stats
