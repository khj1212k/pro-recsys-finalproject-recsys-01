# backend/main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

# ★ CORS 설정 (React 포트 3000번 허용)
origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/test")
def read_root():
    return {"message": "성공! 백엔드와 연결되었습니다. 🎉"}

if __name__ == "__main__":
    # host="0.0.0.0"으로 해야 VPN 외부에서 접속 가능
    uvicorn.run(app, host="0.0.0.0", port=8000)