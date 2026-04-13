from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from config import settings
from voice_handler import router as voice_router

app = FastAPI(title="RouteMaster Voice API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(voice_router)

@app.get("/api/health")
async def health_check():
    return {"status": "healthy"}

@app.post("/api/upload-context")
async def upload_context(request: Request):
    """Endpoint to upload image context for the session."""
    data = await request.json()
    return {"status": "success", "message": "Context uploaded"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.BACKEND_PORT)
