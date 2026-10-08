"""guarded(): una dependencia caída o lenta nunca rompe la API; solo marca su panel como no disponible."""
import asyncio

import pytest

from app.common import TIMEOUT_SECONDS, guarded


async def bien():
    return {"items": [1, 2]}


async def falla():
    raise ConnectionError("localstack caido")


async def lenta():
    await asyncio.sleep(10)
    return {"items": []}


async def con_args(a, b):
    return {"suma": a + b}


async def test_respuesta_correcta_marca_available():
    assert await guarded(bien) == {"available": True, "items": [1, 2]}


async def test_pasa_los_argumentos():
    assert await guarded(con_args, 2, 3) == {"available": True, "suma": 5}


async def test_una_excepcion_se_convierte_en_no_disponible_con_la_clase():
    """Foco de revisión 1: LocalStack o Kafka caídos."""
    assert await guarded(falla) == {"available": False, "error": "ConnectionError"}


async def test_el_tope_de_tiempo_responde_timeout():
    res = await guarded(lenta, timeout=0.05)
    assert res == {"available": False, "error": "TimeoutError"}


def test_el_tope_por_defecto_es_de_5_segundos():
    assert TIMEOUT_SECONDS == 5.0
