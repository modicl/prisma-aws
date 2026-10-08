# Panel de desarrollo (`devtools`): Step Functions, colas SQS y Kafka — especificación

**Fecha:** 2026-10-02 · **Estado:** pendiente de revisión del dueño
**Ubicación del código:** `prisma-aws/local/devtools/` (raíz sin repo, igual que `docker-compose.yml` y `local/`).

## 1. Propósito

Que quien desarrolla vea, mientras prueba en local, qué está haciendo el flujo orquestado por Step Functions y cómo van las colas, sin
correr comandos `awslocal` a mano. Es una herramienta **solo de desarrollo**: no tiene login, no se despliega y no existe en AWS.

Éxito: al lanzar un flujo (desde el front o con `sfn_e2e.py`), en menos de 5 segundos aparece en la página, se puede abrir su
línea de tiempo paso a paso y se ve el estado de `prisma-flujo`, su DLQ y el tópico de Kafka.

## 2. Alcance

**Entra**
1. **Ejecuciones de Step Functions** de la máquina `prisma-flujo`: lista con estado, inicio, fin y duración; detalle por ejecución con cada estado
   recorrido (nombre, tipo, duración, error y causa si falló, y reintentos).
2. **Colas SQS**: todas las colas de LocalStack con mensajes pendientes, en vuelo y retrasados, y la DLQ a la que apuntan.
3. **Kafka**: todos los tópicos (menos los internos `__*`) con particiones y cantidad de mensajes, y el retraso (lag) de cada grupo de consumidores.

**No entra** (decisión deliberada)
- Leer el **contenido** de mensajes de SQS o Kafka. Leer de SQS altera la visibilidad de los mensajes y falsea lo que se observa; los eventos de Kafka
  ya están diseñados para no llevar PII, pero un visor de payloads no es el objetivo.
- Acciones: reencolar, purgar, detener ejecuciones, crear tópicos. Es solo lectura.
- Autenticación, persistencia de histórico, alertas, DynamoDB y S3 (se pueden agregar después como otro panel).

## 3. Arquitectura

```
navegador ──► devtools :3030 (127.0.0.1) ──► LocalStack :4566  (Step Functions + SQS, boto3)
                (FastAPI + página estática) ──► Kafka :9092       (aiokafka: admin + offsets)
```

- **Un servicio Python** (FastAPI + uvicorn + boto3 + aiokafka), el mismo stack que `prisma-monitor`. Sirve la API `/api/*` y la página `web/index.html`.
- **Una página HTML sin paso de build**, con CSS y JS propios, que consulta `/api/*` cada 3 s (botón de pausa).
- Compose: servicio `devtools` publicado como `127.0.0.1:3030:3030`, `depends_on` LocalStack y Kafka sanos. Siempre disponible en el compose local (que ya es solo de
  desarrollo); **sin** pipeline de ECR ni Terraform, por lo que no puede llegar a AWS.

### Estructura de archivos

| Archivo | Responsabilidad |
|---|---|
| `local/devtools/Dockerfile`, `requirements.txt` | Imagen `python:3.12-slim`; `fastapi`, `uvicorn`, `boto3`, `aiokafka` con las mismas versiones que `prisma-monitor`. |
| `local/devtools/app/main.py` | App FastAPI: rutas `/api/*`, `/health` y archivos estáticos. |
| `local/devtools/app/sfn.py` | Consultas de Step Functions: `list_executions`, `execution_timeline`. |
| `local/devtools/app/queues.py` | Consulta de SQS: `list_queues`. |
| `local/devtools/app/kafka_view.py` | Consulta de Kafka: `list_topics`, `consumer_lags`. |
| `local/devtools/web/index.html` | La página (tres paneles). |
| `local/devtools/tests/` | Pruebas con clientes simulados. |

## 4. API

Todas devuelven JSON y **nunca** un 5xx por una dependencia caída: responden `200` con `{"available": false, "error": "<clase de la excepción>"}` para que la página
marque solo ese panel.

| Ruta | Respuesta (cuando `available: true`) |
|---|---|
| `GET /api/executions?status=<opcional>&limit=50` | `{executions: [{name, status, start, stop, duration_ms}]}` ordenadas de la más nueva a la más antigua. `name` es el `session_id`. |
| `GET /api/executions/{name}` | `{name, status, start, stop, steps: [{name, type, start, duration_ms, error, cause, attempt}]}` armado desde `get_execution_history`. |
| `GET /api/queues` | `{queues: [{name, visible, in_flight, delayed, dlq: <nombre o null>, is_dlq: bool}]}`. |
| `GET /api/kafka` | `{topics: [{name, partitions, messages}], groups: [{group, topic, lag}]}`. |
| `GET /health` | `{"status": "ok"}` (no consulta dependencias). |

Reglas de cada consulta:
- **Línea de tiempo.** Cada estado se arma a partir de los eventos `...StateEntered` / `...StateExited`; la duración es la diferencia de marcas de tiempo. Un estado todavía abierto
  muestra su duración hasta ahora y `en curso`. Los reintentos se cuentan por repetición del mismo estado dentro de la ejecución. `error` y `cause` salen de los eventos
  `TaskFailed`, `TaskTimedOut` o `ExecutionFailed` (el historial de esta máquina no lleva PII por diseño; igual se recorta `cause` a 300 caracteres).
- **Colas.** Atributos `ApproximateNumberOfMessages`, `...NotVisible` y `...Delayed`. La DLQ se identifica por la `RedrivePolicy` de la cola principal; una cola es DLQ si otra apunta a ella.
- **Kafka.** `messages` = suma de `(fin − inicio)` por partición (offsets de `beginning` y `end`). `lag` = `fin − offset confirmado` del grupo, sumado por partición; un grupo sin
  offsets confirmados no está asociado a ningún tópico y se muestra con tópico y lag vacíos (`—`).
- Parámetros: `limit` entre 1 y 200 (por defecto 50); `status` solo acepta los valores de Step Functions, otro valor responde `422`.

## 5. Página

Un solo `index.html`, tres paneles apilados en pantalla angosta y en columnas en pantalla ancha; tema claro y oscuro por `prefers-color-scheme`.

1. **Ejecuciones:** tabla con una pastilla de estado (verde `SUCCEEDED`, azul `RUNNING`, rojo `FAILED`, gris `ABORTED`). Clic en una fila abre el detalle debajo: una línea de tiempo con una barra
   por estado, proporcional a su duración, y el error visible si lo hubo. Un filtro por estado.
2. **Colas:** una tarjeta por cola con los tres contadores; la DLQ con mensajes se muestra resaltada. Si no hay colas, dice «sin colas».
3. **Kafka:** tabla de tópicos (particiones, mensajes) y tabla de grupos (tópico, lag).

Cada panel muestra la hora de la última actualización y, si su dependencia no responde, el aviso «no disponible» con el motivo, sin afectar a los otros.

## 6. Configuración (variables de entorno del contenedor)

`AWS_ENDPOINT_URL=http://localstack:4566`, `AWS_DEFAULT_REGION=us-east-1`, `AWS_ACCESS_KEY_ID/SECRET=test` (falsas, como el resto del compose), `SFN_STATE_MACHINE_ARN`
(el mismo que usa el workflow), `KAFKA_BOOTSTRAP_SERVERS=kafka:9092`, `PORT=3030`.

## 7. Errores y límites

- LocalStack caído → paneles de Step Functions y SQS en «no disponible»; Kafka caído → solo el de Kafka. El servicio no se cae ni se reinicia.
- La máquina de estados aún no existe (LocalStack recién arrancado) → lista vacía con el aviso «la máquina `prisma-flujo` no existe todavía».
- Cada consulta tiene un tiempo máximo de 5 s; si lo supera, el panel muestra «no disponible: Timeout».
- Una ejecución con historial muy largo se recorta a los últimos 200 eventos.
- Solo escucha en `127.0.0.1` del anfitrión: no queda expuesto a la red local.

## 8. Pruebas

- **Unitarias** (pytest, clientes `boto3` y Kafka simulados): línea de tiempo con una ejecución feliz, una con falla, una con reintento y una en curso; conteo de colas y detección de la DLQ;
  cálculo de mensajes y lag de Kafka (incluido grupo sin offsets); respuesta `available: false` ante cada excepción; validación de `status` y `limit`.
- **Integración manual contra el compose real:** levantar `devtools`, correr `sfn_e2e.py` y comprobar que las ejecuciones aparecen con sus estados, que `prisma-flujo` y su DLQ se listan y que el
  tópico `prisma.agent-events` muestra mensajes y el lag del archivador.

## 9. Riesgos y decisiones abiertas

- **Offsets de Kafka con aiokafka (resuelto el 2026-10-02):** `aiokafka` 0.14.0 expone `list_consumer_groups`, `list_consumer_group_offsets` y `describe_topics`; los offsets de inicio y fin
  se leen con un consumidor sin grupo. Verificado contra el Kafka del compose; no hace falta `kafka-python`.
- **Costo de consulta:** cada refresco son varias llamadas a LocalStack por ejecución visible; con `limit=50` es aceptable en local. No hay caché.
- **Fuera de alcance, para después:** un panel de DynamoDB (sesiones, cupos) y de S3 (`jobs/`, `state/`) usando el mismo servicio.
