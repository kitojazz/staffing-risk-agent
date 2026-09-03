"""La capa del modelo. Usa Google Gemini via su endpoint compatible con OpenAI.

El LLM hace dos cosas y ninguna mas:
  1. redacta el titular y el "por que importa" de cada grupo de riesgos
  2. desempata identidades que la cascada deterministica no resolvio

Nunca escribe hechos. Nombres, fechas, horas y links los pone el codigo
desde el payload verificado. El modelo devuelve JSON con campos fijos y
el render final es deterministico.

Despues de generar, validate_output chequea que no haya aparecido ninguna
entidad que no estuviera en el payload. Si aparece, se descarta la salida
del modelo y se cae a un template.

Proveedor intercambiable: solo cambian LLM_BASE_URL, LLM_MODEL y la env var
de la key. La logica de grounding y validacion es identica en cualquier proveedor.
"""

import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

LLM_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
)
MODEL = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
API_KEY_ENV = os.environ.get("LLM_API_KEY_ENV", "GEMINI_API_KEY")

TEMPERATURE = 0.0

SYSTEM_PROMPT = """Sos un asistente que redacta alertas de staffing para un delivery lead.

Reglas estrictas:
- Usa UNICAMENTE los hechos del JSON que recibis. No agregues contexto, historia,
  ni suposiciones sobre personas, proyectos o clientes.
- No inventes nombres, fechas, numeros ni porcentajes.
- Si un riesgo viene marcado como certero=false, redactalo como PREGUNTA.
- Escribi en espanol rioplatense, tono directo y breve.
- Responde SOLO con JSON valido, sin markdown ni backticks.

Formato de respuesta:
{
  "titular": "una linea que resuma la situacion general",
  "items": [
    {"id": "<el id del grupo>", "texto": "una o dos oraciones sobre por que importa"}
  ]
}"""


class LLMUnavailable(Exception):
    """El modelo no respondio. El agente sigue con el template deterministico."""


def _call(payload: dict, system: str = SYSTEM_PROMPT, timeout: float = 20.0) -> dict:
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise LLMUnavailable(f"falta {API_KEY_ENV}")

    body = {
        "model": MODEL,
        "temperature": TEMPERATURE,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
    }

    try:
        response = httpx.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=body,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMUnavailable(str(exc)) from exc

    try:
        text = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise LLMUnavailable(f"respuesta inesperada: {response.text[:200]}") from exc

    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"respuesta no es JSON: {text[:200]}") from exc


def _entities(payload: dict) -> set:
    allowed = set()
    for group in payload.get("grupos", []):
        for key in ("persona", "proyecto", "cliente", "rol"):
            value = group.get(key)
            if value:
                allowed.add(str(value).lower())
        for project in group.get("proyectos", []):
            allowed.add(str(project).lower())
    return allowed


def validate_output(generated: dict, payload: dict) -> list:
    violations = []
    allowed = _entities(payload)
    allowed_numbers = {
        str(n)
        for group in payload.get("grupos", [])
        for n in (group.get("dias"), group.get("horas"), len(group.get("proyectos", [])))
        if n is not None
    }
    texts = [generated.get("titular", "")] + [i.get("texto", "") for i in generated.get("items", [])]
    for text in texts:
        for number in re.findall(r"\b\d+(?:[.,]\d+)?\b", text):
            if number not in allowed_numbers:
                violations.append(f"numero inventado: {number}")
        for word in re.findall(r"(?<!^)(?<![.!?]\s)\b([A-ZÁÉÍÓÚÑ][\wáéíóúñ]{2,})\b", text):
            if word.lower() not in allowed and not any(word.lower() in a for a in allowed):
                violations.append(f"entidad no verificada: {word}")
    return violations


def compose(payload: dict):
    generated = _call(payload)
    return generated, validate_output(generated, payload)


def disambiguate_identity(candidate: dict, options: list) -> dict:
    payload = {
        "tarea": "identidad",
        "candidato": candidate,
        "opciones": options,
        "instruccion": 'Devolve {"match_id": <id o null>, "confianza": <0..1>, "motivo": "..."}',
    }
    result = _call(payload, system="Responde solo con JSON valido, sin markdown.")
    result["confianza"] = min(float(result.get("confianza", 0)), 0.85)
    return result
