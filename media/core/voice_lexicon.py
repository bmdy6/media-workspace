"""Small, optional vocabulary data for draft refinement, never a replacement pass."""
from __future__ import annotations

import json
import logging
from pathlib import Path

LEXICON_PATH = Path(__file__).resolve().parents[1] / 'config' / 'voice_lexicon.json'
MAX_FILE_BYTES = 16_384
MAX_ENTRIES = 60  # terms plus alias pairs, not 60 in each collection
MAX_TERM_CHARS = 64
logger = logging.getLogger(__name__)


def _valid_term(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TERM_CHARS
        and value == value.strip()
        and all(char.isprintable() for char in value)
        and '<unk>' not in value.lower()
    )


def _read_lexicon() -> dict | None:
    """Read bounded local data; fail open without logging file contents or errors."""
    try:
        with LEXICON_PATH.open('rb') as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError('lexicon too large')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or set(data) != {'terms', 'aliases'}:
            raise ValueError('invalid lexicon shape')
        terms, aliases = data['terms'], data['aliases']
        if not isinstance(terms, list) or not isinstance(aliases, dict):
            raise ValueError('invalid lexicon collections')
        if len(terms) + len(aliases) > MAX_ENTRIES:
            raise ValueError('too many entries')
        if not all(_valid_term(term) for term in terms):
            raise ValueError('invalid term')
        if not all(_valid_term(key) and _valid_term(value) for key, value in aliases.items()):
            raise ValueError('invalid alias')
        return {'terms': terms, 'aliases': aliases}
    except (OSError, UnicodeError, ValueError, RecursionError):
        logger.warning('Voice lexicon unavailable; continuing without lexicon')
        return None


def lexicon_data(text: str) -> dict | None:
    data = _read_lexicon()
    if data is None:
        return None
    return {'terms': data['terms'], 'aliases': {key: value for key, value in data['aliases'].items() if key in text}}


def all_aliases() -> dict[str, str]:
    """Read validated aliases without mutation; unavailable data degrades to empty."""
    data = _read_lexicon()
    return dict(data['aliases']) if data is not None else {}
