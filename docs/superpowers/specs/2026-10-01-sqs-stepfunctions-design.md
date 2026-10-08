# Subida → SQS → Lambda → Step Functions (espera del docente duradera) — diseño

Estado: **pendiente de revisión del dueño** (enfoque A aprobado en conversación el 2026-10-01; las secciones 1–2 se
presentaron en el chat, la 3 y el resto se escriben aquí por primera vez).
Rama: `cloudnative`, **rama principal desde 2026-10-01** (en `prisma_workflow`, `prisma-monitor` y `prisma-infra`).

## Objetivo

Sacar la espera del docente (HITL) de la memoria de un contenedor para que el flujo agéntico **no dependa de un proceso
vivo**: poder correr más de 1 tarea de ECS, sobrevivir a reinicios y reintentar una fase si un worker muere. Es el
pendiente #5c de `CLAUDE.md` (migrar el HITL a Step Functions con `waitForTaskToken`). Lo que sube el docente entra por
una cola SQS (buffer, reintentos y DLQ) y una Lambda arranca la ejecución de Step Functions.

## Decisiones ya tomadas

| Tema | Decisión |
|---|---|
| Alcance de Step Functions | **Orquesta las fases y espera al docente.** No solo reemplaza el disparador. |
| Dónde corren las fases | **Enfoque A:** en el servicio ECS actual (misma imagen y ADK), disparadas por una Lambda delgada dentro de la VPC; el workflow avisa con `SendTaskSuccess`. |
| Pruebas | Con **LocalStack** (el lab está cerrado). Fases en modo simulado, sin gastar cuota de Gemini. |
| Ramas | Todo en `cloudnative`, que pasa a ser la principal. El CI de ECR escucha esa rama. |
| Entrega ahora | Código + Terraform propuesto (`.pendiente`, **sin aplicar**) + verificación en LocalStack. |

## Hechos del sistema actual (verificados en el código)

- Subir a S3 (`jobs/{id}/paci.*` y `material.*`) dispara la Lambda `prisma-trigger`, que hace `POST /chat/internal/run/{id}`
  (solo reacciona a `paci*`). `chat_router.start_chat` escribe en DynamoDB **antes** del `PUT` a S3 por esa carrera.
- `run_workflow` (`run.py`) carga los documentos, crea **una** sesión ADK con estado inicial y corre `PaciWorkflowAgent`
  de principio a fin; después exporta el `.docx`.
- `PaciWorkflowAgent._run_async_impl` (`agent.py`): Agente 1 → gate normativo (Decretos 170/83) → materiales de referencia →
  bucle HITL (Agente 2 + espera del docente, máx. 3) → bucle Generador/Crítico (máx. 3).
- La espera del docente es `await HITL_CALLBACKS[sid](...)` sobre una `asyncio.Queue` **en memoria**; `POST /hitl` empuja a esa
  misma cola. Por eso hoy `desiredCount = 1` y un reinicio deja la sesión "huérfana".
- Estado de agentes (`session.state`), claves que leen sus instrucciones: `paci_document`, `material_document`,
  `prompt_docente`, `perfil_paci`, `planificacion_adaptada`, `materiales_referencia`, `hitl_feedback_a1`, `hitl_feedback_a2`,
  `critica_previa`, `rubrica`; salidas: `perfil_paci`, `planificacion_adaptada`, `rubrica`, `evaluacion_critica`.
- El front hace polling a `GET /chat/:id/state` (lee DynamoDB) y responde con `POST /chat/:id/hitl`. **Ese contrato no cambia.**

## Arquitectura

```
S3 (jobs/…) ──evento──► SQS ──► Lambda "starter" ──StartExecution(name = session_id)──► Step Functions (Standard)
                         └─ DLQ (3 recepciones)                                            │
                                                                                           ▼
   FaseA ──waitForTaskToken──► Lambda "invoker" (VPC) ──HTTP──► ALB ──► workflow (ECS)  /chat/internal/phase/a
     │                                                                          │ ejecuta en segundo plano
     │ ◄──────────────── SendTaskSuccess / SendTaskHeartbeat ───────────────────┘
     ├─ blocked / timeout / error ──► Finalizar
     └─ ok ─► EsperarDocente (máx. 24 h) ◄── POST /chat/:id/hitl  (= SendTaskSuccess con el token guardado)
                 ├─ aprobado ─────────────► FaseB (Rúbrica + Crítico + docx) ─► Finalizar
                 ├─ rechazado, intento < 3 ► FaseA otra vez (feedback_agent 1 ó 2)
                 └─ rechazado, intento = 3 ► Finalizar(hitl_rejected)
```

### Máquina de estados (Standard; `.waitForTaskToken` no existe en Express)

| Estado | Qué hace | Timeout / reintentos |
|---|---|---|
| `FaseA` | `lambda:invoke.waitForTaskToken` → invoker → `POST /phase/a`. Salida: `{status: ok\|compliance_blocked\|timeout\|error}` | `TimeoutSeconds` 1800, `HeartbeatSeconds` 120. Reintento: **1 vez** solo ante `States.HeartbeatTimeout` (cada reintento cuesta llamadas a Gemini) |
| `RutaFaseA` | Choice sobre `status` | — |
| `EsperarDocente` | `waitForTaskToken` → invoker → `POST /phase/wait` (guarda el token y pasa la sesión a `awaiting_hitl`) | `TimeoutSeconds` 86400 (24 h, configurable); al vencer → `Finalizar(expired)` |
| `DecidirHitl` | Choice: aprobado → `FaseB`; rechazado con intento < 3 → `FaseA`; intento 3 → `Finalizar(hitl_rejected)` | — |
| `FaseB` | Igual que `FaseA` con `POST /phase/b`. Salida: `success\|degraded\|compliance_blocked\|timeout\|error` | 1800 s, heartbeat 120, 1 reintento |
| `Finalizar` | `lambda:invoke` síncrono → `POST /phase/finalize` con el estado final: deja la sesión terminada en DynamoDB y borra el estado de S3 | reintentos de infraestructura |
| Terminales | **`Succeed`** para todo resultado de negocio (`success`, `degraded`, `compliance_blocked`, `hitl_rejected`, `expired`); **`Fail`** solo para errores inesperados | — |

`compliance_blocked` y `hitl_rejected` **no son errores**: son salidas legítimas, así que la ejecución termina en `Succeeded`.
Una cancelación del docente **no pasa por `Finalizar`**: `/cancel` marca la sesión como cancelada por sí mismo y llama `StopExecution`, de modo que
la ejecución queda en `Aborted` (estado propio de Step Functions) y el estado de S3 lo limpia el endpoint de cancelación.

### Estado entre fases (S3, no DynamoDB)

- Objeto `state/{session_id}.json` con la lista **explícita** de claves de arriba. DynamoDB tiene tope de 400 KB por ítem y aquí viajan
  textos completos de documentos y de materiales de referencia.
- **Nunca bajo `jobs/`:** ese prefijo dispara la notificación de S3 y se armaría un bucle.
- Cifrado en reposo y **expiración de 7 días** (regla de ciclo de vida sobre `state/`), igual que el TTL de DynamoDB. Se borra además en
  `Finalizar`. Contiene datos de menores (diagnóstico): es un dato nuevo en reposo y debe tratarse como tal.
- Cada fase crea una **sesión ADK nueva** inicializada con ese estado; no se persiste la sesión ADK.

### Contrato con el workflow (endpoints internos, `X-Internal-Token`)

| Endpoint | Body | Respuesta | Qué hace |
|---|---|---|---|
| `POST /chat/internal/phase/a/{id}` | `{task_token, attempt, feedback_agent}` | `202` | Rehidrata la sesión desde DynamoDB, carga el estado de S3 (o los documentos si es el intento 1), corre la fase en segundo plano, guarda el estado, envía heartbeat cada ~30 s y cierra con `SendTaskSuccess`/`SendTaskFailure` |
| `POST /chat/internal/phase/b/{id}` | `{task_token}` | `202` | Ídem: Generador/Crítico, exporta el `.docx` a S3 `results/` |
| `POST /chat/internal/phase/wait/{id}` | `{task_token}` | `200` | Guarda el token en el ítem de DynamoDB y pone `phase=awaiting_hitl` + `hitl_data` **a la vez** (el front solo puede responder cuando ya existe el token) |
| `POST /chat/internal/phase/finalize/{id}` | `{status}` | `200` | Estado terminal en DynamoDB, limpieza del estado en S3 |

- **Fase A, intento 1:** carga de documentos → Agente 1 → gate normativo → materiales → Agente 2. **Intentos siguientes:** con
  `feedback_agent = 1`, Agente 1 (reintento) + Agente 2; con `2`, solo Agente 2. Es exactamente lo que hoy hace el bucle, sin cambiar prompts.
- **Fase B:** el bucle Generador/Crítico tal cual, con el gate de rúbrica crítica y `_parse_critic_json` sin cambios.
- **`/hitl`** (ya autenticado y con dueño verificado): guarda el texto de la `reason` en el estado de S3, lee el token de DynamoDB, lo limpia con
  escritura condicional y llama `SendTaskSuccess` con **solo** `{approved, agent_to_retry}`. Doble clic o token vencido → `409`.
- **`/cancel`:** marca la sesión como cancelada y llama `StopExecution` (el ARN de la ejecución se deriva de `SFN_STATE_MACHINE_ARN` + `session_id`).
- **Compatibilidad:** si el ítem de DynamoDB **no** tiene `task_token` (sesión del camino antiguo o modo local sin AWS), `/hitl` y `/cancel`
  siguen usando la cola en memoria. El orquestador completo (CLI y local) sigue existiendo, compuesto por las dos fases.

### Guardas contra ejecución duplicada

SQS entrega "al menos una vez" y un heartbeat perdido puede reiniciar una fase mientras la anterior aún corre. Se exige: (1) `StartExecution`
con el `session_id` como nombre (idempotente mientras la ejecución esté abierta; si ya cerró, el starter trata `ExecutionAlreadyExists` como
mensaje consumido), y (2) una escritura condicional en DynamoDB `running_phase = (fase, intento)` que impida que dos workers corran la misma
fase a la vez (con vencimiento para recuperarse de un worker caído).

## Qué cambia en cada componente

| Componente | Cambio |
|---|---|
| `agent.py` (**orquestador, protegido**) | `PaciWorkflowAgent` gana un parámetro de fase (`all`/`a`/`b`). **No cambia ningún prompt, límite, timeout ni la lógica del gate o del Crítico**; solo dónde se cortan los bucles. `all` conserva el comportamiento actual |
| `run.py` | Nueva `run_phase(...)` que reutiliza la carga de documentos y la construcción de la sesión ADK |
| `api/phase_store.py` (nuevo) | Guarda/carga el estado en S3 con la lista de claves y el cifrado/expiración |
| `api/chat_router.py` | Los 4 endpoints internos; `/hitl` y `/cancel` con doble camino |
| `api/workflow_runner.py` | `run_phase_for_api`; rehidratar `SessionData` desde DynamoDB al empezar cada fase |
| Monitor (Kafka) | `flow_started` solo en fase A intento 1; `flow_finished` solo en `Finalizar`; el contexto de sesión para tokens se fija en cada fase |
| `lambda/sfn_starter.py` (nuevo) | SQS → `StartExecution`. **Fuera de la VPC** (necesita llegar a la API pública de Step Functions). Ignora todo lo que no sea `paci*`. `ReportBatchItemFailures` |
| `lambda/sfn_invoker.py` (nuevo) | Step Functions → `POST` al ALB. **Dentro de la VPC**, **solo HTTP** (sin salida a APIs de AWS: las subredes por defecto no dan internet a una Lambda). Lista blanca de fases (`a`,`b`,`wait`,`finalize`); no acepta rutas arbitrarias |
| `statemachine/prisma_flow.asl.json` (nuevo) | La definición; la usan Terraform y el script de LocalStack |
| `prisma-infra` | SQS + DLQ, política de cola para S3, notificación S3→SQS (**reemplaza** la de la Lambda: S3 no admite dos destinos para el mismo prefijo y evento), 2 Lambdas, mapeo de eventos, máquina de estados, regla de ciclo de vida de `state/` — todo en un `.tf.pendiente` |
| `docker-compose.yml` / `local/` | LocalStack con `sqs,stepfunctions`; cola, notificación, Lambdas y máquina de estados creadas por el script de inicio |

No cambian el front, el BFF ni `ms-docs` (el flujo `jobs` de `ms-docs` también escribe en `jobs/` y entra por la misma cola).

## Pruebas

1. **Primero, un spike en LocalStack:** confirmar que soporta `lambda:invoke.waitForTaskToken`, `HeartbeatSeconds` y (si no, se usa JSONPath) JSONata.
   Si algo no está, el diseño se ajusta **antes** de escribir código.
2. **Unitarias** (TDD): fases del orquestador (incluido que `all` ≡ comportamiento actual), `phase_store`, los 4 endpoints, `/hitl` y `/cancel` con ambos caminos,
   las dos Lambdas (con boto3 y HTTP simulados), la rehidratación de sesión, los eventos del monitor.
3. **Definición de la máquina de estados:** validación estructural (todos los `Next` existen, no hay estados huérfanos, tiempos coherentes).
4. **De punta a punta en LocalStack con fases simuladas** (`school_id` que empieza con `__mock`, que no llama a Gemini): aprobado a la primera;
   rechazo + reintento + aprobación; 3 rechazos → `hitl_rejected`; PACI bloqueado por normativa; docente que no responde (timeout corto en local);
   cancelación durante la espera; mensaje SQS duplicado (idempotencia); worker que deja de enviar heartbeat.
5. **Una corrida real con Gemini** desde tu front en local (consume cuota): comprobar que la salida es equivalente a la del camino antiguo.

## Fallas

| Falla | Comportamiento |
|---|---|
| Mensaje que falla 3 veces | Va a la DLQ; la sesión queda en `running` hasta que se atienda, así que se agrega una alarma/aviso (ver "Fuera de alcance") |
| Worker muere durante una fase | Se vence el heartbeat → 1 reintento de la fase desde el estado guardado |
| Docente no responde en 24 h | `Finalizar(expired)`; la sesión queda terminada con un mensaje claro |
| `SendTaskSuccess` con token vencido o ya usado | `/hitl` responde `409`; no se pierde la decisión previa |
| Kafka caído | Igual que hoy: el workflow no se entera |
| S3 `state/` ilegible o ausente | La fase falla con `error`; `Finalizar` limpia y marca la sesión |

## Seguridad y datos personales

- **El historial de ejecuciones de Step Functions guarda entradas y salidas en claro por 90 días.** Por eso por ahí viajan **solo** `session_id`, números y
  estados. El texto de documentos, perfiles, planificaciones y el comentario del docente (`reason`) van por S3/DynamoDB, nunca por Step Functions.
- El token de tarea se trata como secreto (capacidad de completar la espera): vive en DynamoDB, se valida el dueño de la sesión antes de usarlo y se invalida al usarlo.
- Los endpoints internos exigen `INTERNAL_TOKEN`; el invoker solo puede llamar a las cuatro rutas de fase.
- Los logs de las Lambdas no registran cuerpos ni tokens de tarea.

## Corte y reversa

- El corte es **cambiar el destino de la notificación de S3** (Lambda `prisma-trigger` → SQS). El camino antiguo (`/chat/internal/run`, `prisma-trigger` y la
  cola en memoria) sigue en el código mientras se valida; volver atrás es restaurar la notificación.
- Orden: (1) desplegar la imagen nueva (compatible con ambos caminos), (2) crear cola, Lambdas y máquina de estados, (3) cambiar la notificación.

## Fuera de alcance

Alarmas de la DLQ y del heartbeat (CloudWatch), límite de ejecuciones concurrentes (el `MaximumConcurrency` del mapeo SQS→Lambda solo acota los arranques,
no los flujos en curso; si hace falta proteger la cuota de Gemini se agrega un semáforo), una fase por agente, Express Workflows, aplicar el Terraform.

## Riesgos abiertos (se verifican durante la implementación)

1. Que LocalStack (4.14.0, sin token) soporte `waitForTaskToken` con Lambda y `HeartbeatSeconds`. Es el primer paso del plan.
2. **Disponibilidad de Step Functions y permisos `states:*` en el Learner Lab / `LabRole`:** no lo puedo comprobar con el lab cerrado.
3. Un worker "zombi" que sigue corriendo tras un heartbeat perdido (la escritura condicional lo mitiga, no lo elimina).
4. Costo: cada reintento de fase repite llamadas a Gemini; por eso solo 1 reintento y solo ante heartbeat perdido.
5. `ms-docs` (flujo `jobs`) comparte el prefijo `jobs/` y la tabla de sesiones: hay que confirmar que sus ítems tienen los campos que necesitan las fases.

## Ramas y repos

- `prisma_workflow`: `cloudnative` (creada desde `master`; ya incluye el commit del monitor). El pipeline de ECR escucha `cloudnative`.
- `prisma-monitor`: ahora es un repo, rama `cloudnative`, con su commit inicial.
- `prisma-infra`: `HEAD` apunta a `cloudnative`, **sin commits**: el repo nunca tuvo uno y hay archivos que no deben versionarse (`*.tfplan` se agregó a
  `.gitignore` por precaución). El primer commit lo decide el dueño.
- La raíz `prisma-aws/` **no es un repo**: `docker-compose.yml`, `local/`, `LOCAL.md` y `docs/` no tienen rama. Conviene darles un hogar versionado.
- Los edits tuyos sin commitear (cambio de modelo a `3.5` en los 4 agentes y 2 líneas de `agent.py`) se dejaron **fuera** de los commits.
