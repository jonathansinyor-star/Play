"""Text normalization.

Preserves the original text; only produces a cleaned copy for matching.
"""

from __future__ import annotations

import re
import unicodedata


def normalize(text: str) -> str:
    """Return a cleaned, lower-cased copy of *text* suitable for keyword matching.

    Does NOT modify the original – callers should keep raw_text separately.
    """
    if not text:
        return ""
    # Unicode NFC normalization
    text = unicodedata.normalize("NFC", text)
    # Remove zero-width chars and soft hyphens
    text = re.sub(r"[\u200b-\u200f\u00ad\ufeff]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    # Lower-case for matching (preserves Arabic / Hebrew case-insensitivity)
    text = text.lower()
    return text.strip()


def strip_emojis(text: str) -> str:
    """Remove emoji characters (useful for language detection)."""
    emoji_re = re.compile(
        "[\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\u2600-\u26FF\u2700-\u27BF]+",
        flags=re.UNICODE,
    )
    return emoji_re.sub("", text)


def clean_for_langdetect(text: str) -> str:
    """Produce a string better-suited for language detection."""
    text = strip_emojis(text)
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove @mentions and #hashtags
    text = re.sub(r"[@#]\w+", "", text)
    return text.strip()
