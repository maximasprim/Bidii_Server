"""
The only place in the codebase that turns a provider name string ("openai" /
"gemini") into a live AIProvider instance, and the only place that reads AI
API keys out of settings. Everything else — the ATS screening router, the
job-generation router, the evaluation/generation orchestrators — calls
`get_provider(name)` and never touches an SDK or an API key directly.
"""

from app.config import get_settings
from app.services.ai_providers.base import AIProvider, AIProviderNotConfiguredError

SUPPORTED_PROVIDERS = ("openai", "gemini")


def get_provider(name: str) -> AIProvider:
    settings = get_settings()
    name = (name or "").strip().lower()

    if name == "openai":
        if not settings.openai_api_key:
            raise AIProviderNotConfiguredError(
                "OpenAI is selected but OPENAI_API_KEY isn't set on the server. Add it to .env and restart the backend."
            )
        from app.services.ai_providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key)

    if name == "gemini":
        if not settings.gemini_api_key:
            raise AIProviderNotConfiguredError(
                "Gemini is selected but GEMINI_API_KEY isn't set on the server. Add it to .env and restart the backend."
            )
        from app.services.ai_providers.gemini_provider import GeminiProvider

        return GeminiProvider(api_key=settings.gemini_api_key)

    raise AIProviderNotConfiguredError(
        f"Unknown AI provider {name!r}. Supported providers: {', '.join(SUPPORTED_PROVIDERS)}."
    )


def default_model_for(name: str) -> str:
    settings = get_settings()
    if name == "openai":
        return settings.openai_default_model
    if name == "gemini":
        return settings.gemini_default_model
    return ""


def provider_status() -> dict:
    """
    Whether each provider has a server-side API key configured, and its
    default model — used by the admin UI to grey out a provider it can't
    actually use yet, without ever exposing the key itself.
    """
    settings = get_settings()
    return {
        "openai": {
            "configured": bool(settings.openai_api_key),
            "default_model": settings.openai_default_model,
        },
        "gemini": {
            "configured": bool(settings.gemini_api_key),
            "default_model": settings.gemini_default_model,
        },
    }


def first_configured_provider() -> str | None:
    """
    For server-triggered AI calls with no admin picking a provider in the
    UI (e.g. app/services/branch_assignment.py's nearest-branch fallback,
    run automatically on every public loan application submission) - picks
    whichever provider actually has a key configured, preferring Gemini
    since it's the cheaper/faster default for this kind of low-stakes
    classification task. Returns None if neither is configured, so callers
    can fall back to non-AI behavior instead of raising.
    """
    status = provider_status()
    if status["gemini"]["configured"]:
        return "gemini"
    if status["openai"]["configured"]:
        return "openai"
    return None
