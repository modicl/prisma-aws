"""Rutas: respuestas, validación de parámetros y que una dependencia caída no produzca 5xx."""
import pytest
from fastapi.testclient import TestClient

from app import kafka_view, main, queues, sfn

client = TestClient(main.app)


def test_health_no_consulta_dependencias(monkeypatch):
    async def explota(*a, **k):
        raise AssertionError("no debe consultar nada")
    monkeypatch.setattr(sfn, "list_executions", explota)
    assert client.get("/health").json() == {"status": "ok"}


def test_executions_devuelve_la_lista_y_pasa_los_parametros(monkeypatch):
    visto = {}

    async def falso(status=None, limit=50):
        visto.update(status=status, limit=limit)
        return {"executions": [{"name": "s1"}]}

    monkeypatch.setattr(sfn, "list_executions", falso)
    res = client.get("/api/executions?status=FAILED&limit=7")
    assert res.status_code == 200 and res.json() == {"available": True, "executions": [{"name": "s1"}]}
    assert visto == {"status": "FAILED", "limit": 7}


def test_executions_usa_50_por_defecto(monkeypatch):
    visto = {}

    async def falso(status=None, limit=50):
        visto["limit"] = limit
        return {"executions": []}

    monkeypatch.setattr(sfn, "list_executions", falso)
    client.get("/api/executions")
    assert visto["limit"] == 50


@pytest.mark.parametrize("query", ["status=OTRO", "limit=0", "limit=201", "limit=abc"])
def test_parametros_invalidos_dan_422(query):
    assert client.get(f"/api/executions?{query}").status_code == 422


def test_una_dependencia_caida_no_produce_5xx(monkeypatch):
    """Foco de revisión 1."""
    async def cae(*a, **k):
        raise ConnectionError("sin red")

    monkeypatch.setattr(sfn, "list_executions", cae)
    monkeypatch.setattr(queues, "list_queues", cae)
    monkeypatch.setattr(kafka_view, "kafka_overview", cae)
    for ruta in ("/api/executions", "/api/queues", "/api/kafka"):
        res = client.get(ruta)
        assert res.status_code == 200, ruta
        assert res.json() == {"available": False, "error": "ConnectionError"}, ruta


def test_el_detalle_de_una_ejecucion(monkeypatch):
    async def falso(nombre):
        return {"name": nombre, "steps": []}

    monkeypatch.setattr(sfn, "execution_timeline", falso)
    assert client.get("/api/executions/s1").json() == {"available": True, "name": "s1", "steps": []}


def test_una_ejecucion_inexistente_da_404(monkeypatch):
    """Foco de revisión 3."""
    async def no_existe(nombre):
        raise sfn.ExecutionNotFound(nombre)

    monkeypatch.setattr(sfn, "execution_timeline", no_existe)
    assert client.get("/api/executions/no-existe").status_code == 404


@pytest.mark.parametrize("nombre", ["a b", "x" * 81, "a%3Bb"])
def test_nombre_de_ejecucion_con_caracteres_raros_da_422(nombre):
    assert client.get(f"/api/executions/{nombre}").status_code == 422


def test_queues_y_kafka_devuelven_su_contenido(monkeypatch):
    async def colas():
        return {"queues": []}

    async def kafka():
        return {"topics": [], "groups": []}

    monkeypatch.setattr(queues, "list_queues", colas)
    monkeypatch.setattr(kafka_view, "kafka_overview", kafka)
    assert client.get("/api/queues").json() == {"available": True, "queues": []}
    assert client.get("/api/kafka").json() == {"available": True, "topics": [], "groups": []}


def test_el_detalle_tiene_tope_de_tiempo(monkeypatch):
    """I3: la ruta de detalle no pasa por guarded; igual no puede colgarse."""
    import asyncio

    async def lenta(nombre):
        await asyncio.sleep(10)

    monkeypatch.setattr(sfn, "execution_timeline", lenta)
    monkeypatch.setattr(main, "TIMEOUT_SECONDS", 0.05)
    assert client.get("/api/executions/s1").json() == {"available": False, "error": "TimeoutError"}


def test_el_detalle_con_la_dependencia_caida_no_produce_5xx(monkeypatch):
    async def cae(nombre):
        raise ConnectionError("sin red")

    monkeypatch.setattr(sfn, "execution_timeline", cae)
    res = client.get("/api/executions/s1")
    assert res.status_code == 200 and res.json() == {"available": False, "error": "ConnectionError"}
