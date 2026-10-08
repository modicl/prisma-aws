# Entorno local de P.R.I.S.M.A.

Levanta **todo el sistema actual** (front, BFF, 4 microservicios, workflow, Postgres y AWS emulado)
con un solo comando, más el **monitor en vivo con Kafka** (`prisma-monitor`, ver su sección abajo).

> Esto es solo para desarrollo. Producción corre en ECS/Fargate (`prisma-infra/`).

## Requisitos

- Docker Desktop corriendo (Compose v2).
- ~6 GB de RAM libres para Docker.
- Internet: el login usa el **Cognito real** y LocalStack baja la imagen de la Lambda la primera vez.
- Una API key de Google AI Studio para que los agentes respondan.

## Levantar

```bash
cd prisma-aws
cp .env.example .env          # y completa GOOGLE_API_KEY
docker compose up -d --build
```

La primera vez tarda varios minutos (compila 11 imágenes). Después, entra a
**http://localhost:3002**.

Orden de arranque (lo resuelve `depends_on`): Postgres → migraciones → microservicios → BFF → front.
LocalStack debe estar `healthy` antes que `ms-docs` y `workflow`.

## Puertos

| Puerto | Servicio | Nota |
|---|---|---|
| 3002 | front (nginx) | **Único que usa el navegador.** `/api` → BFF, `/chat` → workflow |
| 3010 | bff | |
| 3001 | ms-users | `/api/*` |
| 3000 | ms-docs | `/api/*` |
| 3005 | ms-perfil-alumno | **sin** prefijo `/api` |
| 3004 | adminpanel | Swagger en `/docs` |
| 8000 | prisma_workflow | `/health`, Swagger en `/docs` |
| 5432 | Postgres | usuario `prisma`, ver `POSTGRES_PASSWORD` |
| 4566 | LocalStack | S3 + DynamoDB + Lambda |
| 3020 | prisma-monitor | Monitor en vivo. **Solo `SUPERADMIN`.** |
| 9094 | Kafka | Solo para herramientas en tu máquina; los contenedores usan `kafka:9092` |

Cambia `POSTGRES_PORT` en `.env` si ya tienes otro Postgres en el 5432.

## Qué hay dentro

- **Postgres**: una base por servicio (`prisma_users`, `prisma_docs`, `prisma_perfil`,
  `prisma_adminpanel`). En producción es una sola con esquemas; aquí se separan para que cada
  `_prisma_migrations` sea independiente.
- **Migraciones**: contenedores `migrate-*` que corren y terminan. `adminpanel` usa `prisma db push`
  porque no tiene migración baseline.
- **LocalStack** (`local/localstack/init-aws.sh`) recrea lo de `prisma-infra/05-eventos.tf`: buckets
  `prisma-workflow` y `prisma-schools-repos`, tabla `chat_sessions`, y la Lambda `prisma-trigger`
  con su notificación S3 (prefijo `jobs/`). Usa el mismo `lambda/trigger_handler.py` del repo.
- Con `S3_BUCKET` y `DYNAMO_TABLE` seteados, el workflow corre el flujo **event-driven real**:
  `/chat/start` sube a S3 → LocalStack invoca la Lambda → la Lambda llama a `/chat/internal/run`.

## Login

Usa el pool que define Terraform (`us-east-1_eliG8OuZ8`); `http://localhost:3002/auth/callback` ya está
en sus callback URLs. Para cambiar de pool edita las tres variables `COGNITO_*` en `.env` y reconstruye
el front (los valores se hornean en el bundle):

```bash
docker compose build front && docker compose up -d front
```

Para que un usuario **pase los guards** necesita:

1. Pertenecer a un grupo `SUPERADMIN`, `ADMIN` o `TEACHER`. Sin grupo el rol es `PENDING` y todos
   los backends responden 403 «Cuenta pendiente de autorización».
2. Si no es `SUPERADMIN`, el atributo `custom:colegioId`; sin él `ms-docs` y `ms-perfil-alumno`
   responden 403.

Además, `prisma-infra` puede tener activa la allowlist de correos (Lambda `presignup`).

## Comandos útiles

```bash
docker compose ps -a                       # estado
docker compose logs -f workflow            # logs de un servicio
docker compose up -d --build ms-docs       # reconstruir uno solo
docker compose down                        # apagar (conserva la BD)
docker compose down -v                     # apagar y BORRAR la BD (re-corre init.sql y migraciones)
```

Probar la cadena S3 → Lambda → workflow sin pasar por el front:

```bash
SID=$(python -c "import uuid;print(uuid.uuid4())")
echo "%PDF-fake" > paci.pdf && docker cp paci.pdf prisma-localstack-1:/tmp/paci.pdf
docker exec prisma-localstack-1 awslocal s3 cp /tmp/paci.pdf s3://prisma-workflow/jobs/$SID/paci.pdf
docker compose logs workflow | grep $SID   # debe aparecer POST /chat/internal/run/<id>
```

## Monitor con Kafka

Muestra en vivo **flujos corriendo, tokens usados y costo promedio** del workflow agéntico.
Diseño: `docs/superpowers/specs/2026-09-29-prisma-monitor-kafka-design.md`.

```
prisma_workflow ──eventos──► Kafka (topic prisma.agent-events) ──► prisma-monitor ──► http://localhost:3020
                                          └──► consumer group "archiver" ──► Postgres  monitor.agent_event_log
```

**Abrirlo:** http://localhost:3020 → inicia sesión con Cognito. **Solo entra el grupo `SUPERADMIN`**; cualquier
otro rol (o sin grupo) recibe «Acceso restringido». El servidor lo exige, no solo el front.

**Antes de la primera vez:** agrega `http://localhost:3020` a `front_urls` en `prisma-infra/terraform.tfvars` y aplica
Terraform (o agrégalo a mano en la consola: Cognito → tu User Pool → App integration → App client → Hosted UI →
*Allowed callback URLs* `http://localhost:3020/auth/callback` y *Allowed sign-out URLs* `http://localhost:3020/login`).
Sin eso, Cognito rechaza el login del monitor con `redirect_mismatch`.

**Tarifas (obligatorio para ver costos):** el costo no se inventa. Completa `MODEL_PRICES` en `.env` con USD por millón
de tokens, luego `docker compose up -d monitor`:

```
MODEL_PRICES='{"gemini-3.1-flash-lite": {"input": X, "cached_input": Y, "output": Z}}'
```

Sin tarifa el panel muestra «sin tarifa», nunca 0. Al cambiarlas, todo el historial se recalcula al reiniciar el monitor.

**Ver algo sin gastar cuota de Gemini:**

```bash
docker compose run --rm monitor python scripts/emit_sample_events.py --flows 8 --pace 0.4   # o --live
```

Sus flujos llevan el prefijo `demo-` en el `session_id`. La tabla log es append-only: esos eventos no se borran de ella.

**Analizar el historial** (tabla log, sin límite de retención, a diferencia de Kafka que guarda 7 días):

```bash
docker exec -it prisma-postgres-1 psql -U prisma -d prisma_monitor
#   SELECT * FROM monitor.v_flow_summary ORDER BY started_at DESC LIMIT 20;
#   SELECT event_type, count(*) FROM monitor.agent_event_log GROUP BY 1;
```

Cosas que conviene saber:
- El workflow **no depende** de Kafka: si está caído descarta eventos con un aviso en el log y sigue. Los eventos
  no llevan PII, solo ids aleatorios, contadores y nombres de agente.
- Si el monitor se reinicia reconstruye todo releyendo el topic (`replaying` en el panel mientras lo hace).
- Si Postgres se cae, las métricas siguen; el archivador reintenta y se pone al día sin duplicar filas.
- Un flujo sin eventos por más de 30 min se muestra «Detenido» y no cuenta como corriendo (`STALL_MINUTES`).
- El costo lleva «≈» cuando hay tokens en caché y no definiste `cached_input`: se cobra a tarifa de entrada (cota superior).
- Kafka corre en un solo nodo sin réplicas: es para desarrollo, no para producción.
- Kafka en `apache/kafka:4.2.2` (KRaft, sin ZooKeeper); `KAFKA_TAG` en `.env` cambia la versión.

## Subida → SQS → Step Functions

LocalStack ahora levanta también `sqs` y `stepfunctions`. Al arrancar, `local/localstack/init-aws.sh` crea la cola `prisma-flujo` (+ DLQ), las Lambdas
`prisma-sfn-starter` y `prisma-sfn-invoker`, la máquina de estados `prisma-flujo` y la notificación S3 → SQS.

- `FLOW_MODE=sfn` (por defecto) usa el flujo nuevo. `FLOW_MODE=legacy docker compose up -d --force-recreate localstack` vuelve al camino antiguo
  (Lambda `prisma-trigger` → `/chat/internal/run`); sin la variable vuelve al nuevo.
- E2E con fases simuladas (sin gastar Gemini): `docker compose exec -T workflow python /e2e/sfn_e2e.py` → debe dar `9/9 escenarios OK`.
- `school_id` simulados: `__mock_ok__`, `__mock_blocked__`, `__mock_error__`, `__mock_degraded__`, `__mock_timeout__`, `__mock_dead__`.
- Ver una ejecución: `docker exec prisma-localstack-1 awslocal stepfunctions list-executions --state-machine-arn arn:aws:states:us-east-1:000000000000:stateMachine:prisma-flujo`.
- Límites de LocalStack: `StopExecution` no aborta una espera con token (queda `RUNNING`) y un heartbeat vencido se informa como `States.Timeout`.

## Panel de desarrollo (devtools)

**http://localhost:3030**: solo lectura y solo local (escucha únicamente en 127.0.0.1 y no existe en AWS).

- **Ejecuciones de Step Functions:** estado, inicio y duración; clic en una fila para ver su línea de tiempo (cada estado con su duración, reintentos y error).
- **Colas SQS:** pendientes, en vuelo y retrasados por cola, y a qué DLQ apunta cada una (la DLQ con mensajes se resalta).
- **Kafka:** tópicos con particiones y mensajes, y el lag de cada grupo de consumidores.
- No muestra el contenido de ningún mensaje. Se actualiza cada 3 s (botón «Pausar»). Si LocalStack o Kafka no responden, solo ese panel dice «no disponible».
- Código: `local/devtools/` (FastAPI + una página HTML). Pruebas: `docker compose run --rm --no-deps -T -v "$(pwd -W)/local/devtools:/srv" devtools python -m pytest -q`.

## Decisiones y trampas

- **LocalStack fijado en `4.14.0`.** Desde la serie `2026.x` la imagen `latest` exige un token de cuenta y
  se cae con `License activation failed`. La 4.x sigue siendo gratuita sin token. Si algún día
  actualizas, pon `LOCALSTACK_AUTH_TOKEN` en `.env`.
- **`local/front.Dockerfile`** existe porque `prisma-front/Dockerfile` pisa `.env.production` y deja el
  bundle sin las `VITE_COGNITO_*`; solo funcionaba porque `COPY . .` arrastraba tu `.env` local. Aquí
  entran por build-arg.
- **`.dockerignore`** nuevo en `bff-prisma`, `prisma-ms-users`, `prisma-ms-docs`, `prisma-adminpanel` y
  `prisma_workflow`. Sin él, `COPY . .` pisa el `node_modules` de Linux con el de Windows y Prisma se rompe.
- **Los `.env` de cada repo no se usan** en el compose (quedan fuera de la imagen). Varios apuntan aún al
  pool viejo `us-east-1_h1uG5pYAS`.
- Las credenciales AWS del compose son `test/test`, falsas. No se usan las de tu cuenta.

## Límites conocidos

- `SUPABASE_*` va vacío: el login/registro legacy de `ms-users` y la creación de usuarios por Supabase
  no funcionan. El login real es Cognito.
- `BD_LOGS` va vacío: el workflow guarda sesiones ADK en memoria y no registra tokens en Postgres.
- Las URLs prefirmadas de descarga (workflow y `ms-docs`) se firman con `http://localhost:4566` (`S3_PUBLIC_ENDPOINT_URL` / `AWS_S3_PUBLIC_ENDPOINT`), porque el navegador no resuelve `localstack`. En AWS real esas variables no existen.
  Está sin verificar porque requiere un usuario autenticado.
- `POST /chat/:id/hitl` es en memoria: el workflow debe quedar en **1 sola instancia**.
- Probar el flujo completo de agentes consume cuota real de Gemini.
