"""Defence-in-depth helpers for untrusted text that reaches, or comes back from, a language model.

What this is: deterministic, heuristic filters. They reduce the chance that retrieved documents or other agents'
output steer a model, and that personal data or credentials leave the system. They do NOT make prompt injection
impossible; a determined attacker can phrase around any pattern list. Treat them as one layer next to the
prompt-level "untrusted data" labels, least-privilege tools and human review.

Scope:
  * ``neutralize_untrusted``  - strips control characters and defuses the boundary markers this application uses, so
    a document cannot close the "untrusted" block and speak as the system.
  * ``find_injection_markers`` / ``is_probable_injection`` - flags common instruction-override phrasing.
  * ``redact_pii``            - masks e-mail addresses, phone numbers, US SSNs and Luhn-valid card numbers.
  * ``scrub_model_output``    - removes leaked boundary markers and credential-shaped strings, and bounds length.
"""

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060\ufeff]")

# Boundary markers used in this application's prompts (see app/main.py and app/core/orchestrator.py).
_BOUNDARY_TAG = re.compile(r"<\s*/?\s*untrusted[\w\s=\"'.:/-]*>", re.IGNORECASE)
_BOUNDARY_PHRASE = re.compile(r"UNTRUSTED\s+REFERENCE\s+MATERIAL", re.IGNORECASE)
_REFERENCE_HEADER = re.compile(r"\[\s*Reference\s+\d+\s*\|", re.IGNORECASE)

_INJECTION_PATTERNS = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("override_instructions",
         r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|system|your)\b"
         r"[^.\n]{0,30}\b(instructions?|prompts?|rules?|guidelines?|directives?)\b"),
        ("role_reassignment", r"\b(you are now|from now on,? you|act as|pretend to be)\b[^.\n]{0,60}\b(assistant|ai|model|admin|developer|dan|system)\b"),
        ("reveal_prompt", r"\b(reveal|show|print|repeat|output|leak)\b[^.\n]{0,40}\b(system prompt|hidden prompt|initial instructions|your instructions)\b"),
        ("fake_system_turn", r"(^|\n)\s*(system|assistant)\s*:\s*\S"),
        ("chat_template_token", r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]"),
        ("exfiltration", r"\b(send|post|upload|exfiltrate|forward)\b[^.\n]{0,60}\b(to|at)\b[^.\n]{0,40}(https?://|@)"),
    )
)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_SECRET_SHAPES = tuple(
    re.compile(p) for p in (
        r"sk-ant-[A-Za-z0-9_-]{10,}",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"xox[abprs]-[A-Za-z0-9-]{10,}",
        r"AIza[0-9A-Za-z_-]{35}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)

DEFAULT_OUTPUT_LIMIT = 20000


def neutralize_untrusted(text, max_chars=None):
    """Return ``text`` with control characters removed and prompt boundary markers defused."""
    value = _CONTROL_CHARS.sub("", str(text or ""))
    value = _BOUNDARY_TAG.sub("[removed-marker]", value)
    value = _BOUNDARY_PHRASE.sub("[removed-marker]", value)
    value = _REFERENCE_HEADER.sub("[removed-marker |", value)
    if max_chars is not None:
        value = value[:max(0, max_chars)]
    return value


def find_injection_markers(text):
    """Names of the instruction-override patterns found in ``text`` (empty when none)."""
    value = str(text or "")
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(value)]


def is_probable_injection(text):
    return bool(find_injection_markers(text))


def _luhn_valid(digits):
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _mask_card(match):
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return "[REDACTED-CARD]"
    return match.group(0)


def redact_pii(text):
    """Mask e-mail addresses, SSNs, Luhn-valid card numbers and phone numbers."""
    value = str(text or "")
    value = _EMAIL.sub("[REDACTED-EMAIL]", value)
    value = _SSN.sub("[REDACTED-SSN]", value)
    value = _CARD.sub(_mask_card, value)
    value = _PHONE.sub("[REDACTED-PHONE]", value)
    return value


def sanitize_retrieved_text(text, max_chars=None):
    """Prepare retrieved document text for a prompt: defuse markers, mask PII.

    Returns ``(clean_text, flagged_markers)``. Flagged text is kept (a document may legitimately discuss prompt injection)
    but the caller can drop it or label it; the application drops it from model context.
    """
    markers = find_injection_markers(text)
    clean = redact_pii(neutralize_untrusted(text))
    if max_chars is not None:
        clean = clean[:max(0, max_chars)]
    return clean, markers


def scrub_model_output(text, max_chars=DEFAULT_OUTPUT_LIMIT):
    """Remove leaked boundary markers and credential-shaped strings from model output, and bound its length."""
    value = _CONTROL_CHARS.sub("", str(text or ""))
    value = _BOUNDARY_TAG.sub("", value)
    for pattern in _SECRET_SHAPES:
        value = pattern.sub("[REDACTED-SECRET]", value)
    return value[:max_chars]
