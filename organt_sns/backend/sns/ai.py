"""생성형 AI 연동 (F1302) — OpenAI 호환 챗 엔드포인트 트랜스포트.

AI_API_KEY/AI_BASE_URL이 설정되면 LLM으로 협업 브리핑·추천 사유를 생성한다.
미설정 시 호출부가 규칙기반 폴백을 쓰도록 AINotConfigured를 던진다(데모는 키 없이도 동작).
egress는 CCR 프록시(HTTPS_PROXY)를 requests가 자동 경유 — verify는 프록시 CA 번들이 있으면 사용.
"""
import os

import requests
from django.conf import settings

_CA = "/root/.ccr/ca-bundle.crt"


class AINotConfigured(Exception):
    """AI_API_KEY/AI_BASE_URL 미설정 — 호출부는 폴백으로 처리."""


def is_configured():
    return bool(getattr(settings, "AI_API_KEY", "") and getattr(settings, "AI_BASE_URL", ""))


def chat(messages, max_tokens=600, temperature=0.4, timeout=40):
    """OpenAI 호환 /chat/completions 호출 → 생성 텍스트(str). 미설정/실패 시 예외."""
    if not is_configured():
        raise AINotConfigured("AI_API_KEY/AI_BASE_URL 미설정")
    url = settings.AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.AI_API_KEY}",
               "Content-Type": "application/json"}
    body = {"model": getattr(settings, "AI_MODEL", "gpt-4o-mini"),
            "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    verify = _CA if os.path.exists(_CA) else True
    r = requests.post(url, json=body, headers=headers, timeout=timeout, verify=verify)
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"]["content"] or "").strip()
