# Statistics Module (Experimental)

## 개요
개인화 모델(LightGBM)이 커버하지 못하는 **신규 유저(Cold Start)**에게 제공할 "통계 기반 인기 뉴스"를 생성하는 모듈입니다.

## 주요 기능
1. **Global Top-K:** 최근 24시간 동안 가장 많이 클릭된 뉴스
2. **Age Group Top-K:** 20대, 30대 등 연령대별 인기 뉴스

## 실행 방법
```bash
python src/statistics/daily_aggregator.py
```

## 결과물 포맷 (results/daily_stats.json)
이 파일은 API 서버가 신규 유저 접속 시 참조하는 용도입니다.

```json
{
  "created_at": "2026-02-02 14:00:00",
  "global_top": [101, 102, 105, ...],
  "age_group_top": {
    "1": [201, 202, ...], // 18-24세
    "2": [301, 302, ...], // 25-34세
    "3": [401, 402, ...]  // 35-44세
  }
}
```

## 활용 방안 (제안)
- 배치 타임(17:30)에 이 스크립트를 실행하여 `results/daily_stats.json` 생성
- API 서버는 신규 유저가 들어오면:
  1. 유저의 나이 정보를 확인
  2. `daily_stats.json`의 `age_group_top`에서 해당 나이대 리스트 반환
  3. 나이 정보도 없으면 `global_top` 반환