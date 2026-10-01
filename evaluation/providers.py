from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

class ProviderError(RuntimeError):
    pass


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 300) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ProviderError(f"HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise ProviderError(str(exc)) from exc


@dataclass(frozen=True)
class ModelProvider:
    spec: dict[str, Any]

    def _api_key(self) -> str:
        configured = str(self.spec.get("api_key", "")).strip()
        if configured:
            return configured
        env_name = self.spec.get("api_key_env", "")
        if not env_name:
            raise ProviderError("API key is empty")
        value = os.environ.get(env_name, "")
        if not value:
            raise ProviderError(f"missing API key environment variable: {env_name}")
        return value

    def generate(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int = 4096) -> tuple[str, dict[str, Any]]:
        provider = self.spec["provider"]
        if provider in {"openai-compatible", "vllm"}:
            return self._openai(system_prompt, user_prompt, temperature, max_tokens)
        if provider == "gemini":
            return self._gemini(system_prompt, user_prompt, temperature, max_tokens)
        raise ProviderError(f"unsupported provider: {provider}")

    def _openai(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        base = self.spec["base_url"].rstrip("/")
        key = self._api_key()
        payload = {
            "model": self.spec["model_id"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        raw = _post(
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {key}"},
            payload,
            timeout=int(self.spec.get("timeout_seconds", 300)),
        )
        try:
            return raw["choices"][0]["message"]["content"], raw
        except Exception as exc:
            raise ProviderError(f"unexpected OpenAI-compatible response: {raw}") from exc

    def _gemini(self, system_prompt: str, user_prompt: str, temperature: float, max_tokens: int) -> tuple[str, dict[str, Any]]:
        base = self.spec["base_url"].rstrip("/")
        key = urllib.parse.quote(self._api_key())
        model = self.spec["model_id"]
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
        }
        raw = _post(
            f"{base}/models/{model}:generateContent?key={key}",
            {},
            payload,
            timeout=int(self.spec.get("timeout_seconds", 300)),
        )
        try:
            parts = raw["candidates"][0]["content"]["parts"]
            return "".join(part.get("text", "") for part in parts), raw
        except Exception as exc:
            raise ProviderError(f"unexpected Gemini response: {raw}") from exc
