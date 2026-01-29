## Database Schema 정의

### 주요 파일 및 테이블 설명

#### `news.py`
- **`Category`**: 뉴스 카테고리 정보 (정치, 경제, IT/과학, 사회, 생활/문화, 스포츠, 세계 : 7개)
- **`Press`**: 언론사 정보
- **`RSS_URI`**: 각 언론사 RSS 주소 정보
- **`NewsRaw`**: 크롤링된 원본 뉴스 데이터
- **`NewsLetter`**: 뉴스레터 정보
- **`NewsLetterCategories`**: 뉴스레터와 카테고리 간의 N:M 매핑 테이블
- **`ClusterHistory`**: 뉴스 기사 클러스터링 배치 작업 로그/히스토리

#### `user.py`
- **`User`**: 사용자 계정 및 프로필 정보
- **`UserPreferredCategories`**: 사용자 - 선호 카테고리 매핑
- **`UserPreferredNewsletter`**: 사용자 - 선호 뉴스레터 매핑

#### `batch.py`
- **`NewsLettersCategory`**: 카테고리별 인기/추천 뉴스레터 배치 결과 저장
- **`NewsLetterTodayBatch`**: 사용자별 "오늘의 추천" 뉴스레터 배치 결과 저장