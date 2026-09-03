"""The model layer. Uses Google Gemini via its OpenAI-compatible endpoint.

The LLM does exactly two things and no more:
  1. drafts the headline and the "why it matters" of each risk group
  2. breaks ties on identities the deterministic cascade didn't resolve

It never writes facts. Names, dates, hours and links are placed by the code
from the verified payload. The model returns JSON with fixed fields and the
final render is deterministic.

After generating, validate_output checks that no entity appeared that wasn't
in the payload. If one did, the model output is discarded and a template is
used instead.

Provider is swappable: only LLM_BASE_URL, LLM_MODEL and the key env var change.
The grounding and validation logic is identical for any provider.
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
MODEL = os.environ.get("LLM_MODEL", "gemini-flash-lite-latest")
API_KEY_ENV = os.environ.get("LLM_API_KEY_ENV", "GEMINI_API_KEY")

TEMPERATURE = 0.0

SYSTEM_PROMPT = """You draft staffing alerts for a delivery lead.

Strict rules:
- Use ONLY the facts in the JSON you receive. Do not add context, history,
  or assumptions about people, projects or clients.
- Do not invent names, dates, numbers or percentages.
- If a risk is marked certain=false, phrase it as a QUESTION, not an assertion.
- Write in English, direct and brief.
- Respond with VALID JSON ONLY, no markdown, no backticks.

Response format:
{
  "headline": "one line summarizing the overall situation",
  "items": [
    {"id": "<the group id>", "text": "one or two sentences on why it matters"}
  ]
}"""


class LLMUnavailable(Exception):
    """The model did not respond. The agent continues with the deterministic template."""


def _call(payload: dict, system: str = SYSTEM_PROMPT, timeout: float = 90.0) -> dict:
    api_key = os.environ.get(API_KEY_ENV)
    if not api_key:
        raise LLMUnavailable(f"missing {API_KEY_ENV}")

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
        raise LLMUnavailable(f"unexpected response: {response.text[:200]}") from exc

    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"response is not JSON: {text[:200]}") from exc


def _entities(payload: dict) -> set:
    allowed = set()
    for group in payload.get("groups", []):
        for key in ("person", "project", "client", "role"):
            value = group.get(key)
            if value:
                allowed.add(str(value).lower())
        for project in group.get("projects", []):
            allowed.add(str(project).lower())
    return allowed


def validate_output(generated: dict, payload: dict) -> list:
    """Return the list of violations. Empty = output is usable.

    Checks two kinds of invention:
      - numbers that weren't in the payload
      - proper names that weren't in the payload
    Connecting prose is free: it is not validated word by word.
    """
    violations = []
    allowed = _entities(payload)
    allowed_numbers = {
        str(n)
        for group in payload.get("groups", [])
        for n in (group.get("days"), group.get("hours"), len(group.get("projects", [])))
        if n is not None
    }
    texts = [generated.get("headline", "")] + [i.get("text", "") for i in generated.get("items", [])]
    for text in texts:
        for number in re.findall(r"\b\d+(?:[.,]\d+)?\b", text):
            if number not in allowed_numbers:
                violations.append(f"invented number: {number}")
        for word in re.findall(r"(?<!^)(?<![.!?]\s)\b([A-Z][\w]{2,})\b", text):
            if word.lower() not in allowed and not any(word.lower() in a for a in allowed):
                violations.append(f"unverified entity: {word}")
    return violations


def compose(payload: dict):
    generated = _call(payload)
    return generated, validate_output(generated, payload)


def disambiguate_identity(candidate: dict, options: list) -> dict:
    """Fallback tier of the matching cascade.

    With the provided data this tier does NOT fire: the 14 active users
    resolve by exact email. It stays as a safety net for production, where
    different domains, aliases and missing emails show up.
    """
    payload = {
        "task": "identity",
        "candidate": candidate,
        "options": options,
        "instruction": 'Return {"match_id": <id or null>, "confidence": <0..1>, "reason": "..."}',
    }
    result = _call(payload, system="Respond with valid JSON only, no markdown.")
    result["confidence"] = min(float(result.get("confidence", 0)), 0.85)
    return result
