# src/data/__init__.py
"""
Data 패키지
- DataLoader: DB/File 데이터 로딩 및 전처리
- LGBMDataset: LightGBM용 학습/추론 데이터셋 생성
"""

# 1. 핵심 로더 및 데이터 클래스
from .data_loader import (
    DataLoader,
    UserProfile,
    NewsItem,
    ClickEvent,
)

# 2. 유용한 상수 및 유틸리티 (외부에서 참조할 수 있음)
from .data_loader import (
    CATEGORY_ID_TO_NAME,
    CATEGORY_NAME_TO_ID,
    AGE_BAND_TO_IDX,
    GENDER_STR_TO_IDX,
    NUM_CATEGORIES,
    compute_time_decay
)

# 3. [New] LightGBM 데이터셋 (PyTorch Dataset 제거됨)
from .lgbm_dataset import LGBMDataset

__all__ = [
    'DataLoader',
    'UserProfile',
    'NewsItem',
    'ClickEvent',
    'LGBMDataset',
    'CATEGORY_ID_TO_NAME',
    'CATEGORY_NAME_TO_ID',
    'AGE_BAND_TO_IDX',
    'GENDER_STR_TO_IDX',
    'NUM_CATEGORIES',
    'compute_time_decay',
]