"""Query understanding: extract filters from a natural-language question.

Rule-based on purpose (deterministic, testable, works when the LLM is unavailable). Extracted:
* spoken-language filter ("in Arabic", "بالعربية", "in English")
* explicit video ids (vid_<24 hex>)
* modality hints ("on screen", "caption", "text in the video" → OCR emphasis; "shows", "footage" → visual)
The cleaned query keeps the content words for retrieval.
"""

import re
from dataclasses import dataclass

from newsintel.search.retrieval import QueryFilters

_LANG = [
    (re.compile(r"\b(in|spoken in|speaking)\s+arabic\b|بالعربي(ة)?|باللغة العربية", re.I), "ar"),
    (re.compile(r"\b(in|spoken in|speaking)\s+english\b|بالإنجليزي(ة)?|بالانجليزي(ة)?|باللغة الإنجليزية", re.I), "en"),
]
_VID = re.compile(r"\bvid_[0-9a-f]{24}\b")
_OCR = re.compile(r"\b(on[- ]screen|caption(ed)?|headline|ticker|banner|written|text (in|on) the)\b", re.I)
_VISUAL = re.compile(r"\b(shows?|showing|footage|scene|image|picture|looks? like|visible)\b", re.I)
_FILLER = re.compile(
    r"^\s*(please\s+)?(show me|find( me)?|search( for)?|give me|list|which|what|where)\b[\s,]*(the\s+)?"
    r"((news\s+)?videos?\s+)?(where|in which|that|when)?\s*",
    re.I,
)


@dataclass
class ParsedQuery:
    text: str
    filters: QueryFilters
    hints: list[str]


def parse_query(question: str) -> ParsedQuery:
    q = question.strip()
    f = QueryFilters()
    hints: list[str] = []
    for pat, lang in _LANG:
        if pat.search(q):
            f.language = lang
            q = pat.sub(" ", q)
            hints.append(f"language={lang}")
            break
    ids = _VID.findall(q)
    if ids:
        f.video_ids = ids
        q = _VID.sub(" ", q)
        hints.append(f"videos={len(ids)}")
    if _OCR.search(question):
        hints.append("ocr-emphasis")
    if _VISUAL.search(question):
        hints.append("visual")
    if f.language:
        # A spoken-language constraint applies to speech; visual sources have no language field.
        f.sources = {"transcript"}
    cleaned = _FILLER.sub("", q)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.,") or question.strip()
    return ParsedQuery(text=cleaned, filters=f, hints=hints)
