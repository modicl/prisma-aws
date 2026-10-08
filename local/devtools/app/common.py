"""Utilidades comunes: toda consulta a una dependencia (LocalStack, Kafka) pasa por `guarded`."""
import asyncio

TIMEOUT_SECONDS = 5.0


async def guarded(fn, *args, timeout: float = TIMEOUT_SECONDS) -> dict:
    """Ejecuta `await fn(*args)` (debe devolver un dict) con tope de tiempo.

    Una dependencia caída o lenta no debe tumbar la API: se responde 200 con `available: false` y la clase de la
    excepción (nunca su texto, que podría traer direcciones internas), y la página marca solo ese panel.
    """
    try:
        data = await asyncio.wait_for(fn(*args), timeout)
        return {"available": True, **data}
    except Exception as exc:  # noqa: BLE001 — cualquier fallo de la dependencia es "no disponible"
        return {"available": False, "error": type(exc).__name__}
