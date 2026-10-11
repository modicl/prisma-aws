# RAG normativo para P.R.I.S.M.A. — diseño

Fecha: 2026-10-08 · Estado: **pendiente de revisión del dueño** · Rama de trabajo: `cloudnative` (`prisma_workflow`)

## 1. Objetivo

Que los agentes que adaptan y evalúan se apoyen en documentos oficiales del Mineduc (Decretos 83, 170 y 67, orientaciones DUA y diversificación, Bases Curriculares) en vez de la memoria general del modelo.
Resultado esperado: material adaptado más correcto y con fuentes citables, sin atar el sistema a un modelo generador concreto.

Es un proyecto académico (confirmado por el dueño el 2026-10-08). La política del Mineduc permite el uso no comercial de sus contenidos; si el uso pasara a ser comercial habría que pedir autorización escrita.

## 2. Alcance

**Dentro:** ingesta offline del corpus público, base vectorial, recuperación híbrida, inyección en Fase A y Fase B, degradación segura, medición de calidad de la recuperación.

**Fuera (anotado para specs posteriores, en este orden):**
1. **Salida con justificación por adecuación** (qué cambió y por qué, citando los fragmentos del RAG). Es la siguiente porque depende de las citas.
2. **PACI estructurado en campos** (tipo de NEE, adecuaciones, estilo de aprendizaje, nivel de logro, apoyos).
3. **Validaciones deterministas** (legibilidad, cantidad de ítems, secciones obligatorias).
4. Vacíos del corpus: no hay documento oficial equivalente a las Orientaciones D83 para educación media ni parvularia; DUA no tiene documento oficial propio, está dentro de las Orientaciones D83.

## 3. Decisiones ya tomadas

| Decisión | Razón |
|---|---|
| pgvector en el RDS actual (`database-1`), base nueva `prisma_rag` | Corpus pequeño; no suma servicio ni costo; ya hay RDS, Terraform y SG hacia ECS |
| Interfaz `Embedder` propia; cada fila guarda `embedding_model` y `embedding_version` | El LLM generador es intercambiable; lo que ata el índice es el modelo de embeddings. Cambiarlo = reindexar con un script |
| Embeddings iniciales: Gemini (`gemini-embedding-001`, a confirmar), 768 dimensiones | Misma API key; con este volumen no hace falta índice aproximado (búsqueda exacta) |
| Corpus: normativa + orientaciones + currículum; **formularios excluidos** | Las plantillas casi no tienen contenido y bajan la calidad de la recuperación |
| Nunca se indexan PACI ni material de estudiantes | Datos de menores; no deben quedar en un índice persistente |
| Consulta sin datos identificables | Lo que viaja al servicio de embeddings no debe incluir nombre, RUT ni texto libre del PACI |
| Si el RAG falla, el flujo sigue | Mismo principio que `BookRepository` |

## 4. Corpus (inventario verificado el 2026-10-08, todas las URL respondieron 200)

Base: `https://especial.mineduc.cl/wp-content/uploads/sites/31/` (salvo indicación).

**A. Normativa** (`tipo_documento = norma`)
- Decreto 83/2015 (`2016/08/Decreto-83-2015.pdf`), Decreto 170 (`2018/06/DTO-170_21-ABR-2010.pdf`), Decreto 67/2018 (`2020/08/Decreto-67_31-DIC-2018.pdf`).
- Decretos 1/1998, 332/2011, 1300/2002, 363/1994, 375/1999; Ord. 610 y 702.
- Leyes 20.370, 20.422, 20.609, 20.845, 20.903 y 21.545 (TEA).
- El texto vigente y con modificaciones se contrasta con LeyChile (BCN); ante diferencia gana BCN.

**B. Orientaciones** (`tipo_documento = orientacion`)
- Orientaciones D83 2017 (`2017/05/OrientacionesD83-Web-2017.pdf`), Preguntas frecuentes D83 2019.
- Orientaciones D170: actualización 2025 v2, Orientaciones PIE 2013, Manual PIE, cartillas 2, 4, 5 y 6 (2025–2026), respuestas a la diversidad, escuelas multigrado, PIE jóvenes y adultos, preguntas frecuentes D170 (3 PDF, 2025).
- PAEC (Ley TEA) y Manual de apoyo a docentes con TEA.
- Orientaciones del Decreto 67 en `curriculumnacional.cl/evaluacion/marco-legal`.

**C. Currículum** (`tipo_documento = curriculo`)
- Bases Curriculares por ciclo (1.º–6.º básico, 7.º básico–2.º medio, 3.º–4.º medio, EPJA).
- Un fragmento por Objetivo de Aprendizaje (OA), tomado del HTML de `curriculumnacional.cl/curriculum/<ciclo>/<asignatura>/<curso>`.
- Los PDF de las Bases se descargan y guardan como respaldo, pero la primera versión indexa los OA desde el HTML.

**Excluido:** formularios únicos, anamnesis, poderes, FUDEI y materiales audiovisuales.

Cada documento lleva: `fuente_url`, `sha256`, `anio`, `vigente`. Si hay versiones del mismo documento, solo la más nueva queda `vigente = true`.

## 5. Datos

Base `prisma_rag`, con `CREATE EXTENSION vector`.

```
documentos(id, tipo_documento, titulo, fuente_url, sha256, anio, vigente, ingestado_en)
fragmentos(id, documento_id, orden, ruta, texto, texto_tsv, metadata jsonb,
           embedding vector(768), embedding_model, embedding_version)
```

- `ruta`: texto legible para citar, por ejemplo `Art. 5` o `3.º básico · Matemática · OA 03`.
- `texto_tsv`: columna generada con configuración `spanish`, con índice GIN.
- `metadata`: `decreto`, `articulo`, `asignatura`, `curso`, `eje`, `oa`.
- Sin índice vectorial aproximado en la primera versión (búsqueda exacta). Se agrega HNSW si la tabla supera ~50 000 filas.

## 6. Ingesta (`python -m rag.ingest`)

Script manual y repetible, no un crawler.
1. Lee un manifiesto (`rag/corpus.yaml`) con las URL, el tipo y el año de cada documento.
2. Descarga cada documento una sola vez, con pausa entre peticiones y `User-Agent` identificable (el `robots.txt` de `especial.mineduc.cl` responde 403 a clientes automáticos; por eso no se rastrea, solo se baja lo listado).
3. Calcula `sha256`. Si coincide con el guardado, no hace nada. Si cambió, reemplaza los fragmentos de ese documento en una transacción.
4. Fragmenta según el tipo:
   - normas: un fragmento por artículo; si supera ~800 tokens, por inciso;
   - orientaciones: por encabezado, de 500 a 800 tokens con ~10 % de solapamiento;
   - currículum: un fragmento por OA.
5. Calcula los embeddings por lotes y los guarda con el nombre y la versión del modelo.
6. Imprime un resumen: documentos nuevos, actualizados, sin cambios y fragmentos totales.

## 7. Recuperación (`rag/retriever.py`)

`buscar(consulta, filtros, k) -> list[Fragmento]`, independiente del modelo generador.

- **Consulta:** se arma con asignatura y curso (`_extract_subject_grade`, `normalize_subject`, `normalize_grade`, que ya existen) y el tipo de NEE detectado por palabras clave (TEA, TDAH, DI, TEL, DEA, disfasia, etc.). Nunca incluye nombre, RUT ni texto libre.
- **Búsqueda híbrida:** 20 resultados por similitud de coseno y 20 por texto completo, fusionados con Reciprocal Rank Fusion; filtro por `tipo_documento` y `vigente = true`.
- **Salida:** 5 o 6 fragmentos dentro de un tope de ~3000 tokens, cada uno con su cita (`[D83 · Art. 5]`).

## 8. Integración en el flujo

Dos puntos, ambos en `agent.py`, junto a donde hoy se arma `materiales_referencia`:

| Punto | Filtro | Clave en el state |
|---|---|---|
| Fase A, antes del Agente 2 | D83, DUA/diversificación, D170, orientaciones, OA del curso | `normativa_adaptador` |
| Fase B, antes del Generador | D67 y evaluación | `normativa_evaluacion` |

- Cada texto se envuelve con un encabezado y `<documento_usuario tipo="normativa_referencia">`, igual que `materiales_referencia`. Los delimitadores anti-injection no se tocan.
- Las dos claves se agregan a `_CLAVES_ENTRE_FASES` (`agent.py`) y a la lista explícita de `phase_store.py`, para que viajen por S3 entre fases.
- **Cambios protegidos (requieren la confirmación del dueño, otorgada el 2026-10-08 en sesión):**
  - ~~`requirements.txt`: `psycopg[binary]` y `pgvector`.~~ **Ya no hace falta (corregido en el plan):** el runtime usa `asyncpg` y `google-genai`, que ya estaban, y el vector se pasa como texto `'[..]'::vector`. La ingesta usa `requirements-rag.txt` aparte (no entra en la imagen).
  - `agents/adaptador.py` y `agents/generador_rubrica.py`: un placeholder nuevo en el prompt (`{normativa_adaptador}` y `{normativa_evaluacion}`). Se propone como diff mínimo para revisión; no se reescribe contenido normativo del prompt.

## 9. Degradación y seguridad

- `RAG_DATABASE_URL` vacío ⇒ RAG apagado; las claves quedan en `""`.
- Falla de base, de embedding o sin resultados ⇒ clave vacía, warning en el log y el flujo continúa.
- Tiempo máximo por recuperación: 10 s; superado, se descarta.
- La URL de la base es un secreto (variable de entorno del contenedor, nunca en el código).
- El monitor en vivo no recibe contenido de fragmentos; si se publica un evento, solo cantidad y latencia.

## 10. Infraestructura

- Base `prisma_rag` creada a mano, como las otras cuatro: abrir el RDS un momento (`publicly_accessible = true` y regla 5432 desde la IP del dueño /32), `CREATE DATABASE prisma_rag; CREATE EXTENSION vector;`, cerrar.
- Terraform: agregar `prisma_rag` al comentario de `11-postgres.tf` y una entrada en `local.database_urls` (`03-ecs.tf`) para el contenedor del workflow (`RAG_DATABASE_URL`).
- Local: servicio nuevo `rag-db` (imagen `pgvector/pgvector:pg16`, puerto 5433) en el compose; el Postgres principal no se toca. `local/postgres/init-rag.sql` crea `prisma_rag_test`.
- Riesgo a verificar: soporte de `pgvector` en RDS PostgreSQL 18.3. La documentación de AWS indica 0.8.x, pero no se confirmó en la instancia; se verifica con `CREATE EXTENSION`. Si no estuviera, plan B: bajar a una instancia con PG 17.

## 11. Pruebas y criterio de éxito

- **TDD**, `Embedder` falso y Postgres con pgvector en el compose local.
- Pruebas de fragmentación con PDF y HTML de muestra, sin red.
- Pruebas de degradación: base caída, embedding que falla, resultado vacío, tiempo excedido.
- **Set de oro de ~20 preguntas** con su fragmento esperado (por ejemplo «evaluación diferenciada» → `D67 Art. 5`). **Criterio de éxito: recall@5 ≥ 80 %** antes de conectar el RAG a los agentes. Si no se alcanza, se ajusta la fragmentación y los pesos.
- Comparación manual de una rúbrica con y sin RAG sobre un PACI sintético de `docs_test/synthetic/`.
- Suite existente (≈ 465 pruebas) sin regresiones.

## 12. Orden de implementación

1. Verificar `pgvector` y crear `prisma_rag` (nube y local).
2. Esquema, `Embedder`, fragmentadores.
3. Ingesta con el manifiesto del corpus y carga real.
4. Retriever y set de oro; medir recall@5.
5. Integración en Fase A y Fase B, con los diffs de prompts para revisión.
6. Terraform y variable `RAG_DATABASE_URL`.
