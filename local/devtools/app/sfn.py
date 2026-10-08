"""Step Functions de LocalStack: ejecuciones y línea de tiempo. SOLO lectura (list/describe/get).

El historial de esta máquina no lleva PII por diseño (solo ids, números y estados); igual se recorta `cause`.
"""
import asyncio
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Tiempos cortos: sin esto boto3 espera 60 s y reintenta, y una dependencia colgada deja la página sin responder.
_CONFIG = Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1})

MAX_EVENTOS = 200
MAX_CAUSA = 300


class ExecutionNotFound(Exception):
    """La ejecución pedida no existe."""


def _client():
    return boto3.client("stepfunctions", config=_CONFIG)        # respeta AWS_ENDPOINT_URL / AWS_DEFAULT_REGION


def _arn_maquina() -> str:
    return os.environ["SFN_STATE_MACHINE_ARN"]


def _arn_ejecucion(nombre: str) -> str:
    return _arn_maquina().replace(":stateMachine:", ":execution:") + f":{nombre}"


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(fecha) -> str | None:
    return fecha.isoformat() if fecha else None


def _ms(inicio, fin) -> int:
    return max(0, int(((fin or _ahora()) - inicio).total_seconds() * 1000))


# ── lista ───────────────────────────────────────────────────────────────────
def _listar(status, limit) -> dict:
    kwargs = {"stateMachineArn": _arn_maquina(), "maxResults": limit}
    if status:
        kwargs["statusFilter"] = status
    try:
        items = _client().list_executions(**kwargs)["executions"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "StateMachineDoesNotExist":
            return {"executions": [], "note": "la máquina prisma-flujo no existe todavía"}
        raise
    return {"executions": [
        {
            "name": e["name"],
            "status": e["status"],
            "start": _iso(e["startDate"]),
            "stop": _iso(e.get("stopDate")),
            "duration_ms": _ms(e["startDate"], e.get("stopDate")),
        }
        for e in items
    ]}


async def list_executions(status: str | None = None, limit: int = 50) -> dict:
    return await asyncio.to_thread(_listar, status, limit)


# ── línea de tiempo ─────────────────────────────────────────────────────────
_FALLAS = {
    "TaskFailed": "failed", "TaskTimedOut": "failed", "LambdaFunctionFailed": "failed", "LambdaFunctionTimedOut": "failed",
    "ExecutionFailed": "failed", "ExecutionTimedOut": "failed", "ExecutionAborted": "aborted",
}


def _armar_pasos(eventos: list, cierre=None) -> list:
    """`cierre` = (fecha, estado) de una ejecución YA terminada: los pasos que quedaron abiertos (un estado Fail no emite
    StateExited; una ejecución abortada se corta en plena espera) se cierran en esa fecha en vez de contar para siempre."""
    pasos, veces, actual = [], {}, None
    for ev in eventos:
        tipo, ts = ev["type"], ev["timestamp"]
        if tipo.endswith("StateEntered"):
            nombre = ev["stateEnteredEventDetails"]["name"]
            veces[nombre] = veces.get(nombre, 0) + 1
            actual = {"name": nombre, "type": tipo[: -len("StateEntered")], "start": ts, "end": None, "status": "running",
                      "error": None, "cause": None, "attempt": veces[nombre], "retries": 0, "_programadas": 0}
            pasos.append(actual)
        elif tipo.endswith("StateExited") and actual is not None:
            actual["end"] = ts
            if actual["status"] == "running":
                actual["status"] = "ok"
        elif tipo == "TaskScheduled" and actual is not None:
            actual["_programadas"] += 1
            actual["retries"] = actual["_programadas"] - 1
            if actual["status"] == "failed":            # nuevo intento tras una falla: el error anterior ya no define el paso
                actual["status"], actual["error"], actual["cause"] = "running", None, None
        elif tipo in _FALLAS and actual is not None:
            detalle = ev.get(tipo[0].lower() + tipo[1:] + "EventDetails", {})
            actual["status"] = _FALLAS[tipo]
            actual["error"] = detalle.get("error") or None
            actual["cause"] = (detalle.get("cause") or "")[:MAX_CAUSA] or None
    if cierre is not None:
        fecha, estado_ejecucion = cierre
        for p in pasos:
            if p["end"] is None:
                p["end"] = fecha
                if p["status"] == "running":
                    p["status"] = {"ABORTED": "aborted", "SUCCEEDED": "ok"}.get(estado_ejecucion, "failed")
    return [
        {
            "name": p["name"], "type": p["type"], "start": _iso(p["start"]), "end": _iso(p["end"]),
            "duration_ms": _ms(p["start"], p["end"]), "status": p["status"], "error": p["error"], "cause": p["cause"],
            "attempt": p["attempt"], "retries": p["retries"],
        }
        for p in pasos
    ]


def _linea_de_tiempo(nombre: str) -> dict:
    cli = _client()
    arn = _arn_ejecucion(nombre)
    try:
        d = cli.describe_execution(executionArn=arn)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionDoesNotExist":
            raise ExecutionNotFound(nombre) from exc
        raise
    # Los ÚLTIMOS MAX_EVENTOS (así lo dice la especificación): se piden en orden inverso y se vuelven a ordenar.
    # includeExecutionData=False: las entradas y salidas no se necesitan y no hay por qué moverlas.
    eventos = cli.get_execution_history(
        executionArn=arn, maxResults=MAX_EVENTOS, reverseOrder=True, includeExecutionData=False
    )["events"][::-1]
    terminada = d["status"] != "RUNNING"
    cierre = (d.get("stopDate") or (eventos[-1]["timestamp"] if eventos else d["startDate"]), d["status"]) if terminada else None
    return {
        "name": nombre, "status": d["status"], "start": _iso(d["startDate"]), "stop": _iso(d.get("stopDate")),
        "steps": _armar_pasos(eventos, cierre),
    }


async def execution_timeline(nombre: str) -> dict:
    return await asyncio.to_thread(_linea_de_tiempo, nombre)
