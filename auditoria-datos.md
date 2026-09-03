# Auditoría de datos — Go Nimbly case study

Fecha de referencia: 2026-09-02. Ninguna de estas inconsistencias está documentada en el README.

---

## A. Resolución de identidad (personas)

### A1. Dos personas distintas con el mismo nombre visible
| Sistema | Persona 1 | Persona 2 |
|---|---|---|
| Kantata | `u_10031` Inés Rocha — Solutions Consultant | `u_10077` Ines Rocha Duarte — Technical Architect |
| ClickUp | `4410034` "Ines Rocha" | `4410088` "Ines Rocha" |
| Salesforce | `...SLV02` "Ines Rocha" | `...SLV09` "Ines Rocha" |

Emails: `ines.rocha@` vs `i.rocha.duarte@`.

**Riesgo:** matchear por nombre las fusiona → se concluye que un rol está cubierto cuando no lo está. Falso positivo grave.

### A2. Una persona con nombres distintos por sistema
| Kantata | ClickUp | Salesforce |
|---|---|---|
| M. Ferreira | Matías Ferreira | Matías Ferreira |
| Nathaniel Whitlock | Nate Whitlock | Nate Whitlock |
| Marta Zielinska-Ortiz | Marta Z-O | Marta Zielinska |
| Devika Balasubramanian | Devika B. | Devika Balasubramanian |
| Inés / Tomás / Lucía | Ines / Tomas / Lucia | Ines / Tomas / Lucia |

Tildes inconsistentes, iniciales, apellidos truncados.

**Conclusión combinada A1+A2:** el nombre no sirve como clave en ninguna dirección. El email es la única señal confiable.

### A3. Personas que existen en un solo sistema
- **R. Vance** (`rowan@vanceadvisory.io`, ClickUp id `4410500`, role 4 = guest): contractor externo. Tiene tareas asignadas, **no existe en Kantata**. Trabajo real invisible para el sistema de resourcing.
- **Desmond Kerrigan** (`u_10099`): en Kantata con `active: False`. Ausente de ClickUp y Salesforce.

### A4. Tipos de ID incompatibles
- Kantata: string con prefijo (`u_10024`)
- ClickUp: entero (`4410021`)
- Salesforce: string SF de 15 chars (`005Ho00000FRN01`)

---

## B. Resolución de identidad (clientes)

### B1. Nombres de cuenta distintos entre SF y Kantata
| Salesforce | Kantata |
|---|---|
| Ironvale Data Group | Ironvale |
| Quillspace Software | Quillspace |
| Kestrel Logistics | *(no existe)* |

### B2. Listas de ClickUp sin clave con proyectos de Kantata
`"Veridia Hierarchy"` ↔ `"Veridia — Account Hierarchy Redesign"`.
El join solo se puede hacer por prefijo de nombre de cliente. Frágil.

---

## C. Inconsistencias internas de datos

### C1. `allocation_percentage` con dos escalas mezcladas
Valores presentes: `0.25`, `1.0`, `30`…`100`.

| Allocation | Persona | Proyecto | Raw |
|---|---|---|---|
| `a_9004` | Devika Balasubramanian | Quillspace | `0.25` |
| `a_9012` | Simon Zhao | Corvane CPQ | `1.0` |

**Verificación:** Simon con `1.0` tiene 3 tareas activas en Corvane (una bloqueada) → es dedicación completa, no 1%.
**Decisión:** normalizar `valor ≤ 1 → ×100`. Es una asunción, va declarada en el decision log.

### C2. Referencias huérfanas
- `a_9018` (Simon Zhao, 30%) apunta a `p_5099`, **proyecto inexistente**.
- `p_5005` (Corvane CPQ, Active, 760h) tiene `lead_user_id: null`.

### C3. Campos nulos no documentados
- 10 de 52 tareas de ClickUp con `time_estimate: null`
- `Halden — Phase 3 Scope` (85% prob) con `Estimated_Delivery_Hours__c: null`

### C4. Estados que contradicen las fechas
Proyectos `Active` con `due_date` ya vencida:
- `p_5003` Fernbrook Health — vencido 2026-08-29
- `p_5006` Quillspace — vencido 2026-08-24

### C5. Duplicado en Salesforce
- `Corvane — CPQ Phase 2`
- `Corvane CPQ — Phase II`

Mismo AccountId, misma CloseDate (2026-10-23), mismas 820h, misma probabilidad (25%). **Es la misma oportunidad cargada dos veces.** Sumar demanda sin deduplicar cuenta 1640h inexistentes.

### C6. Contradicción entre sistemas
- Salesforce: `Tessellate — Multi-Track Integration` = **Closed Lost** (2026-08-11)
- Kantata: `p_5007` Tessellate = **Active**, con allocations hasta 2026-11-13

Uno de los dos está desactualizado. Hay gente asignada a un deal que se perdió, o SF miente.

### C7. Demanda entrante sin proyecto
`Kestrel — RevOps Foundation`: 90% probabilidad, 700h estimadas, CloseDate 2026-08-28 (ya pasada). No existe proyecto ni allocation en Kantata.

---

## D. Sobrecarga detectada (tras normalizar C1)

| Persona | Carga | Detalle |
|---|---|---|
| Simon Zhao | **130%** | 100% Corvane + 30% en `p_5099` (proyecto fantasma) |
| Devika Balasubramanian | **125%** | 100% Halden + 25% Quillspace |

Capacidad declarada: 40h semanales para los 15 usuarios (uniforme).

---

## E. Convergencia: Corvane CPQ es el caso de manual

`p_5005` acumula, a la vez:
- Sin `lead_user_id` (rol vacante literal)
- 3 tareas sin assignee, todas suyas (`Untriaged — …`)
- Simon Zhao, su recurso principal, al 130%
- Una oportunidad de expansión duplicada en SF por 820h
- Una tarea en estado `blocked`

Es el escenario que el ejercicio quiere que el agente detecte.

---

## F. Comportamientos del stub (leídos del código, no del README)

### F1. `CANDIDATE_TOKENS`
Si la variable está seteada, todos los endpoints exigen header `X-Candidate-Token`. Por defecto está vacía. Si dan un token, hay que soportarlo.

### F2. `CHAOS_ENABLED=false`
Desactiva la inyección de fallas. Útil para desarrollar; **no** para la demo.

### F3. La latencia es bloqueante
`chaos.py` usa `time.sleep()` dentro de un middleware `async`. Bloquea el event loop, así que paralelizar requests rinde mucho menos de lo esperado contra este stub. Vale la pena medir antes de invertir en concurrencia.

### F4. Paginación sin total
`/clickup/tasks` devuelve `last_page` pero **no** un `totalSize`. Si una página falla con 429 en medio del recorrido, quedan datos faltantes sin forma de detectarlos. Hay que contar páginas del lado del cliente.

### F5. Envelopes inconsistentes
| Endpoint | Forma |
|---|---|
| `/kantata/*` | `{"<coleccion>": [...], "count": n}` |
| `/kantata/projects/{id}` | objeto pelado, sin envelope |
| `/salesforce/*` | `{"records": [...], "totalSize": n}` |
| `/clickup/members` | `{"members": [...]}` — sin count |
| `/clickup/tasks` | `{"tasks": [...], "last_page": bool}` |

### F6. Formatos de fecha por sistema
| Sistema | Formato |
|---|---|
| Kantata | `"2026-08-19"` (ISO date) |
| Salesforce | `"2026-09-10T00:00:00.000+0000"` |
| ClickUp | `"1786233600000"` (epoch ms **como string**) |

`time_estimate` de ClickUp viene en milisegundos (`14400000` = 4h).

---

## G. Resumen para el decision log

**Manejadas:**
- Identidad de personas por email, con señales estructurales de respaldo (A1, A2, A4)
- Normalización de `allocation_percentage` (C1)
- Referencias huérfanas descartadas y registradas (C2)
- Nulos con valor por defecto explícito (C3)
- Deduplicación de oportunidades de SF (C5)
- Normalización de formatos de fecha (F6)

**Detectadas y deliberadamente NO manejadas:**
- Contradicción Tessellate SF/Kantata (C6) — requiere decidir qué sistema es la fuente de verdad; es una decisión de negocio, no técnica
- Proyectos vencidos aún `Active` (C4) — se reportan como observación, no se corrigen
- Contractors externos sin registro en Kantata (A3) — se reportan como incertidumbre en el mensaje
- Matching de clientes por prefijo (B1, B2) — funciona con estos datos, se rompe con nombres ambiguos
