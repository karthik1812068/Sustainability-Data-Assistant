"""Optional LLM client."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("kuki.llm")


def llm_available() -> bool:
    return bool(settings.openai_api_key or settings.groq_api_key or settings.gemini_api_key)


def complete_json(system: str, user: str) -> dict[str, Any] | None:
    text = complete(system, user + "\n\nReturn a single JSON object only.")
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None


def complete(system: str, user: str) -> str | None:
    try:
        if settings.openai_api_key:
            return _openai_compatible(
                settings.openai_base_url,
                settings.openai_api_key,
                settings.openai_model,
                system,
                user,
            )
        if settings.groq_api_key:
            return _openai_compatible(
                "https://api.groq.com/openai/v1",
                settings.groq_api_key,
                settings.groq_model,
                system,
                user,
            )
        if settings.gemini_api_key:
            return _gemini(system, user)
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
    return None


def _openai_compatible(base: str, key: str, model: str, system: str, user: str) -> str:
    url = base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    with httpx.Client(timeout=60) as client:
        r = client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _gemini(system: str, user: str) -> str:
    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0},
    }
    with httpx.Client(timeout=60) as client:
        r = client.post(url, params={"key": settings.gemini_api_key}, json=payload)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
