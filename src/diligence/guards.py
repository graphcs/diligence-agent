"""Deterministic guards: grounding, markup sanitising, injection patterns.

These run in code, not in a prompt, so a manipulated model cannot talk its way past them.
"""

from __future__ import annotations

import re

INSTRUCTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above) instructions",
    r"disregard (all |any )?(previous|prior|above)",
    r"system override",
    r"\byou are now\b",
    r"\b(ai|llm) assistant\s*:",
    r"\bemail\b.{0,80}\bto\b.{0,40}@",
    r"!\[[^\]]*\]\(",            # markdown image (classic exfiltration channel)
    r"<!--",                      # hidden HTML comment
    r"recommend (investing|that you invest)",
]
_INJECTION_RE = re.compile("|".join(INSTRUCTION_PATTERNS), re.IGNORECASE | re.DOTALL)
# Softer cues: text that addresses an AI or gives orders. Not proof of injection, but worth
# a model screen before the page reaches an agent.
_AI_CUE_RE = re.compile(
    r"\b(ai|llm|language model|chatbot|assistant|automated (summari[sz]\w*|system|tool)s?)\b"
    r"|\b(supersede|override|disregard|instruct\w*)\b|\byou (must|should|are to)\b",
    re.IGNORECASE)
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


def has_ai_cues(text: str) -> bool:
    return bool(_AI_CUE_RE.search(text or ""))


def strip_markup(text: str) -> str:
    """Remove images, links, raw URLs and HTML comments from model-written prose.
    Citations are rendered separately from the ledger, so prose never needs a URL."""
    text = _COMMENT_RE.sub("", text)
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = s.replace("**", "").replace("`", "")
    return _WS_RE.sub(" ", s).strip().lower().strip(" .\"'")


def quote_in_source(quote: str, source_texts: list[str]) -> bool:
    """True if the quote (or each '...'-separated part of it) appears verbatim in a source."""
    parts = [p for p in re.split(r"\.\.\.|…", quote) if len(_norm(p)) >= 8]
    if not parts:
        return False
    haystack = " ".join(_norm(t) for t in source_texts)
    return all(_norm(p) in haystack for p in parts)


def cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
