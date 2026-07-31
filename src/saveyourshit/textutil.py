"""Text helpers, most importantly the Meta "mojibake" repair.

Meta's Download-Your-Information JSON exports are famously broken: each byte of a
UTF-8 sequence is emitted as its own ``\\u00XX`` escape, so ``ř`` arrives as
``\\u00c5\\u0099`` and every emoji/non-Latin name is garbled. The fix is to
re-encode the string as Latin-1 (recovering the original bytes) and decode it as
UTF-8. See https://github.com/cbsuh/fix_fbjson and Meta DYI encoding writeups.
"""

from __future__ import annotations


def fix_meta_mojibake(text: str) -> str:
    """Repair Meta's double-encoded UTF-8 text.

    Safe to call on already-correct strings: if the Latin-1 round-trip fails
    (because the text contains real non-Latin-1 code points), the original is
    returned unchanged.
    """
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def fix_meta_mojibake_deep(obj):
    """Recursively apply :func:`fix_meta_mojibake` to all strings in a JSON tree."""
    if isinstance(obj, str):
        return fix_meta_mojibake(obj)
    if isinstance(obj, list):
        return [fix_meta_mojibake_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {fix_meta_mojibake(k): fix_meta_mojibake_deep(v) for k, v in obj.items()}
    return obj
