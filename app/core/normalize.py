"""Normalization used for vendor/invoice-number matching and dedup keys."""

import re
import unicodedata

_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "llp",
    "ltd",
    "limited",
    "gmbh",
    "sarl",
    "sa",
    "bv",
    "co",
    "corp",
    "corporation",
    "company",
    "plc",
}

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_vendor_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).casefold()
    text = _PUNCTUATION.sub(" ", text)
    tokens = _WHITESPACE.split(text.strip())
    while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(token for token in tokens if token)


def normalize_invoice_number(number: str) -> str:
    text = unicodedata.normalize("NFKC", number).upper()
    text = re.sub(r"[\s\-#/.]", "", text)
    return text.lstrip("0") or "0"
