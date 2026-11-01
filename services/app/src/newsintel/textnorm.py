"""Text normalisation for keyword search in Arabic and English.

Arabic users rarely type diacritics and use alef/yaa/taa-marbuta variants interchangeably, so the
indexed text and the query are both folded to one canonical form. Semantic search does not need this
(embedding models handle it); keyword search does.
"""

import re
import unicodedata

_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"
_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = {
    "the", "a", "an", "of", "in", "on", "and", "or", "to", "is", "was", "for", "with", "where", "what", "who",
    "show", "me", "find", "video", "videos", "news", "did", "about", "that", "this", "which",
    "في", "من", "على", "الى", "إلى", "عن", "ما", "هل", "التي", "الذي", "و", "او",
}  # fmt: skip


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _DIACRITICS.sub("", text).replace(_TATWEEL, "")
    return text.translate(_FOLD).translate(_ARABIC_DIGITS).lower()


def keywords(text: str, max_terms: int = 12) -> list[str]:
    """Distinct normalised query terms, stopwords and 1-char tokens removed, order preserved."""
    seen: dict[str, None] = {}
    for tok in _WORD.findall(normalize(text)):
        if len(tok) > 1 and tok not in _STOP and tok not in seen:
            seen[tok] = None
    return list(seen)[:max_terms]
