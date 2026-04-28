from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

load_dotenv()


# Costruisce un client per il modello di chat selezionato.
def get_model(provider: str, model: str, temperature: float = 0, max_tokens: int | None = None):
    provider = provider.lower()

    if provider == "openai":
        kwargs = {
            "model": model,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        kwargs = {
            "model": model,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        return ChatAnthropic(**kwargs)

    if provider == "google":
        kwargs = {
            "model": model,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens

        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")

        kwargs = {
            "model": model,
            "temperature": temperature,
            "api_key": api_key,
            "base_url": "https://openrouter.ai/api/v1",
            "default_headers": {
                "HTTP-Referer": "http://localhost",
                "X-OpenRouter-Title": "NetConfBench",
            },
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        return ChatOpenAI(**kwargs)

    raise ValueError(f"Unsupported provider: {provider}")