# Decision Log — Staffing Risk Agent

## Qué decidí que significa "staffing risk"

Un proyecto activo con trabajo por delante y **(a)** un rol sin nadie asignado, o
**(b)** la única persona en un rol de licencia sin backup. Ventana de **14 días**.

**Por qué este recorte, entre otros defendibles:** cruza los tres sistemas (que es
lo que el ejercicio pesa) y se apoya en `projects`, `allocations` y `time_off` —
evita deliberadamente `time_entries`, el endpoint que falla el 30%. La ventana de
14 días sale del *staffing lead time*: cubrir un rol lleva 1–3 semanas, así que
avisar con menos margen es avisar tarde.

**Rechacé:** "persona sobre-asignada" (vive casi todo en un sistema, cruza menos) y
"demanda entrante de Salesforce" (depende de probabilidades de deal, más ruidoso).
Van como líneas futuras, no como núcleo.

**Con más tiempo:** los roles no están modelados en los datos — un "rol vacante"
sólo es representable hoy como `lead_user_id: null`. Con un modelo de roles real,
la regla (a) sería mucho más rica.

## Backup se cuenta por rol, no por proyecto

Tres personas en un proyecto no son backup entre sí si hacen cosas distintas. Si se
va la única Technical Architect, la QA no la reemplaza. La regla agrupa las
allocations por `job_title` antes de evaluar cobertura. Sin esto, el agente no
detectaba ningún riesgo de licencia (todos los proyectos tienen ≥2 personas).

## Dónde trabaja el modelo, y qué le pongo en el contexto

Redacta el titular y el "por qué importa" de cada grupo, y desempata identidades que
la cascada determinística no resuelve. **Sólo ve un payload chico y ya verificado**
(persona, rol, proyectos, días, horas) — nunca respuestas crudas de la API. Devuelve
JSON con campos fijos; el mensaje final lo arma el código, que es quien escribe
nombres, fechas, horas y links.

**Con los datos provistos, el tier LLM de identidad no se activa:** los 14 usuarios
activos resuelven por email exacto. Lo dejé como red de seguridad para producción
(dominios distintos, alias, emails faltantes) y lo digo explícitamente en vez de
fingir que carga peso. El trabajo real del modelo es la redacción.

## Dónde lo mantuve determinístico, y por qué

Detección de riesgo, aritmética, fechas, ventana, score de severidad y score de
confianza. Todo tiene que ser reproducible y auditable: si el lead pregunta "¿por
qué Corvane primero?", tiene que haber una cuenta que mostrar. El modelo aporta una
señal al confidence, pero el número lo calcula el código; la confianza auto-reportada
por un LLM no está calibrada. Temperatura 0 para bajar varianza — no para garantizar
verdad; de eso se ocupa la validación.

## Cómo sé si la parte del modelo se rompió

`validate_output` compara la salida contra el payload y rechaza números o entidades
que no estaban. Si hay violaciones, se descarta y se usa el template determinístico.
Probado con salidas alucinadas a propósito: inventar una persona, un número, o
agregar background falso → las tres se bloquean con el motivo exacto. El costo de
equivocarse es alto ("being wrong once costs you the channel"), así que ante la duda
el sistema degrada a plantilla, no a silencio.

## Datos: la mugre que manejé y la que dejé pasar

Audité los fixtures y documenté 10 inconsistencias, ninguna en el README (detalle en
`auditoria-datos.md`). **Manejadas:** identidad por email con nombre como respaldo
(hay dos personas distintas llamadas "Ines Rocha" — matchear por nombre las
fusionaría); normalización de `allocation_percentage` (mezcla fracciones y
porcentajes — asumí que ≤1 es fracción, verificado contra la carga real de Simon
Zhao); allocations huérfanas contadas y reportadas; dedupe de oportunidades por
huella; fechas de tres formatos distintos unificadas a UTC.

**Dejadas pasar a propósito:** Tessellate aparece Closed Lost en Salesforce y Active
en Kantata — es una decisión de negocio (qué sistema es la verdad), no técnica, así
que se reporta como conflicto y no se resuelve. Proyectos vencidos aún "Active" se
observan, no se corrigen. El matching de clientes por prefijo funciona con 9 clientes
y se rompe a escala.

## Fallas y re-ejecución

`429` → respeto el `Retry-After` del servidor. `5xx` → backoff exponencial propio
(sin jitter: con un cliente único no aporta). 4 intentos (~99% con 30% de falla),
presupuesto de 120s por corrida. Fuente **crítica** caída → el agente calla y lo
registra; **enriquecedora** caída → sigue y marca datos parciales. Con datos
incompletos afirmo lo positivo ("Simon al 130%") y degrado lo negativo ("nadie
cubre X") a incertidumbre — base en closed-world assumption y CALM. La tabla
`alerted_risks` usa la huella como PK con upsert: re-ejecutar el mismo día no
duplica. El desglose de decisiones (`nuevo/cambio/recordatorio/silencio`) queda en
cada corrida, para que un falso silencio sea auditable y no invisible.

## Vivo vs sano: dos canales

El dead man's switch se pinguea siempre que el agente corra (vivo es vivo, incluso
degradado). La salud de las fuentes va por otro canal. Un solo canal no puede
distinguir "el agente murió" de "la API falló". `/health` hoy es pasivo; el próximo
paso es un dead man's switch externo (Healthchecks.io) que detecte el silencio sin
que nadie tenga que mirar.

## Hosting

**Render**: cron nativo, Postgres free y endpoint HTTP en una sola plataforma.
Postgres y no un JSON en disco porque el filesystem de Render es efímero — la memoria
se borraría en cada redeploy. **En producción**: AWS (EventBridge + Lambda/ECS + RDS
+ Secrets Manager), para vivir donde ya está el resto de la infra en lugar de sumar
una plataforma más.

## Consideré y descarté

**Pydantic** para modelar datos: no habría atrapado ninguna de las 10 trampas (son
semánticas, no de tipo). Un default en la normalización resuelve los nulos sin sumar
dependencia.
