"""Provider-agnostic LLM access.

Listing copy, ad creative and social posts all want a model, but none of them
should care which one. Everything above this module talks to :class:`LLMProvider`,
so swapping Claude for Gemini -- or for a deterministic stub in CI -- is a
constructor argument.

Cloud SDKs are imported lazily inside their providers, so the core package
installs and tests with no third-party dependencies at all. That matters here
more than usual: a system that can spend money should not pull a dependency tree
it does not need.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    """A single completion plus where it came from."""

    text: str
    provider: str
    model: str

    def as_json(self) -> dict | None:
        """Best-effort JSON parse, tolerating fenced code blocks."""
        candidate = self.text.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
        if fence:
            candidate = fence.group(1).strip()
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def as_lines(self, limit: int = 0) -> list[str]:
        """Non-empty lines, stripped of bullet punctuation."""
        lines = [re.sub(r"^\s*[-*•\d.)]+\s*", "", ln).strip() for ln in self.text.splitlines()]
        cleaned = [ln for ln in lines if ln]
        return cleaned[:limit] if limit else cleaned


class LLMProvider(ABC):
    """Minimal surface the agents need: one prompt in, one string out."""

    name: str = "abstract"
    model: str = "unset"

    @abstractmethod
    def complete(
        self, prompt: str, system: str | None = None, temperature: float = 0.0
    ) -> LLMResponse:
        """Return a completion for ``prompt``."""

    def complete_json(self, prompt: str, system: str | None = None) -> dict | None:
        """Completion parsed as a JSON object, or ``None`` if it wasn't one."""
        return self.complete(prompt, system=system, temperature=0.0).as_json()


class StubProvider(LLMProvider):
    """Deterministic offline provider.

    Returns canned responses keyed by substring, falling back to a fixed string.
    Keeps tests hermetic and lets the whole system run with no API key -- which
    is how the demo works end to end on a laptop with no accounts.
    """

    name = "stub"
    model = "stub-1"

    def __init__(self, responses: dict[str, str] | None = None, default: str = "{}") -> None:
        self.responses = responses or {}
        self.default = default
        self.calls: list[str] = []

    def complete(
        self, prompt: str, system: str | None = None, temperature: float = 0.0
    ) -> LLMResponse:
        self.calls.append(prompt)
        for needle, response in self.responses.items():
            if needle.lower() in prompt.lower():
                return LLMResponse(response, self.name, self.model)
        return LLMResponse(self.default, self.name, self.model)


class ClaudeProvider(LLMProvider):
    """Claude via the Anthropic SDK.

    Requires the ``anthropic`` extra and ``ANTHROPIC_API_KEY``.
    """

    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - exercised only with the extra absent
            raise RuntimeError(
                "ClaudeProvider needs the 'anthropic' extra: "
                "pip install 'storefront-agent[anthropic]'"
            ) from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def complete(
        self, prompt: str, system: str | None = None, temperature: float = 0.0
    ) -> LLMResponse:
        client = self._ensure_client()
        message = client.messages.create(
            model=self.model,
            max_tokens=2048,
            temperature=temperature,
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        blocks = [b.text for b in message.content if getattr(b, "type", None) == "text"]
        return LLMResponse("".join(blocks), self.name, self.model)


class GoogleADKProvider(LLMProvider):
    """Gemini via the Google GenAI SDK.

    Requires the ``google`` extra and ``GOOGLE_API_KEY`` (or ADC in a GCP runtime).
    """

    name = "google-adk"

    def __init__(self, model: str = "gemini-2.0-flash", api_key: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from google import genai  # type: ignore import-not-found
        except ImportError as exc:  # pragma: no cover - exercised only with the extra absent
            raise RuntimeError(
                "GoogleADKProvider needs the 'google' extra: "
                "pip install 'storefront-agent[google]'"
            ) from exc
        self._client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        return self._client

    def complete(
        self, prompt: str, system: str | None = None, temperature: float = 0.0
    ) -> LLMResponse:
        client = self._ensure_client()
        contents = f"{system}\n\n{prompt}" if system else prompt
        result = client.models.generate_content(model=self.model, contents=contents)
        return LLMResponse(getattr(result, "text", "") or "", self.name, self.model)


def provider_from_env(default: LLMProvider | None = None) -> LLMProvider:
    """Pick a provider from the environment.

    ``STOREFRONT_LLM`` selects explicitly (``anthropic``/``google``/``stub``).
    Otherwise the first cloud whose API key is present wins, and failing that we
    fall back to the stub so the system always starts.
    """
    choice = os.environ.get("STOREFRONT_LLM", "").strip().lower()
    if choice in ("anthropic", "claude"):
        return ClaudeProvider()
    if choice == "google":
        return GoogleADKProvider()
    if choice == "stub":
        return StubProvider()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeProvider()
    if os.environ.get("GOOGLE_API_KEY"):
        return GoogleADKProvider()
    return default or StubProvider()
