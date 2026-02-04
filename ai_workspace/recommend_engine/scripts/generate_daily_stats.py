# scripts/generate_daily_stats.py
import pandas as pd
from sqlalchemy import create_engine, text
from typing import Dict, List
import json
import os
import sys
from datetime import datetime

# 프로젝트 루트 경로 추가
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# [수정] logger.py 삭제 및 common.py 통합 반영
from src.utils.common import load_config, get_logger

# [수정] setup_logger -> get_logger
logger = get_logger("StatsAggregator")

class DailyStatsAggregator:
    """
    Cold Start 유저를 위한 통계 기반 추천 생성기
    - 전체 인기 뉴스
    - 나이대별 인기 뉴스
    """
    
    def __init__(self):
        config = load_config()
        db_conf = config['database']
        # URL 생성
        url = f"postgresql://{db_conf['user']}:{db_conf['password']}@{db_conf['host']}:{db_conf['port']}/{db_conf['dbname']}?client_encoding=utf8"
        self.engine = create_engine(url)
        self.top_k = config['recommendation'].get('top_k', 20)
        
    def get_global_top_k(self, days_ago: int = 1) -> List[int]:
        """전체 인기 뉴스 (최근 N일 기준)"""
        logger.info(f"📊 Global Top-K 집계 중 (최근 {days_ago}일)...")
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
        logger.info(f"📊 Age Group Top-K 집계 중 (최근 {days_ago}일)...")
        # User 테이블과 조인하여 나이대 계산 후 그룹핑
        query = text(f"""
            SELECT 
                CASE 
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 25 THEN 1 -- 18-24
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 35 THEN 2 -- 25-34
                    WHEN (EXTRACT(YEAR FROM NOW()) - u.user_birth_year) < 45 THEN 3 -- 35-44
                    ELSE 4 -- 45+
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

    def save_stats_to_json(self):
        """계산 결과를 JSON으로 저장"""
        stats = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "global_top": self.get_global_top_k(),
            "age_group_top": self.get_age_group_top_k()
        }
        
        save_path = "results/daily_stats.json"
        os.makedirs("results", exist_ok=True)
        
        with open(save_path, "w", encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ 통계 데이터 저장 완료: {save_path}")
        return stats

if __name__ == "__main__":
    agg = DailyStatsAggregator()
    agg.save_stats_to_json()