"""Bridge the local training strategy to a private-room public observation.

The strategy receives its own cards, public actions and legal bet sizes only.
It does not load AlphaHoldem weights, the training server or debug APIs.
"""
from __future__ import annotations

import numpy as np

from engine.bots import ALL_IN, CALL, FOLD, RAISE_HALF, RAISE_POT, decide


def context(observation):
    options = observation["options"]
    call, bet = options["call"], observation["bet"]
    moves = {CALL: dict(kind="call", amount=None)}
    if options["fold"]:
        moves[FOLD] = dict(kind="fold", amount=None)
    if options["can_raise"]:
        moves[RAISE_HALF] = dict(kind="raise", amount=options["half_pot_to"])
        moves[RAISE_POT] = dict(kind="raise", amount=options["pot_to"])
    if options["all_in"]:
        moves[ALL_IN] = options["all_in"]
    costs = {action: move["amount"] - bet if move["kind"] == "raise" else
             call if move["kind"] == "call" else 0 for action, move in moves.items()}

    ranges = {seat: 0.50 for seat in observation["opponents"]}
    raised = False
    for action in observation["actions"]:
        seat, street = action["seat"], action["street"]
        if seat in ranges:
            if street == 0:
                if action["kind"] == "raise":
                    ranges[seat] = 0.08
                elif action["kind"] == "call":
                    ranges[seat] = min(ranges[seat], 0.15 if raised else 0.45)
            elif action["kind"] == "raise":
                ranges[seat] = min(ranges[seat], 0.10)
        if street == 0 and action["kind"] == "raise":
            raised = True

    # PokerKit uses 'As'; the existing training strategy uses 'SA'.
    cards = lambda values: [c[-1].upper() + c[:-1] for c in values]
    count = len(ranges)
    ctx = dict(hand=cards(observation["hand"]), board=cards(observation["board"]),
               n_players=observation["n_players"], n_opponents=count,
               opp_ranges=list(ranges.values()), pot=observation["pot"],
               to_call=call, stack=observation["stack"], legal=set(moves), costs=costs,
               big_blind=observation["big_blind"], preflop_pos=observation["preflop_pos"],
               rollouts=max(24, min(96, 192 // max(1, count))))
    return ctx, moves


def choose_action(observation, seed):
    ctx, moves = context(observation)
    choice = decide(ctx, np.random.RandomState(seed))
    # A strategy preference cannot bypass PokerKit's legal-action checks.
    return moves.get(choice, moves[CALL])
