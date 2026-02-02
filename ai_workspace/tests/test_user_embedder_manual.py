import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.user_embedder import UserEmbedder

def test_user_embedder():
    """UserEmbedder 수동 테스트 (Bulk Processing)"""
    logging.basicConfig(level=logging.INFO)
    print("="*60 + "\nUserEmbedder Bulk 처리 테스트\n" + "="*60)
    
    try:
        embedder = UserEmbedder(pref_weight=0.4)
        print("✅ 초기화 성공")
        
        print("🚀 배치 업데이트 실행 (Fetch All -> Save All)...")
        stats = embedder.batch_update_all_users()
        
        print(f"✅ 결과: {stats}")
        
    except Exception as e:
        print(f"❌ 오류 발생: {e}")

if __name__ == "__main__":
    test_user_embedder()
