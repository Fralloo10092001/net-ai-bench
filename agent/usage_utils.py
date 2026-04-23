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


def add_usage(acc: dict[str, int], delta: dict[str, int]) -> dict[str, int]:
    return {
        "prompt_tokens": acc.get("prompt_tokens", 0) + delta.get("prompt_tokens", 0),
        "completion_tokens": acc.get("completion_tokens", 0) + delta.get("completion_tokens", 0),
        "total_tokens": acc.get("total_tokens", 0) + delta.get("total_tokens", 0),
    }


def estimate_cost_usd(provider: str, model: str, usage: dict[str, int]) -> float:
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

    key = (provider.lower(), model)
    if key not in pricing:
        return 0.0

    p = pricing[key]
    input_cost = usage.get("prompt_tokens", 0) * p["input_per_million"] / 1_000_000
    output_cost = usage.get("completion_tokens", 0) * p["output_per_million"] / 1_000_000
    return round(input_cost + output_cost, 8)
