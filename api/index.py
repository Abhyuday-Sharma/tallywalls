"""
Vercel Serverless Function entry point for TallyWalls ASGI FastAPI application.
"""
import os
import sys
import traceback

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT_DIR, "app")
for d in [ROOT_DIR, APP_DIR]:
    if d not in sys.path:
        sys.path.insert(0, d)

try:
    from app.main import app
except Exception as e:
    err_tb = traceback.format_exc()
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    app = FastAPI()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def catch_all_error(full_path: str = ""):
        return HTMLResponse(
            content=f"<h1>TallyWalls Startup Error on Vercel</h1><pre>{err_tb}</pre>",
            status_code=500
        )
