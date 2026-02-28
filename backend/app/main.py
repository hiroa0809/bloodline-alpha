from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(
    title="Bloodline Alpha API",
    description="Expected value calculation API for JRA horse racing",
    version="1.0.0"
)

# CORS設定 (Next.js フロントエンドからのアクセスを許可)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"message": "Welcome to Bloodline Alpha API"}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

from app.api import score
app.include_router(score.router)

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
