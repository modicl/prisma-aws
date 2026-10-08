# Monitor en vivo del workflow agéntico con Apache Kafka — diseño

Estado: aprobado en conversación el 2026-09-29 (secciones 1–6). El dueño pidió pasar directo al plan
y a la implementación, sin revisión intermedia de este documento.

## Objetivo

Un monitor en vivo de lo que hace `prisma_workflow`: **flujos corriendo, tokens usados y costo
promedio**, apoyado en Apache Kafka. El workflow publica eventos; un microservicio nuevo
(`prisma-monitor`, FastAPI) los consume y sirve su propio front aislado. Los eventos además se
respaldan en una tabla log de Postgres para análisis posterior. El ramo exige implementar Kafka: el
éxito incluye poder demostrar el flujo de eventos de punta a punta.

## Decisiones ya tomadas

| Tema | Decisión |
|---|---|
| Kafka | `apache/kafka` en KRaft, un broker, solo local (compose). Producción queda fuera de alcance. |
| Front | App **aislada** servida por el propio monitor. No se toca `prisma-front`. |
| Tiempo real | Polling cada 3 s (mismo criterio que el chat; el API Gateway corta a los 30 s). |
| Acceso | Solo `SUPERADMIN`, validado **en el servicio** (el BFF no valida y ocultar el menú no protege). |
| Login | Mismo pool y mismo app client de Cognito, PKCE; requiere agregar la URL del monitor a `front_urls` de `prisma-infra`. |
| Estado | Métricas en memoria, reconstruidas releyendo el topic desde el inicio. Sin BD para las métricas. |
| Respaldo | Tabla log append-only en Postgres, llenada por un consumer group distinto. |
| Stack monitor | Python + FastAPI + `aiokafka`. |

## Arquitectura

```
prisma_workflow ─(aiokafka, sin esperar)─► Kafka topic prisma.agent-events (3 particiones, 7 días)
                                             ├─ consumer "sin grupo", lee desde el inicio ─► agregador en memoria ─► GET /api/monitor/metrics
                                             └─ consumer group prisma-monitor-archiver ────► Postgres monitor.agent_event_log
navegador ── login Cognito (PKCE) ── polling 3 s ──► prisma-monitor :3020 (sirve también el front)
```

Repos tocados: `prisma-monitor` (nuevo), `prisma_workflow` (publicador + enganches + `aiokafka`),
`docker-compose.yml` y `local/` de la raíz. `prisma-infra`: solo instrucción (Cognito real).
`prisma-front`: no se toca.

## Contrato de eventos (v1)

Topic `prisma.agent-events`. Clave del mensaje = `session_id`. Valor = JSON UTF-8:
`{ "v": 1, "type": ..., "session_id": ..., "ts": ISO-8601 UTC ms, ...campos }`.

| `type` | Campos | Origen |
|---|---|---|
| `flow_started` | — | `workflow_runner`, antes de `run_workflow` |
| `agent_started` | `agent` | `agent._run_with_timeout` |
| `agent_finished` | `agent`, `duration_ms`, `ok` | `agent._run_with_timeout` |
| `agent_retry` | `agent`, `attempt`, `reason` (`timeout`\|`503`) | `agent._run_with_timeout` |
| `llm_usage` | `agent`, `model`, `input_tokens`, `cached_tokens`, `output_tokens`, `thoughts_tokens` | eventos ADK con `usage_metadata` y no parciales |
| `hitl_required` | `attempt` | `hitl_callback` |
| `flow_finished` | `status`, `duration_ms` | `finally` de `run_workflow_for_api` |

`status` ∈ `success`, `degraded`, `compliance_blocked`, `hitl_rejected`, `cancelled`, `error`.

Reglas:
- **Sin PII**: nada de texto de prompts, contenido del PACI, nombres, RUT, nombres de archivo ni correo.
- Los eventos llevan **tokens, no costo**. El costo se calcula en el monitor con `MODEL_PRICES`.
- `input_tokens` = `prompt_token_count` (incluye los tokens en caché); `cached_tokens` = `cached_content_token_count`
  (subconjunto); `output_tokens` = `candidates_token_count`; `thoughts_tokens` = `thoughts_token_count`.
- Las sesiones mock (`school_id` que empieza con `__mock`) no publican.
- En modo CLI (sin `api_session_id`) todo es no-op.

## Publicador (`prisma_workflow/prisma_agents/api/event_publisher.py`)

`publish(type, session_id, **campos)` es síncrono: solo encola. Una tarea en segundo plano envía con
`AIOKafkaProducer` (idempotente, `acks=all`). Cola acotada (1000): si se llena o Kafka falla, descarta y
deja un aviso; **nunca lanza ni bloquea**. Desactivado si `KAFKA_BOOTSTRAP_SERVERS` está vacío o si no hay
event loop en ejecución. `flow_finished` se publica solo si el flujo llegó a empezar (bandera `started`).

## Monitor (`prisma-monitor`)

- `settings.py` — configuración por entorno.
- `auth.py` — guard de Cognito: RS256, `iss`, `aud`, `token_use=id`, rol por lista blanca de `cognito:groups`;
  401 sin token/inválido, 403 si el rol no es `SUPERADMIN` (incluye `PENDING`).
- `aggregator.py` — pura y determinista. Estados: `running`, `awaiting_hitl`, `finished`; `stalled` es derivado
  (sin eventos por más de `STALL_MINUTES`, 30). Crea el flujo con el primer evento aunque falte `flow_started`.
  Descarta flujos más viejos que `RETENTION_HOURS` (168).
- `pricing.py` — `MODEL_PRICES` (USD por millón de tokens: `input`, `cached_input` opcional, `output`).
  Costo = `(input − cached) × input + cached × cached_input + (output + thoughts) × output`.
  Modelo sin tarifa ⇒ costo `null` ("sin tarifa"), nunca 0. Sin `cached_input` se cobra a tarifa de entrada.
- `consumer.py` — asigna todas las particiones, `seek_to_beginning`, sin grupo ni commits; reconecta con espera
  creciente; expone `replaying` hasta alcanzar los offsets finales iniciales.
- `archiver.py` + `store.py` — consumer group `prisma-monitor-archiver`; lotes de hasta 200 mensajes o 1 s;
  `INSERT … ON CONFLICT DO NOTHING`; commit del offset **solo después** de guardar; ante error de BD cierra el
  consumer, espera y reabre (retoma desde el último commit).
- `main.py` — `/health`, `/config.json`, `/api/monitor/metrics`, front estático, cabeceras de seguridad.

### Tabla log

`monitor.agent_event_log` (esquema propio, creada al arrancar con DDL idempotente): `id`, `kafka_topic`,
`kafka_partition`, `kafka_offset` (`UNIQUE` los tres), `event_ts`, `received_at`, `schema_version`, `event_type`,
`session_id` (text), `agent`, `model`, `input_tokens`, `cached_tokens`, `output_tokens`, `thoughts_tokens`,
`duration_ms`, `status`, `attempt`, `payload` (jsonb). Append-only. Vista `monitor.v_flow_summary` (una fila por flujo).
Base configurable con `MONITOR_DATABASE_URL` (por defecto `prisma_monitor`; puede apuntar a `BD_LOGS`).

### Endpoint

`GET /api/monitor/metrics` → `generated_at`, `replaying`, `flows` (`running`, `awaiting_hitl`, `stalled`,
`finished`, `by_status`), `tokens` (`input`, `cached`, `output`, `thoughts`, `total`), `cost` (`total`,
`avg_per_flow`, `priced_flows`, `unpriced_flows`, `unpriced_models`, `cached_at_input_rate`), `avg`
(`duration_ms`, `tokens_per_flow`), `by_agent`, `recent` (20 flujos, id corto de 8 caracteres),
`archive` (`enabled`, `ok`, `saved_total`, `last_saved_at`, `last_error`), `skipped_events`.

## Front (`prisma-monitor/web/`)

HTML + CSS + JS plano, sin build ni dependencias. PKCE contra el Hosted UI (`/oauth2/authorize`,
`/oauth2/token`), token en `sessionStorage`, 401 ⇒ nuevo login, 403 ⇒ «Acceso restringido a SUPERADMIN».
Todo se pinta con `textContent`. CSP: solo el propio origen y el dominio de Cognito. Textos en español.

## Infra local

Servicios `kafka` (`apache/kafka`, KRaft), `kafka-init` (crea el topic) y `monitor` (:3020) en el compose. El
workflow recibe `KAFKA_BOOTSTRAP_SERVERS` pero no depende de Kafka. `local/postgres/init.sql` agrega
`prisma_monitor` (en un volumen ya existente hay que crearla a mano una vez).

## Fallas

| Falla | Comportamiento |
|---|---|
| Kafka caído | El workflow descarta eventos con aviso y sigue |
| Kafka vuelve | El productor y los consumers se reconectan |
| BD caída | El archivador reintenta sin confirmar offsets; las métricas siguen |
| Monitor reinicia | Reconstruye leyendo el topic |
| Evento de tipo/versión desconocidos | Se omite y se cuenta en `skipped_events` |
| Modelo sin tarifa | Costo «sin tarifa» |

## Fuera de alcance

Kafka en producción (MSK/ECS), CI/ECR del monitor, métricas desde SQL, alertas, múltiples brokers.

## Riesgos abiertos (se verifican durante la implementación)

1. Que un mismo consumo de tokens no aparezca en más de un evento ADK (se ignoran los parciales; se compara la
   suma contra `total_token_count` en una corrida real).
2. Que `aiokafka` compile en la imagen Alpine del workflow.
3. Las tarifas de `gemini-3.1-flash-lite` no se conocen aquí: las completa el dueño en `MODEL_PRICES`.
