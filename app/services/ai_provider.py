import json
from functools import lru_cache

import requests

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("app.ai_provider")

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency at runtime
    OpenAI = None  # type: ignore[assignment]

_SENTENCE_TRANSFORMER_CLASS = None
_SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED = False


class AIProviderError(RuntimeError):
    pass


def _get_sentence_transformer_class():
    global _SENTENCE_TRANSFORMER_CLASS, _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED
    if _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED:
        return _SENTENCE_TRANSFORMER_CLASS
    _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED = True
    try:
        from sentence_transformers import SentenceTransformer as _SentenceTransformer  # type: ignore
    except ImportError:
        _SENTENCE_TRANSFORMER_CLASS = None
    else:
        _SENTENCE_TRANSFORMER_CLASS = _SentenceTransformer
    return _SENTENCE_TRANSFORMER_CLASS


def get_embedding_provider_status(
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> dict[str, object]:
    provider = normalize_embedding_provider(embedding_provider)
    model = resolve_embedding_model(provider, embedding_model)
    if provider == "local":
        return _get_local_embedding_status(model)
    return {
        "provider": provider,
        "model": model,
        "available": bool(settings.openai_api_key),
        "model_available": True,
        "detail": "OpenAI embedding API is configured." if settings.openai_api_key else "OpenAI API key is missing.",
    }


def get_ai_provider_status(*, ai_provider: str | None = None, ai_model: str | None = None) -> dict[str, object]:
    provider = normalize_ai_provider(ai_provider)
    model = resolve_ai_model(provider, ai_model)
    if provider == "ollama":
        return _get_ollama_status(model)
    return {
        "provider": provider,
        "model": model,
        "available": bool(settings.openai_api_key),
        "model_available": True,
        "base_url": None,
        "detail": "OpenAI API key is configured." if settings.openai_api_key else "OpenAI API key is missing.",
        "models": [],
    }


def normalize_ai_provider(value: str | None) -> str:
    provider = (value or settings.ai_provider or "openai").strip().casefold()
    if provider in {"openai", "ollama"}:
        return provider
    raise AIProviderError(f"Unsupported AI provider: {value}")


def normalize_embedding_provider(value: str | None) -> str:
    provider = (value or settings.embedding_provider or "openai").strip().casefold()
    if provider in {"openai", "local"}:
        return provider
    raise AIProviderError(f"Unsupported embedding provider: {value}")


def resolve_ai_model(provider: str, model: str | None) -> str:
    if model and model.strip():
        return model.strip()
    if provider == "ollama":
        return settings.ollama_model
    return settings.ai_model or settings.openai_model


def resolve_embedding_model(provider: str, model: str | None) -> str:
    if model and model.strip():
        return model.strip()
    if provider == "local":
        return settings.local_embedding_model
    return settings.embedding_model or settings.openai_embedding_model


def embed_text(
    *,
    text: str,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[float] | None:
    provider = normalize_embedding_provider(embedding_provider)
    model = resolve_embedding_model(provider, embedding_model)
    if provider == "local":
        return _embed_text_with_local_model(text=text, model=model)
    return _embed_text_with_openai(text=text, model=model)


def generate_text(
    *,
    instructions: str,
    input_text: str,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    openai_api_key: str | None = None,
) -> str:
    provider = normalize_ai_provider(ai_provider)
    model = resolve_ai_model(provider, ai_model)
    logger.info("AI text request provider=%s model=%s", provider, model)
    if provider == "ollama":
        return _generate_text_with_ollama(model=model, instructions=instructions, input_text=input_text)
    return _generate_text_with_openai(
        model=model,
        instructions=instructions,
        input_text=input_text,
        api_key=openai_api_key or settings.openai_api_key,
    )


def _generate_text_with_openai(
    *,
    model: str,
    instructions: str,
    input_text: str,
    api_key: str | None,
) -> str:
    if not api_key:
        logger.warning("OpenAI text request skipped because API key is missing model=%s", model)
        raise AIProviderError("OpenAI API key is missing.")
    if OpenAI is None:
        logger.error("OpenAI package is not installed for model=%s", model)
        raise AIProviderError("The Python 'openai' package is not installed.")

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        instructions=instructions,
        input=input_text,
        store=False,
    )
    logger.info("OpenAI text request completed model=%s", model)
    return (response.output_text or "").strip()


def _embed_text_with_openai(*, text: str, model: str) -> list[float] | None:
    if not settings.openai_api_key:
        logger.warning("OpenAI embedding request skipped because API key is missing model=%s", model)
        return None
    if OpenAI is None:
        logger.error("OpenAI package is not installed for embedding model=%s", model)
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(
        model=model,
        input=text[:8000],
    )
    if not response.data:
        logger.warning("OpenAI embedding response returned no data model=%s", model)
        return None
    logger.info("OpenAI embedding request completed model=%s", model)
    return list(response.data[0].embedding)


def _embed_text_with_local_model(*, text: str, model: str) -> list[float] | None:
    sentence_transformer_cls = _get_sentence_transformer_class()
    if sentence_transformer_cls is None:
        logger.warning("Local embedding request skipped because sentence-transformers is missing model=%s", model)
        return None
    encoder = _load_local_embedding_model(model)
    vector = encoder.encode(text[:8000], normalize_embeddings=True)
    logger.info("Local embedding request completed model=%s", model)
    return [float(value) for value in vector]


def _generate_text_with_ollama(
    *,
    model: str,
    instructions: str,
    input_text: str,
) -> str:
    prompt = f"{instructions}\n\n{input_text}"
    try:
        response = requests.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
            },
            timeout=300,
        )
    except requests.RequestException as exc:
        logger.warning("Ollama request failed model=%s error=%s", model, exc)
        raise AIProviderError(f"Ollama request failed: {exc}") from exc

    if not response.ok:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload)
        except ValueError:
            detail = response.text.strip()
        logger.warning("Ollama returned error model=%s detail=%s", model, detail or response.status_code)
        raise AIProviderError(f"Ollama request failed: {detail or response.status_code}")

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        logger.warning("Ollama returned invalid JSON model=%s error=%s", model, exc)
        raise AIProviderError(f"Ollama returned invalid JSON: {exc}") from exc

    text = str(payload.get("response") or "").strip()
    if not text:
        logger.warning("Ollama returned empty response model=%s", model)
        raise AIProviderError("Ollama returned an empty response.")
    logger.info("Ollama text request completed model=%s", model)
    return text


def _get_ollama_status(model: str) -> dict[str, object]:
    base_url = settings.ollama_base_url.rstrip("/")
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=10)
    except requests.RequestException as exc:
        return {
            "provider": "ollama",
            "model": model,
            "available": False,
            "model_available": False,
            "base_url": settings.ollama_base_url,
            "detail": f"Ollama is not reachable: {exc}",
            "models": [],
        }

    if not response.ok:
        detail = response.text.strip() or f"HTTP {response.status_code}"
        return {
            "provider": "ollama",
            "model": model,
            "available": False,
            "model_available": False,
            "base_url": settings.ollama_base_url,
            "detail": f"Ollama returned an error: {detail}",
            "models": [],
        }

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        return {
            "provider": "ollama",
            "model": model,
            "available": False,
            "model_available": False,
            "base_url": settings.ollama_base_url,
            "detail": f"Ollama returned invalid JSON: {exc}",
            "models": [],
        }

    models = payload.get("models") or []
    available_models = sorted(
        {
            str(entry.get("model") or entry.get("name") or "").strip()
            for entry in models
            if str(entry.get("model") or entry.get("name") or "").strip()
        }
    )
    requested_aliases = {model}
    if ":" not in model:
        requested_aliases.add(f"{model}:latest")
    normalized_available = set(available_models)
    model_available = any(alias in normalized_available for alias in requested_aliases)
    return {
        "provider": "ollama",
        "model": model,
        "available": True,
        "model_available": model_available,
        "base_url": settings.ollama_base_url,
        "detail": (
            f"Ollama is reachable and model '{model}' is installed."
            if model_available
            else f"Ollama is reachable, but model '{model}' is not installed."
        ),
        "models": available_models,
    }


def _get_local_embedding_status(model: str) -> dict[str, object]:
    if _get_sentence_transformer_class() is None:
        return {
            "provider": "local",
            "model": model,
            "available": False,
            "model_available": False,
            "detail": "sentence-transformers is not installed.",
        }
    return {
        "provider": "local",
        "model": model,
        "available": True,
        "model_available": True,
        "detail": (
            f"Local embedding support is available for '{model}'. "
            "The model will be loaded on first semantic-search use."
        ),
    }


@lru_cache(maxsize=4)
def _load_local_embedding_model(model: str):
    sentence_transformer_cls = _get_sentence_transformer_class()
    if sentence_transformer_cls is None:
        raise AIProviderError("sentence-transformers is not installed.")
    return sentence_transformer_cls(model)
