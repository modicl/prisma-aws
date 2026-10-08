"""E2E de Step Functions en LocalStack con fases SIMULADAS (sin Gemini). Imprime OK/FALLA por escenario.

  docker compose exec -T workflow python /e2e/sfn_e2e.py

Recorre el camino real: S3 -> SQS -> Lambda starter -> Step Functions -> Lambda invoker -> endpoints del workflow -> SendTaskSuccess.
La decisión del docente se envía con la MISMA función que usa POST /hitl (menos la autenticación de Cognito, que no se puede
simular aquí).
"""
import json
import os
import sys
import time
import uuid

sys.path.insert(0, "/app/prisma_agents")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
from botocore.exceptions import ClientError

from api import dynamo_store, sfn_client, workflow_runner

BUCKET = os.environ["S3_BUCKET"]
SM_ARN = os.environ["SFN_STATE_MACHINE_ARN"]
s3 = boto3.client("s3")
sfn = boto3.client("stepfunctions")


def nueva_sesion(colegio):
    sid = str(uuid.uuid4())
    dynamo_store.create_session(sid, phase="running", paci_s3_key=f"jobs/{sid}/paci.pdf",
                                material_s3_key=f"jobs/{sid}/material.docx", prompt="", school_id=colegio, owner_id="e2e")
    return sid


def subir(sid, veces=1):
    s3.put_object(Bucket=BUCKET, Key=f"jobs/{sid}/material.docx", Body=b"material")
    for _ in range(veces):
        s3.put_object(Bucket=BUCKET, Key=f"jobs/{sid}/paci.pdf", Body=b"paci")


def iniciar_directo(sid, **tiempos):
    t = {"fase": 60, "heartbeat": 30, "hitl": 86400}
    t.update(tiempos)
    sfn.start_execution(stateMachineArn=SM_ARN, name=sid, input=json.dumps({"session_id": sid, "timeouts": t}))


def item(sid):
    return dynamo_store.get_session(sid)


def ejec(sid):
    return sfn.describe_execution(executionArn=sfn_client.execution_arn(sid))["status"]


def esperar(cond, seg=90, que=""):
    fin = time.time() + seg
    while time.time() < fin:
        try:
            if cond():
                return
        except Exception:
            pass
        time.sleep(0.4)
    raise AssertionError(f"tiempo agotado esperando: {que}")


def decidir(sid, aprobado, razon=None, agente=None):
    workflow_runner.submit_hitl_decision(sid, item(sid), aprobado, razon, agente)


def esperando(sid, intento):
    return lambda: item(sid)["phase"] == "awaiting_hitl" and item(sid)["hitl_data"]["attempt"] == intento


def estado_borrado(sid):
    try:
        s3.head_object(Bucket=BUCKET, Key=f"state/{sid}.json")
        return False
    except ClientError:
        return True


def s_aprobado_a_la_primera():
    sid = nueva_sesion("__mock_ok__")
    subir(sid)
    esperar(esperando(sid, 1), que="primer checkpoint")
    decidir(sid, True)
    esperar(lambda: item(sid)["phase"] == "completed", que="completed")
    esperar(lambda: ejec(sid) == "SUCCEEDED", 20, "ejecución SUCCEEDED")
    assert item(sid)["workflow_status"] == "success" and item(sid)["docx_s3_key"], item(sid)
    esperar(lambda: estado_borrado(sid), 15, "estado de S3 borrado")


def s_rechazo_y_reintento():
    sid = nueva_sesion("__mock_ok__")
    subir(sid)
    esperar(esperando(sid, 1), que="primer checkpoint")
    decidir(sid, False, "falta claridad", 2)
    esperar(esperando(sid, 2), que="segundo checkpoint")
    assert "retry_agent=2" in item(sid)["hitl_data"]["planificacion_adaptada"]
    decidir(sid, True)
    esperar(lambda: item(sid)["phase"] == "completed", que="completed")


def s_tres_rechazos():
    sid = nueva_sesion("__mock_ok__")
    subir(sid)
    for n in (1, 2, 3):
        esperar(esperando(sid, n), que=f"checkpoint {n}")
        decidir(sid, False, "no", 2)
    esperar(lambda: item(sid)["phase"] == "error", que="sesión terminada")
    assert item(sid)["workflow_status"] == "hitl_rejected"
    esperar(lambda: ejec(sid) == "SUCCEEDED", 20, "ejecución SUCCEEDED")


def s_bloqueado_por_normativa():
    sid = nueva_sesion("__mock_blocked__")
    subir(sid)
    esperar(lambda: item(sid)["phase"] == "error", que="sesión terminada")
    assert item(sid)["workflow_status"] == "compliance_blocked"
    esperar(lambda: ejec(sid) == "SUCCEEDED", 20, "ejecución SUCCEEDED (no es un error)")


def s_error_en_la_fase_b():
    sid = nueva_sesion("__mock_error__")
    subir(sid)
    esperar(esperando(sid, 1), que="checkpoint")
    decidir(sid, True)
    esperar(lambda: item(sid)["phase"] == "error", que="sesión en error")
    esperar(lambda: ejec(sid) == "FAILED", 20, "ejecución FAILED")


def s_el_docente_no_responde():
    sid = nueva_sesion("__mock_ok__")
    iniciar_directo(sid, hitl=4)                      # espera corta: el escenario no dura 24 h
    esperar(esperando(sid, 1), que="checkpoint")
    esperar(lambda: item(sid)["phase"] == "error", 30, "sesión expirada")
    assert "expiró" in (item(sid)["error"] or "")
    esperar(lambda: ejec(sid) == "SUCCEEDED", 20, "ejecución SUCCEEDED")


def s_cancelacion_durante_la_espera():
    sid = nueva_sesion("__mock_ok__")
    subir(sid)
    esperar(esperando(sid, 1), que="checkpoint")
    workflow_runner.cancel_session_sfn(sid, item(sid))
    time.sleep(2)
    it = item(sid)
    assert it["workflow_status"] == "cancelled" and it["phase"] == "error" and not it["task_token"], it
    # AWS real deja la ejecución ABORTED. LocalStack 4.14 NO aborta una espera con token (queda RUNNING): se acepta
    # cualquiera de las dos, pero nunca que el flujo siga avanzando (SUCCEEDED/FAILED) ni que la sesión deje de estar cancelada.
    assert ejec(sid) in ("ABORTED", "RUNNING"), ejec(sid)
    assert item(sid)["workflow_status"] == "cancelled"


def s_mensaje_duplicado():
    sid = nueva_sesion("__mock_ok__")
    subir(sid, veces=2)                               # el PACI genera dos eventos de S3 -> dos mensajes SQS
    esperar(esperando(sid, 1), que="checkpoint")
    time.sleep(3)
    nombres = [e["name"] for e in sfn.list_executions(stateMachineArn=SM_ARN, maxResults=100)["executions"]]
    assert nombres.count(sid) == 1, f"hay {nombres.count(sid)} ejecuciones para la misma sesión"
    decidir(sid, True)
    esperar(lambda: item(sid)["phase"] == "completed", que="completed")


def s_worker_muerto():
    sid = nueva_sesion("__mock_dead__")               # el endpoint responde 202 y nunca trabaja ni envía heartbeat
    iniciar_directo(sid, heartbeat=3)
    esperar(lambda: ejec(sid) in ("FAILED", "SUCCEEDED"), 60, "fin de la ejecución")
    assert ejec(sid) == "FAILED", ejec(sid)
    esperar(lambda: item(sid)["phase"] == "error", 15, "sesión en error")


ESCENARIOS = [
    ("aprobado a la primera", s_aprobado_a_la_primera),
    ("rechazo, reintento y aprobación", s_rechazo_y_reintento),
    ("tres rechazos -> hitl_rejected", s_tres_rechazos),
    ("PACI bloqueado por normativa", s_bloqueado_por_normativa),
    ("error en la fase B", s_error_en_la_fase_b),
    ("el docente no responde (expira)", s_el_docente_no_responde),
    ("cancelación durante la espera", s_cancelacion_durante_la_espera),
    ("mensaje SQS duplicado (idempotencia)", s_mensaje_duplicado),
    ("worker muerto (heartbeat + 1 reintento)", s_worker_muerto),
]

if __name__ == "__main__":
    fallas = 0
    for nombre, fn in ESCENARIOS:
        t0 = time.time()
        try:
            fn()
            print(f"OK    {nombre} ({time.time() - t0:.1f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001
            fallas += 1
            print(f"FALLA {nombre}: {type(exc).__name__}: {exc}", flush=True)
    print(f"\n{len(ESCENARIOS) - fallas}/{len(ESCENARIOS)} escenarios OK")
    sys.exit(1 if fallas else 0)
