from fastapi import FastAPI
from app.api import onboarding

app = FastAPI()

app.include_router(onboarding.router)
