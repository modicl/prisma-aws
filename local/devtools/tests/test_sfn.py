"""Step Functions: lista de ejecuciones y línea de tiempo armada desde el historial. Solo lectura."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app import sfn

ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:prisma-flujo"
T0 = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)


def seg(n):
    return T0 + timedelta(seconds=n)


def _err(codigo):
    return ClientError({"Error": {"Code": codigo, "Message": "x"}}, "Op")


@pytest.fixture
def cli(monkeypatch):
    c = MagicMock()
    monkeypatch.setenv("SFN_STATE_MACHINE_ARN", ARN)
    monkeypatch.setattr(sfn, "_client", lambda: c)
    return c


# ── helpers para armar historiales ──────────────────────────────────────────
def entra(i, t, nombre, tipo="Task"):
    return {"id": i, "type": f"{tipo}StateEntered", "timestamp": seg(t), "stateEnteredEventDetails": {"name": nombre}}


def sale(i, t, nombre, tipo="Task"):
    return {"id": i, "type": f"{tipo}StateExited", "timestamp": seg(t), "stateExitedEventDetails": {"name": nombre}}


def programada(i, t):
    return {"id": i, "type": "TaskScheduled", "timestamp": seg(t)}


def historial(cli, eventos, status="SUCCEEDED", stop=None):
    cli.describe_execution.return_value = {"name": "s1", "status": status, "startDate": seg(0), **({"stopDate": stop} if stop else {})}
    cli.get_execution_history.return_value = {"events": list(reversed(eventos))}      # reverseOrder=True: del más nuevo al más antiguo


# ── list_executions ─────────────────────────────────────────────────────────
async def test_lista_ejecuciones_con_duracion(cli):
    cli.list_executions.return_value = {"executions": [
        {"name": "a", "status": "SUCCEEDED", "startDate": seg(10), "stopDate": seg(14)},
        {"name": "b", "status": "RUNNING", "startDate": seg(20)},
    ]}
    res = await sfn.list_executions(limit=5)
    kw = cli.list_executions.call_args.kwargs
    assert kw["stateMachineArn"] == ARN and kw["maxResults"] == 5 and "statusFilter" not in kw
    a, b = res["executions"]
    assert a == {"name": "a", "status": "SUCCEEDED", "start": seg(10).isoformat(), "stop": seg(14).isoformat(), "duration_ms": 4000}
    assert b["stop"] is None and b["status"] == "RUNNING" and b["duration_ms"] >= 0        # en curso: hasta ahora


async def test_filtra_por_estado(cli):
    cli.list_executions.return_value = {"executions": []}
    await sfn.list_executions(status="FAILED", limit=50)
    assert cli.list_executions.call_args.kwargs["statusFilter"] == "FAILED"


async def test_si_la_maquina_no_existe_aun_devuelve_lista_vacia_con_nota(cli):
    """Foco de revisión 2: LocalStack recién arrancado."""
    cli.list_executions.side_effect = _err("StateMachineDoesNotExist")
    res = await sfn.list_executions()
    assert res["executions"] == [] and "no existe" in res["note"]


async def test_otros_errores_de_aws_se_propagan(cli):
    cli.list_executions.side_effect = _err("ThrottlingException")
    with pytest.raises(ClientError):
        await sfn.list_executions()


# ── execution_timeline ──────────────────────────────────────────────────────
async def test_linea_de_tiempo_de_una_ejecucion_feliz(cli):
    historial(cli, [
        entra(1, 0, "FaseA"), programada(2, 0), sale(3, 5, "FaseA"),
        entra(4, 5, "EsperarDocente"), programada(5, 5), sale(6, 25, "EsperarDocente"),
        entra(7, 25, "Finalizar"), sale(8, 26, "Finalizar"),
    ], stop=seg(26))
    res = await sfn.execution_timeline("s1")
    assert res["status"] == "SUCCEEDED" and res["stop"] == seg(26).isoformat()
    assert [(s["name"], s["duration_ms"], s["status"]) for s in res["steps"]] == [
        ("FaseA", 5000, "ok"), ("EsperarDocente", 20000, "ok"), ("Finalizar", 1000, "ok")]
    assert all(s["retries"] == 0 and s["attempt"] == 1 and s["error"] is None for s in res["steps"])


async def test_una_falla_queda_en_el_paso_con_error_y_causa(cli):
    historial(cli, [
        entra(1, 0, "FaseB"), programada(2, 0),
        {"id": 3, "type": "TaskTimedOut", "timestamp": seg(30), "taskTimedOutEventDetails": {"error": "States.Timeout", "cause": "x" * 500}},
        sale(4, 30, "FaseB"),
    ], status="FAILED", stop=seg(31))
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "failed" and paso["error"] == "States.Timeout"
    assert len(paso["cause"]) == 300                       # la causa se recorta


async def test_un_reintento_se_cuenta_por_tareas_programadas(cli):
    historial(cli, [entra(1, 0, "FaseA"), programada(2, 0), programada(3, 130), sale(4, 140, "FaseA")])
    assert (await sfn.execution_timeline("s1"))["steps"][0]["retries"] == 1


async def test_el_mismo_estado_repetido_numera_los_intentos(cli):
    historial(cli, [
        entra(1, 0, "FaseA"), sale(2, 5, "FaseA"), entra(3, 6, "EsperarDocente"), sale(4, 9, "EsperarDocente"),
        entra(5, 9, "FaseA"), sale(6, 14, "FaseA"),
    ])
    pasos = (await sfn.execution_timeline("s1"))["steps"]
    assert [(p["name"], p["attempt"]) for p in pasos] == [("FaseA", 1), ("EsperarDocente", 1), ("FaseA", 2)]


async def test_un_paso_abierto_figura_en_curso(cli):
    historial(cli, [entra(1, 0, "EsperarDocente"), programada(2, 0)], status="RUNNING")
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "running" and paso["end"] is None and paso["duration_ms"] >= 0


async def test_una_ejecucion_abortada_marca_el_paso(cli):
    historial(cli, [entra(1, 0, "EsperarDocente"), {"id": 2, "type": "ExecutionAborted", "timestamp": seg(9),
                                                     "executionAbortedEventDetails": {"error": "", "cause": "Cancelada por el docente"}}],
              status="ABORTED", stop=seg(9))
    assert (await sfn.execution_timeline("s1"))["steps"][0]["status"] == "aborted"


async def test_una_ejecucion_inexistente_lanza_execution_not_found(cli):
    """Foco de revisión 3: nombre que no existe → 404 en la API."""
    cli.describe_execution.side_effect = _err("ExecutionDoesNotExist")
    with pytest.raises(sfn.ExecutionNotFound):
        await sfn.execution_timeline("no-existe")


async def test_el_arn_de_la_ejecucion_se_deriva_del_de_la_maquina(cli):
    historial(cli, [])
    await sfn.execution_timeline("s1")
    assert cli.describe_execution.call_args.kwargs["executionArn"] == "arn:aws:states:us-east-1:000000000000:execution:prisma-flujo:s1"


async def test_es_solo_lectura(cli):
    historial(cli, [])
    cli.list_executions.return_value = {"executions": []}
    await sfn.execution_timeline("s1")
    await sfn.list_executions()
    for prohibido in ("start_execution", "stop_execution", "send_task_success", "send_task_failure", "delete_state_machine"):
        assert not getattr(cli, prohibido).called, prohibido


# ── correcciones de la revisión final ───────────────────────────────────────
async def test_pide_los_ultimos_200_eventos_en_orden_inverso_y_sin_datos_de_entrada(cli):
    """I4: la especificación dice «los últimos 200 eventos»; M6: no se descargan entradas y salidas."""
    historial(cli, [entra(1, 0, "FaseA"), sale(2, 5, "FaseA")])
    await sfn.execution_timeline("s1")
    kw = cli.get_execution_history.call_args.kwargs
    assert kw["reverseOrder"] is True and kw["maxResults"] == 200 and kw["includeExecutionData"] is False


async def test_un_paso_fail_sin_salida_se_cierra_con_la_ejecucion_terminada(cli):
    """I1: los estados Fail no emiten StateExited; no deben quedar «en curso» para siempre."""
    historial(cli, [entra(1, 0, "Falla", "Fail")], status="FAILED", stop=seg(5))
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "failed" and paso["end"] == seg(5).isoformat() and paso["duration_ms"] == 5000


async def test_una_ejecucion_abortada_cierra_el_paso_abierto_en_su_fecha_de_termino(cli):
    historial(cli, [entra(1, 0, "EsperarDocente")], status="ABORTED", stop=seg(9))
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "aborted" and paso["duration_ms"] == 9000 and paso["end"] == seg(9).isoformat()


async def test_una_ejecucion_en_curso_mantiene_el_paso_abierto(cli):
    historial(cli, [entra(1, 0, "EsperarDocente")], status="RUNNING")
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "running" and paso["end"] is None


async def test_un_paso_que_fallo_y_se_recupero_con_un_reintento_queda_ok(cli):
    """I2: HeartbeatTimeout -> Retry -> el segundo intento funciona; el paso no debe quedar en rojo."""
    historial(cli, [
        entra(1, 0, "FaseA"), programada(2, 0),
        {"id": 3, "type": "TaskTimedOut", "timestamp": seg(120), "taskTimedOutEventDetails": {"error": "States.HeartbeatTimeout", "cause": ""}},
        programada(4, 125),
        {"id": 5, "type": "TaskSucceeded", "timestamp": seg(130)},
        sale(6, 130, "FaseA"),
    ])
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "ok" and paso["error"] is None and paso["cause"] is None and paso["retries"] == 1


async def test_un_paso_cuyo_ultimo_intento_falla_sigue_en_rojo(cli):
    historial(cli, [
        entra(1, 0, "FaseA"), programada(2, 0),
        {"id": 3, "type": "TaskTimedOut", "timestamp": seg(120), "taskTimedOutEventDetails": {"error": "States.HeartbeatTimeout", "cause": ""}},
        programada(4, 125),
        {"id": 5, "type": "TaskTimedOut", "timestamp": seg(245), "taskTimedOutEventDetails": {"error": "States.HeartbeatTimeout", "cause": ""}},
        sale(6, 245, "FaseA"),
    ], status="FAILED", stop=seg(246))
    paso = (await sfn.execution_timeline("s1"))["steps"][0]
    assert paso["status"] == "failed" and paso["error"] == "States.HeartbeatTimeout" and paso["retries"] == 1


def test_el_cliente_de_step_functions_tiene_tiempos_cortos(monkeypatch):
    """I3: sin esto boto3 espera 60 s y reintenta, y la página se queda colgada."""
    visto = {}
    monkeypatch.setattr(sfn.boto3, "client", lambda nombre, **kw: visto.update(nombre=nombre, **kw))
    sfn._client()
    assert visto["nombre"] == "stepfunctions" and visto["config"].read_timeout <= 5 and visto["config"].connect_timeout <= 3
