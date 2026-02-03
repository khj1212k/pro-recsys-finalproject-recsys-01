## API Routers 정의

### 주요 파일 및 기능

| 파일명  | 주요 엔드포인트 |
| :--- | :--- |
| **`auth.py`** | `/auth/signup`, `/auth/login` |
| **`users.py`** | `/users/me`, `/users/me/categories` |
| **`newsletter.py`** | `/newsletters/today`, `/newsletters/{id}` |
| **`onboarding.py`** | `/onboarding/news` |
| **`user_check.py`** | `get_current_user` (JWT 기반 인증) |
| **`logs.py`** | `/logs/newsletter/click` |