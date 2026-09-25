from fastapi import FastAPI
from app.api import onboarding, auth, users, newsletter, log, recsys

app = FastAPI()

app.include_router(onboarding.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(newsletter.router)
app.include_router(log.router)
app.include_router(recsys.router)
