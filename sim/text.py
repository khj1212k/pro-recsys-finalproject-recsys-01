"""Kiwi noun extraction shared by the click model and the behavior metrics."""

from functools import lru_cache
from typing import FrozenSet, Iterable

NOUN_TAGS = frozenset({"NNG", "NNP", "SL"})

_KIWI = None


def _get_kiwi():
    global _KIWI
    if _KIWI is None:
        # Raise instead of falling back to a regex tokenizer: a silently weaker
        # tokenizer would change the keyword feature and every calibrated bias.
        from kiwipiepy import Kiwi

        _KIWI = Kiwi()
    return _KIWI


@lru_cache(maxsize=65536)
def nouns(text: str) -> FrozenSet[str]:
    if not text or not text.strip():
        return frozenset()
    tokens = _get_kiwi().tokenize(text)
    return frozenset(t.form.lower() for t in tokens if t.tag in NOUN_TAGS)


def nouns_of_all(texts: Iterable[str]) -> FrozenSet[str]:
    out = set()
    for t in texts:
        out |= nouns(t)
    return frozenset(out)


def jaccard(a: FrozenSet[str], b: FrozenSet[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
