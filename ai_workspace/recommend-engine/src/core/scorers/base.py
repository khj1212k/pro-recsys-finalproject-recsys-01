from abc import ABC, abstractmethod


class BaseScorer(ABC):
    """모든 점수 계산기(Scorer)가 상속받아야 할 추상 기본 클래스."""

    @abstractmethod
    def score(self, user_vectors, candidate_vectors, weights=None):
        """
        후보군에 대한 추천 점수를 계산함.
        """
        pass

    def calculate_weights(self, history_items, user_prefs):
        """
        사용자 이력에 대한 가중치를 계산함.
        기본적으로는 가중치를 사용하지 않으므로 None을 반환함.
        (필요한 하위 클래스에서 오버라이딩하여 구현)
        
        Args:
            history_items (List[dict]): 사용자가 본 뉴스 정보 리스트
            user_prefs (List[str] or str): 사용자 선호 카테고리
            
        Returns:
            List[float] or None: 각 이력 아이템별 가중치 리스트
        """
        return None
