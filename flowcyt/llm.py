"""
llm.py - DeepSeek API client for FreeFlow.

Thin wrapper around DeepSeek's OpenAI-compatible REST endpoints using the
``requests`` library.  Handles:

    * API-key resolution from env / working-dir / user-home.
    * ``POST /chat/completions`` with optional ``tools`` (function calling).
    * ``GET /user/balance``.
    * Per-response USD cost estimation against published per-token pricing.

DeepSeek revises pricing periodically; edit the ``PRICING`` dict below when
their rate card changes.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)


DEEPSEEK_API_BASE = "https://api.deepseek.com"
CHAT_PATH = "/chat/completions"
BALANCE_PATH = "/user/balance"

# Per-token USD prices.  Update when DeepSeek revises rates.
#   https://api-docs.deepseek.com/quick_start/pricing
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {
        "input": 0.27e-6,
        "input_cache_hit": 0.07e-6,
        "output": 1.10e-6,
    },
    "deepseek-reasoner": {
        "input": 0.55e-6,
        "input_cache_hit": 0.14e-6,
        "output": 2.19e-6,
    },
}


# ---------------------------------------------------------------------------- #
#  API key resolution
# ---------------------------------------------------------------------------- #

ENV_VAR = "DEEPSEEK_API_KEY"
WORKDIR_FILES = ("deepseek_api_key", ".deepseek_api_key")
HOME_KEY_PATH = Path.home() / ".freeflow" / "deepseek_api_key"


def _read_key_file(path: str | os.PathLike) -> str | None:
    try:
        with open(path, "r") as fh:
            key = fh.read().strip()
        return key or None
    except OSError:
        return None


def load_api_key() -> str | None:
    """Resolve the DeepSeek API key from common locations.

    Order:
        1. Environment variable ``DEEPSEEK_API_KEY``.
        2. ``./deepseek_api_key`` (or ``.deepseek_api_key``) in CWD.
        3. ``~/.freeflow/deepseek_api_key``.

    Returns ``None`` if nothing is found, in which case the GUI should
    prompt the user.
    """
    env_key = os.environ.get(ENV_VAR, "").strip()
    if env_key:
        return env_key

    for name in WORKDIR_FILES:
        candidate = Path.cwd() / name
        key = _read_key_file(candidate)
        if key:
            return key

    return _read_key_file(HOME_KEY_PATH)


def save_api_key_to_home(key: str) -> Path:
    """Persist an API key to ``~/.freeflow/deepseek_api_key`` (mode 0600)."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Refusing to save an empty API key.")

    HOME_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HOME_KEY_PATH, "w") as fh:
        fh.write(key + "\n")
    try:
        os.chmod(HOME_KEY_PATH, 0o600)
    except OSError:
        pass  # Best-effort on platforms without POSIX permissions.
    return HOME_KEY_PATH


# ---------------------------------------------------------------------------- #
#  Cost estimation
# ---------------------------------------------------------------------------- #

def estimate_cost(model: str, usage: dict[str, Any] | None) -> float:
    """Estimate USD cost of a single chat completion from its ``usage`` dict.

    Uses ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` when
    present (DeepSeek-specific) to bill cached input at the discounted rate.
    Falls back to ``prompt_tokens`` priced at the regular input rate.
    """
    if not usage:
        return 0.0
    rates = PRICING.get(model)
    if not rates:
        # Default to deepseek-chat rates as a sensible fallback.
        rates = PRICING["deepseek-chat"]

    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if hit or miss:
        prompt_cost = hit * rates["input_cache_hit"] + miss * rates["input"]
    else:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        prompt_cost = prompt_tokens * rates["input"]

    completion_tokens = int(usage.get("completion_tokens") or 0)
    completion_cost = completion_tokens * rates["output"]
    return prompt_cost + completion_cost


# ---------------------------------------------------------------------------- #
#  DeepSeek client
# ---------------------------------------------------------------------------- #

class DeepSeekError(RuntimeError):
    """Raised when the DeepSeek API returns an error response."""


class DeepSeekClient:
    """Minimal DeepSeek REST client (OpenAI-compatible chat schema)."""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = DEEPSEEK_API_BASE,
        timeout: float = 60.0,
    ):
        if not api_key:
            raise ValueError("DeepSeekClient requires a non-empty api_key.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- private helpers ---- #
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.post(
                url, headers=self._headers(),
                data=json.dumps(payload), timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise DeepSeekError(f"Network error contacting DeepSeek: {e}") from e
        if resp.status_code >= 400:
            raise DeepSeekError(
                f"DeepSeek HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise DeepSeekError(f"Invalid JSON from DeepSeek: {e}") from e

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise DeepSeekError(f"Network error contacting DeepSeek: {e}") from e
        if resp.status_code >= 400:
            raise DeepSeekError(
                f"DeepSeek HTTP {resp.status_code}: {resp.text[:500]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise DeepSeekError(f"Invalid JSON from DeepSeek: {e}") from e

    # ---- public API ---- #
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat-completion request.

        Returns the raw JSON dict from DeepSeek.  Caller is responsible for
        inspecting ``choices[0].message`` (which may contain ``content`` or
        ``tool_calls``) and the ``usage`` block.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        return self._post(CHAT_PATH, payload)

    def get_balance(self) -> dict[str, Any]:
        """Return the user balance summary as parsed JSON.

        DeepSeek shape: ``{"is_available": bool, "balance_infos": [
            {"currency": "USD", "total_balance": "...",
             "granted_balance": "...", "topped_up_balance": "..."}]}``.
        """
        return self._get(BALANCE_PATH)

    @staticmethod
    def primary_balance(balance: dict[str, Any]) -> tuple[float, str]:
        """Extract a single (amount, currency) from a balance response.

        Prefers USD if present; otherwise the first entry.
        """
        infos = balance.get("balance_infos") or []
        if not infos:
            return (0.0, "USD")
        chosen = None
        for entry in infos:
            if (entry.get("currency") or "").upper() == "USD":
                chosen = entry
                break
        if chosen is None:
            chosen = infos[0]
        try:
            amount = float(chosen.get("total_balance") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        return amount, (chosen.get("currency") or "USD")
