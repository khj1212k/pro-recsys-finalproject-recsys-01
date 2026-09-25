import os
import sys

BACKEND_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend"
)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

# app.security는 임포트 시점에 int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES"))를 호출하므로
# 라우터를 임포트하는 테스트를 위해 더미 값을 채운다(실제 토큰 검증은 테스트에서 override).
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
