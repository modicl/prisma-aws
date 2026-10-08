# Plan de implementación — monitor en vivo con Kafka

Spec: `docs/superpowers/specs/2026-09-29-prisma-monitor-kafka-design.md`. TDD: cada módulo con su test primero.
No se commitea nada (regla del workspace); los archivos quedan en el árbol de trabajo.

Los tests se corren dentro de Docker con el código montado (mismo Python 3.12 que las imágenes):

```bash
# workflow (usa la imagen ya construida, que trae pytest)
docker run --rm -v "$PWD/prisma_workflow:/w" -w /w prisma-workflow python -m pytest prisma_agents/tests/api/test_event_publisher.py -q
# monitor
docker run --rm -v "$PWD/prisma-monitor:/m" -w /m python:3.12-slim sh -c "pip -q install -r requirements.txt && python -m pytest -q"
```

## Fase A — `prisma-monitor` (repo nuevo)

| # | Archivo | Test | Notas |
|---|---|---|---|
| A1 | `monitor/pricing.py` | `tests/test_pricing.py` | costo con caché y razonamiento; sin tarifa ⇒ `None`; caché a tarifa de entrada |
| A2 | `monitor/aggregator.py` | `tests/test_aggregator.py` | corriendo/esperando/detenido/terminado; flujo sin `flow_started`; repetidos; poda; `skipped` |
| A3 | `monitor/auth.py` | `tests/test_auth.py` | 401/403/200; PENDING y grupo del IdP no cuentan; RSA de prueba |
| A4 | `monitor/store.py` (`event_to_row`, DDL, insert) | `tests/test_store.py` | fila pura sin BD; inserción idempotente contra Postgres si hay `TEST_DATABASE_URL` |
| A5 | `monitor/consumer.py`, `monitor/archiver.py` | `tests/test_consumer.py`, `tests/test_archiver.py` | con consumers falsos: `replaying`, reintento, commit después de guardar |
| A6 | `monitor/main.py`, `monitor/settings.py` | `tests/test_main.py` | rutas, 401/403, cabeceras, `/config.json` |
| A7 | `web/index.html`, `web/app.js`, `web/style.css` | revisión manual + `node --check` | PKCE, polling, `textContent` |
| A8 | `Dockerfile`, `requirements.txt`, `.dockerignore`, `.gitignore`, `.env.example`, `CLAUDE.md`, `scripts/emit_sample_events.py` | — | |

## Fase B — `prisma_workflow`

| # | Cambio | Test |
|---|---|---|
| B1 | `api/event_publisher.py` | `tests/api/test_event_publisher.py`: desactivado, sin loop, cola llena, error de Kafka, no lanza, orden, cierre |
| B2 | `agent.py`: `_emit_monitor_event` + enganches en `_run_with_timeout` | `tests/test_monitor_events_agent.py`: started/finished/retry/usage; parciales ignorados; CLI no-op |
| B3 | `api/workflow_runner.py`: `flow_started`, `hitl_required`, `flow_finished` | ampliar `tests/api/test_workflow_runner.py` o archivo nuevo: `cancelled`, bandera `started`, mock no publica |
| B4 | `requirements.txt`: `aiokafka==0.14.0` (autorizado por el dueño) | build de la imagen |

## Fase C — Compose y docs

| # | Cambio |
|---|---|
| C1 | `docker-compose.yml`: `kafka`, `kafka-init`, `monitor`; variables del workflow |
| C2 | `local/postgres/init.sql`: `prisma_monitor` |
| C3 | `.env.example`: `MODEL_PRICES`, `KAFKA_*`, `MONITOR_*` |
| C4 | `LOCAL.md` y `CLAUDE.md` (raíz): sección del monitor y `prisma-infra` (URL de callback) |

## Fase D — Verificación

1. `docker compose up -d --build` y comprobar `kafka`, `kafka-init`, `monitor` sanos.
2. `scripts/emit_sample_events.py`: eventos sintéticos ⇒ el panel y la tabla log reflejan lo emitido; comparar conteos.
3. Reiniciar `monitor` ⇒ mismas métricas (replay). Apagar Postgres ⇒ métricas siguen, el archivador reintenta; encender ⇒ se pone al día sin duplicar.
4. Apagar `kafka` con el workflow corriendo ⇒ el workflow no falla.
5. Sin token ⇒ 401; con token de no-SUPERADMIN ⇒ 403 (a nivel de test; el login real lo prueba el dueño).
6. Corrida real con la key de Gemini: suma de `llm_usage` vs `total_token_count`.
7. Suite completa del workflow (`pytest`) sin regresiones.
