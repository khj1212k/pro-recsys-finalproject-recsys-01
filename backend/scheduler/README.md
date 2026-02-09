## Scheduler Scripts

Backend 배치(Batch) 스크립트 포함  

### 주요 파일 및 기능

#### `calculate_ranking.py`
**인기도 및 최신성 기반 뉴스레터 순위 계산 배치**

- **실행 시간**: 매일 오후 5:45 (KST)
- **기능**:
  1. 당일 17:45 KST 이전에 생성된 뉴스레터들을 조회
  2. 각 뉴스레터에 대해 점수를 계산
     - `Score = exp(-age_hours/48) + log1p(raw_news_count)/5`
     - 최신 글일수록 원본 기사 수가 많을수록 높은 점수 부여
  3. 점수 순으로 정렬 후 상위 항목들을 선정
  4. `NewsLettersCategory` 테이블에 배치 결과(추천 목록)를 저장