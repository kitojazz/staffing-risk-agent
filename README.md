# Staffing Risk Agent

Agente que detecta riesgos de cobertura en proyectos de delivery cruzando Kantata,
Salesforce y ClickUp, y los avisa en Slack.

---

## Qué considera "staffing risk"

> Un proyecto activo tiene trabajo por delante y **(a)** un rol sin nadie asignado,
> o **(b)** la única persona que cubre ese rol está de licencia sin backup.

Ventana: **14 días** (configurable). El criterio es el *staffing lead time*: cubrir
un rol lleva de 1 a 3 semanas, así que avisar con menos margen es avisar tarde.

Lo que el agente **no** hace: no sugiere a quién asignar. Esa decisión necesita
contexto que no está en los datos (skills reales, relación con el cliente, planes
de carrera). Fingir que lo sabe sería peor que callarse.

---

## Cómo correrlo

### Local

```bash
# 1. Levantar el mock API (en otra terminal)
cd eng-case-study && uvicorn app.main:app --port 8000

# 2. Correr el agente una vez
pip install -r requirements.txt
API_BASE_URL=http://localhost:8000 python -m agent.main
```

Sin `DATABASE_URL` usa un store en memoria. Sin `ANTHROPIC_API_KEY` cae al
template determinístico. Sin `SLACK_WEBHOOK_URL` imprime el mensaje al log.
Las tres degradaciones son intencionales.

### Como servicio

```bash
uvicorn agent.main:app --host 0.0.0.0 --port 8000
```

| Endpoint | Qué hace |
|---|---|
| `POST /run` | Fuerza una corrida y devuelve el resumen |
| `GET /health` | Heartbeat: timestamp de la última corrida exitosa |

El cron y el endpoint llaman a la **misma** función (`run_once`). No hay dos
caminos que puedan divergir.

---

## Variables de entorno

| Variable | Default | Para qué |
|---|---|---|
| `API_BASE_URL` | `http://localhost:8000` | Base del mock API |
| `CANDIDATE_TOKEN` | vacío | Header `X-Candidate-Token` si el stub lo exige |
| `DATABASE_URL` | vacío | Postgres. Sin esto, store en memoria |
| `ANTHROPIC_API_KEY` | vacío | Sin esto, template determinístico |
| `SLACK_WEBHOOK_URL` | vacío | Sin esto, el mensaje va al log |
| `WINDOW_DAYS` | `14` | Ventana de análisis |
| `MAX_ATTEMPTS` | `4` | Reintentos por request |
| `RUN_BUDGET_SECONDS` | `120` | Tope de tiempo por corrida |
| `REMINDER_DAYS` | `7` | Cada cuánto repetir un riesgo que sigue vivo |
| `RESOLUTION_TTL_DAYS` | `90` | Vencimiento de las confirmaciones humanas |
| `LLM_MODEL` | `claude-haiku-4-5-20251001` | El más barato que sirve |

---

## Arquitectura

```
Disparador        cron semanal + POST /run
    ↓
Ingesta           3 APIs · Retry-After honrado · backoff exponencial
                  4 intentos · tope de 120s · marca completitud
    ↓
Normalización     escalas, fechas, nulos, huérfanos
    ↓
Identidad         email → nombre inequívoco → señales → LLM
    ↓
Reglas            determinístico, con evidencia trazable
    ↓
Memoria           Postgres: ¿nuevo, cambió, o ya lo avisé?
    ↓
Redacción         LLM con salida estructurada + validación de hechos
    ↓
Salida            webhook de Slack, o log
```

### Módulos

| Archivo | Responsabilidad |
|---|---|
| `config.py` | Todo lo ajustable, en un solo lugar |
| `ingest.py` | HTTP con reintentos. Devuelve `Fetched`, nunca una lista pelada |
| `normalize.py` | Limpieza de la basura documentada en la auditoría |
| `identity.py` | Resolución de identidad en cascada, con score |
| `rules.py` | Detección de riesgo. Sin modelo |
| `state.py` | Memoria entre corridas + heartbeat |
| `llm.py` | Redacción y desempate. Con validación de salida |
| `compose.py` | Agrupación por causa, priorización, render |
| `main.py` | Orquestación y endpoints |

---

## Dónde trabaja el modelo, y dónde no

**Sí:**
- Redacta el titular y el "por qué importa" de cada grupo de riesgos
- Desempata identidades que la cascada determinística no resolvió

**No:**
- Aritmética, fechas, ventanas
- Detección de vacantes
- Score de severidad
- Score de confianza (el modelo aporta una señal; el código calcula)

El motivo es reproducibilidad. Si el lead pregunta "¿por qué Corvane está primero?",
tiene que haber una cuenta que mostrar.

### Grounding

El modelo nunca ve datos crudos de la API. Recibe un payload chico y ya verificado,
devuelve JSON con campos fijos, y el mensaje final lo arma el código: nombres,
fechas, horas y links los escribe `compose.py`, no el modelo.

### Cómo sé si se rompió

`llm.validate_output` compara la salida contra el payload y rechaza números o
entidades que no estaban. Si hay violaciones, se descarta la salida y se usa el
template determinístico.

Probado con salidas alucinadas a propósito:

| Caso | Resultado |
|---|---|
| Salida limpia | pasa |
| Inventa una persona ("Pedro Gomez") | bloquea |
| Inventa un número (1200 horas) | bloquea |
| Agrega background falso ("trabaja en Accenture desde 2019") | bloquea |

---

## Manejo de fallas

El stub falla a propósito: `429` (~12,5%) en cualquier endpoint y `500` (~30%) en
`/kantata/time_entries`.

- **429** → se respeta el `Retry-After` del servidor. Adivinar sale más caro.
- **5xx** → backoff exponencial propio (1s, 2s, 4s). Sin jitter: con un solo
  cliente no aporta, y agrega ruido al log.
- **4 intentos** → ~99% de éxito con 30% de falla.
- **Presupuesto de 120s** por corrida, compartido. Sin esto, tres endpoints en
  retry simultáneo cuelgan la corrida.

### Fuentes críticas vs enriquecedoras

| Tipo | Colecciones | Si falla |
|---|---|---|
| Críticas | `projects`, `allocations`, `time_off`, `users` | El agente **se calla** |
| Enriquecedoras | `tasks`, `members`, `opportunities`, `time_entries` | Sigue, y marca datos parciales |

El recorte del problema esquiva `time_entries`, que es el endpoint más frágil.
Eso no fue casualidad: la definición de riesgo se eligió sabiendo dónde estaba
la fragilidad.

### Datos parciales: qué se puede afirmar

Regla operativa, con base en la *closed-world assumption* (Reiter, 1978) y en el
teorema CALM (Hellerstein, 2010):

- **Afirmaciones positivas** ("Simon está al 130%") salen de datos que sí tenemos.
  Más datos solo pueden confirmarlas → **seguras con datos parciales**.
- **Afirmaciones negativas** ("nadie cubre este rol") dependen de datos que no
  tenemos. Un solo registro faltante las da vuelta → **exigen completitud**.

Con datos incompletos, el agente afirma lo positivo y degrada lo negativo a
incertidumbre. No pierde el caso en silencio: lo reporta como "no pude evaluar X".

---

## Cuándo el agente se calla

- No hay riesgos → no manda nada
- El riesgo es idéntico a uno ya avisado y tiene menos de 7 días → silencio
- Una fuente crítica está caída → silencio, y queda registrado en `runs`

El silencio es una decisión, no un default. Por eso existe `/health`: sin
heartbeat, un agente sano y uno muerto se ven igual.

## Cuándo pregunta en vez de afirmar

Cuando la ambigüedad **cambia la decisión**. Ejemplo real de estos datos:
R. Vance tiene tareas en ClickUp y no existe en Kantata. Si cubre un rol, no hay
riesgo; si no, sí lo hay. El agente pregunta en lugar de asumir.

La respuesta se guarda en `resolutions` con confianza 1.0 y no se vuelve a
preguntar, salvo que pasen 90 días o cambien los datos de esa persona o proyecto.

---

## Idempotencia

Correr el agente dos veces el mismo día no duplica alertas. La tabla
`alerted_risks` usa la huella del riesgo como clave primaria, con
`ON CONFLICT DO UPDATE`.

---

## Deploy

`render.yaml` define tres cosas: el web service, un cron semanal (lunes 12:00 UTC)
y una base Postgres en free tier.

Render se eligió por tres razones concretas: cron nativo, Postgres gratis y
endpoint HTTP en la misma plataforma. En producción esto iría a AWS
(EventBridge + Lambda/ECS + RDS + Secrets Manager), para vivir donde ya vive el
resto de la infraestructura en vez de sumar una plataforma más.

Postgres y no un archivo JSON porque el filesystem de Render es efímero: la
memoria del agente se borraría en cada redeploy.
