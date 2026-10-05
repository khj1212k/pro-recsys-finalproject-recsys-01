from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.api import onboarding, auth, users, newsletter, log, recsys
from app.recsys import runtime as recsys_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 추천 서비스를 기동할 때 조립해 설정 오류를 바로 드러내고, 종료할 때 작업 스레드와
    # 전용 커넥션 풀을 정리한다(app/recsys/runtime.py).
    recsys_runtime.startup()
    try:
        yield
    finally:
        recsys_runtime.shutdown()


app = FastAPI(lifespan=lifespan)

app.include_router(onboarding.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(newsletter.router)
app.include_router(log.router)
app.include_router(recsys.router)
