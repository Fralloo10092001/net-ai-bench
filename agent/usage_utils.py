from __future__ import annotations

from typing import Any

pricing = {
    ("openai", "gpt-4o"): {
        "input_per_million": 1.25,
        "output_per_million": 5.00,
    },
    ("openrouter", "openai/gpt-4o-mini"): {
        "input_per_million": 0.60,
        "output_per_million": 2.40,
    },
    ("anthropic", "claude-opus-4-5"): {
        "input_per_million": 5.00,
        "output_per_million": 25.00,
    },
    ("anthropic", "claude-sonnet-4-5"): {
        "input_per_million": 3.00,
        "output_per_million": 15.00,
    },
    ("google", "gemini-2.5-pro"): {
        "input_per_million": 1.25,
        "output_per_million": 10.00,
    },
    ("google", "gemini-2.5-flash"): {
        "input_per_million": 0.30,
        "output_per_million": 2.50,
    },
}


# Estrae i metadati sull'utilizzo dei token da una risposta del modello.
def extract_usage_from_response(response: Any) -> dict[str, int]:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    usage = getattr(response, "usage_metadata", None)
    response_metadata = getattr(response, "response_metadata", None)

    if isinstance(usage, dict):
        prompt_tokens = (
            usage.get("input_tokens")
            or usage.get("prompt_tokens")
            or usage.get("input_token_count")
            or 0
        )
        completion_tokens = (
            usage.get("output_tokens")
            or usage.get("completion_tokens")
            or usage.get("candidates_token_count")
            or 0
        )
        total_tokens = (
            usage.get("total_tokens")
            or (prompt_tokens + completion_tokens)
        )

    elif isinstance(response_metadata, dict):
        meta_usage = response_metadata.get("token_usage") or response_metadata.get("usage") or {}

        if isinstance(meta_usage, dict):
            prompt_tokens = (
                meta_usage.get("prompt_tokens")
                or meta_usage.get("input_tokens")
                or 0
            )
            completion_tokens = (
                meta_usage.get("completion_tokens")
                or meta_usage.get("output_tokens")
                or 0
            )
            total_tokens = (
                meta_usage.get("total_tokens")
                or (prompt_tokens + completion_tokens)
            )

    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
    }


# Somma due dizionari di utilizzo.
def add_usage(acc: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    return {
        "prompt_tokens": acc.get("prompt_tokens", 0) + delta.get("prompt_tokens", 0),
        "completion_tokens": acc.get("completion_tokens", 0) + delta.get("completion_tokens", 0),
        "total_tokens": acc.get("total_tokens", 0) + delta.get("total_tokens", 0),
    }


# Stima il costo per una coppia provider/modello singola.
def _estimate_single_cost_usd(provider: str, model: str, usage: dict[str, int]) -> float:
    key = (provider.lower(), model)
    if key not in pricing:
        return 0.0

    p = pricing[key]
    input_cost = usage.get("prompt_tokens", 0) * p["input_per_million"] / 1_000_000
    output_cost = usage.get("completion_tokens", 0) * p["output_per_million"] / 1_000_000
    return input_cost + output_cost


# Stima il costo totale, supportando sia le esecuzioni con un singolo modello che quelle con planner ed executor.
def estimate_cost_usd(
    provider: str,
    model: str,
    usage: dict[str, int],
    usage_breakdown: dict[str, dict[str, int]] | None = None,
) -> float:
    if "/" not in provider or " | " not in model:
        return round(_estimate_single_cost_usd(provider, model, usage), 8)

    if not usage_breakdown:
        return 0.0

    provider_parts = provider.split("/")
    model_parts = model.split(" | ")

    if len(provider_parts) != 2 or len(model_parts) != 2:
        return 0.0

    total = 0.0

    total += _estimate_single_cost_usd(
        provider_parts[0],
        model_parts[0],
        usage_breakdown.get("planner", {}),
    )
    total += _estimate_single_cost_usd(
        provider_parts[1],
        model_parts[1],
        usage_breakdown.get("executor", {}),
    )
    total += _estimate_single_cost_usd(
        provider_parts[1],
        model_parts[1],
        usage_breakdown.get("validator", {}),
    )

    return round(total, 8)