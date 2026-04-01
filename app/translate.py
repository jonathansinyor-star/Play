"""Translation module.

Provider priority:
  1. Google (via deep-translator) – free, no key, rate-limited
  2. DeepL (via deep-translator) – needs DEEPL_API_KEY
  3. LibreTranslate – self-hosted, needs LIBRE_TRANSLATE_URL

Falls back gracefully: if translation fails we still return None so the
caller can proceed with keyword-matching on the original text.
"""

from __future__ import annotations

import asyncio
import logging

from app import config

log = logging.getLogger(__name__)

# ─── Provider implementations ────────────────────────────────────────────────

def _translate_google(text: str, source_lang: str) -> str:
    """Synchronous Google translate via deep-translator."""
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source=source_lang, target="en")
    result = translator.translate(text)
    return result or ""


def _translate_deepl(text: str, source_lang: str) -> str:
    from deep_translator import DeepLTranslator
    lang_map = {"ar": "AR", "he": None, "en": "EN"}  # DeepL doesn't support Hebrew
    src = lang_map.get(source_lang, source_lang.upper())
    if src is None:
        raise ValueError(f"DeepL does not support source language: {source_lang}")
    translator = DeepLTranslator(
        api_key=config.DEEPL_API_KEY, source=src, target="EN-US"
    )
    return translator.translate(text) or ""


def _translate_libre(text: str, source_lang: str) -> str:
    import httpx
    resp = httpx.post(
        f"{config.LIBRE_TRANSLATE_URL}/translate",
        json={"q": text, "source": source_lang, "target": "en"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("translatedText", "")


# ─── Async public API ────────────────────────────────────────────────────────

async def translate_to_english(text: str, source_lang: str) -> tuple[str | None, str | None]:
    """Translate *text* from *source_lang* to English.

    Returns (translated_text, provider_name) on success, (None, None) on failure.
    """
    if not text or not text.strip():
        return None, None
    if source_lang == "en":
        return text, "passthrough"

    provider = config.TRANSLATION_PROVIDER.lower()

    async def _run_in_thread(fn, *args):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    # Try configured provider first, then fall back to Google
    providers_to_try: list[tuple[str, any]] = []

    if provider == "deepl" and config.DEEPL_API_KEY:
        providers_to_try.append(("deepl", _translate_deepl))
    if provider == "libre" and config.LIBRE_TRANSLATE_URL:
        providers_to_try.append(("libre", _translate_libre))

    # Always have Google as last resort
    providers_to_try.append(("google", _translate_google))

    for provider_name, fn in providers_to_try:
        try:
            result = await _run_in_thread(fn, text, source_lang)
            if result and result.strip():
                log.debug("Translated via %s: %s → %s", provider_name, source_lang, "en")
                return result.strip(), provider_name
        except Exception as exc:
            log.warning("Translation via %s failed: %s", provider_name, exc)

    log.error("All translation providers failed for lang=%s, text=%r", source_lang, text[:80])
    return None, None
