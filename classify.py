"""Frontier vs cheap model classification shared across harness adapters.

The fusion plugin never assumes Jordan's fleet. It reads what each harness
actually has installed and tiers it with lightweight name heuristics.
"""
from __future__ import annotations

FRONTIER_HINTS = (
    "opus", "claude-4", "sonnet-4", "gpt-5.6", "gpt-5.5", "gpt-5.2",
    "grok-4.5", "grok-4.6", "gemini-2.5-pro", "gemini-3", "o3", "o4",
    "thinking", "ultra", "max", "sol", "luna", "terra",
)
CHEAP_HINTS = (
    "flash", "haiku", "mini", "lite", "nano", "fast", "deepseek-chat",
    "deepseek-reasoner", "deepseek-v4-flash", "glm-4-flash", "glm-5-flash",
    "qwen-turbo", "grok-4.1", "grok-4-fast", "gpt-5-mini", "gpt-5-flash",
)

# Providers that route cheap models by default even when the model name is
# ambiguous (custom gateways / cheap relays).
_CHEAP_PROVIDERS = {"deepseek", "inferx", "venice", "zai", "glm", "groq",
                    "xrtoken-cheap", "ds-sp"}


def is_frontier(provider: str, model: str) -> bool:
    name = f"{provider} {model}".lower()
    if any(h in name for h in FRONTIER_HINTS):
        return True
    if any(h in name for h in CHEAP_HINTS):
        return False
    return provider.lower() not in _CHEAP_PROVIDERS
