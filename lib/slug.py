"""Slug generation — normalise room names into URL-safe slugs."""

import re
import secrets
import unicodedata

# The three supported slug modes
SLUG_MODES = ("hex8", "name4", "name")

# Transliteration for Latin chars that NFD/NFKD won't decompose
_LATIN_MAP = str.maketrans({
    'Ł': 'L', 'ł': 'l', 'Đ': 'D', 'đ': 'd',
    'Ø': 'O', 'ø': 'o', 'Æ': 'AE', 'æ': 'ae',
    'Ð': 'D', 'ð': 'd', 'Þ': 'Th', 'þ': 'th',
    'ß': 'ss',
})


def normalize_name(name: str) -> str:
    """Slugify a room name: accents→ascii, lowercase, spaces→hyphens,
    remove non-alphanumeric (except hyphens), collapse hyphens, strip edges."""
    name = name.translate(_LATIN_MAP)
    nfkd = unicodedata.normalize("NFKD", name)
    no_combining = "".join(c for c in nfkd if unicodedata.category(c) != "Mn")
    ascii_only = no_combining.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    cleaned = re.sub(r"[^a-z0-9\s-]", "", lower)
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned or "room"


def generate_slug(name: str, mode: str = "name4") -> str:
    """Generate a slug based on the chosen mode.

    hex8  → 8-char random hex (classic)
    name4 → normalized name + 4-char hex suffix (default)
    name  → normalized name only (collision = error)
    """
    if mode == "hex8":
        return secrets.token_hex(4)
    normalized = normalize_name(name)
    if mode == "name":
        return normalized
    suffix = secrets.token_hex(2)
    return f"{normalized}-{suffix}"
