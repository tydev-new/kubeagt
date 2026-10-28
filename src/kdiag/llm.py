"""LLM providers. One real client for any OpenAI-compatible chat completions API
(OpenRouter with a Grok model by default), plus mock providers for tests and keyless runs.

Only the Python standard library is used for HTTP.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .redact import redact_text

# Provider presets. Every field can still be overridden by env vars or CLI flags, so any
# other OpenAI-compatible endpoint (vLLM, Ollama, LiteLLM, Azure, ...) works via "custom".
# Model slugs change; check the provider's current model list (for OpenRouter:
# https://openrouter.ai/models?q=grok) and override with --llm-model / KDIAG_LLM_MODEL.
PRESETS: Dict[str, Dict[str, Any]] = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "x-ai/grok-4.7",
                   "api_key_env": "OPENROUTER_API_KEY", "price_in_per_mtok": 2.0, "price_out_per_mtok": 6.0},
    "xai": {"base_url": "https://api.x.ai/v1", "model": "grok-4.7",
            "api_key_env": "XAI_API_KEY", "price_in_per_mtok": 2.0, "price_out_per_mtok": 6.0},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "",
               "api_key_env": "OPENAI_API_KEY", "price_in_per_mtok": 2.0, "price_out_per_mtok": 10.0},
    "custom": {"base_url": "", "model": "", "api_key_env": "KDIAG_LLM_API_KEY",
               "price_in_per_mtok": 2.0, "price_out_per_mtok": 6.0},
}
DEFAULT_PROVIDER = "openrouter"
DEFAULT_BASE_URL = PRESETS[DEFAULT_PROVIDER]["base_url"]
DEFAULT_MODEL = PRESETS[DEFAULT_PROVIDER]["model"]
DEFAULT_KEY_ENV = PRESETS[DEFAULT_PROVIDER]["api_key_env"]


class LLMError(RuntimeError):
    pass


@dataclass
class LLMConfig:
    provider: str = DEFAULT_PROVIDER
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key_env: str = DEFAULT_KEY_ENV
    timeout: int = 120
    temperature: float = 0.1
    # USD per million tokens, used only for the spend cap. Override for your provider.
    price_in_per_mtok: float = 2.0
    price_out_per_mtok: float = 6.0

    @classmethod
    def from_env(cls, **overrides: Any) -> "LLMConfig":
        """Precedence: CLI overrides, then KDIAG_LLM_* env vars, then the provider preset."""
        env = os.environ
        provider = overrides.pop("provider", None) or env.get("KDIAG_LLM_PROVIDER") or DEFAULT_PROVIDER
        if provider not in PRESETS:
            raise LLMError(f"unknown LLM provider {provider!r}; choose one of {', '.join(PRESETS)}")
        p = PRESETS[provider]
        cfg = cls(
            provider=provider,
            base_url=env.get("KDIAG_LLM_BASE_URL") or p["base_url"],
            model=env.get("KDIAG_LLM_MODEL") or p["model"],
            api_key_env=env.get("KDIAG_LLM_API_KEY_ENV") or p["api_key_env"],
            price_in_per_mtok=float(env.get("KDIAG_PRICE_IN_PER_MTOK") or p["price_in_per_mtok"]),
            price_out_per_mtok=float(env.get("KDIAG_PRICE_OUT_PER_MTOK") or p["price_out_per_mtok"]),
        )
        for k, v in overrides.items():
            if v is not None:
                setattr(cfg, k, v)
        return cfg

    def validate(self) -> None:
        if not self.base_url:
            raise LLMError(f"provider {self.provider!r} needs a base URL: set --llm-base-url or KDIAG_LLM_BASE_URL")
        if not self.model:
            raise LLMError(f"provider {self.provider!r} needs a model: set --llm-model or KDIAG_LLM_MODEL")

    def api_key(self) -> Optional[str]:
        v = os.environ.get(self.api_key_env, "").strip()
        return v or None


@dataclass
class ChatResponse:
    message: Dict[str, Any]
    usage: Dict[str, int] = field(default_factory=dict)


class ChatClient:
    name = "base"

    def chat(self, messages: List[dict], tools: List[dict]) -> ChatResponse:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatClient(ChatClient):
    """POST {base_url}/chat/completions with tool definitions."""

    def __init__(self, cfg: LLMConfig, api_key: Optional[str] = None, retries: int = 3):
        cfg.validate()
        self.cfg = cfg
        self.key = api_key or cfg.api_key()
        if not self.key:
            raise LLMError(f"no API key: set {cfg.api_key_env}")
        self.retries = retries
        self.name = f"{cfg.model} @ {cfg.base_url} ({cfg.provider})"

    def _extra_headers(self) -> Dict[str, str]:
        # Optional OpenRouter attribution headers; harmless elsewhere but only sent to OpenRouter.
        if "openrouter.ai" in self.cfg.base_url:
            return {"HTTP-Referer": "https://github.com/tydev-new/kdiag", "X-Title": "kdiag"}
        return {}

    def chat(self, messages: List[dict], tools: List[dict]) -> ChatResponse:
        body = {"model": self.cfg.model, "messages": messages, "temperature": self.cfg.temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        data = json.dumps(body).encode()
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        last_err = ""
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.key}",
                "User-Agent": "kdiag/0.1", **self._extra_headers()})
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                choice = (payload.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                return ChatResponse(message=msg, usage=payload.get("usage") or {})
            except urllib.error.HTTPError as e:
                detail = redact_text(e.read().decode("utf-8", "replace"))[:500]
                last_err = f"HTTP {e.code}: {detail}"
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(last_err) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"network error: {e}"
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise LLMError(last_err) from None
        raise LLMError(last_err)


def tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM(ChatClient):
    """Returns pre-written assistant messages in order. For unit tests."""
    name = "scripted-mock"

    def __init__(self, script: List[dict], usage: Optional[Dict[str, int]] = None):
        self.script = list(script)
        self.calls: List[List[dict]] = []
        self.usage = usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    def chat(self, messages: List[dict], tools: List[dict]) -> ChatResponse:
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            return ChatResponse({"role": "assistant", "content": "done"}, dict(self.usage))
        step = self.script.pop(0)
        if callable(step):
            step = step(messages)
        return ChatResponse(step, dict(self.usage))


HINTS_RE = re.compile(r"<candidate_fixes>\s*(.*?)\s*</candidate_fixes>", re.S)


class HintFollowingMock(ChatClient):
    """Keyless stand-in for a real model. It exercises the agent loop for real (it calls
    read-only kubectl tools against the cluster) and then proposes the first deterministic
    candidate fix it was given. It has no reasoning of its own; use it to test plumbing."""
    name = "mock (follows rule hints)"

    def chat(self, messages: List[dict], tools: List[dict]) -> ChatResponse:
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if n_tool == 0:
            return ChatResponse({"role": "assistant", "content": None, "tool_calls": [
                tool_call("kubectl", {"args": ["get", "pods", "-o", "wide"]}, "m1")]}, usage)
        if n_tool == 1:
            return ChatResponse({"role": "assistant", "content": None, "tool_calls": [
                tool_call("kubectl", {"args": ["get", "events", "--sort-by", ".lastTimestamp"]}, "m2")]}, usage)
        task = next((m.get("content") or "" for m in messages if m.get("role") == "user"), "")
        m = HINTS_RE.search(task)
        hints = json.loads(m.group(1)) if m else []
        rejected = [m2.get("content", "") for m2 in messages if m2.get("role") == "tool" and "INVALID" in (m2.get("content") or "")]
        idx = len(rejected)
        if idx < len(hints):
            h = hints[idx]
            return ChatResponse({"role": "assistant", "content": None, "tool_calls": [tool_call("propose_fix", {
                "kind": h["kind"], "name": h["name"], "patch_type": h["patch_type"], "patch": h["patch"],
                "diagnosis": h.get("diagnosis", ""), "evidence": h.get("evidence", []),
                "reasoning": "Mock provider: accepting deterministic candidate " + h.get("rule", ""),
                "rule": h.get("rule")}, f"m{3 + idx}")]}, usage)
        return ChatResponse({"role": "assistant", "content": None, "tool_calls": [
            tool_call("finish", {"summary": "Mock provider: no candidate fix to propose."}, "mf")]}, usage)


def make_client(mode: str, cfg: LLMConfig) -> Optional[ChatClient]:
    """mode: auto | none | mock | live. auto uses the configured provider when its key env var is set.
    "openai", "xai" and "grok" are accepted as older aliases of "live"."""
    if mode == "none":
        return None
    if mode == "mock":
        return HintFollowingMock()
    if mode in ("live", "openai", "xai", "grok"):
        return OpenAICompatClient(cfg)
    if mode == "auto":
        return OpenAICompatClient(cfg) if cfg.api_key() else None
    raise LLMError(f"unknown --llm mode {mode!r}")
