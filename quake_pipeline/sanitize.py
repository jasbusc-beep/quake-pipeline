"""Text cleaning for untrusted strings (API place names, model output)."""

import unicodedata

from .config import MAX_LABEL_CHARS, MAX_SUMMARY_CHARS

_LABEL_PUNCTUATION = set(",.-'()/")


def _is_control(ch: str) -> bool:
    # Unicode categories starting with "C": control, format, private use...
    return unicodedata.category(ch).startswith("C")


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def clean_label(text: object) -> str:
    """Reduce a label to letters, digits, spaces and a few safe marks."""
    if not isinstance(text, str):
        return "Unknown"
    text = unicodedata.normalize("NFKC", text)
    kept = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif _is_control(ch):
            continue
        elif unicodedata.category(ch)[0] in ("L", "M"):  # letters, accents
            kept.append(ch)
        elif unicodedata.category(ch) == "Nd":  # digits
            kept.append(ch)
        elif ch in _LABEL_PUNCTUATION:
            kept.append(ch)
    cleaned = _collapse_whitespace("".join(kept))[:MAX_LABEL_CHARS].strip()
    return cleaned or "Unknown"


def clean_summary(text: object) -> str:
    """Remove control characters and cap the length.

    This does not try to strip HTML. Safety comes from the viewer, which
    only ever shows this as plain text.
    """
    if not isinstance(text, str):
        return ""
    kept = [" " if ch.isspace() else ch for ch in text]
    kept = [ch for ch in kept if not _is_control(ch)]
    return _collapse_whitespace("".join(kept))[:MAX_SUMMARY_CHARS].strip()
