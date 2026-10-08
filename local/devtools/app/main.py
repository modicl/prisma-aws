"""API del panel de desarrollo. SOLO lectura y SOLO local: no tiene autenticación y no se despliega a AWS."""
import asyncio
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Path as RutaParam, Query
from fastapi.staticfiles import StaticFiles

from app import kafka_view, queues, sfn
from app.common import TIMEOUT_SECONDS, guarded

app = FastAPI(title="prisma-devtools", docs_url=None, redoc_url=None)

Estado = Literal["RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/executions")
async def executions(status: Optional[Estado] = None, limit: int = Query(50, ge=1, le=200)):
    return await guarded(sfn.list_executions, status, limit)


@app.get("/api/executions/{name}")
async def execution(name: str = RutaParam(pattern=r"^[A-Za-z0-9._-]{1,80}$")):
    try:
        data = await asyncio.wait_for(sfn.execution_timeline(name), TIMEOUT_SECONDS)
    except sfn.ExecutionNotFound:
        raise HTTPException(status_code=404, detail="La ejecución no existe")
    except Exception as exc:  # noqa: BLE001 — dependencia caída: se marca el panel, no se rompe la API
        return {"available": False, "error": type(exc).__name__}
    return {"available": True, **data}


@app.get("/api/queues")
async def colas():
    return await guarded(queues.list_queues)


@app.get("/api/kafka")
async def kafka():
    return await guarded(kafka_view.kafka_overview)


# La página va al final: así `/api/*` y `/health` tienen prioridad sobre los archivos estáticos.
_WEB = Path(__file__).resolve().parent.parent / "web"
if _WEB.is_dir():
    app.mount("/", StaticFiles(directory=_WEB, html=True), name="web")
