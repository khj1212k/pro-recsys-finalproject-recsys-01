# scripts/generate_daily_stats.py
import pandas as pd
from sqlalchemy import create_engine, text
from typing import Dict, List, Any
import json
import os
import sys
from datetime import datetime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.common import load_config, get_logger

logger = get_logger("StatsAggregator")

class DailyStatsAggregator:
    """
    Cold Start 대응 및 EDA 분석을 위한 통계 집계 클래스
    """
    
    def __init__(self):
        config = load_config()
        db_conf = config['database']
        url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
        self.engine = create_engine(url)
        self.top_k = config['recommendation'].get('top_k', 20)
        
    def get_global_top_k(self, days_ago: int = 1) -> List[int]:
        """전체 인기 뉴스 (최근 N일 기준)"""
        logger.info(f"📊 Global Top-K 집계 중...")
        query = text(f"""
            SELECT news_letter_id, COUNT(*) as click_cnt
            FROM user_newsletter_ctr_log
            WHERE created_at >= NOW() - INTERVAL '{days_ago} DAYS'
            GROUP BY news_letter_id
            ORDER BY click_cnt DESC
            LIMIT :k
        """)
        with self.engine.connect() as conn:
            result = conn.execute(query, {"k": self.top_k}).fetchall()
        return [row[0] for row in result]

    def get_age_group_top_k(self, days_ago: int = 1) -> Dict[int, List[int]]:
        """나이대별 인기 뉴스"""
        logger.info(f"📊 Age Group Top-K 집계 중...")
        query = text(f"""
            SELECT 
                CASE 
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 25 THEN 1
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 35 THEN 2
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 45 THEN 3
                    ELSE 4
                END as age_group,
                l.news_letter_id,
                COUNT(*) as click_cnt
            FROM user_newsletter_ctr_log l
            JOIN "user" u ON l.user_id = u.user_id
            WHERE l.created_at >= NOW() - INTERVAL '{days_ago} DAYS'
            GROUP BY age_group, l.news_letter_id
            ORDER BY age_group, click_cnt DESC
        """)
        with self.engine.connect() as conn:
            df = pd.read_sql(query, conn)
        result = {}
        for group, sub_df in df.groupby('age_group'):
            result[int(group)] = sub_df['news_letter_id'].head(self.top_k).tolist()
        return result

    # --- [추가] EDA용 분석 함수 ---
    
    def analyze_recency_decay(self):
        """뉴스 발행 후 경과 시간별 클릭 수 분포 (최신성 근거)"""
        logger.info("📈 Recency Decay 분석 중...")
        query = text("""
            SELECT 
                FLOOR(EXTRACT(EPOCH FROM (l.created_at - n.created_at))/3600) as hours_diff,
                COUNT(*) as click_cnt
            FROM user_newsletter_ctr_log l
            JOIN news_letter n ON l.news_letter_id = n.news_letter_id
            WHERE l.created_at >= n.created_at -- 로그가 발행보다 늦은 경우만
            AND l.created_at >= NOW() - INTERVAL '28 DAYS'
            GROUP BY hours_diff
            HAVING hours_diff < 72 -- 72시간 이내만 조회
            ORDER BY hours_diff ASC
        """)
        with self.engine.connect() as conn:
            data = conn.execute(query).fetchall()
        return {str(int(row[0])): int(row[1]) for row in data}

    def analyze_user_diversity(self):
        """유저별 선호 카테고리 개수 분포 (MMR Lambda 근거)"""
        logger.info("👥 User Category Diversity 분석 중...")
        query = text("""
            SELECT 
                user_id, COUNT(*) as cat_cnt 
            FROM user_preferred_category 
            GROUP BY user_id
        """)
        with self.engine.connect() as conn:
            df = pd.read_sql(query, conn)
        
        # 분포 계산 (1개, 2개, 3개...)
        dist = df['cat_cnt'].value_counts().sort_index().to_dict()
        return {str(k): int(v) for k, v in dist.items()}

    def analyze_sparsity(self):
        """데이터 희소성 분석 (LightGBM 선정 근거)"""
        logger.info("📉 Data Sparsity 분석 중...")
        with self.engine.connect() as conn:
            user_cnt = conn.execute(text('SELECT COUNT(*) FROM "user"')).scalar()
            log_cnt = conn.execute(text('SELECT COUNT(*) FROM user_newsletter_ctr_log')).scalar()
            news_cnt = conn.execute(text('SELECT COUNT(*) FROM news_letter')).scalar()
            
        return {
            "total_users": user_cnt,
            "total_logs": log_cnt,
            "avg_clicks_per_user": round(log_cnt / user_cnt, 2) if user_cnt else 0
        }

    def save_stats_to_json(self):
        stats = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "recsys_cold_start": {
                "global_top": self.get_global_top_k(),
                "age_group_top": self.get_age_group_top_k()
            },
            "eda_analysis": {
                "recency_decay": self.analyze_recency_decay(),
                "user_diversity": self.analyze_user_diversity(),
                "sparsity": self.analyze_sparsity()
            }
        }
        
        save_path = "results/daily_stats.json"
        os.makedirs("results", exist_ok=True)
        
        with open(save_path, "w", encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ 통계 및 EDA 데이터 저장 완료: {save_path}")

if __name__ == "__main__":
    agg = DailyStatsAggregator()
    agg.save_stats_to_json()