from fastapi import FastAPI
from app.api import onboarding, auth, users

app = FastAPI()

app.include_router(onboarding.router)
app.include_router(auth.router)
app.include_router(users.router)