"""7-card hand evaluation (used only for display / showdown narration)."""

from __future__ import annotations

import itertools

RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANK_ORDER)}
SUIT_SYMBOL = {"C": "♣", "D": "♦", "H": "♥", "S": "♠"}

_CATEGORY_EN = {
    8: "Royal Flush",
    7: "Straight Flush",
    6: "Four of a Kind",
    5: "Full House",
    4: "Flush",
    3: "Straight",
    2: "Three of a Kind",
    1: "Two Pair",
    0: "Pair",
    -1: "High Card",
}


def _evaluate5(cards) -> tuple:
    """Return a comparable rank tuple for exactly 5 cards (larger is better)."""
    ranks = sorted((RANK_VALUE[c[1]] for c in cards), reverse=True)
    suits = [c[0] for c in cards]

    is_flush = len(set(suits)) == 1
    unique = sorted(set(ranks), reverse=True)

    # wheel straight (A-2-3-4-5)
    straight_high = None
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            straight_high = unique[0]
        elif unique == [12, 3, 2, 1, 0]:
            straight_high = 3  # 5-high straight

    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    # sort by (count, rank) descending
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    pattern = [g[1] for g in groups]
    kickers = [g[0] for g in groups]

    if is_flush and straight_high is not None:
        if straight_high == 12:
            return (8, straight_high)
        return (7, straight_high)
    if pattern == [4, 1]:
        return (6, kickers[0], kickers[1])
    if pattern == [3, 2]:
        return (5, kickers[0], kickers[1])
    if is_flush:
        return (4, *ranks)
    if straight_high is not None:
        return (3, straight_high)
    if pattern == [3, 1, 1]:
        return (2, kickers[0], kickers[1], kickers[2])
    if pattern == [2, 2, 1]:
        return (1, kickers[0], kickers[1], kickers[2])
    if pattern == [2, 1, 1, 1]:
        return (0, kickers[0], kickers[1], kickers[2], kickers[3])
    return (-1, *ranks)


def best_of(cards) -> tuple:
    """Best 5-card rank tuple out of 5..7 cards."""
    if len(cards) < 5:
        return (-2,)
    return max(_evaluate5(c) for c in itertools.combinations(cards, 5))


def describe_en(cards) -> str:
    """English name of the made hand, e.g. 'Full House (K K K 9 9)'."""
    return _describe(cards, _CATEGORY_EN)


def _describe(cards, categories) -> str:
    if len(cards) < 5:
        return "—"
    best_combo, best_rank = None, None
    for combo in itertools.combinations(cards, 5):
        r = _evaluate5(combo)
        if best_rank is None or r > best_rank:
            best_rank, best_combo = r, combo
    category = best_rank[0]
    pretty = " ".join(
        RANK_ORDER[RANK_VALUE[c[1]]] + SUIT_SYMBOL[c[0]] for c in best_combo
    )
    return f"{categories[category]} ({pretty})"


def card_text(card) -> str:
    return RANK_ORDER[RANK_VALUE[card[1]]] + SUIT_SYMBOL[card[0]]
