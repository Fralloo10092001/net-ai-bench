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

    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is not set")

        kwargs = {
            "model": model,
            "temperature": temperature,
            "api_key": api_key,
            "base_url": "https://api.deepseek.com",
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        return ChatOpenAI(**kwargs)

    if provider == "nvidia":
        api_key_var = "NVIDIA_KIMI_API_KEY" if model == "moonshotai/kimi-k2.6" else "NVIDIA_API_KEY"
        api_key = os.environ.get(api_key_var) or os.environ.get("NVIDIA_API_KEY")
        if not api_key:
            raise ValueError(f"{api_key_var} is not set")

        # ChatNVIDIA al momento fallisce il lookup di questo modello quando il catalogo
        # restituisce voci duplicate, quindi usa l'endpoint NVIDIA compatibile con OpenAI.
        top_p = 1 if model == "moonshotai/kimi-k2.6" else 0.95
        chat_template_kwargs = {"thinking": True}
        if model in {"deepseek-ai/deepseek-v4-pro", "deepseek-ai/deepseek-v4-flash"}:
            chat_template_kwargs["reasoning_effort"] = "max"

        kwargs = {
            "model": model,
            "api_key": api_key,
            "base_url": "https://integrate.api.nvidia.com/v1",
            "temperature": temperature,
            "top_p": top_p,
            "extra_body": {
                "chat_template_kwargs": chat_template_kwargs,
            },
        }
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_completion_tokens"] = 16384

        return ChatOpenAI(**kwargs)

    raise ValueError(f"Unsupported provider: {provider}")
