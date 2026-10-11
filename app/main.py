from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health, qbo

app = FastAPI(
    title="Invoice QBO Automation",
    description="Invoice processing and QuickBooks Online integration",
    version="0.1.0",
)

app.include_router(health.router, tags=["Health"])
app.include_router(
    documents.router,
    prefix="/api/documents",
    tags=["Documents"],
)
app.include_router(
    qbo.router,
    prefix="/api/qbo",
    tags=["QuickBooks"],
)
# Resolve paths relative to this module, including on Windows.
app_directory = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(app_directory / "static")), name="static")


@app.get("/", include_in_schema=False)
async def web_ui():
    return FileResponse(app_directory / "templates" / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@app.get("/api")
async def api_info():
    return {
        "name": "Invoice QBO Automation",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
    }