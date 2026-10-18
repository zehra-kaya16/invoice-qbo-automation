from pathlib import Path
import time
from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI, Depends, Request
from app.core.auth import company_access
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health, qbo, recovery, auth, export, batch, analytics, integrations

app = FastAPI(
    title="Invoice QBO Automation",
    description="Invoice processing and QuickBooks Online integration",
    version="0.1.0",
)

app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(
    documents.router,
    prefix="/api/documents",
    tags=["Documents"],
    dependencies=[Depends(company_access)],
)
app.include_router(
    qbo.router,
    prefix="/api/qbo",
    tags=["QuickBooks"],
    dependencies=[Depends(company_access)],
)
app.include_router(recovery.router, prefix="/api/documents", tags=["QBO recovery"], dependencies=[Depends(company_access)])
app.include_router(batch.router, prefix="/api/batch", tags=["Batch"], dependencies=[Depends(company_access)])
app.include_router(export.router, prefix="/api/export", tags=["Export"], dependencies=[Depends(company_access)])

app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"], dependencies=[Depends(company_access)])

app.include_router(integrations.router, prefix="/api/integrations", tags=["Integrations"], dependencies=[Depends(company_access)])
app.include_router(integrations.ingress, prefix="/api/webhooks", tags=["Signed webhooks"])

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

async def record_request_metric(request, clock, status):
    user = getattr(request.state, "analytics_principal", None)
    if user is not None and request.url.path.startswith(("/api/documents", "/api/batch", "/api/qbo")):
        from app.services.analytics.tracker import track_request
        await run_in_threadpool(track_request, user, request.method, status, (time.perf_counter() - clock) * 1000)


@app.middleware("http")
async def private_responses(request: Request, call_next):
    clock = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        await record_request_metric(request, clock, 500)
        raise
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    await record_request_metric(request, clock, response.status_code)
    return response
