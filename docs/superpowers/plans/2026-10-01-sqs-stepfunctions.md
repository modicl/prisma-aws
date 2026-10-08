# Subida → SQS → Lambda → Step Functions: plan de implementación

> **Para agentes:** SUB-SKILL REQUERIDA: usa `superpowers:subagent-driven-development` (recomendada) o `superpowers:executing-plans`
> para implementar este plan tarea por tarea. Los pasos usan casillas (`- [ ]`) para seguimiento.

**Objetivo:** que el flujo agéntico no dependa de un proceso vivo: lo que sube el docente entra por SQS, una Lambda arranca Step Functions, y
Step Functions orquesta las fases y la espera del docente (HITL) con `waitForTaskToken`.

**Arquitectura:** el workflow (ECS) gana 4 endpoints internos de fase. Step Functions (Standard) los dispara mediante una Lambda delgada dentro
de la VPC y espera el `SendTaskSuccess` del workflow. El estado entre fases va a S3 (`state/{id}.json`). `PaciWorkflowAgent` se parte en fases
`a`/`b` sin cambiar prompts ni lógica; el modo completo (`all`) sigue funcionando para CLI y modo local.

**Tecnología:** Python 3.12 (FastAPI, google-adk, boto3), AWS SQS / Lambda / Step Functions (Amazon States Language en JSONPath), Terraform,
LocalStack 4.14.0 (sin token) para probar, pytest.

**Especificación:** `docs/superpowers/specs/2026-10-01-sqs-stepfunctions-design.md` (aprobada el 2026-10-01). Léela antes de empezar.

## Restricciones globales

Valen para **todas** las tareas.

- Rama: **`cloudnative`** (principal). Nunca `push`. En `prisma_workflow` se commitea con `git add` de archivos concretos, jamás `git add -A`.
- **No commitear las ediciones del dueño**: `prisma_agents/agents/*.py` (modelo `3.5`) y los 2 fragmentos de `agent.py` que cambian `gemini-3.1` → `gemini-3.5`.
  Para `agent.py` se usa el script de la Tarea 0 (`stage_sin_modelo.py`), que excluye cualquier fragmento con `flash-lite`.
- La raíz `prisma-aws/` **no es un repo**: `docker-compose.yml`, `local/`, `LOCAL.md` y `docs/` se editan pero **no se commitean**.
- **Step Functions Standard** (el `waitForTaskToken` no existe en Express). `MAX_HITL_ITERATIONS = 3`, timeouts por defecto: fase 1800 s, heartbeat 120 s, HITL 86400 s.
- **Sin PII por Step Functions** (su historial guarda entradas y salidas 90 días en claro): por ahí viajan solo `session_id`, `attempt`, `feedback_agent`, `status`,
  `approved`, `agent_to_retry` y el token. El texto de documentos, perfiles, planificaciones y la `reason` del docente van por S3/DynamoDB.
- El estado de fases vive bajo `state/` en S3, **nunca bajo `jobs/`** (ese prefijo dispara la notificación de S3 y se armaría un bucle). Cifrado `AES256` y expiración de 7 días.
- La Lambda `invoker` está en la VPC y es **solo HTTP stdlib** (sin boto3, sin llamadas a APIs de AWS); acepta únicamente las fases `a`, `b`, `wait`, `finalize`.
  La Lambda `starter` va **fuera** de la VPC.
- **No se tocan** los prompts, `MAX_ITERATIONS`, `MAX_HITL_ITERATIONS`, `AGENT_TIMEOUT_SECONDS`, el contrato del Crítico ni la lógica de eliminación de PDFs de la Files API.
  `PaciWorkflowAgent(phase="all")` debe comportarse exactamente como hoy (hay un test de regresión).
- Endpoints internos exigen `X-Internal-Token` (= `INTERNAL_TOKEN`). Los logs no registran cuerpos, tokens de tarea ni texto de documentos.
- Reintentos de fase: **1 sola vez** y solo ante `States.HeartbeatTimeout` (cada reintento repite llamadas a Gemini).
- Terraform: archivos `.tf.pendiente` (no se cargan), **sin `apply`**. Validar con `terraform validate` en una copia temporal sin `tfstate`/`tfvars`.
- Textos y comentarios en español de Chile (tuteo), sin voseo.

## Foco de revisión

Entradas y fallas que el diseño implica pero que ninguna tarea cubriría "de paso"; cada una tiene su test en la tarea indicada.

1. **Mensaje SQS repetido o de una ejecución ya cerrada** → `ExecutionAlreadyExists` no es un fallo ni manda el mensaje a la DLQ. *(Tarea 7)*
2. **El docente responde dos veces** (doble clic) o con el token ya usado → `409`, nunca dos `SendTaskSuccess`. *(Tareas 1 y 5)*
3. **El token de tarea ya no existe durante una fase** (Step Functions la reintentó, venció o se canceló) → la fase se cancela sola, sin seguir gastando Gemini. *(Tarea 3)*
4. **El estado en S3 no existe** al arrancar la fase B (expiró o se borró) → falla controlada con `SendTaskFailure`, sin colgar la ejecución. *(Tarea 5)*
5. **Texto del docente o de documentos en el payload de Step Functions** → no debe ocurrir: `/hitl` envía solo `{approved, agent_to_retry}` y la definición solo admite claves permitidas. *(Tareas 5 y 8)*

## Mapa de archivos

| Archivo | Responsabilidad | Tarea |
|---|---|---|
| `prisma_agents/api/dynamo_store.py` (mod.) | Campos `task_token`, `started_at`; candado de fase y consumo atómico del token | 1 |
| `prisma_agents/api/phase_store.py` (nuevo) | Estado entre fases en S3 (`state/{id}.json`) | 2 |
| `prisma_agents/api/sfn_client.py` (nuevo) | `SendTask*`, `StopExecution`, heartbeat que cancela la fase si el token se pierde | 3 |
| `prisma_agents/utils/hitl_feedback.py` (nuevo) | Textos de retroalimentación del docente (los mismos de hoy, extraídos) | 4 |
| `prisma_agents/agent.py` (mod.) | `PaciWorkflowAgent(phase=…)` y 2 raíces nuevas | 4 |
| `prisma_agents/run.py` (mod.) | `run_phase(...)` | 4 |
| `prisma_agents/api/mock_phases.py` (nuevo) | Fases simuladas (`__mock_*`) para probar sin Gemini | 5 |
| `prisma_agents/api/workflow_runner.py` (mod.) | Lógica de las fases, espera, finalización y decisión HITL | 5 |
| `prisma_agents/api/schemas.py`, `api/chat_router.py` (mod.) | 4 endpoints internos; `/hitl` y `/cancel` con doble camino | 6 |
| `lambda/sfn_starter.py`, `lambda/sfn_invoker.py` (nuevos) | SQS → `StartExecution`; Step Functions → HTTP al ALB | 7 |
| `statemachine/prisma_flow.asl.json` (nuevo) | Definición de la máquina de estados | 8 |
| `docker-compose.yml`, `local/localstack/init-aws.sh`, `local/e2e/sfn_e2e.py` (raíz, sin repo) | LocalStack con SQS + Step Functions y pruebas de punta a punta | 0, 9 |
| `prisma-infra/08-flujo-sfn.tf.pendiente` | SQS, DLQ, Lambdas, máquina de estados, ciclo de vida de `state/` | 10 |
| `CLAUDE.md` (raíz y workflow), `LOCAL.md` | Documentación | 11 |

## Convenciones de ejecución

Los tests del workflow corren en Docker con el código montado (misma imagen `prisma-workflow` que ya existe; no hay dependencias nuevas). Desde `prisma_workflow/`:

```bash
wt() { MSYS_NO_PATHCONV=1 docker run --rm -u root -v "$(pwd -W):/w" -w /w -e PYTHONDONTWRITEBYTECODE=1 prisma-workflow python -m pytest -q -p no:cacheprovider "$@"; }
# uso:  wt prisma_agents/tests/api/test_dynamo_store_fases.py
# todo: wt prisma_agents/tests
```

Los tests asíncronos del workflow se marcan con `@pytest.mark.asyncio` (el `asyncio_mode = auto` de `setup.cfg` **no** se aplica).
Línea base antes de empezar: `wt prisma_agents/tests` → **312 passed**. Si no, detente y averigua por qué.

---

### Tarea 0: Spike de LocalStack y herramientas

Antes de escribir código de la aplicación hay que saber si LocalStack 4.14.0 (sin token) soporta lo que el diseño necesita. Si algo falla, **el diseño se ajusta aquí**.

**Archivos:**
- Modificar: `docker-compose.yml` (raíz), servicio `localstack`: `SERVICES`
- Crear: `local/spike/spike_sfn.py` (raíz, sin repo)
- Crear: `local/tools/stage_sin_modelo.py` (raíz, sin repo)

**Interfaces:**
- Produce: `stage_sin_modelo.py <archivo>`: prepara en el índice de git solo los fragmentos de ese archivo que no contengan `flash-lite`. Lo usan las tareas 4, 5 y 6.
- Produce: el veredicto del spike (tabla de decisión al final de esta tarea).

- [ ] **Paso 1: habilitar `sqs` y `stepfunctions` en LocalStack**

En `docker-compose.yml`, servicio `localstack`, reemplaza la línea de `SERVICES`:

```yaml
      SERVICES: s3,dynamodb,lambda,sqs,stepfunctions
```

Luego: `docker compose up -d localstack` y espera a que esté `healthy` (`docker compose ps localstack`).

- [ ] **Paso 2: crear la herramienta de preparación parcial de git**

```python
"""Prepara en el índice de git SOLO los fragmentos de un archivo que no tocan el modelo (`flash-lite`).

Uso (desde prisma_workflow/):  python ../local/tools/stage_sin_modelo.py prisma_agents/agent.py

Motivo: agent.py tiene ediciones del dueño (cambio de modelo a 3.5) mezcladas con las nuestras y no deben commitearse.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

archivo = sys.argv[1]
diff = subprocess.run(["git", "diff", "-U0", "--", archivo], capture_output=True, text=True, encoding="utf-8").stdout
if not diff.strip():
    sys.exit(f"sin cambios en {archivo}")
cabecera, *fragmentos = re.split(r"(?m)^(?=@@ )", diff)
nuestros = [f for f in fragmentos if "flash-lite" not in f]
print(f"{archivo}: {len(fragmentos)} fragmentos, excluidos (modelo): {len(fragmentos) - len(nuestros)}, preparados: {len(nuestros)}")
if nuestros:
    parche = Path(tempfile.mkstemp(suffix=".patch")[1])
    parche.write_text(cabecera + "".join(nuestros), encoding="utf-8", newline="\n")
    subprocess.run(["git", "apply", "--cached", "--unidiff-zero", str(parche)], check=True)
    parche.unlink()
```

- [ ] **Paso 3: escribir el spike**

```python
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
```

- [ ] **Paso 4: ejecutar el spike**

Desde la raíz `prisma-aws/`:

```bash
MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -T -v "$(pwd -W)/local/spike:/spike:ro" workflow python /spike/spike_sfn.py
```

Esperado: `5/5 capacidades OK`.

- [ ] **Paso 5: decidir según el resultado**

| Resultado | Decisión |
|---|---|
| 5/5 OK | Seguir con el plan tal cual. |
| Falla **B** o **C** (`...SecondsPath`) | Cambiar en el ASL (Tarea 8) a `TimeoutSeconds`/`HeartbeatSeconds` literales y generarlos por plantilla en `init-aws.sh` y Terraform. |
| Falla **D** solo porque el historial no trae el token | No es bloqueante: el token real llega por la Lambda `invoker`. Anotarlo y seguir. |
| Falla **A** | **Detenerse y reportar**: sin `waitForTaskToken` con Lambda en LocalStack no se puede verificar el diseño localmente; hay que decidir con el dueño. |
| Falla **E** | Anotarlo: la idempotencia solo se podrá comprobar con el comportamiento documentado de AWS; los tests unitarios del `starter` siguen cubriéndola. |

Registra el veredicto al final de este archivo, en una sección `## Resultado del spike`.

- [ ] **Paso 6: sin commit** (los archivos de esta tarea viven en la raíz, que no es un repo).

---

### Tarea 1: DynamoDB — campos de fase, candado y consumo atómico del token

**Archivos:**
- Modificar: `prisma_agents/api/dynamo_store.py` (tabla de campos, `get_session`, funciones nuevas al final)
- Crear: `prisma_agents/tests/api/test_dynamo_store_fases.py`

**Interfaces:**
- Produce: `update_session(session_id, task_token=..., started_at=...)` acepta los campos nuevos (`"S"`).
- Produce: `get_session(sid)` devuelve además `task_token: str | None` y `started_at: float | None`.
- Produce: `acquire_phase(session_id: str, phase_key: str, ttl_seconds: int = 1800) -> bool`, `release_phase(session_id: str) -> None`, `consume_token(session_id: str) -> str | None`.

- [ ] **Paso 1: escribir los tests que fallan**

```python
"""Campos y operaciones atómicas de DynamoDB para las fases (token de tarea y candado de ejecución)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

from api import dynamo_store as ds


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "UpdateItem")


@pytest.fixture
def cli(monkeypatch):
    c = MagicMock()
    monkeypatch.setattr(ds, "TABLE", "t")
    monkeypatch.setattr(ds, "_client", c)
    return c


def test_update_session_acepta_task_token_y_started_at(cli):
    ds.update_session("s1", task_token="tok", started_at="123.5")
    kw = cli.update_item.call_args.kwargs
    assert set(kw["ExpressionAttributeNames"].values()) == {"task_token", "started_at"}
    assert {"S": "tok"} in kw["ExpressionAttributeValues"].values()


def test_task_token_vacio_se_guarda_como_cadena_vacia(cli):
    ds.update_session("s1", task_token=None)
    assert {"S": ""} in cli.update_item.call_args.kwargs["ExpressionAttributeValues"].values()


def test_get_session_devuelve_los_campos_de_fase(cli):
    cli.get_item.return_value = {"Item": {"session_id": {"S": "s1"}, "phase": {"S": "awaiting_hitl"},
                                          "task_token": {"S": "tok"}, "started_at": {"S": "100.5"}}}
    item = ds.get_session("s1")
    assert item["task_token"] == "tok" and item["started_at"] == 100.5
    cli.get_item.return_value = {"Item": {"session_id": {"S": "s1"}, "phase": {"S": "running"}}}
    item = ds.get_session("s1")
    assert item["task_token"] is None and item["started_at"] is None


def test_acquire_phase_true_si_toma_el_candado(cli):
    assert ds.acquire_phase("s1", "a:1", ttl_seconds=60) is True
    kw = cli.update_item.call_args.kwargs
    assert "attribute_not_exists(running_until)" in kw["ConditionExpression"]
    assert kw["ExpressionAttributeValues"][":p"] == {"S": "a:1"}


def test_acquire_phase_false_si_ya_esta_tomado(cli):
    cli.update_item.side_effect = _err("ConditionalCheckFailedException")
    assert ds.acquire_phase("s1", "a:1") is False


def test_acquire_phase_propaga_otros_errores(cli):
    cli.update_item.side_effect = _err("InternalServerError")
    with pytest.raises(ClientError):
        ds.acquire_phase("s1", "a:1")


def test_release_phase_borra_el_candado(cli):
    ds.release_phase("s1")
    assert "REMOVE running_phase, running_until" in cli.update_item.call_args.kwargs["UpdateExpression"]


def test_consume_token_devuelve_el_token_y_lo_borra(cli):
    cli.update_item.return_value = {"Attributes": {"task_token": {"S": "tok"}}}
    assert ds.consume_token("s1") == "tok"
    kw = cli.update_item.call_args.kwargs
    assert kw["UpdateExpression"] == "REMOVE task_token" and kw["ReturnValues"] == "UPDATED_OLD"
    assert "attribute_exists(task_token)" in kw["ConditionExpression"]


def test_consume_token_none_si_ya_fue_usado(cli):
    """Foco de revisión 2: el segundo clic del docente no obtiene token."""
    cli.update_item.side_effect = _err("ConditionalCheckFailedException")
    assert ds.consume_token("s1") is None


def test_sin_dynamodb_las_operaciones_son_inocuas(monkeypatch):
    monkeypatch.setattr(ds, "TABLE", "")
    assert ds.acquire_phase("s1", "a:1") is True
    assert ds.consume_token("s1") is None
    ds.release_phase("s1")
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/api/test_dynamo_store_fases.py`
Esperado: FALLA (`AttributeError: ... has no attribute 'acquire_phase'` y los campos nuevos no se guardan).

- [ ] **Paso 3: implementar**

En `prisma_agents/api/dynamo_store.py`:

1. Agrega al diccionario `_FIELD_TYPES` (después de `"warnings": "S",`):

```python
    "task_token": "S",     # token de Step Functions de la espera del docente
    "started_at": "S",     # epoch (texto) del inicio del flujo, para medir duración
```

2. Agrega al diccionario `_TRANSFORMS` (después de `"warnings": json.dumps,`):

```python
    "task_token": lambda v: v or "",
```

3. En `get_session`, agrega al diccionario devuelto (después de la línea de `owner_id`):

```python
        "task_token":      item.get("task_token", {}).get("S") or None,
        "started_at":      float(item.get("started_at", {}).get("S") or 0) or None,
```

4. Al final del archivo agrega:

```python
def acquire_phase(session_id: str, phase_key: str, ttl_seconds: int = 1800) -> bool:
    """Toma el candado de fase: evita que dos workers corran la misma fase a la vez.

    Escritura condicional con vencimiento (`running_until`) para recuperarse de un worker caído.
    Devuelve True si lo obtuvo. Sin DynamoDB (modo local) siempre True.
    """
    if not enabled():
        return True
    now = int(time.time())
    try:
        _get_client().update_item(
            TableName=TABLE,
            Key={"session_id": {"S": session_id}},
            UpdateExpression="SET running_phase = :p, running_until = :u",
            ConditionExpression="attribute_not_exists(running_until) OR running_until < :now",
            ExpressionAttributeValues={
                ":p": {"S": phase_key},
                ":u": {"N": str(now + ttl_seconds)},
                ":now": {"N": str(now)},
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def release_phase(session_id: str) -> None:
    if not enabled():
        return
    try:
        _get_client().update_item(
            TableName=TABLE,
            Key={"session_id": {"S": session_id}},
            UpdateExpression="REMOVE running_phase, running_until",
        )
    except Exception as e:
        logger.error("DynamoDB error inesperado en release_phase %s: %s", session_id, e)


def consume_token(session_id: str) -> Optional[str]:
    """Lee y BORRA el token de tarea en una sola operación atómica (un solo uso).

    Devuelve None si no había token (por ejemplo, un segundo clic del docente).
    """
    if not enabled():
        return None
    try:
        resp = _get_client().update_item(
            TableName=TABLE,
            Key={"session_id": {"S": session_id}},
            UpdateExpression="REMOVE task_token",
            ConditionExpression="attribute_exists(task_token) AND task_token <> :vacio",
            ExpressionAttributeValues={":vacio": {"S": ""}},
            ReturnValues="UPDATED_OLD",
        )
        return resp["Attributes"]["task_token"]["S"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
```

- [ ] **Paso 4: ver que pasan**

Ejecuta: `wt prisma_agents/tests/api/test_dynamo_store_fases.py prisma_agents/tests/api/test_dynamo_store.py`
Esperado: todo PASA (los tests existentes de `dynamo_store` siguen verdes).

- [ ] **Paso 5: commit**

```bash
git add prisma_agents/api/dynamo_store.py prisma_agents/tests/api/test_dynamo_store_fases.py
git commit -m "feat(sfn): campos de fase, candado de ejecucion y consumo atomico del token en DynamoDB"
```

---

### Tarea 2: estado entre fases en S3 (`phase_store`)

**Archivos:**
- Crear: `prisma_agents/api/phase_store.py`
- Crear: `prisma_agents/tests/api/test_phase_store.py`

**Interfaces:**
- Produce: `STATE_KEYS: tuple[str, ...]`, `state_key(session_id) -> str`, `save_state(session_id, state: dict) -> None`,
  `load_state(session_id) -> dict` (lanza `PhaseStateNotFound`), `update_state(session_id, **delta) -> dict`, `delete_state(session_id) -> None`, `class PhaseStateNotFound(Exception)`.

- [ ] **Paso 1: escribir los tests que fallan**

```python
"""Estado del flujo entre fases (S3 state/{id}.json): lista explícita de claves, cifrado y sin tocar jobs/."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

from api import phase_store as ps


@pytest.fixture
def s3(monkeypatch):
    c = MagicMock()
    monkeypatch.setenv("S3_BUCKET", "bucket-test")
    monkeypatch.setattr(ps, "_client", lambda: c)
    return c


def _cuerpo(put_call):
    return json.loads(put_call.kwargs["Body"].decode("utf-8"))


def test_la_clave_va_bajo_state_y_nunca_bajo_jobs():
    assert ps.state_key("abc") == "state/abc.json"
    assert not ps.state_key("abc").startswith("jobs/")


def test_save_guarda_solo_las_claves_permitidas_y_cifra(s3):
    ps.save_state("s1", {"perfil_paci": "P", "api_session_id": "no", "otra": 1, "warnings": ["w"]})
    kw = s3.put_object.call_args.kwargs
    assert kw["Bucket"] == "bucket-test" and kw["Key"] == "state/s1.json"
    assert kw["ServerSideEncryption"] == "AES256"
    cuerpo = _cuerpo(s3.put_object.call_args)
    assert cuerpo == {"perfil_paci": "P", "warnings": ["w"]}      # api_session_id y otra NO se persisten


def test_load_devuelve_el_estado(s3):
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps({"perfil_paci": "P"}).encode())}
    assert ps.load_state("s1") == {"perfil_paci": "P"}


def test_load_de_un_objeto_ausente_lanza_phase_state_not_found(s3):
    """Foco de revisión 4: el estado expiró o se borró."""
    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
    with pytest.raises(ps.PhaseStateNotFound):
        ps.load_state("s1")


def test_load_propaga_otros_errores_de_s3(s3):
    s3.get_object.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "GetObject")
    with pytest.raises(ClientError):
        ps.load_state("s1")


def test_update_state_mezcla_con_lo_existente(s3):
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps({"perfil_paci": "P"}).encode())}
    nuevo = ps.update_state("s1", hitl_reason="falta claridad")
    assert nuevo == {"perfil_paci": "P", "hitl_reason": "falta claridad"}
    assert _cuerpo(s3.put_object.call_args) == nuevo


def test_update_state_crea_si_no_existia(s3):
    s3.get_object.side_effect = ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
    assert ps.update_state("s1", hitl_reason="x") == {"hitl_reason": "x"}


def test_delete_state_ignora_errores(s3):
    s3.delete_object.side_effect = RuntimeError("boom")
    ps.delete_state("s1")                       # no lanza
    s3.delete_object.assert_called_once_with(Bucket="bucket-test", Key="state/s1.json")


def test_sin_bucket_configurado_falla_con_mensaje_claro(monkeypatch):
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="S3_BUCKET"):
        ps.save_state("s1", {"perfil_paci": "P"})
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/api/test_phase_store.py`
Esperado: FALLA (`ModuleNotFoundError: No module named 'api.phase_store'`).

- [ ] **Paso 3: implementar**

```python
"""
Estado del flujo ENTRE FASES, en S3 (`state/{session_id}.json`).

Por qué S3 y no DynamoDB: el ítem de DynamoDB tiene tope de 400 KB y aquí viajan textos completos de documentos
y de materiales de referencia.

Reglas (ver la especificación):
  * Va bajo `state/`, NUNCA bajo `jobs/`: ese prefijo dispara la notificación de S3 y se armaría un bucle.
  * Solo se persiste la lista explícita STATE_KEYS (nada de `api_session_id` ni claves internas).
  * Contiene datos de menores (diagnóstico): cifrado en reposo y expiración de 7 días (regla de ciclo de vida del bucket).
    Además se borra al finalizar el flujo.
"""
import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

STATE_PREFIX = "state/"

STATE_KEYS = (
    # entradas
    "paci_document", "material_document", "prompt_docente", "school_id",
    # salidas de los agentes
    "perfil_paci", "planificacion_adaptada", "rubrica", "evaluacion_critica",
    # control entre fases
    "materiales_referencia", "hitl_feedback_a1", "hitl_feedback_a2", "hitl_reason", "critica_previa",
    "status", "warnings", "validation_code", "validation_reason",
)


class PhaseStateNotFound(Exception):
    """El estado de la sesión no existe en S3 (expiró o se borró)."""


def state_key(session_id: str) -> str:
    return f"{STATE_PREFIX}{session_id}.json"


def _bucket() -> str:
    bucket = os.environ.get("S3_BUCKET", "")
    if not bucket:
        raise RuntimeError("S3_BUCKET no está configurado: no se puede guardar el estado de las fases")
    return bucket


def _client():
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def save_state(session_id: str, state: dict) -> None:
    permitido = {k: v for k, v in state.items() if k in STATE_KEYS}
    _client().put_object(
        Bucket=_bucket(),
        Key=state_key(session_id),
        Body=json.dumps(permitido, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )


def load_state(session_id: str) -> dict:
    try:
        resp = _client().get_object(Bucket=_bucket(), Key=state_key(session_id))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise PhaseStateNotFound(session_id) from e
        raise
    return json.loads(resp["Body"].read().decode("utf-8"))


def update_state(session_id: str, **delta) -> dict:
    try:
        actual = load_state(session_id)
    except PhaseStateNotFound:
        actual = {}
    actual.update(delta)
    save_state(session_id, actual)
    return {k: v for k, v in actual.items() if k in STATE_KEYS}


def delete_state(session_id: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=state_key(session_id))
    except Exception as e:                       # limpieza best-effort: la regla de ciclo de vida lo borra igual
        logger.warning("no se pudo borrar el estado de %s: %s", session_id, type(e).__name__)
```

- [ ] **Paso 4: ver que pasan**

Ejecuta: `wt prisma_agents/tests/api/test_phase_store.py`
Esperado: PASA.

- [ ] **Paso 5: commit**

```bash
git add prisma_agents/api/phase_store.py prisma_agents/tests/api/test_phase_store.py
git commit -m "feat(sfn): estado entre fases en S3 (state/, cifrado, lista explicita de claves)"
```

---

### Tarea 3: cliente de Step Functions (`sfn_client`)

**Archivos:**
- Crear: `prisma_agents/api/sfn_client.py`
- Crear: `prisma_agents/tests/api/test_sfn_client.py`

**Interfaces:**
- Produce: `enabled() -> bool`, `send_success(token: str, output: dict)`, `send_failure(token: str, error: str, cause: str = "")`,
  `send_heartbeat(token: str)`, `execution_arn(session_id: str) -> str`, `stop_execution(session_id: str) -> None`,
  `async with_heartbeat(token: str, work: Awaitable, interval: float = 30.0)`, `class TaskTokenGone(Exception)`, `class PhaseAbandoned(Exception)`.

- [ ] **Paso 1: escribir los tests que fallan**

```python
"""Cliente de Step Functions: callbacks de la tarea, StopExecution y heartbeat que cancela la fase si el token se pierde."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

from api import sfn_client as sc


def _err(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


@pytest.fixture
def cli(monkeypatch):
    c = MagicMock()
    monkeypatch.setenv("SFN_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:111111111111:stateMachine:prisma-flujo")
    monkeypatch.setattr(sc, "_client", lambda: c)
    return c


def test_enabled_depende_de_la_variable(monkeypatch):
    monkeypatch.delenv("SFN_STATE_MACHINE_ARN", raising=False)
    assert sc.enabled() is False
    monkeypatch.setenv("SFN_STATE_MACHINE_ARN", "arn:x")
    assert sc.enabled() is True


def test_send_success_serializa_la_salida(cli):
    sc.send_success("tok", {"status": "ok"})
    cli.send_task_success.assert_called_once_with(taskToken="tok", output=json.dumps({"status": "ok"}))


@pytest.mark.parametrize("codigo", ["TaskDoesNotExist", "TaskTimedOut", "InvalidToken"])
def test_token_ya_no_valido_se_traduce_a_task_token_gone(cli, codigo):
    cli.send_task_success.side_effect = _err(codigo)
    with pytest.raises(sc.TaskTokenGone):
        sc.send_success("tok", {})


def test_otros_errores_se_propagan(cli):
    cli.send_task_success.side_effect = _err("ThrottlingException")
    with pytest.raises(ClientError):
        sc.send_success("tok", {})


def test_send_failure_recorta_error_y_causa(cli):
    sc.send_failure("tok", "E" * 500, "C" * 40000)
    kw = cli.send_task_failure.call_args.kwargs
    assert len(kw["error"]) == 256 and len(kw["cause"]) == 32768


def test_execution_arn_se_deriva_del_arn_de_la_maquina(cli):
    assert sc.execution_arn("sid-1") == "arn:aws:states:us-east-1:111111111111:execution:prisma-flujo:sid-1"


def test_stop_execution_llama_a_la_api_con_el_arn_derivado(cli):
    sc.stop_execution("sid-1")
    assert cli.stop_execution.call_args.kwargs["executionArn"].endswith(":execution:prisma-flujo:sid-1")


def test_stop_execution_ignora_una_ejecucion_inexistente(cli):
    cli.stop_execution.side_effect = _err("ExecutionDoesNotExist")
    sc.stop_execution("sid-1")                    # no lanza


# ── with_heartbeat ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_with_heartbeat_devuelve_el_resultado_y_envia_latidos():
    latidos = []

    async def trabajo():
        await asyncio.sleep(0.25)
        return {"status": "ok"}

    with patch.object(sc, "send_heartbeat", lambda tok: latidos.append(tok)):
        resultado = await sc.with_heartbeat("tok", trabajo(), interval=0.05)
    assert resultado == {"status": "ok"} and len(latidos) >= 2 and set(latidos) == {"tok"}


@pytest.mark.asyncio
async def test_with_heartbeat_cancela_la_fase_si_el_token_se_pierde():
    """Foco de revisión 3: Step Functions reintentó o canceló; seguir gastando Gemini no sirve de nada."""
    terminado = asyncio.Event()

    async def trabajo():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            terminado.set()
            raise

    def latido_perdido(tok):
        raise sc.TaskTokenGone("TaskDoesNotExist")

    with patch.object(sc, "send_heartbeat", latido_perdido):
        with pytest.raises(sc.PhaseAbandoned):
            await sc.with_heartbeat("tok", trabajo(), interval=0.05)
    assert terminado.is_set()                     # el trabajo fue cancelado


@pytest.mark.asyncio
async def test_with_heartbeat_propaga_los_errores_del_trabajo():
    async def trabajo():
        raise ValueError("boom")

    with patch.object(sc, "send_heartbeat", lambda tok: None):
        with pytest.raises(ValueError):
            await sc.with_heartbeat("tok", trabajo(), interval=0.05)
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/api/test_sfn_client.py`
Esperado: FALLA (`ModuleNotFoundError: No module named 'api.sfn_client'`).

- [ ] **Paso 3: implementar**

```python
"""
Cliente de Step Functions para el workflow: completa las tareas con `waitForTaskToken`, envía heartbeats y cancela ejecuciones.

`boto3` respeta AWS_ENDPOINT_URL, así que en local apunta a LocalStack sin tocar código.

Datos personales: por Step Functions solo viajan ids, números y estados (su historial guarda entradas y salidas 90 días en claro).
Por eso `send_failure` NO recibe texto de excepciones (podrían incluir contenido de documentos): solo el nombre de la clase.
"""
import asyncio
import json
import logging
import os
from typing import Awaitable

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_TOKEN_INVALIDO = {"TaskDoesNotExist", "TaskTimedOut", "InvalidToken"}


class TaskTokenGone(Exception):
    """El token de tarea ya no es válido (la tarea venció, fue reintentada o la ejecución se detuvo)."""


class PhaseAbandoned(Exception):
    """La fase se canceló porque su token dejó de ser válido."""


def _client():
    return boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def enabled() -> bool:
    return bool(os.environ.get("SFN_STATE_MACHINE_ARN"))


def _traducir(e: ClientError) -> Exception:
    return TaskTokenGone(e.response["Error"]["Code"]) if e.response["Error"]["Code"] in _TOKEN_INVALIDO else e


def send_success(token: str, output: dict) -> None:
    try:
        _client().send_task_success(taskToken=token, output=json.dumps(output))
    except ClientError as e:
        raise _traducir(e) from e


def send_failure(token: str, error: str, cause: str = "") -> None:
    try:
        _client().send_task_failure(taskToken=token, error=error[:256], cause=cause[:32768])
    except ClientError as e:
        raise _traducir(e) from e


def send_heartbeat(token: str) -> None:
    try:
        _client().send_task_heartbeat(taskToken=token)
    except ClientError as e:
        raise _traducir(e) from e


def execution_arn(session_id: str) -> str:
    """El ARN de la ejecución se deriva del de la máquina: la ejecución se nombra con el session_id."""
    return os.environ["SFN_STATE_MACHINE_ARN"].replace(":stateMachine:", ":execution:") + f":{session_id}"


def stop_execution(session_id: str) -> None:
    try:
        _client().stop_execution(executionArn=execution_arn(session_id), cause="Cancelada por el docente")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ExecutionDoesNotExist":
            raise


async def with_heartbeat(token: str, work: Awaitable, interval: float = 30.0):
    """Corre `work` enviando un heartbeat cada `interval` segundos.

    Si el heartbeat descubre que el token ya no es válido, CANCELA el trabajo y lanza PhaseAbandoned: nadie está
    esperando ese resultado y seguir sería gastar llamadas a Gemini.
    """
    tarea = asyncio.ensure_future(work)
    try:
        while True:
            hecho, _ = await asyncio.wait({tarea}, timeout=interval)
            if hecho:
                return tarea.result()
            try:
                await asyncio.to_thread(send_heartbeat, token)
            except TaskTokenGone as e:
                tarea.cancel()
                await asyncio.gather(tarea, return_exceptions=True)
                raise PhaseAbandoned("el token de tarea ya no es válido") from e
    except asyncio.CancelledError:
        tarea.cancel()
        raise
```

- [ ] **Paso 4: ver que pasan**

Ejecuta: `wt prisma_agents/tests/api/test_sfn_client.py`
Esperado: PASA.

- [ ] **Paso 5: commit**

```bash
git add prisma_agents/api/sfn_client.py prisma_agents/tests/api/test_sfn_client.py
git commit -m "feat(sfn): cliente de Step Functions con heartbeat que cancela la fase si el token se pierde"
```

---

### Tarea 4: orquestador por fases (`PaciWorkflowAgent(phase=…)`) y `run_phase`

Es la tarea que toca `agent.py` (orquestador protegido; la aprobación de la especificación es la confirmación humana). **No cambia ningún prompt,
límite ni timeout.** Solo se reparte el cuerpo de `_run_async_impl` en métodos y se agrega un parámetro de fase.

**Archivos:**
- Crear: `prisma_agents/utils/hitl_feedback.py`, `prisma_agents/tests/test_hitl_feedback.py`
- Modificar: `prisma_agents/agent.py` (clase `PaciWorkflowAgent` y la línea final `root_agent = ...`)
- Modificar: `prisma_agents/run.py` (función nueva `run_phase`, antes de `if __name__ == "__main__":`)
- Crear: `prisma_agents/tests/test_agent_phases.py`, `prisma_agents/tests/test_run_phase.py`

**Interfaces:**
- Produce: `feedback_a1(razon: str) -> str`, `feedback_a2(razon: str) -> str` (`utils/hitl_feedback.py`).
- Produce: `PaciWorkflowAgent(phase: str = "all")` con `phase ∈ {"all","a","b"}`; singletons `root_agent`, `root_agent_fase_a`, `root_agent_fase_b`.
- Produce: `async run_phase(phase: str, state: dict, *, user_id: str, api_session_id: str) -> dict` en `run.py`.
- Contrato de la fase `a`: lee `state["retry_agent"]` (0 = intento inicial; 1 = reanaliza Agente 1 y luego Agente 2; 2 = solo Agente 2). La fase `b` es el bucle Generador/Crítico.
- Contrato de salida de `a`/`b`: al terminar emiten un evento final con `state_delta` de `materiales_referencia`, `status`, `warnings`, `validation_code`, `validation_reason`
  (lo que se escribe directo en `ctx.session.state` no sobrevive al `get_session`; solo viaja lo que va en un `state_delta`).

- [ ] **Paso 1: tests de los textos de retroalimentación**

`prisma_agents/tests/test_hitl_feedback.py`:

```python
"""Los textos de retroalimentación del docente son los de siempre: este test los fija para detectar cualquier deriva."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.hitl_feedback import feedback_a1, feedback_a2


def test_feedback_a1_es_el_texto_original():
    assert feedback_a1("falta claridad") == (
        "\nRETROALIMENTACIÓN DEL DOCENTE — Debes revisar tu análisis "
        "considerando el siguiente problema señalado:\n"
        "\"falta claridad\"\n"
        "Ajusta tu respuesta para abordar específicamente este punto."
    )


def test_feedback_a2_es_el_texto_original():
    assert feedback_a2("muy largo") == (
        "\nRETROALIMENTACIÓN DEL DOCENTE — Debes revisar la adaptación "
        "considerando el siguiente problema señalado:\n"
        "\"muy largo\"\n"
        "Ajusta tu respuesta para abordar específicamente este punto."
    )
```

- [ ] **Paso 2: ver que falla**

Ejecuta: `wt prisma_agents/tests/test_hitl_feedback.py` → FALLA (`ModuleNotFoundError: utils.hitl_feedback`).

- [ ] **Paso 3: implementar el módulo**

`prisma_agents/utils/hitl_feedback.py`:

```python
"""Textos de retroalimentación que el docente le da al Agente 1 (análisis) o al Agente 2 (adaptación) al rechazar.

Se extrajeron de agent.py SIN cambiar una coma: los usan tanto el bucle HITL en memoria (modo completo) como las fases
de Step Functions (workflow_runner). Fuente única para que ambos caminos no se desvíen.
"""


def feedback_a1(razon: str) -> str:
    return (
        f"\nRETROALIMENTACIÓN DEL DOCENTE — Debes revisar tu análisis "
        f"considerando el siguiente problema señalado:\n"
        f"\"{razon}\"\n"
        f"Ajusta tu respuesta para abordar específicamente este punto."
    )


def feedback_a2(razon: str) -> str:
    return (
        f"\nRETROALIMENTACIÓN DEL DOCENTE — Debes revisar la adaptación "
        f"considerando el siguiente problema señalado:\n"
        f"\"{razon}\"\n"
        f"Ajusta tu respuesta para abordar específicamente este punto."
    )
```

Ejecuta: `wt prisma_agents/tests/test_hitl_feedback.py` → PASA.

- [ ] **Paso 4: tests de las fases del orquestador**

`prisma_agents/tests/test_agent_phases.py`:

```python
"""Fases del orquestador: `phase="all"` conserva el comportamiento de siempre; `a` y `b` lo parten sin cambiar la lógica."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agent as ag
from utils.hitl_feedback import feedback_a2


class Camino:
    """Reemplaza a _run_with_timeout: registra qué agentes corren y simula sus salidas (sin Gemini)."""

    def __init__(self, status_en=None):
        self.labels = []
        self.status_en = status_en or {}

    async def __call__(self, agente, ctx, label):
        self.labels.append(label)
        if label in self.status_en:
            ctx.session.state["status"] = self.status_en[label]
        if label.startswith("Agente 1"):
            ctx.session.state["perfil_paci"] = "PERFIL"
        elif label == "Agente 2":
            ctx.session.state["planificacion_adaptada"] = "PLAN"
        elif label.startswith("Agente 3"):
            ctx.session.state["rubrica"] = "RUBRICA"
        elif label.startswith("Agente Crítico"):
            ctx.session.state["evaluacion_critica"] = {"acceptable": True}
        for _ in ():
            yield _


def ctx(**estado):
    base = {"school_id": "", "prompt_docente": "", "paci_document": "x", "material_document": "y"}
    base.update(estado)
    return SimpleNamespace(session=SimpleNamespace(state=base))


async def correr(agente, c):
    return [e async for e in agente._run_async_impl(c)]


def delta(eventos):
    return eventos[-1].actions.state_delta


SIN_BLOQUEO = SimpleNamespace(blocked=False, code="", reason="")
ACEPTA = SimpleNamespace(action="accept", score=95, warnings=["w"], critical_issues=[], regeneration_instructions="")
REGENERA = SimpleNamespace(action="regenerate", score=10, warnings=[], critical_issues=[], regeneration_instructions="mejora")


@pytest.fixture
def parches():
    with patch("agent.evaluate_paci_compliance", return_value=SIN_BLOQUEO) as comp, \
         patch("agent.interpret_critic_decision", return_value=ACEPTA) as dec, \
         patch("agent.get_reference_materials_async", new=AsyncMock(return_value="")) as libros:
        yield SimpleNamespace(comp=comp, dec=dec, libros=libros)


# ── phase="all": regresión del comportamiento actual ────────────────────────
@pytest.mark.asyncio
async def test_all_corre_el_flujo_completo_como_antes(parches):
    camino, c = Camino(), ctx()
    with patch("agent._run_with_timeout", camino), patch("agent._hitl_checkpoint", new=AsyncMock(return_value=(True, "", 0))):
        await correr(ag.PaciWorkflowAgent(), c)
    assert camino.labels == ["Agente 1", "Agente 2", "Agente 3 (it.1)", "Agente Crítico (it.1)"]
    assert c.session.state["status"] == "success"


@pytest.mark.asyncio
async def test_all_rechazo_del_docente_reintenta_solo_el_agente_2_con_su_feedback(parches):
    camino, c = Camino(), ctx()
    hitl = AsyncMock(side_effect=[(False, "muy largo", 2), (True, "", 0)])
    with patch("agent._run_with_timeout", camino), patch("agent._hitl_checkpoint", new=hitl):
        await correr(ag.PaciWorkflowAgent(), c)
    assert camino.labels[:3] == ["Agente 1", "Agente 2", "Agente 2"]
    assert c.session.state["hitl_feedback_a2"] == feedback_a2("muy largo")


@pytest.mark.asyncio
async def test_all_rechazo_agente_1_reanaliza_y_luego_adapta(parches):
    camino, c = Camino(), ctx()
    hitl = AsyncMock(side_effect=[(False, "x", 1), (True, "", 0)])
    with patch("agent._run_with_timeout", camino), patch("agent._hitl_checkpoint", new=hitl):
        await correr(ag.PaciWorkflowAgent(), c)
    assert camino.labels[:4] == ["Agente 1", "Agente 2", "Agente 1 (retry)", "Agente 2"]


@pytest.mark.asyncio
async def test_all_tres_rechazos_terminan_en_hitl_rejected(parches):
    camino, c = Camino(), ctx()
    hitl = AsyncMock(side_effect=[(False, "x", 2)] * 3)
    with patch("agent._run_with_timeout", camino), patch("agent._hitl_checkpoint", new=hitl):
        await correr(ag.PaciWorkflowAgent(), c)
    assert c.session.state["status"] == "hitl_rejected" and "Agente 3 (it.1)" not in camino.labels


# ── phase="a" ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fase_a_inicial_corre_agente_1_materiales_y_agente_2(parches):
    camino, c = Camino(), ctx(retry_agent=0)
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert camino.labels == ["Agente 1", "Agente 2"]
    assert parches.comp.called                                          # el gate normativo corrió
    assert delta(eventos)["materiales_referencia"] == ""               # viaja en el state_delta final


@pytest.mark.asyncio
async def test_fase_a_pasa_por_los_materiales_cuando_hay_colegio_ramo_y_curso(parches):
    parches.libros.return_value = "TEXTO DE LIBROS"
    camino = Camino()
    c = ctx(retry_agent=0, school_id="colegio_x", prompt_docente="matematica 5basico")
    with patch("agent._run_with_timeout", camino), \
         patch("agent.normalize_subject", return_value="matematica"), patch("agent.normalize_grade", return_value="5basico"):
        eventos = await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert parches.libros.await_count == 1
    assert "TEXTO DE LIBROS" in delta(eventos)["materiales_referencia"]
    assert "<documento_usuario" in delta(eventos)["materiales_referencia"]     # delimitador anti-injection intacto


@pytest.mark.asyncio
async def test_fase_a_bloqueada_por_normativa_se_detiene_en_el_agente_1(parches):
    parches.comp.return_value = SimpleNamespace(blocked=True, code="paci_vencido", reason="vencido")
    camino, c = Camino(), ctx(retry_agent=0)
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert camino.labels == ["Agente 1"]
    assert delta(eventos)["status"] == "validation_failed" and delta(eventos)["validation_code"] == "paci_vencido"


@pytest.mark.asyncio
async def test_fase_a_timeout_del_agente_1_llega_al_estado_final(parches):
    camino, c = Camino(status_en={"Agente 1": "timeout"}), ctx(retry_agent=0)
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert camino.labels == ["Agente 1"] and delta(eventos)["status"] == "timeout"


@pytest.mark.asyncio
async def test_fase_a_reintento_1_reanaliza_sin_gate_ni_materiales(parches):
    camino, c = Camino(), ctx(retry_agent=1, perfil_paci="P", materiales_referencia="M")
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert camino.labels == ["Agente 1 (retry)", "Agente 2"]
    assert not parches.comp.called and not parches.libros.called
    assert delta(eventos)["materiales_referencia"] == "M"              # se conserva lo ya guardado


@pytest.mark.asyncio
async def test_fase_a_reintento_2_solo_vuelve_a_adaptar(parches):
    camino, c = Camino(), ctx(retry_agent=2)
    with patch("agent._run_with_timeout", camino):
        await correr(ag.PaciWorkflowAgent(phase="a"), c)
    assert camino.labels == ["Agente 2"]


# ── phase="b" ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fase_b_acepta_a_la_primera(parches):
    camino, c = Camino(), ctx(perfil_paci="P", planificacion_adaptada="PL")
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="b"), c)
    assert camino.labels == ["Agente 3 (it.1)", "Agente Crítico (it.1)"]
    assert delta(eventos)["status"] == "success" and delta(eventos)["warnings"] == ["w"]


@pytest.mark.asyncio
async def test_fase_b_regenera_hasta_el_maximo_y_entrega_la_ultima_version(parches):
    parches.dec.return_value = REGENERA
    camino, c = Camino(), ctx(perfil_paci="P", planificacion_adaptada="PL")
    with patch("agent._run_with_timeout", camino):
        eventos = await correr(ag.PaciWorkflowAgent(phase="b"), c)
    assert len(camino.labels) == 6 and camino.labels[-2] == "Agente 3 (it.3)"
    assert delta(eventos)["status"] == "fail"


def test_fase_desconocida_falla():
    with pytest.raises(ValueError, match="fase"):
        ag.PaciWorkflowAgent(phase="z")


def test_existen_las_tres_raices():
    assert ag.root_agent.phase == "all" and ag.root_agent_fase_a.phase == "a" and ag.root_agent_fase_b.phase == "b"
```

- [ ] **Paso 5: ver que fallan**

Ejecuta: `wt prisma_agents/tests/test_agent_phases.py` → FALLA (`PaciWorkflowAgent() got an unexpected keyword argument 'phase'`).

- [ ] **Paso 6: implementar en `agent.py`**

6a. Junto a los otros imports de `utils`, agrega:

```python
from utils.hitl_feedback import feedback_a1, feedback_a2
```

6b. Justo **antes** de `class PaciWorkflowAgent(BaseAgent):` agrega:

```python
# Estados que detienen el flujo antes del bucle HITL (los dos los escribe el propio orquestador).
_ESTADOS_QUE_DETIENEN = ("timeout", "validation_failed")

# Claves de control que deben quedar en el estado persistido de ADK al terminar una FASE. Lo que se escribe directo en
# `ctx.session.state` no sobrevive al get_session(); solo viaja lo que va en el state_delta de un Event.
_CLAVES_ENTRE_FASES = ("materiales_referencia", "status", "warnings", "validation_code", "validation_reason")
```

6c. En la clase `PaciWorkflowAgent`, después de la línea `critico_agent: LlmAgent | None = None` agrega el campo:

```python
    # "all": flujo completo (CLI y modo local) · "a": Agente 1 + gate + materiales + Agente 2 · "b": Generador + Crítico
    phase: str = "all"
```

y reemplaza el `__init__` por:

```python
    def __init__(self, phase: str = "all"):
        if phase not in ("all", "a", "b"):
            raise ValueError(f"fase desconocida: {phase!r} (use 'all', 'a' o 'b')")
        _analizador = make_analizador_paci_agent()
        _adaptador = make_adaptador_agent()
        _generador = make_generador_rubrica_agent()
        _critico = make_critico_agent()
        super().__init__(
            name="PaciWorkflow",
            description=(
                "Coordina el flujo completo: analiza el PACI, adapta el material "
                "educativo y genera una rúbrica revisada críticamente."
            ),
            sub_agents=[_analizador, _adaptador, _generador, _critico],
            analizador_paci_agent=_analizador,
            adaptador_agent=_adaptador,
            generador_rubrica_agent=_generador,
            critico_agent=_critico,
            phase=phase,
        )
```

6d. Reemplaza el método `_run_async_impl` completo (desde `async def _run_async_impl(` hasta el último `yield self._commit_state(ctx, status="fail")` del bucle del Generador/Crítico, justo antes de `def _parse_critic_json`) por este bloque. La lógica es la misma de antes, cortada en métodos:

```python
    async def _analisis_inicial(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Agente 1 + gate de compliance + materiales de referencia (todo lo que va antes del bucle HITL)."""
        # ── Agente 1: Análisis inicial del PACI ──────────────────────────────
        print("\n[Agente 1] Analizando PACI...\n")
        async for event in _run_with_timeout(self.analizador_paci_agent, ctx, "Agente 1"):
            yield event
        if ctx.session.state.get("status") == "timeout":
            return

        # ── Gate de compliance del PACI (Decretos 170/83) ────────────────────
        meta = _extract_metadatos(ctx.session.state.get("perfil_paci", ""))
        compliance = evaluate_paci_compliance(meta, date.today())
        if compliance.blocked:
            print(f"\n[GATE] PACI bloqueado: {compliance.code} — {compliance.reason}\n")
            # El evento SSE terminal lo emite _finalize_result (camino API). Aquí solo
            # persistimos el estado terminal vía state_delta para que run.py lo lea.
            yield self._commit_state(
                ctx,
                status="validation_failed",
                validation_code=compliance.code,
                validation_reason=compliance.reason,
            )
            return

        # ── Book Repository: materiales de referencia del establecimiento ────
        perfil_paci = ctx.session.state.get("perfil_paci", "")
        prompt_docente = ctx.session.state.get("prompt_docente", "")
        subject_raw, grade_raw = _extract_subject_grade(perfil_paci)
        school_id = ctx.session.state.get("school_id", "")
        subject = normalize_subject(subject_raw) if subject_raw else None
        grade = normalize_grade(grade_raw) if grade_raw else None

        # Fallback: si el PACI no dio ramo o curso, intentar desde el prompt del docente
        if not subject and prompt_docente:
            subject = normalize_subject(prompt_docente)
        if not grade and prompt_docente:
            grade = normalize_grade(prompt_docente)

        # Fallback: si sigue sin ramo o curso (ej. PACI dice "Todas las asignaturas"),
        # inferir desde el material_document sin costo extra de tokens
        if not subject or not grade:
            material_doc = ctx.session.state.get("material_document", "")
            if not subject:
                subject = normalize_subject(material_doc)
            if not grade:
                grade = normalize_grade(material_doc)

        materiales_texto = ""
        if school_id and subject and grade:
            print(f"\n[BookRepository] Buscando materiales: {subject}/{grade} — colegio {school_id}...\n")
            raw = await get_reference_materials_async(school_id, subject, grade, perfil_paci)
            if raw:
                # Envuelve con encabezado y etiqueta de seguridad para que el LLM identifique la sección
                materiales_texto = (
                    "### MATERIALES DE REFERENCIA DEL ESTABLECIMIENTO:\n"
                    "<documento_usuario tipo=\"materiales_referencia\">\n"
                    f"{raw}\n"
                    "</documento_usuario>"
                )
                print("  ✓ Materiales de referencia cargados.\n")
            else:
                print("  ℹ Sin materiales disponibles para este ramo/curso.\n")
        else:
            print("\n[BookRepository] Sin school_id o ramo/curso no reconocido — omitiendo materiales.\n")

        ctx.session.state["materiales_referencia"] = materiales_texto

    async def _fase_b(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        """Bucle Generador de Rúbrica + Agente Crítico (después de la aprobación del docente)."""
        for iteration in range(1, MAX_ITERATIONS + 1):
            print(f"\n[Agente 3 — Iteración {iteration}/{MAX_ITERATIONS}] Generando rúbrica...\n")
            async for event in _run_with_timeout(self.generador_rubrica_agent, ctx, f"Agente 3 (it.{iteration})"):
                yield event
            if ctx.session.state.get("status") == "timeout":
                return

            print(f"\n[Agente Crítico — Iteración {iteration}/{MAX_ITERATIONS}] Evaluando rúbrica...\n")
            async for event in _run_with_timeout(self.critico_agent, ctx, f"Agente Crítico (it.{iteration})"):
                yield event
            if ctx.session.state.get("status") == "timeout":
                return

            evaluacion_raw = ctx.session.state.get("evaluacion_critica", "")
            print(f"\n[DEBUG] Respuesta cruda del Agente Crítico:\n{str(evaluacion_raw)[:500]}\n")
            evaluacion = _parse_critic_json(evaluacion_raw)
            decision = interpret_critic_decision(evaluacion)

            if decision.action == "block_critical":
                codigos = ", ".join(decision.critical_issues)
                print(f"\n[GATE] Rúbrica con ítem crítico ({codigos}) — deteniendo sin reintentar.\n")
                yield self._commit_state(
                    ctx,
                    status="compliance_blocked",
                    validation_code="rubrica_critica",
                    validation_reason=(
                        "La rúbrica generada incumple una restricción normativa crítica "
                        f"({codigos}: datos personales, diagnóstico expuesto, lenguaje "
                        "estigmatizante o exención). El proceso se detuvo por seguridad. "
                        "(Decreto 67/2018 — Decreto 170/2010)"
                    ),
                )
                break

            if decision.action == "accept":
                print(f"\n✓ Rúbrica aprobada en iteración {iteration} (score {decision.score}).\n")
                yield self._commit_state(ctx, status="success", warnings=decision.warnings)
                break

            # decision.action == "regenerate"
            if iteration < MAX_ITERATIONS:
                ctx.session.state["critica_previa"] = (
                    f"RETROALIMENTACIÓN EVALUADOR (iteración {iteration}, score {decision.score}):\n"
                    f"{decision.regeneration_instructions}"
                )
                print("\n✗ Rúbrica rechazada. Reintentando con retroalimentación...\n")
            else:
                print("\n⚠ Máximo de iteraciones alcanzado. Se entrega la última versión generada.\n")
                yield self._commit_state(ctx, status="fail")

    async def _orquestar(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        if self.phase == "b":
            async for event in self._fase_b(ctx):
                yield event
            return

        # En la fase "a", `retry_agent` (lo fija workflow_runner desde la decisión del docente) dice qué repetir:
        # 0 = intento inicial · 1 = reanalizar (Agente 1) y adaptar · 2 = solo volver a adaptar.
        retry = ctx.session.state.get("retry_agent", 0) if self.phase == "a" else 0

        if retry == 0:
            async for event in self._analisis_inicial(ctx):
                yield event
            if ctx.session.state.get("status") in _ESTADOS_QUE_DETIENEN:
                return
        elif retry == 1:
            async for event in _run_with_timeout(self.analizador_paci_agent, ctx, "Agente 1 (retry)"):
                yield event
            if ctx.session.state.get("status") == "timeout":
                return

        if self.phase == "a":
            print("\n[Agente 2] Adaptando material educativo...\n")
            async for event in _run_with_timeout(self.adaptador_agent, ctx, "Agente 2"):
                yield event
            return          # el docente revisa fuera de este proceso (Step Functions); no hay checkpoint aquí

        # ── phase == "all": Loop HITL — Agente 2 + aprobación del profesor (en memoria, como siempre) ──
        for hitl_attempt in range(1, MAX_HITL_ITERATIONS + 1):
            print("\n[Agente 2] Adaptando material educativo...\n")
            async for event in _run_with_timeout(self.adaptador_agent, ctx, "Agente 2"):
                yield event
            if ctx.session.state.get("status") == "timeout":
                return

            aprobado, razon, agente = await _hitl_checkpoint(
                ctx.session.state, attempt=hitl_attempt, max_attempts=MAX_HITL_ITERATIONS
            )

            if aprobado:
                break

            # Intentos agotados — cancela el flujo
            # agente == 0: camino CLI (última iteración retorna 0 explícitamente)
            # hitl_attempt == MAX_HITL_ITERATIONS: camino API (agent_to_retry siempre 1 o 2)
            if agente == 0 or hitl_attempt == MAX_HITL_ITERATIONS:
                ctx.session.state["status"] = "hitl_rejected"
                return

            # Inyectar feedback según el agente elegido por el profesor
            if agente == 1:
                ctx.session.state["hitl_feedback_a1"] = feedback_a1(razon)
                ctx.session.state["hitl_feedback_a2"] = ""
                print("\n[Agente 1] Re-analizando PACI con feedback del docente...\n")
                async for event in _run_with_timeout(self.analizador_paci_agent, ctx, "Agente 1 (retry)"):
                    yield event
                if ctx.session.state.get("status") == "timeout":
                    return
            else:  # agente == 2
                ctx.session.state["hitl_feedback_a1"] = ""   # limpia feedback previo de a1
                ctx.session.state["hitl_feedback_a2"] = feedback_a2(razon)

        async for event in self._fase_b(ctx):
            yield event

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        async for event in self._orquestar(ctx):
            yield event
        if self.phase != "all":
            # Cada fase corre en una sesión ADK propia y nueva: lo que se escribió directo en ctx.session.state
            # (materiales, status de timeout) se pierde salvo que viaje en un state_delta.
            yield self._commit_state(
                ctx, **{k: ctx.session.state[k] for k in _CLAVES_ENTRE_FASES if k in ctx.session.state}
            )
```

6e. Reemplaza la última línea del archivo `root_agent = PaciWorkflowAgent()` por:

```python
root_agent = PaciWorkflowAgent()                       # flujo completo: CLI y modo local
root_agent_fase_a = PaciWorkflowAgent(phase="a")       # Step Functions: Agente 1 + gate + materiales + Agente 2
root_agent_fase_b = PaciWorkflowAgent(phase="b")       # Step Functions: Generador + Crítico
```

- [ ] **Paso 7: ver que pasan**

Ejecuta: `wt prisma_agents/tests/test_agent_phases.py prisma_agents/tests/test_hitl.py prisma_agents/tests/test_monitor_events_agent.py prisma_agents/tests/test_agent_factories.py`
Esperado: todo PASA (incluidos los tests existentes de HITL y del monitor).

- [ ] **Paso 8: test de `run_phase` (con el `Runner` real de ADK, sin Gemini)**

`prisma_agents/tests/test_run_phase.py`:

```python
"""run_phase: corre UNA fase con el Runner real de ADK (agentes simulados) y devuelve el estado persistido."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.adk.events import Event, EventActions

import run

SIN_BLOQUEO = SimpleNamespace(blocked=False, code="", reason="")
BASE = {"paci_document": "PACI", "material_document": "MAT", "prompt_docente": "", "school_id": ""}


async def agentes_falsos(agente, ctx, label):
    delta = {}
    if label.startswith("Agente 1"):
        delta = {"perfil_paci": "PERFIL"}
    elif label == "Agente 2":
        delta = {"planificacion_adaptada": "PLAN"}
    if delta:
        yield Event(author="falso", actions=EventActions(state_delta=delta))


@pytest.mark.asyncio
async def test_fase_a_devuelve_las_salidas_de_los_agentes_y_los_materiales():
    with patch("agent._run_with_timeout", agentes_falsos), patch("agent.evaluate_paci_compliance", return_value=SIN_BLOQUEO):
        estado = await run.run_phase("a", {**BASE, "retry_agent": 0}, user_id="u1", api_session_id="s1")
    assert estado["perfil_paci"] == "PERFIL" and estado["planificacion_adaptada"] == "PLAN"
    assert estado["materiales_referencia"] == ""


@pytest.mark.asyncio
async def test_un_timeout_escrito_directo_llega_al_estado_final():
    async def con_timeout(agente, ctx, label):
        ctx.session.state["status"] = "timeout"           # así lo escribe _run_with_timeout
        for _ in ():
            yield _

    with patch("agent._run_with_timeout", con_timeout):
        estado = await run.run_phase("a", {**BASE, "retry_agent": 0}, user_id="u1", api_session_id="s1")
    assert estado["status"] == "timeout"


@pytest.mark.asyncio
async def test_cada_fase_usa_una_sesion_adk_nueva_y_el_estado_de_entrada_se_conserva():
    with patch("agent._run_with_timeout", agentes_falsos), patch("agent.evaluate_paci_compliance", return_value=SIN_BLOQUEO):
        e1 = await run.run_phase("a", {**BASE, "retry_agent": 0}, user_id="u1", api_session_id="s1")
        e2 = await run.run_phase("a", {**e1, "retry_agent": 2}, user_id="u1", api_session_id="s1")   # mismo id de sesión API
    assert e2["perfil_paci"] == "PERFIL" and e2["paci_document"] == "PACI"


def test_fase_desconocida_falla():
    with pytest.raises(KeyError):
        asyncio.run(run.run_phase("z", {}, user_id="u", api_session_id="s"))
```

Ejecuta: `wt prisma_agents/tests/test_run_phase.py` → FALLA (`AttributeError: module 'run' has no attribute 'run_phase'`).

- [ ] **Paso 9: implementar `run_phase` en `run.py`**

Inserta esta función **antes** de `if __name__ == "__main__":` (después de `run_workflow`):

```python
async def run_phase(phase: str, state: dict, *, user_id: str, api_session_id: str) -> dict:
    """Corre UNA fase ('a' o 'b') del flujo a partir de un estado dado y devuelve el estado resultante.

    A diferencia de run_workflow no carga documentos ni exporta el DOCX: el estado viaja por S3 entre fases
    (api/phase_store.py). Cada fase usa una sesión ADK nueva e independiente: con BD_LOGS configurado, el id de la
    sesión ADK lleva el sufijo de la fase para no chocar (el estado `api_session_id` sigue siendo el de la API).
    """
    from agent import root_agent_fase_a, root_agent_fase_b
    from api.phase_store import STATE_KEYS

    raiz = {"a": root_agent_fase_a, "b": root_agent_fase_b}[phase]
    db_url = os.environ.get("BD_LOGS")
    session_service = DatabaseSessionService(db_url=db_url) if db_url else InMemorySessionService()

    inicial = {
        "critica_previa": "", "hitl_feedback_a1": "", "hitl_feedback_a2": "", "materiales_referencia": "",
        **state,
        "api_session_id": api_session_id,
    }
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=f"{api_session_id}-{phase}-{uuid.uuid4().hex[:8]}",
        state=inicial,
    )
    runner = Runner(agent=raiz, session_service=session_service, app_name=APP_NAME, plugins=[LoggingPlugin()])
    mensaje = state.get("prompt_docente") or "Inicia el flujo PACI con los documentos proporcionados."

    from langfuse import get_client, propagate_attributes
    nombre = f"paci-workflow-fase-{phase}"
    with get_client().start_as_current_observation(name=nombre):
        with propagate_attributes(
            user_id=user_id,
            session_id=api_session_id,
            trace_name=nombre,
            metadata={
                "school_id": state.get("school_id") or "sin_colegio",
                "channel": "api",
                "fase": phase,
                "env": os.environ.get("ENV", "dev"),
            },
            tags=["api", f"fase:{phase}"],
        ):
            async for _evento in runner.run_async(
                user_id=user_id, session_id=session.id, new_message=Content(parts=[Part(text=mensaje)])
            ):
                pass
            final = await session_service.get_session(app_name=APP_NAME, user_id=user_id, session_id=session.id)

    return {**state, **{k: final.state[k] for k in STATE_KEYS if k in final.state}}
```

- [ ] **Paso 10: ver que pasa**

Ejecuta: `wt prisma_agents/tests/test_run_phase.py prisma_agents/tests/test_agent_phases.py`
Esperado: PASA. Luego la suite completa: `wt prisma_agents/tests` → **312 + los nuevos**, todo verde.

- [ ] **Paso 11: commit** (sin las ediciones de modelo del dueño)

```bash
python ../local/tools/stage_sin_modelo.py prisma_agents/agent.py
git add prisma_agents/utils/hitl_feedback.py prisma_agents/tests/test_hitl_feedback.py prisma_agents/tests/test_agent_phases.py \
        prisma_agents/tests/test_run_phase.py prisma_agents/run.py
git diff --cached --stat | tail -8
git diff --cached | grep -c "3\.5-flash-lite"      # debe imprimir 0
git commit -m "feat(sfn): orquestador por fases (all/a/b) y run_phase, sin cambiar prompts ni logica"
```

---

### Tarea 5: lógica de las fases en `workflow_runner` (+ fases simuladas)

**Archivos:**
- Crear: `prisma_agents/api/mock_phases.py`
- Modificar: `prisma_agents/api/workflow_runner.py` (imports arriba; funciones nuevas al final del archivo)
- Crear: `prisma_agents/tests/api/test_workflow_phases.py`

**Interfaces:**
- Consume: `phase_store.*` (Tarea 2), `sfn_client.*` (Tarea 3), `dynamo_store.*` (Tarea 1), `run.run_phase` (Tarea 4), `utils.hitl_feedback.*`.
- Produce (todas en `api/workflow_runner.py`):
  - `async run_phase_a(session_id: str, item: dict, attempt: int, feedback_agent: int) -> dict` → `{"status": "ok"|"compliance_blocked"|"timeout"}`
  - `async run_phase_b(session_id: str, item: dict) -> dict` → `{"status": "success"|"degraded"|"compliance_blocked"|"error"}`
  - `async run_phase_job(phase: str, session_id: str, item: dict, task_token: str, attempt: int, feedback_agent: int) -> None`
  - `register_wait(session_id: str, item: dict, task_token: str, attempt: int) -> None`
  - `finalize_session(session_id: str, item: dict, status: str) -> None`
  - `emit_flow_finished(session_id: str, item: dict, status: str) -> None`
  - `submit_hitl_decision(session_id: str, item: dict, approved: bool, reason: str | None, agent_to_retry: int | None) -> None` (lanza `HitlDecisionError(status_code, detail)`)
  - `cancel_session_sfn(session_id: str, item: dict) -> None`
- Produce (`api/mock_phases.py`): `initial_state(school_id, prompt) -> dict`, `async run(phase, state, school_id) -> dict`. `school_id` simulados:
  `__mock_ok__`, `__mock_blocked__`, `__mock_timeout__`, `__mock_degraded__`, `__mock_error__` (y `__mock_dead__`, que maneja el endpoint de la Tarea 6).

- [ ] **Paso 1: escribir los tests que fallan**

`prisma_agents/tests/api/test_workflow_phases.py`:

```python
"""Fases de Step Functions en el workflow: carga/guardado del estado, finalización, espera y decisión del docente."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import MagicMock, patch

from api import dynamo_store, mock_phases, phase_store, sfn_client, workflow_runner as wr


class Emisor:
    def __init__(self):
        self.calls = []

    def __call__(self, tipo, sid, **campos):
        self.calls.append((tipo, sid, campos))

    def de(self, tipo):
        return [c[2] for c in self.calls if c[0] == tipo]


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    """Estado en memoria en vez de S3, DynamoDB espiado y publicador de eventos capturado."""
    almacen = {}

    def guardar(sid, st):
        almacen[sid] = {k: v for k, v in st.items() if k in phase_store.STATE_KEYS}

    def cargar(sid):
        if sid not in almacen:
            raise phase_store.PhaseStateNotFound(sid)
        return dict(almacen[sid])

    def actualizar(sid, **delta):
        almacen.setdefault(sid, {}).update(delta)
        return dict(almacen[sid])

    monkeypatch.setattr(phase_store, "save_state", guardar)
    monkeypatch.setattr(phase_store, "load_state", cargar)
    monkeypatch.setattr(phase_store, "update_state", actualizar)
    monkeypatch.setattr(phase_store, "delete_state", lambda sid: almacen.pop(sid, None))
    monkeypatch.setattr(wr, "S3_BUCKET", "")
    monkeypatch.setattr(mock_phases, "STEP_DELAY", 0)
    sync = MagicMock()
    monkeypatch.setattr(wr, "sync_to_dynamo", sync)
    upd = MagicMock()
    monkeypatch.setattr(dynamo_store, "update_session", upd)
    emisor = Emisor()
    with patch("api.event_publisher.publish", emisor):
        yield type("E", (), {"almacen": almacen, "sync": sync, "upd": upd, "emisor": emisor, "tmp": tmp_path})


def item(school="__mock_ok__", **extra):
    base = {"school_id": school, "prompt": "", "owner_id": "u1", "messages": [], "warnings": [],
            "paci_s3_key": "jobs/s1/paci.pdf", "material_s3_key": "jobs/s1/material.docx", "phase": "running"}
    base.update(extra)
    return base


# ── Fase A ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fase_a_inicial_simulada_guarda_el_estado_y_devuelve_ok(entorno):
    res = await wr.run_phase_a("s1", item(), attempt=1, feedback_agent=0)
    assert res == {"status": "ok"}
    assert entorno.almacen["s1"]["perfil_paci"] == "PERFIL SIMULADO"
    assert [c[0] for c in entorno.emisor.calls] == ["flow_started"]
    assert any("started_at" in c.kwargs for c in entorno.upd.call_args_list)


@pytest.mark.asyncio
async def test_fase_a_bloqueada_por_normativa_deja_la_sesion_terminal(entorno):
    res = await wr.run_phase_a("s1", item("__mock_blocked__"), attempt=1, feedback_agent=0)
    assert res == {"status": "compliance_blocked"}
    sd = entorno.sync.call_args.args[1]
    assert sd.phase == "error" and sd.workflow_status == "compliance_blocked"


@pytest.mark.asyncio
async def test_fase_a_timeout_se_informa_como_timeout(entorno):
    res = await wr.run_phase_a("s1", item("__mock_timeout__"), attempt=1, feedback_agent=0)
    assert res == {"status": "timeout"}


@pytest.mark.asyncio
async def test_fase_a_reintento_usa_el_estado_guardado_y_el_feedback_del_agente_elegido(entorno):
    from utils.hitl_feedback import feedback_a1
    entorno.almacen["s1"] = {"perfil_paci": "P", "planificacion_adaptada": "PL", "hitl_reason": "falta X"}
    capturado = {}

    async def falso(fase, estado, **kw):
        capturado.update(estado)
        return {**estado, "planificacion_adaptada": "PL2"}

    with patch.object(wr, "run_phase", falso):
        res = await wr.run_phase_a("s1", item("colegio_real"), attempt=2, feedback_agent=1)
    assert res == {"status": "ok"}
    assert capturado["retry_agent"] == 1
    assert capturado["hitl_feedback_a1"] == feedback_a1("falta X") and capturado["hitl_feedback_a2"] == ""
    assert entorno.almacen["s1"]["planificacion_adaptada"] == "PL2"
    assert entorno.emisor.calls == []                      # flow_started solo en el intento 1


@pytest.mark.asyncio
async def test_fase_a_el_contexto_de_sesion_se_fija_y_se_libera(entorno):
    from utils.usage_events import monitor_session_id
    visto = []

    async def falso(fase, estado, **kw):
        visto.append(monitor_session_id.get())
        return estado

    with patch.object(wr, "run_phase", falso), patch.object(wr, "_cargar_documentos_sync", lambda it: {"paci_document": "x"}):
        await wr.run_phase_a("s1", item("colegio_real"), attempt=1, feedback_agent=0)
    assert visto == ["s1"] and monitor_session_id.get() == ""


# ── Fase B ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fase_b_simulada_exporta_el_docx_y_completa_la_sesion(entorno):
    entorno.almacen["s1"] = {"perfil_paci": "P", "planificacion_adaptada": "PL"}
    docx = entorno.tmp / "r.docx"
    docx.write_bytes(b"x")
    with patch.object(wr, "export_results_to_docx", return_value=str(docx)) as exp:
        res = await wr.run_phase_b("s1", item())
    assert res == {"status": "success"}
    assert exp.call_args.kwargs["output_filename"] == "rubrica_adaptada_material.docx"
    sd = entorno.sync.call_args.args[1]
    assert sd.phase == "completed" and sd.workflow_status == "success"


@pytest.mark.asyncio
async def test_fase_b_degradada(entorno):
    entorno.almacen["s1"] = {"perfil_paci": "P"}
    with patch.object(wr, "export_results_to_docx", return_value=str(entorno.tmp / "r.docx")):
        res = await wr.run_phase_b("s1", item("__mock_degraded__"))
    assert res == {"status": "degraded"}


@pytest.mark.asyncio
async def test_fase_b_sin_estado_en_s3_lanza_phase_state_not_found(entorno):
    with pytest.raises(phase_store.PhaseStateNotFound):
        await wr.run_phase_b("s1", item())


# ── run_phase_job ───────────────────────────────────────────────────────────
@pytest.fixture
def sfn(monkeypatch):
    s = MagicMock()
    monkeypatch.setattr(sfn_client, "send_success", s.send_success)
    monkeypatch.setattr(sfn_client, "send_failure", s.send_failure)
    monkeypatch.setattr(sfn_client, "send_heartbeat", s.send_heartbeat)
    monkeypatch.setattr(wr, "PHASE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(dynamo_store, "release_phase", s.release)
    return s


@pytest.mark.asyncio
async def test_job_exitoso_envia_success_y_libera_el_candado(entorno, sfn):
    async def fase(*a, **k):
        return {"status": "ok"}

    with patch.object(wr, "run_phase_a", fase):
        await wr.run_phase_job("a", "s1", item(), "tok", 1, 0)
    sfn.send_success.assert_called_once_with("tok", {"status": "ok"})
    sfn.release.assert_called_once_with("s1")


@pytest.mark.asyncio
async def test_job_con_estado_ausente_envia_failure_controlado_sin_texto(entorno, sfn):
    """Foco de revisión 4: el estado de S3 no existe; la ejecución no se cuelga y no se filtra texto."""
    await wr.run_phase_job("b", "s1", item(), "tok", 1, 0)
    sfn.send_failure.assert_called_once_with("tok", "PhaseStateNotFound", "")
    sfn.release.assert_called_once_with("s1")


@pytest.mark.asyncio
async def test_job_abandonado_no_envia_nada(entorno, sfn):
    async def lenta(*a, **k):
        await asyncio.sleep(30)

    sfn.send_heartbeat.side_effect = sfn_client.TaskTokenGone("TaskDoesNotExist")
    with patch.object(wr, "run_phase_a", lenta):
        await wr.run_phase_job("a", "s1", item(), "tok", 1, 0)
    sfn.send_success.assert_not_called()
    sfn.send_failure.assert_not_called()
    sfn.release.assert_called_once_with("s1")


# ── espera del docente y finalización ───────────────────────────────────────
def test_register_wait_guarda_token_y_pasa_a_awaiting_hitl_en_una_sola_escritura(entorno):
    entorno.almacen["s1"] = {"perfil_paci": "P", "planificacion_adaptada": "PL"}
    wr.register_wait("s1", item(), "tok", attempt=2)
    assert entorno.upd.call_count == 1
    kw = entorno.upd.call_args.kwargs
    assert kw["phase"] == "awaiting_hitl" and kw["task_token"] == "tok"
    assert kw["hitl_data"] == {"perfil_paci": "P", "planificacion_adaptada": "PL", "attempt": 2, "max_attempts": 3}
    assert entorno.emisor.de("hitl_required") == [{"attempt": 2}]


@pytest.mark.parametrize("estado, workflow_status", [("hitl_rejected", "hitl_rejected"), ("expired", "error")])
def test_finalize_marca_la_sesion_y_limpia_el_estado(entorno, estado, workflow_status):
    entorno.almacen["s1"] = {"perfil_paci": "P"}
    wr.finalize_session("s1", item(started_at=100.0), estado)
    kw = entorno.upd.call_args.kwargs
    assert kw["phase"] == "error" and kw["workflow_status"] == workflow_status and kw["task_token"] == ""
    assert "s1" not in entorno.almacen
    assert entorno.emisor.de("flow_finished")[0]["status"] == ("hitl_rejected" if estado == "hitl_rejected" else "error")


def test_finalize_de_un_exito_no_pisa_lo_que_dejo_la_fase_b(entorno):
    wr.finalize_session("s1", item(phase="completed", started_at=None), "success")
    entorno.upd.assert_not_called()
    assert entorno.emisor.de("flow_finished")[0]["status"] == "success"


def test_finalize_de_un_error_inesperado_marca_error_si_la_sesion_no_estaba_terminada(entorno):
    wr.finalize_session("s1", item(phase="running"), "error")
    assert entorno.upd.call_args.kwargs["workflow_status"] == "error"


def test_la_duracion_sale_de_started_at(entorno):
    with patch("time.time", return_value=110.0):
        wr.emit_flow_finished("s1", item(started_at=100.0), "success")
    assert entorno.emisor.de("flow_finished")[0]["duration_ms"] == 10000


# ── decisión del docente ────────────────────────────────────────────────────
@pytest.fixture
def hitl(monkeypatch, entorno):
    h = MagicMock()
    h.consume.return_value = "tok"
    monkeypatch.setattr(dynamo_store, "consume_token", h.consume)
    monkeypatch.setattr(sfn_client, "send_success", h.send)
    return h


def test_rechazo_guarda_la_razon_en_s3_y_envia_solo_approved_y_agente(entorno, hitl):
    """Foco de revisión 5: el comentario del docente NO viaja por Step Functions."""
    wr.submit_hitl_decision("s1", item(phase="awaiting_hitl"), False, "El alumno Juan Pérez, RUT 1-9, necesita más apoyo", 1)
    assert entorno.almacen["s1"]["hitl_reason"].startswith("El alumno Juan")
    token, salida = hitl.send.call_args.args
    assert token == "tok" and set(salida) == {"approved", "agent_to_retry"} and salida == {"approved": False, "agent_to_retry": 1}
    assert "Juan" not in json.dumps(salida) and "RUT" not in json.dumps(salida)
    assert entorno.upd.call_args.kwargs["phase"] == "running"


def test_agente_a_reintentar_por_defecto_es_el_2(entorno, hitl):
    wr.submit_hitl_decision("s1", item(phase="awaiting_hitl"), False, "x", None)
    assert hitl.send.call_args.args[1]["agent_to_retry"] == 2


def test_aprobar_no_toca_la_razon(entorno, hitl):
    wr.submit_hitl_decision("s1", item(phase="awaiting_hitl"), True, None, None)
    assert "s1" not in entorno.almacen
    assert hitl.send.call_args.args[1] == {"approved": True, "agent_to_retry": 2}


def test_sesion_que_no_espera_al_docente_da_409(entorno, hitl):
    with pytest.raises(wr.HitlDecisionError) as e:
        wr.submit_hitl_decision("s1", item(phase="running"), True, None, None)
    assert e.value.status_code == 409
    hitl.send.assert_not_called()


def test_doble_clic_no_envia_dos_veces(entorno, hitl):
    """Foco de revisión 2: el token ya fue consumido."""
    hitl.consume.return_value = None
    with pytest.raises(wr.HitlDecisionError) as e:
        wr.submit_hitl_decision("s1", item(phase="awaiting_hitl"), True, None, None)
    assert e.value.status_code == 409
    hitl.send.assert_not_called()


def test_token_vencido_marca_la_sesion_en_error_y_da_409(entorno, hitl):
    hitl.send.side_effect = sfn_client.TaskTokenGone("TaskTimedOut")
    with pytest.raises(wr.HitlDecisionError) as e:
        wr.submit_hitl_decision("s1", item(phase="awaiting_hitl"), True, None, None)
    assert e.value.status_code == 409
    assert entorno.upd.call_args.kwargs["phase"] == "error"


def test_cancelar_marca_cancelada_detiene_la_ejecucion_y_limpia(entorno, monkeypatch):
    parar = MagicMock()
    monkeypatch.setattr(sfn_client, "stop_execution", parar)
    entorno.almacen["s1"] = {"perfil_paci": "P"}
    wr.cancel_session_sfn("s1", item(started_at=None))
    kw = entorno.upd.call_args.kwargs
    assert kw["phase"] == "error" and kw["workflow_status"] == "cancelled" and kw["task_token"] == ""
    parar.assert_called_once_with("s1")
    assert "s1" not in entorno.almacen
    assert entorno.emisor.de("flow_finished")[0]["status"] == "cancelled"
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/api/test_workflow_phases.py` → FALLA (`ImportError: cannot import name 'mock_phases'` / `AttributeError: run_phase_a`).

- [ ] **Paso 3: implementar las fases simuladas**

`prisma_agents/api/mock_phases.py`:

```python
"""Fases SIMULADAS para probar Step Functions de punta a punta sin gastar cuota de Gemini.

Se activan con un `school_id` que empieza con `__mock` (igual que api/mock_runner.py para el camino antiguo):
  __mock_ok__        flujo feliz                       __mock_degraded__  la rúbrica termina "degraded"
  __mock_blocked__   el gate normativo bloquea el PACI  __mock_error__     la fase B falla
  __mock_timeout__   un agente agota el tiempo          __mock_dead__      (lo maneja el endpoint: la fase A nunca responde)
"""
import asyncio
import os

STEP_DELAY = float(os.environ.get("MOCK_PHASE_DELAY", "0.5"))


def initial_state(school_id: str, prompt: str) -> dict:
    return {"paci_document": "PACI SIMULADO", "material_document": "MATERIAL SIMULADO", "prompt_docente": prompt, "school_id": school_id}


async def run(phase: str, state: dict, school_id: str) -> dict:
    await asyncio.sleep(STEP_DELAY)
    nuevo = dict(state)
    if phase == "a":
        if school_id == "__mock_blocked__":
            nuevo.update(status="validation_failed", validation_code="paci_vencido", validation_reason="PACI simulado vencido")
        elif school_id == "__mock_timeout__":
            nuevo["status"] = "timeout"
        else:
            nuevo.update(
                perfil_paci="PERFIL SIMULADO",
                planificacion_adaptada=f"PLAN SIMULADO (retry_agent={state.get('retry_agent', 0)})",
                materiales_referencia="",
            )
        return nuevo
    if school_id == "__mock_error__":
        raise RuntimeError("fallo simulado de la fase B")
    nuevo.update(
        rubrica="RUBRICA SIMULADA",
        evaluacion_critica={"acceptable": True},
        status="fail" if school_id == "__mock_degraded__" else "success",
        warnings=[],
    )
    return nuevo
```

- [ ] **Paso 4: implementar la lógica en `workflow_runner.py`**

4a. Imports. Reemplaza estas tres líneas del principio del archivo:

```python
from api import dynamo_store
from api.session_store import SESSIONS, HITL_CALLBACKS, sync_to_dynamo
from run import run_workflow
```

por:

```python
from api import dynamo_store, mock_phases, phase_store, sfn_client
from api.session_store import SESSIONS, HITL_CALLBACKS, SessionData, sync_to_dynamo
from run import run_phase, run_workflow
```

Junto a `from utils.input_validator import validate_prompt_docente` agrega:

```python
from utils.document_exporter import export_results_to_docx
from utils.document_loader import load_document
from utils.hitl_feedback import feedback_a1, feedback_a2
```

Y agrega `import asyncio` y `import logging` entre los imports estándar si faltan, y debajo de los imports `logger = logging.getLogger(__name__)` si no existe.

4b. Al **final** del archivo agrega:

```python


# ══ Fases de Step Functions ══════════════════════════════════════════════════
# Cada fase corre en segundo plano (ver `run_phase_job`), carga su estado de S3, lo guarda al terminar y avisa a Step Functions
# con SendTaskSuccess. Ningún proceso queda esperando al docente: la espera la sostiene Step Functions (waitForTaskToken).

MAX_HITL_ATTEMPTS = 3
PHASE_HEARTBEAT_SECONDS = float(os.environ.get("PHASE_HEARTBEAT_SECONDS", "30"))
_MOCK_PREFIX = "__mock"

_FLOW_STATUS = {"success": "success", "degraded": "degraded", "compliance_blocked": "compliance_blocked"}
_MONITOR_STATUS = {
    "success": "success", "degraded": "degraded", "compliance_blocked": "compliance_blocked",
    "hitl_rejected": "hitl_rejected", "cancelled": "cancelled", "expired": "error", "timeout": "error", "error": "error",
}
_MSG_HITL_RECHAZADO = (
    "Proceso cancelado: el análisis inicial no obtuvo aprobación del docente en el número máximo de intentos."
)
_MSG_EXPIRADA = "El tiempo para revisar el plan expiró. Inicia un nuevo proceso."
_MSG_ERROR = "El proceso terminó con un error. Intenta nuevamente."
_MSG_CANCELADA = "Sesión cancelada por el docente."


class HitlDecisionError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _rehydrate(item: dict) -> SessionData:
    """SessionData transitorio con lo que ya mostró el flujo (el estado real vive en DynamoDB)."""
    sd = SessionData(owner_id=item.get("owner_id"))
    sd.messages = list(item.get("messages") or [])
    sd.warnings = list(item.get("warnings") or [])
    return sd


def _results_from_state(state: dict) -> dict:
    """Mismo diccionario que arma run_workflow al terminar, para reutilizar _finalize_result."""
    return {
        "status": state.get("status") or "success",
        "perfil_paci": state.get("perfil_paci", ""),
        "planificacion_adaptada": state.get("planificacion_adaptada", ""),
        "rubrica_final": state.get("rubrica", ""),
        "docx_path": None,
        "validation_reason": state.get("validation_reason", ""),
        "validation_code": state.get("validation_code", ""),
        "warnings": state.get("warnings") or [],
    }


def _cargar_documentos_sync(item: dict) -> dict:
    """Descarga y lee el PACI y el material. Bloqueante (OCR con Gemini): se ejecuta en un hilo para no frenar el heartbeat."""
    paci_path = _download_from_s3(item["paci_s3_key"])
    material_path = _download_from_s3(item["material_s3_key"])
    try:
        return {
            "paci_document": load_document(paci_path, label="PACI del Estudiante"),
            "material_document": load_document(material_path, label="Material Base"),
            "prompt_docente": item.get("prompt", ""),
            "school_id": item.get("school_id", ""),
        }
    finally:
        for ruta in (paci_path, material_path):
            try:
                os.remove(ruta)
            except OSError:
                pass


async def run_phase_a(session_id: str, item: dict, attempt: int, feedback_agent: int) -> dict:
    """Fase A: Agente 1 + gate normativo + materiales + Agente 2 (intento inicial), o la repetición pedida por el docente."""
    from utils.usage_events import monitor_session_id

    sd = _rehydrate(item)
    school_id = item.get("school_id", "") or ""
    mock = school_id.startswith(_MOCK_PREFIX)
    contexto = monitor_session_id.set(session_id)
    try:
        if attempt <= 1:
            _emit("flow_started", session_id)
            dynamo_store.update_session(session_id, started_at=str(time.time()))
            _push_message(sd, "Documentos recibidos. Iniciando análisis del PACI...")
            sync_to_dynamo(session_id, sd)
            if mock:
                state = mock_phases.initial_state(school_id, item.get("prompt", ""))
            else:
                state = await asyncio.to_thread(_cargar_documentos_sync, item)
            state.update(retry_agent=0, hitl_feedback_a1="", hitl_feedback_a2="", critica_previa="", hitl_reason="")
        else:
            state = phase_store.load_state(session_id)
            razon = state.get("hitl_reason", "")
            state["retry_agent"] = feedback_agent
            state["hitl_feedback_a1"] = feedback_a1(razon) if feedback_agent == 1 else ""
            state["hitl_feedback_a2"] = feedback_a2(razon) if feedback_agent == 2 else ""
            _push_message(sd, "Revisando según los comentarios del docente...")
            sync_to_dynamo(session_id, sd)

        if mock:
            nuevo = await mock_phases.run("a", state, school_id)
        else:
            nuevo = await run_phase("a", state, user_id=item.get("owner_id") or session_id, api_session_id=session_id)
        phase_store.save_state(session_id, nuevo)

        estado = nuevo.get("status") or ""
        if estado in ("validation_failed", "timeout"):
            _finalize_result(session_id, sd, _results_from_state(nuevo), [False])
            return {"status": "compliance_blocked" if estado == "validation_failed" else "timeout"}
        return {"status": "ok"}
    finally:
        monitor_session_id.reset(contexto)


async def run_phase_b(session_id: str, item: dict) -> dict:
    """Fase B: Generador de Rúbrica + Crítico, exporta el DOCX y deja la sesión terminada."""
    from utils.usage_events import monitor_session_id

    sd = _rehydrate(item)
    school_id = item.get("school_id", "") or ""
    contexto = monitor_session_id.set(session_id)
    try:
        state = phase_store.load_state(session_id)           # PhaseStateNotFound: la fase falla de forma controlada
        _push_message(sd, "Plan aprobado. Generando la rúbrica...")
        sync_to_dynamo(session_id, sd)
        if school_id.startswith(_MOCK_PREFIX):
            nuevo = await mock_phases.run("b", state, school_id)
        else:
            nuevo = await run_phase("b", state, user_id=item.get("owner_id") or session_id, api_session_id=session_id)

        results = _results_from_state(nuevo)
        if results["rubrica_final"]:
            try:
                base = Path(item.get("material_s3_key") or "material").stem
                results["docx_path"] = str(export_results_to_docx(results, output_filename=f"rubrica_adaptada_{base}.docx"))
            except Exception as exc:                          # igual que run_workflow: sin DOCX, la sesión igual termina
                logger.error("error al exportar el DOCX [%s]: %s", session_id, type(exc).__name__)
        _finalize_result(session_id, sd, results, [False])
        return {"status": _FLOW_STATUS.get(sd.workflow_status or "", "error")}
    finally:
        monitor_session_id.reset(contexto)


async def run_phase_job(phase: str, session_id: str, item: dict, task_token: str, attempt: int, feedback_agent: int) -> None:
    """Ejecuta una fase en segundo plano y completa la tarea de Step Functions (éxito, fallo o abandono)."""
    try:
        trabajo = run_phase_a(session_id, item, attempt, feedback_agent) if phase == "a" else run_phase_b(session_id, item)
        resultado = await sfn_client.with_heartbeat(task_token, trabajo, interval=PHASE_HEARTBEAT_SECONDS)
        await asyncio.to_thread(sfn_client.send_success, task_token, resultado)
    except sfn_client.PhaseAbandoned:
        logger.warning("fase %s de %s abandonada: su token de tarea ya no es válido", phase, session_id)
    except sfn_client.TaskTokenGone:
        logger.warning("fase %s de %s terminó, pero su token de tarea ya no es válido", phase, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("fase %s de %s falló: %s", phase, session_id, type(exc).__name__, exc_info=True)
        try:
            # Solo el nombre de la clase: el texto de la excepción podría traer contenido de documentos y Step Functions
            # guarda sus entradas/salidas 90 días en claro.
            await asyncio.to_thread(sfn_client.send_failure, task_token, type(exc).__name__, "")
        except Exception:
            logger.warning("no se pudo informar el fallo de la fase %s de %s", phase, session_id)
    finally:
        dynamo_store.release_phase(session_id)


def register_wait(session_id: str, item: dict, task_token: str, attempt: int) -> None:
    """Estado `EsperarDocente`: guarda el token y pasa la sesión a awaiting_hitl en UNA sola escritura.

    El front solo puede responder cuando ve `awaiting_hitl`, y para entonces el token ya existe en DynamoDB.
    """
    state = phase_store.load_state(session_id)
    sd = _rehydrate(item)
    hitl_data = {
        "perfil_paci": state.get("perfil_paci", ""),
        "planificacion_adaptada": state.get("planificacion_adaptada", ""),
        "attempt": attempt,
        "max_attempts": MAX_HITL_ATTEMPTS,
    }
    _push_message(sd, f"Revisión requerida — intento {attempt} de {MAX_HITL_ATTEMPTS}. Por favor revise el análisis y la planificación.")
    dynamo_store.update_session(session_id, phase="awaiting_hitl", hitl_data=hitl_data, messages=sd.messages, task_token=task_token)
    _emit("hitl_required", session_id, attempt=attempt)


def emit_flow_finished(session_id: str, item: dict, status: str) -> None:
    inicio = item.get("started_at")
    _emit(
        "flow_finished", session_id,
        status=_MONITOR_STATUS.get(status, "error"),
        duration_ms=int((time.time() - inicio) * 1000) if inicio else 0,
    )


def finalize_session(session_id: str, item: dict, status: str) -> None:
    """Estado `Finalizar`: deja la sesión terminada (si la fase no lo hizo), borra el estado de S3 y cierra el flujo en el monitor."""
    if status == "hitl_rejected":
        dynamo_store.update_session(session_id, phase="error", workflow_status="hitl_rejected", error=_MSG_HITL_RECHAZADO, hitl_data=None, task_token="")
    elif status == "expired":
        dynamo_store.update_session(session_id, phase="error", workflow_status="error", error=_MSG_EXPIRADA, hitl_data=None, task_token="")
    elif status in ("error", "timeout") and item.get("phase") not in ("completed", "error"):
        dynamo_store.update_session(session_id, phase="error", workflow_status="error", error=_MSG_ERROR, hitl_data=None, task_token="")
    phase_store.delete_state(session_id)
    emit_flow_finished(session_id, item, status)


def submit_hitl_decision(session_id: str, item: dict, approved: bool, reason: str | None, agent_to_retry: int | None) -> None:
    """Decisión del docente: completa la espera de Step Functions. Lo usa POST /hitl (ya autenticado) y las pruebas E2E."""
    if item.get("phase") != "awaiting_hitl":
        raise HitlDecisionError(409, "La sesión no está esperando revisión HITL")
    if not approved:
        # La razón puede contener datos de un menor: va a S3 (cifrado), NUNCA por Step Functions.
        phase_store.update_state(session_id, hitl_reason=reason or "")
    token = dynamo_store.consume_token(session_id)
    if not token:
        raise HitlDecisionError(409, "La decisión ya fue enviada")
    try:
        sfn_client.send_success(token, {"approved": bool(approved), "agent_to_retry": int(agent_to_retry or 2)})
    except sfn_client.TaskTokenGone:
        dynamo_store.update_session(session_id, phase="error", workflow_status="error", error=_MSG_EXPIRADA, hitl_data=None)
        raise HitlDecisionError(409, "La revisión expiró") from None
    dynamo_store.update_session(session_id, phase="running", hitl_data=None)


def cancel_session_sfn(session_id: str, item: dict) -> None:
    """Cancelación del docente en una sesión orquestada por Step Functions (la ejecución queda ABORTED)."""
    dynamo_store.update_session(session_id, phase="error", workflow_status="cancelled", error=_MSG_CANCELADA, hitl_data=None, task_token="")
    try:
        sfn_client.stop_execution(session_id)
    except Exception as exc:
        logger.warning("no se pudo detener la ejecución de %s: %s", session_id, type(exc).__name__)
    phase_store.delete_state(session_id)
    emit_flow_finished(session_id, item, "cancelled")
```

- [ ] **Paso 5: ver que pasan**

Ejecuta: `wt prisma_agents/tests/api/test_workflow_phases.py prisma_agents/tests/api/test_workflow_runner.py prisma_agents/tests/api/test_workflow_runner_events.py`
Esperado: todo PASA. Si aparece un `ImportError` circular al importar `run`, importa `run_phase` dentro de las funciones que lo usan.

- [ ] **Paso 6: commit**

```bash
git add prisma_agents/api/mock_phases.py prisma_agents/api/workflow_runner.py prisma_agents/tests/api/test_workflow_phases.py
git commit -m "feat(sfn): fases A/B, espera del docente, finalizacion y decision HITL en workflow_runner"
```

---

### Tarea 6: endpoints internos de fase y `/hitl` + `/cancel` con doble camino

**Archivos:**
- Modificar: `prisma_agents/api/schemas.py` (dos modelos nuevos al final)
- Modificar: `prisma_agents/api/chat_router.py` (imports; `respond_hitl` y `cancel_session` reemplazadas; 3 endpoints nuevos después de `internal_run`)
- Crear: `prisma_agents/tests/api/test_chat_router_fases.py`

**Interfaces:**
- Consume: `workflow_runner.run_phase_job`, `register_wait`, `finalize_session`, `submit_hitl_decision`, `cancel_session_sfn`, `HitlDecisionError` (Tarea 5); `dynamo_store.acquire_phase` (Tarea 1).
- Produce (HTTP, todos con `X-Internal-Token`):
  - `POST /chat/internal/phase/a/{id}` y `/b/{id}`, body `PhaseBody`, **202** `{"started": bool}`
  - `POST /chat/internal/phase/wait/{id}`, body `PhaseBody`, **200** `{"ok": true}`
  - `POST /chat/internal/phase/finalize/{id}`, body `FinalizeBody`, **200** `{"ok": true}`
- `PhaseBody`: `task_token: str`, `attempt: int = 1`, `feedback_agent: int = 0`. `FinalizeBody`: `status: str`.
- Compatibilidad: una sesión que **está** en `SESSIONS` (camino antiguo) sigue usando la cola en memoria; una que **no** está pero existe en DynamoDB (camino Step Functions) usa los callbacks.

- [ ] **Paso 1: escribir los tests que fallan**

`prisma_agents/tests/api/test_chat_router_fases.py`:

```python
"""Endpoints internos de fase y el doble camino de /hitl y /cancel (memoria vs Step Functions)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from api import chat_router, dynamo_store
from api.auth import get_current_user
from api.main import app
from api.session_store import SESSIONS

client = TestClient(app)
INTERNO = {"X-Internal-Token": "tok-interno"}
BODY = {"task_token": "tok", "attempt": 2, "feedback_agent": 1}
ITEM = {"session_id": "s1", "phase": "awaiting_hitl", "owner_id": "u1", "school_id": "colegio", "messages": []}


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setattr(chat_router, "INTERNAL_TOKEN", "tok-interno")
    previo = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: {"sub": "u1"}
    SESSIONS.clear()
    yield
    if previo is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = previo


@pytest.fixture
def db(monkeypatch):
    d = MagicMock()
    d.get_session.return_value = dict(ITEM)
    d.acquire_phase.return_value = True
    monkeypatch.setattr(chat_router, "dynamo_store", d)
    return d


@pytest.fixture
def wr(monkeypatch):
    w = MagicMock()
    w.run_phase_job = AsyncMock()
    w.HitlDecisionError = chat_router.workflow_runner.HitlDecisionError
    monkeypatch.setattr(chat_router, "workflow_runner", w)
    return w


# Nota: `run_phase_job` es un AsyncMock; la llamada queda registrada en el momento de crear la corrutina (dentro del endpoint),
# así que no hace falta esperar a que la tarea en segundo plano corra. No se parchea asyncio: el cliente de pruebas lo usa por dentro.

# ── autenticación interna ───────────────────────────────────────────────────
@pytest.mark.parametrize("ruta", ["a", "b", "wait", "finalize"])
def test_sin_token_interno_es_401(db, wr, ruta):
    body = {"status": "success"} if ruta == "finalize" else BODY
    assert client.post(f"/chat/internal/phase/{ruta}/s1", json=body).status_code == 401
    assert client.post(f"/chat/internal/phase/{ruta}/s1", json=body, headers={"X-Internal-Token": "otro"}).status_code == 401


def test_sesion_inexistente_es_404(db, wr):
    db.get_session.return_value = None
    assert client.post("/chat/internal/phase/a/s1", json=BODY, headers=INTERNO).status_code == 404


def test_fase_desconocida_es_422(db, wr):
    assert client.post("/chat/internal/phase/z/s1", json=BODY, headers=INTERNO).status_code == 422


# ── fases a / b ─────────────────────────────────────────────────────────────
def test_fase_a_toma_el_candado_y_lanza_el_trabajo(db, wr):
    res = client.post("/chat/internal/phase/a/s1", json=BODY, headers=INTERNO)
    assert res.status_code == 202 and res.json() == {"started": True}
    assert db.acquire_phase.call_args.args[:2] == ("s1", "a:2")
    wr.run_phase_job.assert_called_once()
    assert wr.run_phase_job.call_args.args == ("a", "s1", db.get_session.return_value, "tok", 2, 1)


def test_fase_b_usa_su_propia_clave_de_candado(db, wr):
    client.post("/chat/internal/phase/b/s1", json={"task_token": "tok"}, headers=INTERNO)
    assert db.acquire_phase.call_args.args[:2] == ("s1", "b:1")


def test_si_otra_ejecucion_tiene_el_candado_no_se_lanza_nada(db, wr):
    db.acquire_phase.return_value = False
    res = client.post("/chat/internal/phase/a/s1", json=BODY, headers=INTERNO)
    assert res.status_code == 202 and res.json() == {"started": False}
    wr.run_phase_job.assert_not_called()


def test_el_colegio_mock_dead_simula_un_worker_que_no_responde(db, wr):
    db.get_session.return_value = {**ITEM, "school_id": "__mock_dead__"}
    res = client.post("/chat/internal/phase/a/s1", json=BODY, headers=INTERNO)
    assert res.json() == {"started": True}
    db.acquire_phase.assert_not_called()
    wr.run_phase_job.assert_not_called()


# ── wait / finalize (no deben ser capturados por /phase/{phase}) ────────────
def test_wait_registra_el_token(db, wr):
    res = client.post("/chat/internal/phase/wait/s1", json=BODY, headers=INTERNO)
    assert res.status_code == 200 and res.json() == {"ok": True}
    wr.register_wait.assert_called_once_with("s1", db.get_session.return_value, "tok", 2)


def test_finalize_cierra_la_sesion(db, wr):
    res = client.post("/chat/internal/phase/finalize/s1", json={"status": "expired"}, headers=INTERNO)
    assert res.status_code == 200
    wr.finalize_session.assert_called_once_with("s1", db.get_session.return_value, "expired")


# ── /hitl ───────────────────────────────────────────────────────────────────
def test_hitl_de_una_sesion_de_step_functions(db, wr):
    db.enabled.return_value = True
    res = client.post("/chat/s1/hitl", json={"approved": False, "reason": "falta X", "agent_to_retry": 1})
    assert res.status_code == 200 and res.json() == {"ok": True}
    wr.submit_hitl_decision.assert_called_once_with("s1", db.get_session.return_value, False, "falta X", 1)


def test_hitl_de_otro_docente_es_403(db, wr):
    db.enabled.return_value = True
    db.get_session.return_value = {**ITEM, "owner_id": "otro"}
    assert client.post("/chat/s1/hitl", json={"approved": True}).status_code == 403
    wr.submit_hitl_decision.assert_not_called()


def test_hitl_doble_clic_se_traduce_a_409(db, wr):
    db.enabled.return_value = True
    wr.submit_hitl_decision.side_effect = chat_router.workflow_runner.HitlDecisionError(409, "La decisión ya fue enviada")
    res = client.post("/chat/s1/hitl", json={"approved": True})
    assert res.status_code == 409 and "ya fue enviada" in res.json()["detail"]


def test_hitl_de_una_sesion_en_memoria_sigue_usando_la_cola(db, wr):
    """Compatibilidad: el camino antiguo no cambia."""
    from api.session_store import SessionData
    db.enabled.return_value = True
    SESSIONS["s1"] = SessionData(owner_id="u1")
    SESSIONS["s1"].phase = "awaiting_hitl"
    res = client.post("/chat/s1/hitl", json={"approved": True})
    assert res.status_code == 200 and SESSIONS["s1"].hitl_response_queue.qsize() == 1
    wr.submit_hitl_decision.assert_not_called()


def test_hitl_de_sesion_desconocida_es_404(db, wr):
    db.enabled.return_value = True
    db.get_session.return_value = None
    assert client.post("/chat/s1/hitl", json={"approved": True}).status_code == 404


# ── /cancel ─────────────────────────────────────────────────────────────────
def test_cancel_de_una_sesion_de_step_functions(db, wr):
    db.enabled.return_value = True
    res = client.post("/chat/s1/cancel")
    assert res.status_code == 200
    wr.cancel_session_sfn.assert_called_once_with("s1", db.get_session.return_value)


def test_cancel_de_una_sesion_ya_terminada_es_409(db, wr):
    db.enabled.return_value = True
    db.get_session.return_value = {**ITEM, "phase": "completed"}
    assert client.post("/chat/s1/cancel").status_code == 409
    wr.cancel_session_sfn.assert_not_called()


def test_cancel_de_otro_docente_es_403(db, wr):
    db.enabled.return_value = True
    db.get_session.return_value = {**ITEM, "owner_id": "otro"}
    assert client.post("/chat/s1/cancel").status_code == 403
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/api/test_chat_router_fases.py` → FALLA (`AttributeError: module 'api.chat_router' has no attribute 'workflow_runner'`).

- [ ] **Paso 3: modelos nuevos en `schemas.py`**

Al final de `prisma_agents/api/schemas.py` agrega:

```python
class PhaseBody(BaseModel):
    """Cuerpo de las fases internas de Step Functions. El token es secreto: nunca se registra en logs."""
    task_token: str = Field(..., description="Token de la tarea de Step Functions (waitForTaskToken)")
    attempt: int = Field(1, ge=1, description="Número del intento HITL (1 = análisis inicial)")
    feedback_agent: int = Field(0, ge=0, le=2, description="0 = ninguno; 1 = rehacer Agente 1; 2 = rehacer solo Agente 2")


class FinalizeBody(BaseModel):
    status: str = Field(..., description="Estado final: success, degraded, compliance_blocked, hitl_rejected, expired, timeout o error")
```

- [ ] **Paso 4: cambios en `chat_router.py`**

4a. Imports. Reemplaza las líneas:

```python
from api import dynamo_store
```
por:
```python
from typing import Literal

from api import dynamo_store, workflow_runner
```
(`Optional` ya viene importado de `typing`; si el import `from typing import Optional` está en otra línea, deja ambas.) En el bloque `from api.schemas import (...)` agrega `FinalizeBody` y `PhaseBody`.

4b. Reemplaza la función `respond_hitl` completa (conserva su decorador `@router.post("/{session_id}/hitl", ...)`) por:

```python
async def respond_hitl(session_id: str, body: HitlResponseBody, _user: dict = Depends(get_current_user)):
    sd = SESSIONS.get(session_id)
    if sd is None and dynamo_store.enabled():
        item = dynamo_store.get_session(session_id)
        if item is not None:
            # Sesión orquestada por Step Functions: no vive en la RAM de este proceso (otra tarea de ECS pudo correr la fase).
            _assert_owner(item.get("owner_id"), _user["sub"])
            try:
                await asyncio.to_thread(
                    workflow_runner.submit_hitl_decision, session_id, item, body.approved, body.reason, body.agent_to_retry
                )
            except workflow_runner.HitlDecisionError as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail)
            return {"ok": True}
    if sd is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    _assert_owner(sd.owner_id, _user["sub"])
    if sd.phase != "awaiting_hitl":
        raise HTTPException(status_code=409, detail="La sesión no está esperando revisión HITL")
    await sd.hitl_response_queue.put({
        "approved": body.approved,
        "reason": body.reason,
        "agent_to_retry": body.agent_to_retry,
    })
    return {"ok": True}
```

4c. Reemplaza la función `cancel_session` completa (conserva su decorador) por:

```python
async def cancel_session(session_id: str, _user: dict = Depends(get_current_user)):
    sd = SESSIONS.get(session_id)
    if sd is None and dynamo_store.enabled():
        item = dynamo_store.get_session(session_id)
        if item is not None:
            _assert_owner(item.get("owner_id"), _user["sub"])
            if item.get("phase") in ("completed", "error"):
                raise HTTPException(status_code=409, detail="La sesión ya ha terminado")
            await asyncio.to_thread(workflow_runner.cancel_session_sfn, session_id, item)
            return {"ok": True}
    if sd is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    _assert_owner(sd.owner_id, _user["sub"])
    if sd.phase in ("completed", "error"):
        raise HTTPException(status_code=409, detail="La sesión ya ha terminado")

    error_msg = "Sesión cancelada por el docente."
    sd.cancelled = True
    sd.phase = "error"
    sd.workflow_status = "cancelled"
    sd.error = error_msg

    sd.event_queue.put_nowait({"type": "error", "message": error_msg, "workflow_status": "cancelled"})
    sync_to_dynamo(session_id, sd)

    # Cancelar la tarea asyncio — interrumpe el agente en el próximo await
    if sd.task and not sd.task.done():
        sd.task.cancel()

    return {"ok": True}
```

4d. Al final del archivo (después de `internal_run`) agrega. **El orden importa:** `wait` y `finalize` deben declararse **antes** que la ruta genérica `/{phase}/{session_id}`, o ésta las captura y responde 422.

```python


# ── Fases (Step Functions): las llama la Lambda `invoker` ────────────────────────────────────────────────
PHASE_LOCK_SECONDS = int(os.environ.get("PHASE_LOCK_SECONDS", "1800"))
_BACKGROUND: set = set()          # referencias a las tareas en segundo plano (si no, el recolector puede cancelarlas)


def _require_internal(token: Optional[str]) -> None:
    if not INTERNAL_TOKEN or token != INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="Token interno inválido")


def _item_or_404(session_id: str) -> dict:
    item = dynamo_store.get_session(session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada en DynamoDB")
    return item


@router.post("/internal/phase/wait/{session_id}", tags=["Internal"], response_model=OkResponse,
             summary="Estado EsperarDocente: guarda el token de tarea y pasa la sesión a awaiting_hitl")
async def internal_phase_wait(session_id: str, body: PhaseBody, x_internal_token: Optional[str] = Header(None)):
    _require_internal(x_internal_token)
    item = _item_or_404(session_id)
    await asyncio.to_thread(workflow_runner.register_wait, session_id, item, body.task_token, body.attempt)
    return {"ok": True}


@router.post("/internal/phase/finalize/{session_id}", tags=["Internal"], response_model=OkResponse,
             summary="Estado Finalizar: deja la sesión terminada y limpia el estado")
async def internal_phase_finalize(session_id: str, body: FinalizeBody, x_internal_token: Optional[str] = Header(None)):
    _require_internal(x_internal_token)
    item = _item_or_404(session_id)
    await asyncio.to_thread(workflow_runner.finalize_session, session_id, item, body.status)
    return {"ok": True}


@router.post("/internal/phase/{phase}/{session_id}", tags=["Internal"], response_model=InternalRunResponse, status_code=202,
             summary="Ejecutar una fase del flujo (a: análisis y adaptación · b: rúbrica y crítico)")
async def internal_phase(phase: Literal["a", "b"], session_id: str, body: PhaseBody,
                         x_internal_token: Optional[str] = Header(None)):
    _require_internal(x_internal_token)
    item = _item_or_404(session_id)
    if phase == "a" and item.get("school_id") == "__mock_dead__":
        return {"started": True}          # simula un worker que no responde: prueba de heartbeat en LocalStack
    if not dynamo_store.acquire_phase(session_id, f"{phase}:{body.attempt}", PHASE_LOCK_SECONDS):
        return {"started": False}         # otra ejecución de esta fase está en curso (mensaje repetido o heartbeat perdido)
    tarea = asyncio.create_task(
        workflow_runner.run_phase_job(phase, session_id, item, body.task_token, body.attempt, body.feedback_agent)
    )
    _BACKGROUND.add(tarea)
    tarea.add_done_callback(_BACKGROUND.discard)
    return {"started": True}
```

- [ ] **Paso 5: ver que pasan, y que nada del router existente se rompió**

Ejecuta: `wt prisma_agents/tests/api/test_chat_router_fases.py prisma_agents/tests/api/test_chat_router.py prisma_agents/tests/api/test_main.py`
Esperado: todo PASA.

- [ ] **Paso 6: commit**

```bash
git add prisma_agents/api/schemas.py prisma_agents/api/chat_router.py prisma_agents/tests/api/test_chat_router_fases.py
git commit -m "feat(sfn): endpoints internos de fase y /hitl y /cancel con doble camino (memoria / Step Functions)"
```

---

### Tarea 7: Lambdas `starter` e `invoker`

**Archivos:**
- Crear: `lambda/sfn_starter.py`, `lambda/sfn_invoker.py`
- Crear: `prisma_agents/tests/lambdas/__init__.py` (vacío), `prisma_agents/tests/lambdas/test_sfn_starter.py`, `prisma_agents/tests/lambdas/test_sfn_invoker.py`

**Interfaces:**
- `sfn_starter.handler(event, context) -> {"batchItemFailures": [{"itemIdentifier": messageId}, ...]}`. Entrada: evento SQS cuyo `body` es una notificación de S3.
  Variables: `STATE_MACHINE_ARN` (requerida), `FASE_TIMEOUT_SECONDS` (1800), `HEARTBEAT_SECONDS` (120), `HITL_TIMEOUT_SECONDS` (86400).
  Input de la ejecución: `{"session_id": str, "timeouts": {"fase": int, "heartbeat": int, "hitl": int}}`.
- `sfn_invoker.handler(event, context) -> {"http_status": int}`. Entrada: `{"phase": "a"|"b"|"wait"|"finalize", "session_id": uuid, "task_token"?, "attempt"?, "feedback_agent"?, "status"?}`.
  Variables: `BACKEND_INTERNAL_URL`, `INTERNAL_TOKEN`, `API_TIMEOUT` (30), `RETRY_ATTEMPTS` (3).

- [ ] **Paso 1: escribir los tests del `starter`**

`prisma_agents/tests/lambdas/test_sfn_starter.py`:

```python
"""Lambda `starter`: SQS (evento de S3) -> StartExecution. Idempotente, tolera repeticiones y no deja mensajes envenenados sin dueño."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda"))

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

import sfn_starter as st

SID = "123e4567-e89b-42d3-a456-426614174000"


def mensaje(key, mid="m1"):
    return {"messageId": mid, "body": json.dumps({"Records": [{"s3": {"object": {"key": key}}}]})}


@pytest.fixture
def sfn(monkeypatch):
    c = MagicMock()
    monkeypatch.setenv("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:1:stateMachine:prisma-flujo")
    for v in ("FASE_TIMEOUT_SECONDS", "HEARTBEAT_SECONDS", "HITL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(st, "_client", lambda: c)
    return c


def test_el_paci_arranca_la_ejecucion_nombrada_con_el_session_id(sfn):
    res = st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf")]}, None)
    assert res == {"batchItemFailures": []}
    kw = sfn.start_execution.call_args.kwargs
    assert kw["name"] == SID and kw["stateMachineArn"].endswith("stateMachine:prisma-flujo")
    assert json.loads(kw["input"]) == {"session_id": SID, "timeouts": {"fase": 1800, "heartbeat": 120, "hitl": 86400}}


def test_acepta_paci_docx_y_claves_url_encoded(sfn):
    st.handler({"Records": [mensaje(f"jobs%2F{SID}%2Fpaci.docx")]}, None)
    assert sfn.start_execution.call_args.kwargs["name"] == SID


@pytest.mark.parametrize("key", [f"jobs/{SID}/material.docx", f"results/{SID}/rubrica.docx", f"state/{SID}.json",
                                 "jobs/no-es-uuid/paci.pdf", f"jobs/{SID}/otra/paci.pdf", f"jobs/{SID}/paci"])
def test_ignora_todo_lo_que_no_sea_el_paci(sfn, key):
    res = st.handler({"Records": [mensaje(key)]}, None)
    assert res == {"batchItemFailures": []}
    sfn.start_execution.assert_not_called()


def test_ignora_el_evento_de_prueba_de_s3(sfn):
    res = st.handler({"Records": [{"messageId": "m1", "body": json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"})}]}, None)
    assert res == {"batchItemFailures": []}
    sfn.start_execution.assert_not_called()


def test_mensaje_repetido_no_es_un_fallo(sfn):
    """Foco de revisión 1: SQS entrega 'al menos una vez'; la ejecución ya existe (abierta o cerrada)."""
    sfn.start_execution.side_effect = ClientError({"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}}, "StartExecution")
    res = st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf")]}, None)
    assert res == {"batchItemFailures": []}


def test_otro_error_de_aws_devuelve_el_mensaje_para_reintento(sfn):
    sfn.start_execution.side_effect = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "StartExecution")
    res = st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf", "m9")]}, None)
    assert res == {"batchItemFailures": [{"itemIdentifier": "m9"}]}


def test_cuerpo_que_no_es_json_se_devuelve_para_que_termine_en_la_dlq(sfn):
    res = st.handler({"Records": [{"messageId": "m3", "body": "no soy json"}]}, None)
    assert res == {"batchItemFailures": [{"itemIdentifier": "m3"}]}


def test_un_lote_con_un_fallo_solo_devuelve_ese_mensaje(sfn):
    otra = "223e4567-e89b-42d3-a456-426614174999"
    sfn.start_execution.side_effect = [None, ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"}}, "StartExecution")]
    res = st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf", "a"), mensaje(f"jobs/{otra}/paci.pdf", "b")]}, None)
    assert res == {"batchItemFailures": [{"itemIdentifier": "b"}]}


def test_los_timeouts_se_leen_del_entorno(sfn, monkeypatch):
    monkeypatch.setenv("FASE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("HEARTBEAT_SECONDS", "10")
    monkeypatch.setenv("HITL_TIMEOUT_SECONDS", "300")
    st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf")]}, None)
    assert json.loads(sfn.start_execution.call_args.kwargs["input"])["timeouts"] == {"fase": 60, "heartbeat": 10, "hitl": 300}


def test_no_registra_el_contenido_del_mensaje(sfn, capsys):
    st.handler({"Records": [mensaje(f"jobs/{SID}/paci.pdf")]}, None)
    assert "paci.pdf" not in capsys.readouterr().out
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/lambdas/test_sfn_starter.py` → FALLA (`ModuleNotFoundError: sfn_starter`).

- [ ] **Paso 3: implementar el `starter`**

`lambda/sfn_starter.py`:

```python
"""
PRISMA — Lambda `starter`: SQS (evento de S3) -> Step Functions StartExecution.

Cadena:  S3 PUT jobs/{id}/paci.* -> SQS -> esta Lambda -> máquina de estados `prisma-flujo`.

* Va FUERA de la VPC: necesita llegar a la API pública de Step Functions (las subredes por defecto no dan internet a una Lambda en VPC).
* Idempotente: la ejecución se nombra con el session_id. SQS entrega "al menos una vez": si ya existe (abierta o cerrada),
  `ExecutionAlreadyExists` NO es un fallo.
* Solo reacciona al PACI: el material también dispara un evento de S3 y se ignora.
* Sin PII: por Step Functions viajan solo el session_id y los tiempos.
* ReportBatchItemFailures: solo vuelven a la cola los mensajes que fallaron.
* No registra el contenido de los mensajes.

Variables: STATE_MACHINE_ARN (requerida), FASE_TIMEOUT_SECONDS (1800), HEARTBEAT_SECONDS (120), HITL_TIMEOUT_SECONDS (86400)
"""
import json
import os
import re
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

_PACI = re.compile(
    r"^jobs/(?P<sid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/paci\.[A-Za-z0-9]+$"
)
_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def _timeouts() -> dict:
    return {
        "fase": int(os.environ.get("FASE_TIMEOUT_SECONDS", "1800")),
        "heartbeat": int(os.environ.get("HEARTBEAT_SECONDS", "120")),
        "hitl": int(os.environ.get("HITL_TIMEOUT_SECONDS", "86400")),
    }


def _session_ids(cuerpo: dict):
    for rec in cuerpo.get("Records", []):
        clave = unquote_plus(rec.get("s3", {}).get("object", {}).get("key", ""))
        m = _PACI.match(clave)
        if m:
            yield m.group("sid")


def _iniciar(session_id: str) -> None:
    try:
        _client().start_execution(
            stateMachineArn=os.environ["STATE_MACHINE_ARN"],
            name=session_id,
            input=json.dumps({"session_id": session_id, "timeouts": _timeouts()}),
        )
        print(f"ejecucion iniciada: {session_id}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ExecutionAlreadyExists":
            print(f"ejecucion ya existia (mensaje repetido): {session_id}")
            return
        raise


def handler(event: dict, context) -> dict:
    fallidos = []
    for registro in event.get("Records", []):
        try:
            for session_id in _session_ids(json.loads(registro["body"])):
                _iniciar(session_id)
        except Exception as exc:                                # noqa: BLE001 — cualquier fallo devuelve el mensaje a la cola
            print(f"fallo {type(exc).__name__} en el mensaje {registro.get('messageId')}")
            fallidos.append({"itemIdentifier": registro["messageId"]})
    return {"batchItemFailures": fallidos}
```

Ejecuta: `wt prisma_agents/tests/lambdas/test_sfn_starter.py` → PASA.

- [ ] **Paso 4: escribir los tests del `invoker`**

`prisma_agents/tests/lambdas/test_sfn_invoker.py`:

```python
"""Lambda `invoker`: Step Functions -> POST al ALB. Solo HTTP, lista blanca de fases, sin registrar tokens ni cuerpos."""
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda"))

import pytest
from unittest.mock import MagicMock, patch

import sfn_invoker as inv

SID = "123e4567-e89b-42d3-a456-426614174000"


class Resp:
    def __init__(self, status=202):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"{}"


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("BACKEND_INTERNAL_URL", "http://backend:8000/")
    monkeypatch.setenv("INTERNAL_TOKEN", "tok-interno")
    monkeypatch.setenv("RETRY_ATTEMPTS", "3")
    with patch.object(inv.time, "sleep"):
        yield


def test_la_fase_a_hace_post_con_el_token_interno_y_solo_las_claves_permitidas():
    abierto = MagicMock(return_value=Resp(202))
    with patch.object(inv.urllib.request, "urlopen", abierto):
        res = inv.handler({"phase": "a", "session_id": SID, "task_token": "TOK", "attempt": 2, "feedback_agent": 1,
                           "texto_del_docente": "NO DEBE PASAR"}, None)
    assert res == {"http_status": 202}
    req = abierto.call_args.args[0]
    assert req.full_url == f"http://backend:8000/chat/internal/phase/a/{SID}" and req.get_method() == "POST"
    assert req.get_header("X-internal-token") == "tok-interno"
    cuerpo = json.loads(req.data)
    assert cuerpo == {"task_token": "TOK", "attempt": 2, "feedback_agent": 1}      # la clave extra se descarta


@pytest.mark.parametrize("fase", ["a", "b", "wait", "finalize"])
def test_las_cuatro_fases_permitidas_van_a_su_ruta(fase):
    abierto = MagicMock(return_value=Resp(200))
    with patch.object(inv.urllib.request, "urlopen", abierto):
        inv.handler({"phase": fase, "session_id": SID}, None)
    assert abierto.call_args.args[0].full_url.endswith(f"/chat/internal/phase/{fase}/{SID}")


@pytest.mark.parametrize("fase", ["run", "../admin", "A", "", None, "a/../b"])
def test_rechaza_fases_fuera_de_la_lista_blanca(fase):
    with patch.object(inv.urllib.request, "urlopen") as abierto:
        with pytest.raises(ValueError):
            inv.handler({"phase": fase, "session_id": SID}, None)
    abierto.assert_not_called()


@pytest.mark.parametrize("sid", ["../etc/passwd", "abc", "", SID + "/x", "123e4567-e89b-42d3-a456-42661417400g"])
def test_rechaza_session_id_que_no_sea_uuid(sid):
    with patch.object(inv.urllib.request, "urlopen") as abierto:
        with pytest.raises(ValueError):
            inv.handler({"phase": "a", "session_id": sid}, None)
    abierto.assert_not_called()


def test_un_error_4xx_no_se_reintenta():
    err = urllib.error.HTTPError("u", 404, "no", {}, None)
    with patch.object(inv.urllib.request, "urlopen", MagicMock(side_effect=err)) as abierto:
        with pytest.raises(urllib.error.HTTPError):
            inv.handler({"phase": "a", "session_id": SID}, None)
    assert abierto.call_count == 1


def test_un_error_5xx_se_reintenta_y_luego_falla():
    err = urllib.error.HTTPError("u", 503, "no", {}, None)
    with patch.object(inv.urllib.request, "urlopen", MagicMock(side_effect=err)) as abierto:
        with pytest.raises(urllib.error.HTTPError):
            inv.handler({"phase": "a", "session_id": SID}, None)
    assert abierto.call_count == 3


def test_un_fallo_transitorio_de_red_se_recupera():
    abierto = MagicMock(side_effect=[urllib.error.URLError("sin ruta"), Resp(202)])
    with patch.object(inv.urllib.request, "urlopen", abierto):
        assert inv.handler({"phase": "b", "session_id": SID}, None) == {"http_status": 202}
    assert abierto.call_count == 2


def test_no_registra_tokens_ni_cuerpos(capsys):
    with patch.object(inv.urllib.request, "urlopen", MagicMock(return_value=Resp(202))):
        inv.handler({"phase": "a", "session_id": SID, "task_token": "TOKEN-SECRETO"}, None)
    salida = capsys.readouterr()
    assert "TOKEN-SECRETO" not in salida.out + salida.err and "tok-interno" not in salida.out + salida.err
```

- [ ] **Paso 5: ver que fallan**

Ejecuta: `wt prisma_agents/tests/lambdas/test_sfn_invoker.py` → FALLA (`ModuleNotFoundError: sfn_invoker`).

- [ ] **Paso 6: implementar el `invoker`**

`lambda/sfn_invoker.py`:

```python
"""
PRISMA — Lambda `invoker`: Step Functions -> POST al workflow (ALB interno).

Va DENTRO de la VPC (llega al ALB) y por eso es SOLO HTTP con la biblioteca estándar: sin boto3 y sin llamadas a APIs de AWS
(las subredes por defecto no dan internet a una Lambda en VPC). Todo lo que requiere AWS lo hace el workflow, que sí tiene salida.

Seguridad:
  * Lista blanca de fases: a, b, wait, finalize. No acepta rutas arbitrarias.
  * `session_id` debe ser un UUID (evita path traversal).
  * Solo reenvía las claves permitidas del evento (nunca texto libre).
  * No registra tokens de tarea, cuerpos ni el token interno.

Para `waitForTaskToken` el valor de retorno se ignora: la tarea termina cuando el workflow llama a SendTaskSuccess.
Variables: BACKEND_INTERNAL_URL, INTERNAL_TOKEN, API_TIMEOUT (30), RETRY_ATTEMPTS (3)
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

_FASES = {"a", "b", "wait", "finalize"}
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_CLAVES_CUERPO = ("task_token", "attempt", "feedback_agent", "status")


def _post(url: str, cuerpo: dict) -> int:
    intentos = int(os.environ.get("RETRY_ATTEMPTS", "3"))
    espera_http = int(os.environ.get("API_TIMEOUT", "30"))
    datos = json.dumps(cuerpo).encode("utf-8")
    ultimo = None
    for intento in range(1, intentos + 1):
        req = urllib.request.Request(
            url, data=datos, method="POST",
            headers={"Content-Type": "application/json", "X-Internal-Token": os.environ["INTERNAL_TOKEN"]},
        )
        try:
            with urllib.request.urlopen(req, timeout=espera_http) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:          # 4xx: error nuestro, reintentar no lo arregla
                raise
            ultimo = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            ultimo = exc
        print(f"intento {intento}/{intentos} fallo: {type(ultimo).__name__}")
        if intento < intentos:
            time.sleep(2 ** intento)
    raise ultimo


def handler(event: dict, context) -> dict:
    fase = event.get("phase")
    session_id = event.get("session_id") or ""
    if fase not in _FASES:
        raise ValueError(f"fase no permitida: {fase!r}")
    if not _UUID.match(session_id):
        raise ValueError("session_id invalido")
    base = os.environ["BACKEND_INTERNAL_URL"].rstrip("/")
    cuerpo = {k: event[k] for k in _CLAVES_CUERPO if k in event}
    estado = _post(f"{base}/chat/internal/phase/{fase}/{session_id}", cuerpo)
    return {"http_status": estado}
```

- [ ] **Paso 7: ver que pasan**

Ejecuta: `wt prisma_agents/tests/lambdas`
Esperado: todo PASA.

- [ ] **Paso 8: commit**

```bash
git add lambda/sfn_starter.py lambda/sfn_invoker.py prisma_agents/tests/lambdas
git commit -m "feat(sfn): Lambdas starter (SQS->StartExecution) e invoker (Step Functions->ALB)"
```

---

### Tarea 8: definición de la máquina de estados (ASL)

**Archivos:**
- Crear: `statemachine/prisma_flow.asl.json`
- Crear: `prisma_agents/tests/test_state_machine.py`

**Interfaces:**
- Produce: la definición en **JSONPath** (válida en AWS y en LocalStack). El único marcador de plantilla es `${InvokerArn}` (ARN de la Lambda `invoker`); el archivo es JSON válido incluso sin sustituirlo.
- Entrada de la ejecución (la arma la Lambda `starter`): `{"session_id": str, "timeouts": {"fase": int, "heartbeat": int, "hitl": int}}`.
- Contrato con el workflow: las fases devuelven `{"status": ...}`; `EsperarDocente` recibe `{"approved": bool, "agent_to_retry": int}` por `/hitl`.

- [ ] **Paso 1: escribir los tests que fallan**

`prisma_agents/tests/test_state_machine.py`:

```python
"""Validación estructural de la máquina de estados: destinos, alcanzabilidad, esperas con token, reintentos y que no viaje PII."""
import json
import os
import re

ASL = os.path.join(os.path.dirname(__file__), "..", "..", "statemachine", "prisma_flow.asl.json")
CLAVES_PERMITIDAS = {"phase", "session_id", "attempt", "feedback_agent", "task_token", "status"}


def cargar() -> dict:
    with open(ASL, encoding="utf-8") as f:
        return json.load(f)


def destinos(estado: dict) -> list:
    d = [estado[k] for k in ("Next", "Default") if k in estado]
    d += [c["Next"] for c in estado.get("Choices", [])]
    d += [c["Next"] for c in estado.get("Catch", [])]
    return d


def alcanzables(definicion: dict) -> set:
    estados, vistos, pila = definicion["States"], set(), [definicion["StartAt"]]
    while pila:
        n = pila.pop()
        if n in vistos:
            continue
        vistos.add(n)
        pila.extend(destinos(estados[n]))
    return vistos


def tareas(definicion: dict) -> dict:
    return {n: e for n, e in definicion["States"].items() if e["Type"] == "Task"}


def test_el_inicio_existe():
    d = cargar()
    assert d["StartAt"] in d["States"]


def test_todos_los_destinos_existen():
    d = cargar()
    for nombre, estado in d["States"].items():
        for destino in destinos(estado):
            assert destino in d["States"], f"{nombre} apunta a {destino}, que no existe"


def test_no_hay_estados_huerfanos():
    d = cargar()
    assert alcanzables(d) == set(d["States"]), f"huérfanos: {set(d['States']) - alcanzables(d)}"


def test_todo_estado_no_terminal_tiene_salida():
    for nombre, e in cargar()["States"].items():
        if e["Type"] in ("Succeed", "Fail"):
            continue
        assert destinos(e) or e.get("End"), f"{nombre} no tiene salida"
        if e["Type"] == "Choice":
            assert "Default" in e, f"{nombre} (Choice) no tiene Default"


def test_las_esperas_usan_waitForTaskToken_con_token_y_tiempos():
    d = cargar()
    for nombre in ("FaseA", "FaseB", "EsperarDocente"):
        e = d["States"][nombre]
        assert e["Resource"].endswith(":lambda:invoke.waitForTaskToken"), nombre
        assert e["Parameters"]["Payload"]["task_token.$"] == "$$.Task.Token", nombre
        assert "TimeoutSecondsPath" in e, nombre
    for nombre in ("FaseA", "FaseB"):
        assert "HeartbeatSecondsPath" in d["States"][nombre], nombre


def test_el_reintento_es_unico_y_solo_ante_heartbeat():
    """Cada reintento de una fase repite llamadas a Gemini: máximo 1 y únicamente si el worker dejó de dar señales."""
    d = cargar()
    for nombre in ("FaseA", "FaseB"):
        retry = d["States"][nombre]["Retry"]
        assert len(retry) == 1 and retry[0]["ErrorEquals"] == ["States.HeartbeatTimeout"] and retry[0]["MaxAttempts"] == 1


def test_el_docente_que_no_responde_termina_como_expirada():
    e = cargar()["States"]["EsperarDocente"]
    timeout = [c for c in e["Catch"] if c["ErrorEquals"] == ["States.Timeout"]]
    assert timeout and timeout[0]["Next"] == "PrepExpirada"
    assert cargar()["States"]["PrepExpirada"]["Result"] == {"status": "expired"}


def test_el_payload_solo_lleva_claves_permitidas():
    """Foco de revisión 5: por Step Functions no viaja texto de documentos ni comentarios del docente."""
    for nombre, e in tareas(cargar()).items():
        claves = {k.removesuffix(".$") for k in e["Parameters"]["Payload"]}
        assert claves <= CLAVES_PERMITIDAS, f"{nombre} envía {claves - CLAVES_PERMITIDAS}"


def test_el_unico_marcador_de_plantilla_es_el_arn_del_invoker():
    texto = open(ASL, encoding="utf-8").read()
    assert set(re.findall(r"\$\{(\w+)\}", texto)) == {"InvokerArn"}


def test_el_limite_de_intentos_del_docente_es_3():
    elecciones = cargar()["States"]["DecidirHitl"]["Choices"]
    rechazo_final = [c for c in elecciones if "And" in c]
    assert rechazo_final and rechazo_final[0]["Next"] == "PrepRechazada"
    assert {"Variable": "$.ctx.attempt", "NumericGreaterThanEquals": 3} in rechazo_final[0]["And"]


def test_el_siguiente_intento_suma_uno_y_lleva_el_agente_elegido():
    p = cargar()["States"]["ProximoIntento"]["Parameters"]
    assert p["attempt.$"] == "States.MathAdd($.ctx.attempt, 1)" and p["feedback_agent.$"] == "$.decision.agent_to_retry"


def test_los_resultados_de_negocio_terminan_en_succeed_y_los_errores_en_fail():
    e = cargar()["States"]
    assert e["Resultado"]["Default"] == "Fin" and e["Fin"]["Type"] == "Succeed"
    fallan = {c["Variable"] + "=" + c["StringEquals"] for c in e["Resultado"]["Choices"][0]["Or"]}
    assert fallan == {"$.final.status=error", "$.final.status=timeout"}
    assert e[e["Resultado"]["Choices"][0]["Next"]]["Type"] == "Fail"


def test_todo_camino_pasa_por_finalizar_antes_de_terminar():
    d = cargar()["States"]
    for nombre in ("PrepError", "PrepExpirada", "PrepRechazada", "FinalDesdeFase"):
        assert d[nombre]["Next"] == "Finalizar", nombre
```

- [ ] **Paso 2: ver que fallan**

Ejecuta: `wt prisma_agents/tests/test_state_machine.py` → FALLA (`FileNotFoundError ... prisma_flow.asl.json`).

- [ ] **Paso 3: escribir la definición**

`statemachine/prisma_flow.asl.json`:

```json
{
  "Comment": "Flujo PRISMA: fases con espera del docente (HITL) duradera. Por aqui solo viajan ids, numeros y estados: el historial de ejecuciones guarda entradas y salidas 90 dias en claro.",
  "StartAt": "Iniciar",
  "States": {
    "Iniciar": {
      "Type": "Pass",
      "Result": { "attempt": 1, "feedback_agent": 0 },
      "ResultPath": "$.ctx",
      "Next": "FaseA"
    },
    "FaseA": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
      "Parameters": {
        "FunctionName": "${InvokerArn}",
        "Payload": {
          "phase": "a",
          "session_id.$": "$.session_id",
          "attempt.$": "$.ctx.attempt",
          "feedback_agent.$": "$.ctx.feedback_agent",
          "task_token.$": "$$.Task.Token"
        }
      },
      "TimeoutSecondsPath": "$.timeouts.fase",
      "HeartbeatSecondsPath": "$.timeouts.heartbeat",
      "ResultPath": "$.fase",
      "Retry": [
        { "ErrorEquals": ["States.HeartbeatTimeout"], "IntervalSeconds": 5, "MaxAttempts": 1, "BackoffRate": 1.0 }
      ],
      "Catch": [
        { "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "PrepError" }
      ],
      "Next": "RutaFaseA"
    },
    "RutaFaseA": {
      "Type": "Choice",
      "Choices": [
        { "Variable": "$.fase.status", "StringEquals": "ok", "Next": "EsperarDocente" }
      ],
      "Default": "FinalDesdeFase"
    },
    "EsperarDocente": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
      "Parameters": {
        "FunctionName": "${InvokerArn}",
        "Payload": {
          "phase": "wait",
          "session_id.$": "$.session_id",
          "attempt.$": "$.ctx.attempt",
          "task_token.$": "$$.Task.Token"
        }
      },
      "TimeoutSecondsPath": "$.timeouts.hitl",
      "ResultPath": "$.decision",
      "Catch": [
        { "ErrorEquals": ["States.Timeout"], "ResultPath": "$.error", "Next": "PrepExpirada" },
        { "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "PrepError" }
      ],
      "Next": "DecidirHitl"
    },
    "DecidirHitl": {
      "Type": "Choice",
      "Choices": [
        { "Variable": "$.decision.approved", "BooleanEquals": true, "Next": "FaseB" },
        {
          "And": [
            { "Variable": "$.decision.approved", "BooleanEquals": false },
            { "Variable": "$.ctx.attempt", "NumericGreaterThanEquals": 3 }
          ],
          "Next": "PrepRechazada"
        }
      ],
      "Default": "ProximoIntento"
    },
    "ProximoIntento": {
      "Type": "Pass",
      "Parameters": {
        "attempt.$": "States.MathAdd($.ctx.attempt, 1)",
        "feedback_agent.$": "$.decision.agent_to_retry"
      },
      "ResultPath": "$.ctx",
      "Next": "FaseA"
    },
    "FaseB": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
      "Parameters": {
        "FunctionName": "${InvokerArn}",
        "Payload": {
          "phase": "b",
          "session_id.$": "$.session_id",
          "task_token.$": "$$.Task.Token"
        }
      },
      "TimeoutSecondsPath": "$.timeouts.fase",
      "HeartbeatSecondsPath": "$.timeouts.heartbeat",
      "ResultPath": "$.fase",
      "Retry": [
        { "ErrorEquals": ["States.HeartbeatTimeout"], "IntervalSeconds": 5, "MaxAttempts": 1, "BackoffRate": 1.0 }
      ],
      "Catch": [
        { "ErrorEquals": ["States.ALL"], "ResultPath": "$.error", "Next": "PrepError" }
      ],
      "Next": "FinalDesdeFase"
    },
    "FinalDesdeFase": {
      "Type": "Pass",
      "Parameters": { "status.$": "$.fase.status" },
      "ResultPath": "$.final",
      "Next": "Finalizar"
    },
    "PrepError": {
      "Type": "Pass",
      "Result": { "status": "error" },
      "ResultPath": "$.final",
      "Next": "Finalizar"
    },
    "PrepExpirada": {
      "Type": "Pass",
      "Result": { "status": "expired" },
      "ResultPath": "$.final",
      "Next": "Finalizar"
    },
    "PrepRechazada": {
      "Type": "Pass",
      "Result": { "status": "hitl_rejected" },
      "ResultPath": "$.final",
      "Next": "Finalizar"
    },
    "Finalizar": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "${InvokerArn}",
        "Payload": {
          "phase": "finalize",
          "session_id.$": "$.session_id",
          "status.$": "$.final.status"
        }
      },
      "ResultPath": null,
      "Retry": [
        {
          "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException", "States.TaskFailed"],
          "IntervalSeconds": 3,
          "MaxAttempts": 3,
          "BackoffRate": 2.0
        }
      ],
      "Next": "Resultado"
    },
    "Resultado": {
      "Type": "Choice",
      "Choices": [
        {
          "Or": [
            { "Variable": "$.final.status", "StringEquals": "error" },
            { "Variable": "$.final.status", "StringEquals": "timeout" }
          ],
          "Next": "Falla"
        }
      ],
      "Default": "Fin"
    },
    "Fin": { "Type": "Succeed" },
    "Falla": {
      "Type": "Fail",
      "Error": "FlujoConError",
      "Cause": "El flujo termino con error o timeout. El detalle esta en la sesion (DynamoDB)."
    }
  }
}
```

- [ ] **Paso 4: ver que pasan**

Ejecuta: `wt prisma_agents/tests/test_state_machine.py` → PASA.

- [ ] **Paso 5: commit**

```bash
git add statemachine/prisma_flow.asl.json prisma_agents/tests/test_state_machine.py
git commit -m "feat(sfn): maquina de estados del flujo (fases, espera del docente, reintento unico por heartbeat)"
```

---

### Tarea 9: LocalStack con SQS + Step Functions y pruebas de punta a punta

Sin repo: estos archivos viven en la raíz (no se commitean). Es la tarea que demuestra que el diseño funciona, así que **no se da por terminada hasta que el E2E pase completo**.

**Archivos:**
- Modificar: `docker-compose.yml` (raíz): servicios `localstack` y `workflow`
- Reescribir: `local/localstack/init-aws.sh`
- Crear: `local/e2e/sfn_e2e.py`

**Interfaces:**
- Consume: `statemachine/prisma_flow.asl.json`, `lambda/sfn_starter.py`, `lambda/sfn_invoker.py` (montados), endpoints de la Tarea 6, `workflow_runner.submit_hitl_decision` / `cancel_session_sfn` (Tarea 5).
- Produce: `FLOW_MODE=sfn|legacy` (por defecto `sfn`); cola `prisma-flujo` + DLQ; máquina `prisma-flujo`; Lambdas `prisma-sfn-starter` y `prisma-sfn-invoker`.

- [ ] **Paso 1: cambios en `docker-compose.yml`**

Servicio `localstack`: agrega a `environment` y `volumes` (conserva lo que ya tiene):

```yaml
    environment:
      FLOW_MODE: ${FLOW_MODE:-sfn}           # sfn = SQS + Step Functions · legacy = S3 -> Lambda prisma-trigger -> /internal/run
    volumes:
      - ./prisma_workflow/statemachine:/opt/statemachine:ro
```

Servicio `workflow`: agrega a `environment` y crea `volumes`:

```yaml
    environment:
      SFN_STATE_MACHINE_ARN: arn:aws:states:us-east-1:000000000000:stateMachine:prisma-flujo
      PHASE_HEARTBEAT_SECONDS: "2"           # heartbeat corto: las pruebas con worker muerto no tardan minutos
      MOCK_PHASE_DELAY: "0.3"
    volumes:
      - ./local/e2e:/e2e:ro
```

Valida: `docker compose config --quiet && echo OK`.

- [ ] **Paso 2: reescribir `local/localstack/init-aws.sh`**

```bash
#!/bin/bash
# Se ejecuta dentro del contenedor de LocalStack al terminar de arrancar (/etc/localstack/init/ready.d).
# Recrea lo que en AWS real define prisma-infra:
#   - buckets S3 y tabla DynamoDB de sesiones
#   - FLOW_MODE=sfn (por defecto): cola SQS + DLQ, Lambdas starter/invoker, máquina de estados y la notificación S3 -> SQS
#   - FLOW_MODE=legacy: Lambda `prisma-trigger` + notificación S3 -> Lambda (camino anterior, sin Step Functions)
# Debe ser idempotente: LocalStack puede reiniciarse sin recrear el contenedor.
set -uo pipefail

BUCKET_WORKFLOW="prisma-workflow"
BUCKET_SCHOOLS="prisma-schools-repos"
TABLE="chat_sessions"
REGION="us-east-1"
ACCT="000000000000"
FLOW_MODE="${FLOW_MODE:-sfn}"
WORKDIR="$(mktemp -d)"

echo "[init] buckets"
awslocal s3api create-bucket --bucket "$BUCKET_WORKFLOW" --region "$REGION" >/dev/null 2>&1 || true
awslocal s3api create-bucket --bucket "$BUCKET_SCHOOLS"  --region "$REGION" >/dev/null 2>&1 || true

echo "[init] tabla $TABLE"
awslocal dynamodb create-table \
  --table-name "$TABLE" \
  --attribute-definitions AttributeName=session_id,AttributeType=S \
  --key-schema AttributeName=session_id,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST \
  --region "$REGION" >/dev/null 2>&1 || true

# zip_lambda <archivo en /opt/lambda-src> <zip de salida>: se empaqueta como lambda_function.py
zip_lambda() {
  cp "/opt/lambda-src/$1" "$WORKDIR/lambda_function.py"
  (cd "$WORKDIR" && python3 -c "import zipfile; z=zipfile.ZipFile('$2','w',zipfile.ZIP_DEFLATED); z.write('lambda_function.py'); z.close()")
}

# crear_lambda <nombre> <zip> <variables "A=1,B=2"> <timeout>
crear_lambda() {
  if ! awslocal lambda create-function --function-name "$1" --runtime python3.12 --handler lambda_function.handler \
        --role "arn:aws:iam::$ACCT:role/lambda-role" --timeout "$4" --memory-size 128 \
        --environment "Variables={$3}" --zip-file "fileb://$WORKDIR/$2" --region "$REGION" >/dev/null 2>&1; then
    awslocal lambda update-function-code --function-name "$1" --zip-file "fileb://$WORKDIR/$2" --region "$REGION" >/dev/null 2>&1 || true
    awslocal lambda update-function-configuration --function-name "$1" --environment "Variables={$3}" --region "$REGION" >/dev/null 2>&1 || true
  fi
  awslocal lambda wait function-active-v2 --function-name "$1" --region "$REGION" 2>/dev/null || true
}

if [ "$FLOW_MODE" = "legacy" ]; then
  echo "[init] modo legacy: Lambda prisma-trigger"
  zip_lambda trigger_handler.py trigger.zip
  crear_lambda prisma-trigger trigger.zip "BACKEND_INTERNAL_URL=http://workflow:8000,INTERNAL_TOKEN=${INTERNAL_TOKEN}" 60
  awslocal s3api put-bucket-notification-configuration --bucket "$BUCKET_WORKFLOW" --region "$REGION" \
    --notification-configuration "{\"LambdaFunctionConfigurations\":[{\"Id\":\"prisma-event\",\"LambdaFunctionArn\":\"arn:aws:lambda:$REGION:$ACCT:function:prisma-trigger\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"prefix\",\"Value\":\"jobs/\"}]}}}]}"
else
  echo "[init] modo sfn: SQS + Step Functions"

  # 1) colas: principal con redrive a la DLQ tras 3 recepciones
  awslocal sqs create-queue --queue-name prisma-flujo-dlq --region "$REGION" >/dev/null 2>&1 || true
  DLQ_URL=$(awslocal sqs get-queue-url --queue-name prisma-flujo-dlq --query QueueUrl --output text --region "$REGION")
  DLQ_ARN=$(awslocal sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text --region "$REGION")
  cat > "$WORKDIR/q.json" <<JSON
{"VisibilityTimeout":"180","RedrivePolicy":"{\"deadLetterTargetArn\":\"$DLQ_ARN\",\"maxReceiveCount\":\"3\"}"}
JSON
  awslocal sqs create-queue --queue-name prisma-flujo --attributes "file://$WORKDIR/q.json" --region "$REGION" >/dev/null 2>&1 || true
  Q_URL=$(awslocal sqs get-queue-url --queue-name prisma-flujo --query QueueUrl --output text --region "$REGION")
  Q_ARN=$(awslocal sqs get-queue-attributes --queue-url "$Q_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text --region "$REGION")

  # 2) Lambda invoker (la que llama al workflow)
  zip_lambda sfn_invoker.py invoker.zip
  crear_lambda prisma-sfn-invoker invoker.zip "BACKEND_INTERNAL_URL=http://workflow:8000,INTERNAL_TOKEN=${INTERNAL_TOKEN}" 30
  INVOKER_ARN="arn:aws:lambda:$REGION:$ACCT:function:prisma-sfn-invoker"

  # 3) máquina de estados (se sustituye el único marcador de plantilla: el ARN del invoker)
  sed "s#\${InvokerArn}#$INVOKER_ARN#g" /opt/statemachine/prisma_flow.asl.json > "$WORKDIR/def.json"
  SM_ARN="arn:aws:states:$REGION:$ACCT:stateMachine:prisma-flujo"
  if awslocal stepfunctions describe-state-machine --state-machine-arn "$SM_ARN" --region "$REGION" >/dev/null 2>&1; then
    awslocal stepfunctions update-state-machine --state-machine-arn "$SM_ARN" --definition "file://$WORKDIR/def.json" --region "$REGION" >/dev/null
  else
    awslocal stepfunctions create-state-machine --name prisma-flujo --definition "file://$WORKDIR/def.json" \
      --role-arn "arn:aws:iam::$ACCT:role/sfn-role" --region "$REGION" >/dev/null
  fi

  # 4) Lambda starter + mapeo de eventos SQS -> starter (un mensaje por invocación, reporta fallos parciales)
  zip_lambda sfn_starter.py starter.zip
  crear_lambda prisma-sfn-starter starter.zip \
    "STATE_MACHINE_ARN=$SM_ARN,AWS_ENDPOINT_URL=http://localstack:4566,AWS_ACCESS_KEY_ID=test,AWS_SECRET_ACCESS_KEY=test,AWS_DEFAULT_REGION=$REGION" 30
  if [ -z "$(awslocal lambda list-event-source-mappings --function-name prisma-sfn-starter --region "$REGION" --query 'EventSourceMappings[0].UUID' --output text 2>/dev/null | grep -v None)" ]; then
    awslocal lambda create-event-source-mapping --function-name prisma-sfn-starter --event-source-arn "$Q_ARN" \
      --batch-size 1 --function-response-types ReportBatchItemFailures --region "$REGION" >/dev/null
  fi

  # 5) S3 -> SQS (REEMPLAZA la notificación hacia la Lambda: S3 no admite dos destinos para el mismo evento y prefijo)
  awslocal s3api put-bucket-notification-configuration --bucket "$BUCKET_WORKFLOW" --region "$REGION" \
    --notification-configuration "{\"QueueConfigurations\":[{\"Id\":\"prisma-flujo\",\"QueueArn\":\"$Q_ARN\",\"Events\":[\"s3:ObjectCreated:*\"],\"Filter\":{\"Key\":{\"FilterRules\":[{\"Name\":\"prefix\",\"Value\":\"jobs/\"}]}}}]}"
fi

echo "[init] listo (FLOW_MODE=$FLOW_MODE)"
```

- [ ] **Paso 3: el E2E**

`local/e2e/sfn_e2e.py`:

```python
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
    esperar(lambda: ejec(sid) == "ABORTED", 20, "ejecución ABORTED")
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
```

- [ ] **Paso 4: levantar el entorno**

```bash
docker compose up -d --build workflow          # imagen con el código nuevo
docker compose up -d --force-recreate localstack   # el script de inicio corre al arrancar
docker compose ps localstack workflow          # ambos healthy
docker logs prisma-localstack-1 2>&1 | grep "\[init\]"
```

Esperado en el log: `modo sfn: SQS + Step Functions` y `listo (FLOW_MODE=sfn)`. Comprueba los recursos:

```bash
docker exec prisma-localstack-1 awslocal sqs list-queues
docker exec prisma-localstack-1 awslocal stepfunctions list-state-machines --query 'stateMachines[].name'
docker exec prisma-localstack-1 awslocal lambda list-functions --query 'Functions[].FunctionName'
```

- [ ] **Paso 5: ejecutar el E2E**

```bash
MSYS_NO_PATHCONV=1 docker compose exec -T workflow python /e2e/sfn_e2e.py
```

Esperado: `9/9 escenarios OK`. La primera corrida tarda más (LocalStack baja la imagen de la Lambda).
Si un escenario falla: **diagnostica antes de tocar** (`docker logs prisma-workflow-1`, `docker logs prisma-localstack-1`, `aws stepfunctions get-execution-history`). Los fallos esperables son
de integración (formato del evento de S3, `...Path` de timeouts, permisos de red de la Lambda); corrige donde esté la causa y, si cambia código de las Tareas 1–8, vuelve a correr los tests unitarios de esa tarea.

- [ ] **Paso 6: comprobar el modo legacy (reversa)**

```bash
FLOW_MODE=legacy docker compose up -d --force-recreate localstack
docker logs prisma-localstack-1 2>&1 | grep "\[init\]"      # modo legacy: Lambda prisma-trigger
```
Sube un PACI de prueba como en `LOCAL.md` (sección «Probar la cadena S3 → Lambda → workflow») y confirma que el workflow recibe `POST /chat/internal/run/<id>`.
Vuelve al modo por defecto: `docker compose up -d --force-recreate localstack`.

- [ ] **Paso 7: sin commit** (raíz sin repo). Anota el resultado del E2E (`9/9`) al final de este plan, en una sección `## Resultado del E2E`.

---

### Tarea 10: Terraform propuesto (sin aplicar)

**Archivos:**
- Crear: `prisma-infra/08-flujo-sfn.tf.pendiente`

**Interfaces:**
- Consume (ya existen en `prisma-infra`): `aws_security_group.lambda`, `aws_lb.alb`, `data.aws_iam_role.lab`, `local.subnet_ids`, `local.bucket_workflow`, `local.bucket_workflow_arn`, `local.account_id`, `var.nombre`, `var.internal_token`,
  `terraform_data.bucket_workflow`, y los archivos `../prisma_workflow/lambda/sfn_*.py` y `../prisma_workflow/statemachine/prisma_flow.asl.json`.
- Produce: variables `sfn_*`, recursos SQS/Lambda/Step Functions/ciclo de vida y las salidas `flujo_state_machine_arn`, `flujo_queue_url`.

- [ ] **Paso 1: escribir el archivo**

```hcl
# ──────────────────────────────────────────────────────────────────────────────
# 08-flujo-sfn.tf.pendiente — Subida → SQS → Lambda → Step Functions (espera del docente duradera).
#
# ESTADO: PROPUESTA, NO APLICADA. La extensión ".pendiente" hace que Terraform NO cargue este archivo. Se validó con
# `terraform validate` (sin tocar AWS) y el flujo completo se probó en LocalStack (ver docs/superpowers/specs/2026-10-01-sqs-stepfunctions-design.md).
#
# QUÉ CREA
#   SQS       cola `prisma-flujo` + DLQ (3 recepciones) y la política que deja escribir a S3.
#   Lambdas   `sfn-starter` (SQS → StartExecution, FUERA de la VPC) y `sfn-invoker` (Step Functions → ALB, DENTRO de la VPC, solo HTTP).
#   Step Fn   máquina `prisma-flujo` (Standard) con la definición de prisma_workflow/statemachine/prisma_flow.asl.json.
#   S3        notificación jobs/ → SQS (REEMPLAZA la que hoy llama a la Lambda `prisma-trigger`) y expiración de 7 días de `state/`.
#
# CÓMO ACTIVARLO (en este orden; el corte es el paso 4)
#   1. Despliega primero la imagen NUEVA del workflow (compatible con ambos caminos; sin SFN_STATE_MACHINE_ARN se comporta como hoy).
#   2. Renombra este archivo a 08-flujo-sfn.tf y corre `terraform plan`. Debe mostrar SOLO recursos nuevos (to add) salvo lo del paso 3.
#   3. En 05-eventos.tf elimina `aws_s3_bucket_notification.workflow` y `aws_lambda_permission.s3_invoca_trigger` (S3 admite una sola
#      configuración de notificaciones por bucket: la de este archivo la reemplaza). La Lambda `trigger` puede quedarse para la reversa.
#   4. `terraform apply`. Ese apply hace el CORTE: desde ese momento las subidas entran por SQS.
#   5. En 03-ecs.tf, `local.env.workflow`, agrega  SFN_STATE_MACHINE_ARN = aws_sfn_state_machine.flujo.arn  y vuelve a aplicar.
#      Sin esa variable el workflow no sabe cancelar ejecuciones (/cancel) ni derivar el ARN.
#
# REVERSA
#   Restaurar `aws_s3_bucket_notification.workflow` (la que llama a la Lambda `prisma-trigger`) y quitar la de este archivo.
#   El camino antiguo (/chat/internal/run) sigue en el código.
#
# DEPENDE DEL LAB (no se pudo comprobar con el lab cerrado)
#   * Que Step Functions esté habilitado en el Learner Lab y que `LabRole` tenga `states:*`, `sqs:*` y `lambda:*`.
#   * El rol de la tarea de ECS (LabRole) necesita `states:SendTaskSuccess/Failure/Heartbeat` y `states:StopExecution`.
# ──────────────────────────────────────────────────────────────────────────────

variable "sfn_fase_timeout_segundos" {
  description = "Tiempo máximo de una fase (A o B)."
  type        = number
  default     = 1800
}

variable "sfn_heartbeat_segundos" {
  description = "Una fase sin señales de vida por este tiempo se da por muerta y se reintenta UNA vez."
  type        = number
  default     = 120
}

variable "sfn_hitl_timeout_segundos" {
  description = "Cuánto espera el flujo la decisión del docente antes de expirar (24 h)."
  type        = number
  default     = 86400
}

# ── SQS ───────────────────────────────────────────────────────────────────────
resource "aws_sqs_queue" "flujo_dlq" {
  name                      = "${var.nombre}-flujo-dlq"
  message_retention_seconds = 1209600 # 14 días
}

resource "aws_sqs_queue" "flujo" {
  name                       = "${var.nombre}-flujo"
  visibility_timeout_seconds = 180 # >= 6 veces el timeout de la Lambda starter (30 s)

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.flujo_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue_policy" "flujo" {
  queue_url = aws_sqs_queue.flujo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "S3EnviaEventos"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.flujo.arn
      Condition = {
        ArnEquals    = { "aws:SourceArn" = local.bucket_workflow_arn }
        StringEquals = { "aws:SourceAccount" = local.account_id }
      }
    }]
  })
}

# ── S3: notificación hacia SQS y expiración del estado de las fases ───────────
# REEMPLAZA a aws_s3_bucket_notification.workflow de 05-eventos.tf (ver el paso 3 del encabezado).
resource "aws_s3_bucket_notification" "workflow_sqs" {
  bucket = local.bucket_workflow

  queue {
    id            = "${var.nombre}-flujo"
    queue_arn     = aws_sqs_queue.flujo.arn
    events        = ["s3:ObjectCreated:*"]
    filter_prefix = "jobs/"
  }

  depends_on = [aws_sqs_queue_policy.flujo, terraform_data.bucket_workflow]
}

# `state/` guarda texto de PACI y planificaciones (datos de menores) entre fases: expira a los 7 días, igual que el TTL de DynamoDB.
resource "aws_s3_bucket_lifecycle_configuration" "workflow_state" {
  bucket = local.bucket_workflow

  rule {
    id     = "expirar-estado-de-fases"
    status = "Enabled"

    filter {
      prefix = "state/"
    }

    expiration {
      days = 7
    }
  }

  depends_on = [terraform_data.bucket_workflow]
}

# ── Lambdas ───────────────────────────────────────────────────────────────────
data "archive_file" "sfn_starter" {
  type        = "zip"
  output_path = "${path.module}/.build/prisma-sfn-starter.zip"

  source {
    content  = file("${path.module}/../prisma_workflow/lambda/sfn_starter.py")
    filename = "lambda_function.py"
  }
}

data "archive_file" "sfn_invoker" {
  type        = "zip"
  output_path = "${path.module}/.build/prisma-sfn-invoker.zip"

  source {
    content  = file("${path.module}/../prisma_workflow/lambda/sfn_invoker.py")
    filename = "lambda_function.py"
  }
}

resource "aws_cloudwatch_log_group" "sfn_starter" {
  name = "/aws/lambda/${var.nombre}-sfn-starter"
}

resource "aws_cloudwatch_log_group" "sfn_invoker" {
  name = "/aws/lambda/${var.nombre}-sfn-invoker"
}

# FUERA de la VPC: necesita llegar a la API pública de Step Functions (las subredes por defecto no dan internet a una Lambda en VPC).
resource "aws_lambda_function" "sfn_starter" {
  function_name = "${var.nombre}-sfn-starter"
  role          = data.aws_iam_role.lab.arn
  runtime       = "python3.12"
  handler       = "lambda_function.handler"
  architectures = ["x86_64"]
  timeout       = 30
  memory_size   = 128

  filename         = data.archive_file.sfn_starter.output_path
  source_code_hash = data.archive_file.sfn_starter.output_base64sha256

  environment {
    variables = {
      STATE_MACHINE_ARN     = aws_sfn_state_machine.flujo.arn
      FASE_TIMEOUT_SECONDS  = tostring(var.sfn_fase_timeout_segundos)
      HEARTBEAT_SECONDS     = tostring(var.sfn_heartbeat_segundos)
      HITL_TIMEOUT_SECONDS  = tostring(var.sfn_hitl_timeout_segundos)
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.sfn_starter.name
  }
}

# DENTRO de la VPC: llega al ALB interno. Solo HTTP (sin boto3, sin llamadas a APIs de AWS).
resource "aws_lambda_function" "sfn_invoker" {
  function_name = "${var.nombre}-sfn-invoker"
  role          = data.aws_iam_role.lab.arn
  runtime       = "python3.12"
  handler       = "lambda_function.handler"
  architectures = ["x86_64"]
  timeout       = 30
  memory_size   = 128

  filename         = data.archive_file.sfn_invoker.output_path
  source_code_hash = data.archive_file.sfn_invoker.output_base64sha256

  vpc_config {
    subnet_ids         = local.subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      BACKEND_INTERNAL_URL = "http://${aws_lb.alb.dns_name}" # con http://, sin él falla en silencio
      INTERNAL_TOKEN       = var.internal_token
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.sfn_invoker.name
  }
}

# Un mensaje por invocación y reporte de fallos parciales; como máximo 2 arranques concurrentes
# (acota los ARRANQUES, no los flujos en curso: si hace falta proteger la cuota de Gemini se agrega un semáforo).
resource "aws_lambda_event_source_mapping" "flujo" {
  event_source_arn        = aws_sqs_queue.flujo.arn
  function_name           = aws_lambda_function.sfn_starter.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 2
  }
}

# ── Step Functions (Standard: waitForTaskToken no existe en Express) ──────────
resource "aws_sfn_state_machine" "flujo" {
  name     = "${var.nombre}-flujo"
  role_arn = data.aws_iam_role.lab.arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/../prisma_workflow/statemachine/prisma_flow.asl.json", {
    InvokerArn = aws_lambda_function.sfn_invoker.arn
  })
}

output "flujo_state_machine_arn" {
  description = "Agregar a las variables del contenedor `workflow` como SFN_STATE_MACHINE_ARN (paso 5 del encabezado)."
  value       = aws_sfn_state_machine.flujo.arn
}

output "flujo_queue_url" {
  value = aws_sqs_queue.flujo.url
}
```

- [ ] **Paso 2: formato y validación offline** (copia temporal **hermana** de `prisma-infra`, sin `tfstate` ni `tfvars`, para que `../prisma_workflow/...` resuelva)

```bash
cd C:/Users/Villa/Downloads/prisma-aws
rm -rf _tfcheck_tmp && mkdir _tfcheck_tmp
cp prisma-infra/*.tf _tfcheck_tmp/ && cp prisma-infra/08-flujo-sfn.tf.pendiente _tfcheck_tmp/08-flujo-sfn.tf
cp prisma-infra/.terraform.lock.hcl _tfcheck_tmp/ && cp -r prisma-infra/.terraform _tfcheck_tmp/
cd _tfcheck_tmp
terraform fmt -check -diff 08-flujo-sfn.tf && echo "formato OK"
terraform validate -no-color
```
Esperado: `formato OK` y `Success! The configuration is valid.` Si `fmt` pide cambios de alineación, aplícalos con `terraform fmt 08-flujo-sfn.tf` y copia el resultado de vuelta a `prisma-infra/08-flujo-sfn.tf.pendiente`.

- [ ] **Paso 3: la definición renderizada es JSON válido**

```bash
python - <<'EOF'
import json
t = open("prisma_workflow/statemachine/prisma_flow.asl.json", encoding="utf-8").read().replace("${InvokerArn}", "arn:aws:lambda:us-east-1:111111111111:function:x")
d = json.loads(t)
print("estados:", len(d["States"]), "| inicio:", d["StartAt"])
EOF
```

- [ ] **Paso 4: limpiar la copia temporal**: `rm -rf _tfcheck_tmp`

- [ ] **Paso 5: sin commit** (`prisma-infra` no tiene commits; el primer commit lo decide el dueño).

---

### Tarea 11: documentación y verificación final

**Archivos:**
- Modificar: `CLAUDE.md` (raíz): §5.3 y §12 (pendiente #5c, #5b); nueva §16
- Modificar: `prisma_workflow/CLAUDE.md`: decisión HITL (§6) y nota del flujo por fases
- Modificar: `LOCAL.md`: sección «Subida → SQS → Step Functions»; `docs/superpowers/plans/…` (resultados)

- [ ] **Paso 1: `prisma_workflow/CLAUDE.md`**

En §6, la decisión «Comunicación HITL vía `asyncio.Queue`…» queda con una **actualización** (no se borra):

```markdown
> ⚠️ **Actualizado el 2026-10-01 (rama `cloudnative`).** Con Step Functions la espera del docente ya no vive en un proceso: la sostiene
> `waitForTaskToken` (el token se guarda en DynamoDB y `POST /hitl` hace `SendTaskSuccess`). **No es polling a BD.** La `asyncio.Queue`
> sigue siendo el camino del modo local sin AWS y del CLI. `POST /chat/:id/hitl` elige el camino según la sesión: si está en `SESSIONS`
> (camino antiguo) usa la cola; si solo existe en DynamoDB (Step Functions) usa el token. Ver `docs/superpowers/specs/2026-10-01-sqs-stepfunctions-design.md`.
```

Y en §3 (estructura) agrega una línea por archivo nuevo: `api/phase_store.py`, `api/sfn_client.py`, `api/mock_phases.py`, `utils/hitl_feedback.py`, `lambda/sfn_starter.py`, `lambda/sfn_invoker.py`, `statemachine/prisma_flow.asl.json`.

- [ ] **Paso 2: `CLAUDE.md` (raíz)**

- §12 «Pendiente»: marca **#5c** como hecho en código y probado en LocalStack, **pendiente de aplicar la infraestructura** (`prisma-infra/08-flujo-sfn.tf.pendiente`); y aclara que **`desiredCount` ya puede ser > 1 en el workflow solo con Step Functions activo** (el camino antiguo sigue siendo en memoria).
- Nueva §16 «Flujo orquestado por Step Functions (rama `cloudnative`)»: diagrama de la especificación, las 4 reglas que no se rompen (nada de PII por Step Functions; `state/` nunca bajo `jobs/`; el `invoker` sin llamadas a AWS; reintento único), el corte (cambiar la notificación de S3) y la reversa.
  Incluye el hallazgo verificado el 2026-10-01 sobre `ms-docs`: nombra los archivos igual (`jobs/{id}/paci.pdf|docx`, `material.docx`) y escribe en la misma tabla, así que su flujo `jobs` entra por la misma cola;
  **pero** guarda `user_id` en vez de `owner_id`, por lo que la comprobación de dueño de `/hitl` y `/cancel` queda permisiva para esas sesiones (igual que hoy en `/state`). No se modificó `ms-docs` (otro repo).
- Nota en §5.3: el diagrama S3 → Lambda → workflow es el camino **antiguo**; el nuevo es S3 → SQS → Lambda → Step Functions.

- [ ] **Paso 3: `LOCAL.md`**

Agrega la sección «Subida → SQS → Step Functions» con: qué levanta LocalStack ahora (`sqs`, `stepfunctions`), `FLOW_MODE=sfn|legacy`, cómo correr el E2E
(`docker compose exec -T workflow python /e2e/sfn_e2e.py`), los `school_id` simulados (`__mock_ok__`, `__mock_blocked__`, `__mock_error__`, `__mock_degraded__`, `__mock_timeout__`, `__mock_dead__`) y cómo ver una ejecución
(`docker exec prisma-localstack-1 awslocal stepfunctions list-executions --state-machine-arn arn:aws:states:us-east-1:000000000000:stateMachine:prisma-flujo`).

- [ ] **Paso 4: verificación final**

```bash
wt prisma_agents/tests                                   # suite completa del workflow: todo verde
git status --short                                       # solo deben quedar sin commitear las ediciones del dueño (agents/*.py y 2 líneas de agent.py)
git log --oneline -12                                    # los commits de las tareas 1–8 sobre cloudnative
git diff master..cloudnative --stat | tail -3
```
Y repite el E2E (`9/9`) con el código final: `MSYS_NO_PATHCONV=1 docker compose exec -T workflow python /e2e/sfn_e2e.py` (desde la raíz).

- [ ] **Paso 5: commit de la documentación del repo**

```bash
git add CLAUDE.md
git commit -m "docs(sfn): decision HITL actualizada y archivos nuevos del flujo por fases"
```

(`CLAUDE.md` raíz, `LOCAL.md` y `docs/` están fuera de repos: se editan, no se commitean.)

- [ ] **Paso 6: reportar al dueño**, sin adornos: resultado del E2E (qué escenarios pasaron), qué **no** se pudo verificar (Step Functions y permisos en el Learner Lab real; una corrida real con Gemini), y los pasos de activación de `prisma-infra`.

## Resultado del spike

Ejecutado el 2026-10-01 contra LocalStack 4.14.0 (sin token), `sqs,stepfunctions,lambda,s3,dynamodb`.

| Capacidad | Resultado | Detalle |
|---|---|---|
| A `waitForTaskToken` con Lambda + `SendTaskSuccess` + `States.MathAdd` | **OK con matiz** | Si la Lambda llama `SendTaskSuccess` *dentro* de la invocación, LocalStack lo pierde (carrera); si el éxito llega después (como hará el workflow), la ejecución sigue y termina bien. El spike se dejó con el stub síncrono; la comprobación manual con el token del historial dio `{"fase":{"status":"ok"},"sig":{"n":2}}`. |
| B `TimeoutSecondsPath` | OK | Vence y entra al `Catch` con `States.Timeout`. |
| C `HeartbeatSecondsPath` | **Parcial** | LocalStack respeta el valor (vence a los 3 s), pero informa `States.Timeout` en vez de `States.HeartbeatTimeout`. El `Retry` de una sola vez ante `HeartbeatTimeout` **no se puede probar local**; el `Catch States.ALL` sí. |
| D `SendTaskHeartbeat` | OK | Mantiene viva la tarea. |
| E idempotencia / `StopExecution` | **Parcial** | `StartExecution` es idempotente y reabrir una ejecución cerrada da `ExecutionAlreadyExists`. `StopExecution` sobre una tarea `waitForTaskToken` **no la aborta** en LocalStack (queda `RUNNING`). |

Veredicto: sin fallas bloqueantes. Se mantiene `...SecondsPath`. Ajustes para tareas posteriores: (1) el E2E de cancelación no puede exigir `ABORTED`, solo que la sesión
quede `cancelled` en DynamoDB y el flujo no avance; (2) el E2E de heartbeat perdido espera el camino de error vía `Catch`, sin comprobar el reintento. Ambos se comprobarán
contra el comportamiento documentado de AWS cuando exista el laboratorio.

## Resultado del E2E

Ejecutado el 2026-10-01 en LocalStack 4.14.0 con fases simuladas (`docker compose exec -T workflow python /e2e/sfn_e2e.py`): **9/9 escenarios OK**
(aprobado a la primera, rechazo+reintento, tres rechazos, PACI bloqueado, error en fase B, docente que no responde, cancelación, mensaje SQS duplicado, worker muerto).
Matices: la cancelación acepta `RUNNING` además de `ABORTED` (LocalStack no aborta una espera con token); el reintento único por `HeartbeatTimeout` no se prueba local
(LocalStack informa `States.Timeout`). El modo `FLOW_MODE=legacy` recrea la Lambda `prisma-trigger` y la notificación S3→Lambda; el modo `sfn` deja la notificación S3→SQS.
No verificado: corrida real con Gemini, ni Step Functions/permisos (`LabRole`) en AWS Academy.
