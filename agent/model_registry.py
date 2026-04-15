from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
import os
from dotenv import load_dotenv

load_dotenv()

#Questo file è una factory per i modelli
def get_model(provider: str, model: str, temperature: float = 0):
    provider = provider.lower()

    if provider == "openai":
        return ChatOpenAI(
            model=model,
            temperature=temperature,
        )

    if provider == "anthropic":
        return ChatAnthropic(
            model=model,
            temperature=temperature,
        )

    if provider == "google":
        return ChatGoogleGenerativeAI(
            model=model,
            temperature=temperature,
        )
    
    if provider == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")

        return ChatOpenAI(
            model=model,
            temperature=temperature,
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "HTTP-Referer": "http://localhost",
                "X-OpenRouter-Title": "NetConfBench",
            },
        )

    raise ValueError(f"Unsupported provider: {provider}")