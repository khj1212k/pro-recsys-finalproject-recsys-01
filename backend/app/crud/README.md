## DB 쿼리 함수 정의
데이터베이스와 직접 상호작용하는 CRUD(Create, Read, Update, Delete) 로직 정의

### 주요 파일 및 기능

#### `user.py`
사용자 관리와 관련된 핵심 DB 작업을 처리합니다.

| 함수명 | 설명 |
| :--- | :--- |
| **`create_user`** | 새로운 사용자 생성 (회원가입) |
| **`get_user_by_email`** | 이메일로 사용자 조회 (로그인) |
| **`update_user_categories`** | 사용자의 관심 카테고리 업데이트 |
| **`update_user_newsletters`** | 사용자가 선택한 관심 뉴스레터 업데이트 |