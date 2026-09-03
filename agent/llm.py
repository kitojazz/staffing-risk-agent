"""La capa del modelo.

El LLM hace dos cosas y ninguna más:

  1. redacta el titular y el "por qué importa" de cada grupo de riesgos
  2. desempata identidades que la cascada determinística no resolvió

Nunca escribe hechos. Nombres, fechas, horas y links los pone el código
desde el payload verificado. El modelo devuelve JSON con campos fijos y
el render final es determinístico.

Después de generar, `validate_output` chequea que no haya aparecido ninguna
entidad que no estuviera en el payload. Si aparece, se descarta la salida
del modelo y se cae a un template. Eso es la respuesta a "¿cómo sabés que
la parte del modelo funciona?".
"""

import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
API_URL = "https://api.anthropic.com/v1/messages"
TEMPERATURE = 0.0          # baja varianza. NO garantiza verdad: para eso está la validación.

SYSTEM_PROMPT = """Sos un asistente que redacta alertas de staffing para un delivery lead.

Reglas estrictas:
- Usá ÚNICAMENTE los hechos del JSON que recibís. No agregues contexto, historia,
  ni suposiciones sobre personas, proyectos o clientes.
- No inventes nombres, fechas, números ni porcentajes.
- Si un riesgo viene marcado como certain=false, redactalo como PREGUNTA, no como afirmación.
- Escribí en español rioplatense, tono directo y breve.
- Respondé SOLO con JSON válido, sin markdown ni backticks.

Formato de respuesta:
{
  "titular": "una línea que resuma la situación general",
  "items": [
    {"id": "<el id del grupo>", "texto": "una o dos oraciones sobre por qué importa"}
  ]
}"""


class LLMUnavailable(Exception):
    """El modelo no respondió. El agente sigue con el template determinístico."""


def _call(payload: dict, timeout: float = 20.0) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("falta ANTHROPIC_API_KEY")

    body = {
        "model": MODEL,
        "max_tokens": 1000,
        "temperature": TEMPERATURE,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}],
    }

    try:
        response = httpx.post(
            API_URL,
            json=body,
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMUnavailable(str(exc)) from exc

    text = "".join(
        block.get("text", "") for block in response.json().get("content", []) if block.get("type") == "text"
    )
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"respuesta no es JSON: {text[:200]}") from exc


def _entities(payload: dict) -> set[str]:
    """Todas las entidades nombrables que el modelo tiene permitido mencionar."""
    allowed: set[str] = set()
    for group in payload.get("grupos", []):
        for key in ("persona", "proyecto", "cliente", "rol"):
            value = group.get(key)
            if value:
                allowed.add(str(value).lower())
        for project in group.get("proyectos", []):
            allowed.add(str(project).lower())
    return allowed


def validate_output(generated: dict, payload: dict) -> list[str]:
    """Devuelve la lista de violaciones. Vacía = la salida es usable.

    Chequea dos clases de invento:
      - números que no estaban en el payload
      - nombres propios que no estaban en el payload
    La prosa que conecta es libre: no se valida palabra por palabra.
    """
    violations: list[str] = []
    allowed = _entities(payload)
    allowed_numbers = {
        str(n)
        for group in payload.get("grupos", [])
        for n in (group.get("dias"), group.get("horas"), len(group.get("proyectos", [])))
        if n is not None
    }

    texts = [generated.get("titular", "")] + [item.get("texto", "") for item in generated.get("items", [])]

    for text in texts:
        for number in re.findall(r"\b\d+(?:[.,]\d+)?\b", text):
            if number not in allowed_numbers:
                violations.append(f"número inventado: {number}")

        # Palabras capitalizadas en medio de la frase = probable nombre propio
        for word in re.findall(r"(?<!^)(?<![.!?]\s)\b([A-ZÁÉÍÓÚÑ][\wáéíóúñ]{2,})\b", text):
            if word.lower() not in allowed and not any(word.lower() in a for a in allowed):
                violations.append(f"entidad no verificada: {word}")

    return violations


def compose(payload: dict) -> tuple[dict, list[str]]:
    """Redacta. Devuelve (salida, violaciones). Si hay violaciones, no usarla."""
    generated = _call(payload)
    return generated, validate_output(generated, payload)


def disambiguate_identity(candidate: dict, options: list[dict]) -> dict:
    """Tier de fallback del matching.

    Con los datos provistos este tier NO se activa: los 14 usuarios activos
    resuelven por email exacto. Queda como red de seguridad para producción,
    donde aparecen dominios distintos, alias y emails faltantes.
    """
    payload = {
        "tarea": "identidad",
        "candidato": candidate,
        "opciones": options,
        "instruccion": "Devolvé {\"match_id\": <id o null>, \"confianza\": <0..1>, \"motivo\": \"...\"}",
    }
    result = _call(payload)
    # La opinión del modelo se topea: nunca supera al tier determinístico.
    result["confianza"] = min(float(result.get("confianza", 0)), 0.85)
    return result
