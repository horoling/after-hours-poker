"""Unified strong opponent engine ("GTO-lite") for the 6-max table.

AlphaHoldem's weights are heads-up only (its state encoding has two seat
channels), so the 6-max table runs *five seats of the same strong heuristic
engine* instead. The design goal is a coach-grade baseline, not 5 gimmick
personalities:

* preflop — every one of the 169 hand classes is ranked by simulated all-in
  equity vs one random hand (computed once, cached to ``preflop_equity.json``
  next to this file). Decisions come from percentile ranges per position
  (UTG ~16% ... BTN ~44%), with 3-bet / defend thresholds scaled by the size
  of the raise (log2 scaling) and position. All-in Monte-Carlo equity vs N
  random hands is a bad preflop signal: it assumes everyone sees showdown.

* postflop — Monte-Carlo equity against *modeled opponent ranges*: each live
  opponent's likely holding percentile is inferred from their actions this
  hand (raised preflop ≈ top 8%, called a raise ≈ 15%, limped ≈ 45%,
  unopened ≈ 50%), and equity is sampled against hands drawn from those
  ranges instead of uniformly random holdings. Decisions use pot odds, with
  value bets/raises, occasional heads-up bluffs, and multiway tightening.

* ``advise(ctx)`` — the same engine, deterministic (no bluffs), returning a
  structured textbook recommendation used by the Coach panel. This is the
  "AI teaches you" half: pot odds %, equity vs the estimated range and a
  one-line explanation in poker English.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

if __package__:
    from .hands import best_of, describe_en
else:  # The original local training server imports this module directly.
    from hands import best_of, describe_en

FULL_DECK = [s + r for s in "CDHS" for r in "23456789TJQKA"]
RANK_VALUE = {r: i for i, r in enumerate("23456789TJQKA")}
RANKS = "23456789TJQKA"

# action ids, mirroring rlcard's nolimit round enum
FOLD, CALL, RAISE_HALF, RAISE_POT, ALL_IN = 0, 1, 2, 3, 4

CACHE_PATH = Path(__file__).parent / "preflop_equity.json"


# --------------------------------------------------------------------- #
# preflop hand classes and percentile ranking
# --------------------------------------------------------------------- #
def hand_class(c1: str, c2: str) -> str:
    """'SA','HK' -> 'AKs' / 'AKo' / 'AA'."""
    r1, r2 = RANK_VALUE[c1[1]], RANK_VALUE[c2[1]]
    hi, lo = max(r1, r2), min(r1, r2)
    if hi == lo:
        return RANKS[hi] + RANKS[lo]
    suited = c1[0] == c2[0]
    return RANKS[hi] + RANKS[lo] + ("s" if suited else "o")


def _all_classes() -> list[str]:
    out = []
    for hi in range(12, -1, -1):
        for lo in range(hi, -1, -1):
            if hi == lo:
                out.append(RANKS[hi] + RANKS[lo])
            else:
                out.append(RANKS[hi] + RANKS[lo] + "s")
                out.append(RANKS[hi] + RANKS[lo] + "o")
    return out


def _combo_for(cls: str, rng: np.random.RandomState) -> tuple[str, str]:
    """A concrete two-card combo ('SA','HK') for a hand class."""
    suits = "CDHS"
    if len(cls) == 2:  # pocket pair
        s1, s2 = rng.choice(4, size=2, replace=False)
        return suits[s1] + cls[0], suits[s2] + cls[1]
    if cls[2] == "s":
        s = suits[rng.randint(4)]
        return s + cls[0], s + cls[1]
    s1, s2 = rng.choice(4, size=2, replace=False)
    return suits[s1] + cls[0], suits[s2] + cls[1]


def _compute_preflop_equity() -> dict[str, float]:
    """All-in equity of each hand class vs one random hand (heads-up).

    Two combos per class x 600 rollouts keeps the standard error around
    1.4% — tight enough for a sane percentile ordering (adjacent pairs
    like KK/QQ may swap, which does not affect decisions).
    """
    rng = np.random.RandomState(42)
    table = {}
    for cls in _all_classes():
        wins = 0.0
        n = 0
        for combo_idx in range(2):
            c1, c2 = _combo_for(cls, rng)
            for _ in range(600):
                deck = [c for c in FULL_DECK if c not in (c1, c2)]
                idx = rng.choice(len(deck), size=7, replace=False)
                draw = [deck[i] for i in idx]
                board = draw[:5]
                mine = best_of([c1, c2] + board)
                opp = best_of(draw[5:] + board)
                if mine > opp:
                    wins += 1.0
                elif mine == opp:
                    wins += 0.5
                n += 1
        table[cls] = wins / n
    return table


def _load_preflop_equity() -> dict[str, float]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
    table = _compute_preflop_equity()
    try:
        CACHE_PATH.write_text(json.dumps(table))
    except Exception:
        pass
    return table


PREFLOP_EQUITY: dict[str, float] = _load_preflop_equity()
# strongest first, weakest last; ties broken arbitrarily but stably
RANKED_CLASSES: list[str] = sorted(
    PREFLOP_EQUITY, key=lambda c: (-PREFLOP_EQUITY[c], c)
)
CLASS_PCT: dict[str, float] = {
    cls: (rank + 1) / len(RANKED_CLASSES)
    for rank, cls in enumerate(RANKED_CLASSES)
}
# combo weights for range sampling: pairs 6, suited 4, offsuit 12
CLASS_COMBOS = {
    cls: (6 if len(cls) == 2 else (4 if cls[2] == "s" else 12))
    for cls in RANKED_CLASSES
}


def hand_pct(hand: list[str]) -> float:
    """Percentile of a concrete hand (0 = nuts, 1 = worst)."""
    return CLASS_PCT[hand_class(hand[0], hand[1])]


def _straight_draw(unique_ranks) -> str:
    """'open-ender' / 'gutshot' / '' for a set of distinct rank indices 0..12."""
    r = set(unique_ranks)
    if 12 in r:  # ace also plays low
        r.add(-1)
    for low in range(-1, 9):
        window = [low + i for i in range(5)]
        if len([x for x in window if x in r]) != 4:
            continue
        missing = [x for x in window if x not in r][0]
        # the wheel (A-2-3-4-5) is a 1-out draw, not a real open-ender
        if low == -1:
            return "gutshot"
        at_edge = missing == window[0] or missing == window[4]
        return "open-ender" if at_edge else "gutshot"
    return ""


def draw_label(hand: list[str], board: list[str]) -> str:
    """Flush draw / straight draw label for the coach panel."""
    # a draw only exists on flop and turn — the river is already complete
    if len(board) < 3 or len(board) >= 5:
        return ""
    suits = [c[0] for c in hand + board]
    out = []
    for s in "CDHS":
        if suits.count(s) == 4 and any(c[0] == s for c in hand):
            out.append("flush draw")
            break
    sd = _straight_draw({RANK_VALUE[c[1]] for c in hand + board})
    if sd:
        out.append(sd)
    return " + ".join(out)


_RANGE_CACHE: dict[int, tuple[list[str], np.ndarray]] = {}


def _range_bucket(pct: float) -> tuple[list[str], np.ndarray]:
    """Eligible classes + combo-weighted probabilities for the top-``pct`` range.

    Cached: ``range_equity`` samples thousands of hands per decision and
    re-filtering all 169 classes each time dominated the runtime.
    """
    key = int(round(pct * 100))
    hit = _RANGE_CACHE.get(key)
    if hit is not None:
        return hit
    eligible = [c for c in RANKED_CLASSES if CLASS_PCT[c] <= pct] or RANKED_CLASSES[:3]
    w = np.array([CLASS_COMBOS[c] for c in eligible], dtype=float)
    w /= w.sum()
    hit = (eligible, w)
    _RANGE_CACHE[key] = hit
    return hit


def sample_range_hand(
    rng: np.random.RandomState, pct: float, known: set[str]
) -> tuple[str, str] | None:
    """Random combo from the top-``pct`` fraction of hands, avoiding known cards."""
    eligible, weights = _range_bucket(pct)
    n = len(eligible)
    for _ in range(8):
        cls = eligible[int(rng.choice(n, p=weights))]
        c1, c2 = _combo_for(cls, rng)
        if c1 not in known and c2 not in known:
            return c1, c2
    return None


# --------------------------------------------------------------------- #
# Monte-Carlo equity vs modeled ranges
# --------------------------------------------------------------------- #
def range_equity(
    hand: list[str],
    board: list[str],
    opp_pcts: list[float],
    rollouts: int,
    rng: np.random.RandomState,
) -> float:
    """P(this hand wins/ties at showdown) vs hands sampled from opp ranges."""
    known = set(hand) | set(board)
    deck = [c for c in FULL_DECK if c not in known]
    need = 5 - len(board)
    score = 0.0
    for _ in range(rollouts):
        opp_hands = []
        used = set()
        ok = True
        for p in opp_pcts:
            combo = sample_range_hand(rng, p, known | used)
            if combo is None:
                ok = False
                break
            opp_hands.append(combo)
            used.update(combo)
        if not ok:
            continue
        pool = [c for c in deck if c not in used]
        idx = rng.choice(len(pool), size=need, replace=False)
        full_board = board + [pool[i] for i in idx]
        mine = best_of(hand + full_board)
        best_opp = max(best_of(list(h) + full_board) for h in opp_hands)
        if mine > best_opp:
            score += 1.0
        elif mine == best_opp:
            score += 0.5
    return score / max(1, rollouts)


# --------------------------------------------------------------------- #
# the unified engine
# --------------------------------------------------------------------- #
# 6-max order after the BB: UTG, MP(HJ), CO, BTN, SB, BB
OPEN_PCT = {6: [0.18, 0.22, 0.30, 0.48, 0.38, 0.40]}


def _open_pct(pos: int, n: int) -> float:
    """Opening range width (fraction of hands) for a position."""
    tbl = OPEN_PCT.get(n)
    if tbl and pos < len(tbl):
        return tbl[pos]
    if pos == n - 2:
        return 0.38  # SB
    if pos == n - 1:
        return 0.40  # BB
    if n == 3:
        return 0.48  # Button in a three-player hand.
    # Extend the same early-to-button range to 3–11 players.
    return 0.16 + 0.32 * pos / max(1, n - 3)


def _pick_raise(ctx, strong: bool, rng) -> int:
    legal = ctx["legal"]
    stack = ctx["stack"]
    if strong and RAISE_POT in legal:
        return RAISE_POT
    if RAISE_HALF in legal and ctx["costs"][RAISE_HALF] < stack * 0.6:
        return RAISE_HALF
    if RAISE_POT in legal and ctx["costs"][RAISE_POT] < stack * 0.9:
        return RAISE_POT
    return ALL_IN if ALL_IN in legal else (CALL if CALL in legal else FOLD)


def _opp_ranges(ctx) -> list[float]:
    """Estimated range widths for active opponents (already set by table)."""
    return ctx.get("opp_ranges") or [0.5] * ctx["n_opponents"]


def decide(ctx, rng, info: dict | None = None, bluff_ok: bool = True) -> int:
    """One decision for the unified engine. ``info`` collects explanations.

    ``bluff_ok=False`` turns off the deliberate bluff branches: opponents keep
    them (an opponent that never bluffs is exploitable and teaches the wrong
    lessons), the Coach panel drops them to show the plain textbook line.
    """
    info = {} if info is None else info
    if not ctx["board"]:
        return _decide_preflop(ctx, rng, info)
    return _decide_postflop(ctx, rng, info, bluff_ok)


def _decide_preflop(ctx, rng, info) -> int:
    legal = ctx["legal"]
    p = hand_pct(ctx["hand"])
    pos = ctx.get("preflop_pos", 0)  # 0 = UTG ... n-1 = BB
    to_call, bb = ctx["to_call"], ctx["big_blind"]
    stack_bb = ctx["stack"] / bb
    jitter = (rng.rand() - 0.5) * 0.02
    p_eff = p + jitter

    def can_raise():
        return RAISE_HALF in legal or RAISE_POT in legal

    info.update(percentile=round(p * 100, 1), phase="preflop")

    # short-stack shove with a playable hand
    if ALL_IN in legal and stack_bb <= 10 and p <= 0.16:
        info["note"] = f"short stack ({stack_bb:.0f} BB) — shove top-16% ({p*100:.0f}%ile)"
        return ALL_IN

    raised_pot = to_call > bb  # facing a raise (or a 3-bet)
    n = ctx["n_players"]
    is_sb, is_bb = pos == n - 2, pos == n - 1

    # ---- facing a raise: 3-bet, defend or fold ----
    if raised_pot:
        if p_eff <= 0.055 + (0.01 if pos >= n - 3 else 0) and can_raise():
            info["note"] = f"top-{p*100:.0f}%ile hand — 3-bet for value"
            return _pick_raise(ctx, True, rng)
        allow = 0.20 - 0.055 * np.log2(max(1.0, to_call / bb))
        if pos >= n - 3:
            allow += 0.04  # in position defends wider
        if is_bb:
            allow += 0.03  # closed action, cheaper to defend
        if p_eff <= max(0.06, allow):
            info["note"] = (
                f"top-{p*100:.1f}%ile — defend vs raise "
                f"(need top-{max(0.06, allow)*100:.1f}%)"
            )
            return CALL
        if _implied_odds_call(hand_class(*ctx["hand"]), ctx):
            kind = "set-mining" if len(hand_class(*ctx["hand"])) == 2 else "implied odds"
            info["note"] = (
                f"{kind} call — the raise is only "
                f"{to_call / max(1.0, ctx['stack']) * 100:.0f}% of your stack, "
                f"so you are getting the implied odds to continue"
            )
            return CALL
        info["note"] = (
            f"top-{p*100:.1f}%ile — too weak vs a raise "
            f"(defend top-{max(0.06, allow)*100:.1f}%)"
        )
        return FOLD

    # ---- unraised pot ----
    if to_call == 0:  # BB option, everyone limped / checked
        if p_eff <= 0.30 and can_raise():
            info["note"] = "BB option — squeeze a limped pot with top-30%"
            return _pick_raise(ctx, p <= 0.10, rng)
        info["note"] = "BB option — check"
        return CALL

    if to_call <= bb / 2 and is_sb:
        # SB completing vs limpers: raise top ~34%, otherwise complete cheaply
        if p_eff <= 0.34 and can_raise():
            info["note"] = "SB — isolate the limpers (top 34%)"
            return _pick_raise(ctx, p <= 0.10, rng)
        info["note"] = "SB — cheap complete (1 chip)"
        return CALL

    open_pct = _open_pct(pos, ctx["n_players"])
    if p_eff <= open_pct and can_raise():
        info["note"] = (
            f"{_pos_name(pos, ctx['n_players'])} open — top-{open_pct*100:.0f}% range"
        )
        return _pick_raise(ctx, p <= 0.04, rng)
    if p_eff <= open_pct + 0.10 and to_call <= bb:
        info["note"] = (
            f"top-{p*100:.0f}%ile — call for a cheap flop (open = top-{open_pct*100:.0f}%)"
        )
        return CALL
    info["note"] = (
        f"top-{p*100:.0f}%ile — fold ({_pos_name(pos, ctx['n_players'])} opens "
        f"top-{open_pct*100:.0f}%)"
    )
    return FOLD


def _implied_odds_call(cls: str, ctx) -> bool:
    """Set-mining / implied-odds call — the textbook "rule of 5 and 10".

    Hot-and-cold all-in equity badly underrates two families whose value
    comes from *implied* odds rather than raw showdown equity:

    * small pocket pairs — you flop a set ~12% of the time and win a big pot
    * suited connectors / one-gappers — straight and flush potential

    Raw equity ranks 22 near the 51st percentile, yet every opening chart
    plays it, because when you hit you stack someone. So these hands get an
    explicit implied-odds call: take off a raise when it costs at most ~5%
    of your stack with a pair (you need ~20:1 to break even set-mining) and
    ~10% with a suited connector.
    """
    to_call, stack = ctx["to_call"], ctx["stack"]
    if to_call <= 0 or to_call > stack:
        return False
    frac = to_call / max(1.0, stack)
    if len(cls) == 2:  # pocket pair
        return frac <= 0.05
    if cls[2] == "s" and abs(RANK_VALUE[cls[0]] - RANK_VALUE[cls[1]]) <= 2:
        return frac <= 0.10
    return False


def _pos_name(pos: int, n: int) -> str:
    names6 = ["UTG", "HJ", "CO", "BTN", "SB", "BB"]
    tbl = names6 if n == 6 else [f"P{pos}"]
    return tbl[pos] if pos < len(tbl) else f"P{pos}"


def _decide_postflop(ctx, rng, info, bluff_ok: bool = True) -> int:
    legal = ctx["legal"]
    pot, to_call = ctx["pot"], ctx["to_call"]
    opps = _opp_ranges(ctx)
    n_opp = len(opps)
    eq = range_equity(ctx["hand"], ctx["board"], opps, ctx["rollouts"], rng)
    pot_odds = to_call / (pot + to_call) if (to_call > 0 and pot + to_call > 0) else 0.0
    made = describe_en(ctx["hand"] + ctx["board"]) if len(ctx["board"]) >= 3 else ""
    draw = draw_label(ctx["hand"], ctx["board"])

    if info is not None:
        info.update(
            phase="postflop",
            equity=round(eq, 3),
            pot_odds=round(pot_odds, 3),
            made=made,
            draw=draw,
        )

    def can_raise():
        return RAISE_HALF in legal or RAISE_POT in legal

    # short-stack shove
    if ALL_IN in legal and ctx["stack"] <= 10 * ctx["big_blind"] and eq >= 0.52:
        info["note"] = f"short stack — shove with {eq*100:.0f}% equity"
        return ALL_IN

    if to_call > 0:
        value_th = 0.70 if n_opp <= 1 else 0.75
        if eq >= value_th and can_raise():
            info["note"] = (
                f"{eq*100:.0f}% equity vs {pot_odds*100:.0f}% pot odds — raise for value"
            )
            return _pick_raise(ctx, eq >= 0.82, rng)
        if eq >= pot_odds + 0.05:
            info["note"] = (
                f"equity {eq*100:.0f}% beats pot odds {pot_odds*100:.0f}% — call"
            )
            return CALL
        if draw and eq + _draw_bonus(draw) >= pot_odds and CALL in legal:
            info["note"] = (
                f"{draw} — implied odds make this a call "
                f"(raw equity {eq*100:.0f}% vs {pot_odds*100:.0f}% odds)"
            )
            return CALL
        if bluff_ok and n_opp <= 1 and eq < 0.40 and rng.rand() < 0.10 and can_raise():
            info["note"] = "bluff-raise for fold equity"
            return _pick_raise(ctx, False, rng)
        info["note"] = (
            f"equity {eq*100:.0f}% is below pot odds {pot_odds*100:.0f}% — fold"
        )
        return FOLD

    # checked to me: value bet / bluff / check
    bet_th = 0.60 if n_opp <= 1 else 0.66
    if eq >= bet_th and can_raise():
        info["note"] = f"{eq*100:.0f}% equity — value bet ({'pot' if eq >= 0.80 else 'half pot'})"
        return _pick_raise(ctx, eq >= 0.80, rng)
    if bluff_ok and n_opp <= 1 and eq < 0.45 and rng.rand() < 0.24 and can_raise():
        info["note"] = "bluff bet for fold equity"
        return RAISE_HALF if RAISE_HALF in legal else CALL
    info["note"] = f"equity {eq*100:.0f}% — check"
    return CALL


def _draw_bonus(draw: str) -> float:
    """Rough equity a draw adds beyond its raw showdown equity (implied odds)."""
    if "flush draw" in draw and "open-ender" in draw:
        return 0.22
    if "flush draw" in draw:
        return 0.16
    if "open-ender" in draw:
        return 0.10
    if "gutshot" in draw:
        return 0.04
    return 0.0


# --------------------------------------------------------------------- #
# bot wrapper + coach
# --------------------------------------------------------------------- #
class StrongBot:
    """One seat of the unified engine. All five seats run the same model."""

    def __init__(self, name: str, persona: str, seed: int):
        self.name = name
        self.persona = persona
        self.rng = np.random.RandomState(seed)
        self.last_reason = ""
        self.last_info: dict = {}

    def decide(self, ctx) -> int:
        self.last_info = {}
        action = decide(ctx, self.rng, self.last_info)
        self.last_reason = self.last_info.get("note", "")
        return action


def make_bots(n: int, base_seed: int) -> list[StrongBot]:
    """n identical strong engines for seats 1..n (the user asked for 5x)."""
    return [
        StrongBot(f"GTO Bot {i + 1}", "GTO Engine", base_seed + i)
        for i in range(n)
    ]


def advise(ctx) -> dict:
    """Deterministic textbook recommendation for the human — Coach panel.

    Same engine as the opponents but with bluffs switched off, so the panel
    always shows the plain value / pot-odds line instead of a random bluff.
    """
    info: dict = {}
    rng = np.random.RandomState(0)
    action = decide(ctx, rng, info, bluff_ok=False)
    names = {
        FOLD: "Fold",
        CALL: "Check" if ctx["to_call"] == 0 else "Call",
        RAISE_HALF: "Raise ½ Pot",
        RAISE_POT: "Raise Pot",
        ALL_IN: "All-In",
    }
    cls = hand_class(ctx["hand"][0], ctx["hand"][1])

    parts = []
    if info.get("phase") == "preflop":
        pct = info.get("percentile")
        pos = _pos_name(ctx.get("preflop_pos", 0), ctx["n_players"])
        eq = PREFLOP_EQUITY.get(cls)
        eq_txt = f", {eq * 100:.0f}% all-in equity vs a random hand" if eq else ""
        parts.append(f"{cls} from {pos} — top {pct}% of hands{eq_txt}.")
    else:
        made = info.get("made") or ""
        draw = info.get("draw") or ""
        bits = [b for b in (made.split(" (")[0] if made else "", draw) if b]
        parts.append(f"{cls}: {', '.join(bits)}." if bits else f"{cls}.")
    parts.append(info.get("note", ""))

    return {
        "action": action,
        "action_name": names[action],
        "phase": info.get("phase"),
        "hand_class": cls,
        "percentile": info.get("percentile"),
        "equity": info.get("equity"),
        "pot_odds": info.get("pot_odds"),
        "made": info.get("made", ""),
        "draw": info.get("draw", ""),
        "note": " ".join(p for p in parts if p),
    }
