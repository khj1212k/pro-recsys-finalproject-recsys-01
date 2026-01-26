from fastapi import FastAPI
from app.api import onboarding, newsletter

app = FastAPI()

app.include_router(onboarding.router)
app.include_router(newsletter.router)
