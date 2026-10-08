"""Spike: ¿LocalStack 4.14.0 soporta lo que necesita el diseño? Imprime OK/FALLA por capacidad.

Se ejecuta DENTRO de la red del compose (necesita llegar a `localstack`):
  docker compose run --rm --no-deps -T -v "$PWD/local/spike:/spike:ro" workflow python /spike/spike_sfn.py
"""
import io
import json
import os
import time
import uuid
import zipfile

import boto3
from botocore.exceptions import ClientError

EP = os.environ.get("AWS_ENDPOINT_URL", "http://localstack:4566")
KW = dict(endpoint_url=EP, region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
lam, sfn = boto3.client("lambda", **KW), boto3.client("stepfunctions", **KW)

# El stub de la Lambda completa la tarea solo si se lo piden ("auto"); si no, la deja esperando.
CODIGO = '''
import json, os, boto3
def handler(event, context):
    if event.get("modo") == "auto":
        c = boto3.client("stepfunctions", region_name="us-east-1", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
        c.send_task_success(taskToken=event["task_token"], output=json.dumps({"status": "ok"}))
    return {"visto": True}
'''


def zip_codigo():
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("lambda_function.py", CODIGO)
    return b.getvalue()


def crea_lambda(nombre):
    try:
        lam.delete_function(FunctionName=nombre)
    except ClientError:
        pass
    lam.create_function(FunctionName=nombre, Runtime="python3.12", Handler="lambda_function.handler",
                        Role="arn:aws:iam::000000000000:role/r", Code={"ZipFile": zip_codigo()}, Timeout=30,
                        Environment={"Variables": {"AWS_ENDPOINT_URL": EP}})
    lam.get_waiter("function_active_v2").wait(FunctionName=nombre)
    return f"arn:aws:lambda:us-east-1:000000000000:function:{nombre}"


def crea_maquina(nombre, arn_lambda):
    asl = {"StartAt": "Esperar", "States": {
        "Esperar": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                    "Parameters": {"FunctionName": arn_lambda,
                                   "Payload": {"modo.$": "$.modo", "task_token.$": "$$.Task.Token"}},
                    "TimeoutSecondsPath": "$.timeouts.fase", "HeartbeatSecondsPath": "$.timeouts.heartbeat",
                    "ResultPath": "$.fase",
                    "Retry": [{"ErrorEquals": ["States.HeartbeatTimeout"], "IntervalSeconds": 1, "MaxAttempts": 1, "BackoffRate": 1.0}],
                    "Catch": [{"ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "Sumar"}],
                    "Next": "Sumar"},
        "Sumar": {"Type": "Pass", "Parameters": {"n.$": "States.MathAdd($.n, 1)"}, "ResultPath": "$.sig", "End": True}}}
    for sm in sfn.list_state_machines()["stateMachines"]:
        if sm["name"] == nombre:
            sfn.delete_state_machine(stateMachineArn=sm["stateMachineArn"])
            time.sleep(1)
    return sfn.create_state_machine(name=nombre, definition=json.dumps(asl), roleArn="arn:aws:iam::000000000000:role/r")["stateMachineArn"]


def espera_fin(arn_ejec, segundos=40):
    for _ in range(segundos * 2):
        d = sfn.describe_execution(executionArn=arn_ejec)
        if d["status"] != "RUNNING":
            return d
        time.sleep(0.5)
    return sfn.describe_execution(executionArn=arn_ejec)


def inicia(sm, entrada, nombre=None):
    return sfn.start_execution(stateMachineArn=sm, name=nombre or uuid.uuid4().hex, input=json.dumps(entrada))["executionArn"]


def token_desde_historial(arn_ejec):
    for _ in range(20):
        for ev in sfn.get_execution_history(executionArn=arn_ejec)["events"]:
            det = ev.get("taskScheduledEventDetails")
            if det and "task_token" in det.get("parameters", ""):
                payload = json.loads(det["parameters"])["Payload"]
                if isinstance(payload, str):                # según la versión, el Payload llega como texto JSON
                    payload = json.loads(payload)
                return payload["task_token"]
        time.sleep(0.5)
    return None


resultados = []


def caso(nombre, fn):
    try:
        ok, detalle = fn()
    except Exception as exc:  # noqa: BLE001 — es un spike: queremos ver cualquier fallo
        ok, detalle = False, f"{type(exc).__name__}: {exc}"
    resultados.append((nombre, ok))
    print(f"{'OK   ' if ok else 'FALLA'} {nombre} -> {detalle}")


arn_l = crea_lambda("spike-stub")
sm = crea_maquina("spike-flujo", arn_l)
T = {"fase": 30, "heartbeat": 20}


def a():
    d = espera_fin(inicia(sm, {"modo": "auto", "n": 1, "timeouts": T}))
    out = json.loads(d.get("output", "{}"))
    return d["status"] == "SUCCEEDED" and out.get("fase", {}).get("status") == "ok" and out.get("sig", {}).get("n") == 2, f"{d['status']} {out}"


def b():
    d = espera_fin(inicia(sm, {"modo": "no", "n": 1, "timeouts": {"fase": 3, "heartbeat": 20}}), 15)
    out = json.loads(d.get("output", "{}"))
    return d["status"] == "SUCCEEDED" and out.get("error", {}).get("Error") == "States.Timeout", f"{d['status']} {out.get('error')}"


def c():
    d = espera_fin(inicia(sm, {"modo": "no", "n": 1, "timeouts": {"fase": 60, "heartbeat": 3}}), 20)
    out = json.loads(d.get("output", "{}"))
    return d["status"] == "SUCCEEDED" and out.get("error", {}).get("Error") == "States.HeartbeatTimeout", f"{d['status']} {out.get('error')}"


def d_():
    e = inicia(sm, {"modo": "no", "n": 1, "timeouts": {"fase": 60, "heartbeat": 4}})
    tok = token_desde_historial(e)
    if not tok:
        return False, "el historial no trae el token"
    for _ in range(4):          # 4 s * 2 > heartbeat de 4 s: sigue vivo solo si el heartbeat funciona
        time.sleep(2)
        sfn.send_task_heartbeat(taskToken=tok)
    sfn.send_task_success(taskToken=tok, output=json.dumps({"status": "ok"}))
    r = espera_fin(e, 10)
    return r["status"] == "SUCCEEDED" and json.loads(r["output"]).get("fase", {}).get("status") == "ok", r["status"]


def e_():
    nombre = uuid.uuid4().hex
    a1 = inicia(sm, {"modo": "no", "n": 1, "timeouts": T}, nombre)
    a2 = inicia(sm, {"modo": "no", "n": 1, "timeouts": T}, nombre)            # mismo nombre y entrada, ejecución abierta
    idem = a1 == a2
    sfn.stop_execution(executionArn=a1)
    estado = espera_fin(a1, 10)["status"]
    try:
        inicia(sm, {"modo": "no", "n": 1, "timeouts": T}, nombre)            # ya cerrada
        cerrada = "sin error"
    except ClientError as exc:
        cerrada = exc.response["Error"]["Code"]
    return idem and estado == "ABORTED" and cerrada == "ExecutionAlreadyExists", f"idempotente={idem} stop={estado} reabrir={cerrada}"


caso("A waitForTaskToken con Lambda + SendTaskSuccess + States.MathAdd", a)
caso("B TimeoutSecondsPath (vence y entra al Catch)", b)
caso("C HeartbeatSecondsPath + Retry/Catch de States.HeartbeatTimeout", c)
caso("D SendTaskHeartbeat mantiene viva la tarea", d_)
caso("E StartExecution idempotente, StopExecution=ABORTED, ExecutionAlreadyExists", e_)
print(f"\n{sum(ok for _, ok in resultados)}/{len(resultados)} capacidades OK")
