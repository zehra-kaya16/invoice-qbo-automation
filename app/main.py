from fastapi import FastAPI

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
@app.get("/api")
async def api_info():
    return {
        "name": "Invoice QBO Automation",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
    }