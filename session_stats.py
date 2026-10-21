"""Settled-hand accounting for room members. No hole cards or credentials."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import time

from pokerkit import (BetCollection, BlindOrStraddlePosting, CheckingOrCalling,
                      ChipsPushing, CompletionBettingOrRaisingTo, Folding)


def ensure_session(room):
    if "session" not in room:
        previous = room.get("hand")
        legacy = bool(previous or room.get("history"))
        room["session"] = dict(
            tracked_at=time.time(), started_at=time.time() if legacy else None,
            ended_at=None, legacy=legacy,
            first_hand=(previous["number"] + (1 if previous.get("settled") else 0)) if previous else 1,
            participants={}, hands=[],
        )
    sync_players(room)
    return room["session"]


def sync_players(room):
    session = room["session"]
    hand = room.get("hand")
    last_number = hand["number"] - (0 if hand.get("settled") else 1) if hand else 0
    for player in room["players"]:
        record = session["participants"].setdefault(player["id"], dict(
            id=player["id"], entry=len(session["participants"]) + 1,
            joined_at=time.time(), joined_after_hand=last_number, left_at=None,
            baseline_net=player["stack"] - player["buyin"],
            hands_played=0, hands_won=0, cashout=0,
        ))
        record.update(name=player["name"], buyin=player["buyin"], stack=player["stack"])


def hand_totals(state):
    """Contested chips, excluding uncalled bets; a split/side pot counts as a win.

    PokerKit retains the last aggressor's street bet on a fold. Add only its
    matched portion to the collected bets, rather than counting that refund.
    """
    bets = [0] * state.player_count
    folded = set()
    winners = set()
    pot = 0
    for operation in state.operations:
        if isinstance(operation, (BlindOrStraddlePosting, CheckingOrCalling)):
            bets[operation.player_index] += operation.amount
        elif isinstance(operation, CompletionBettingOrRaisingTo):
            bets[operation.player_index] = operation.amount
        elif isinstance(operation, Folding):
            folded.add(operation.player_index)
        elif isinstance(operation, BetCollection):
            pot += operation.total_bets
            if len(folded) == state.player_count - 1:
                winner = next(i for i in range(state.player_count) if i not in folded)
                pot += min(bets[winner], max(b for i, b in enumerate(bets) if i != winner))
                winners.add(winner)
            bets = [0] * state.player_count
        elif isinstance(operation, ChipsPushing):
            winners.update(i for i, amount in enumerate(operation.amounts) if amount > 0)
    return pot, winners


def record_settlement(room, state):
    session = room["session"]
    hand = room["hand"]
    sync_players(room)
    pot, winners = hand_totals(state)
    results = []
    for i, seat in enumerate(hand["seats"]):
        player_id = hand["player_ids"][str(seat)]
        record = session["participants"][player_id]
        record["hands_played"] += 1
        record["hands_won"] += i in winners
        results.append(dict(id=player_id, name=record["name"], delta=state.payoffs[i],
                            net=record["stack"] - record["buyin"],
                            won=i in winners))
    session["hands"].append(dict(number=hand["number"], ended_at=time.time(),
                                 pot=pot, board=[repr(c) for c in state.get_board_cards(0)],
                                 results=results))


def session_summary(room):
    session = ensure_session(room)
    players = [dict(p, net=p["stack"] + p["cashout"] - p["buyin"],
                    status="已离桌" if p["left_at"] is not None else "已结算" if session["ended_at"] is not None else "在桌")
               for p in session["participants"].values()]
    players.sort(key=lambda p: (-p["net"], p["entry"]))
    started, ended = session["started_at"], session["ended_at"]
    hands = session["hands"]
    largest = max(hands, key=lambda h: h["pot"], default=None)
    return dict(code=room["code"], version=room["version"], started_at=started,
                ended_at=ended, server_time=time.time(),
                elapsed_seconds=max(0, int((ended or time.time()) - started)) if started else 0,
                legacy=session["legacy"], first_hand=session["first_hand"],
                active=bool(room["hand"] and not room["hand"].get("settled")),
                completed_hands=len(hands), players=players, hands=hands,
                largest_pot=largest, total_buyin=sum(p["buyin"] for p in players),
                total_chips=sum(p["stack"] + p["cashout"] for p in players),
                net_total=sum(p["net"] for p in players))


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp is not None else ""


def csv_text(summary, detailed=False):
    """UTF-8 BOM for spreadsheet apps; neutralize names interpreted as formulas."""
    out = io.StringIO(newline="")
    writer = csv.writer(out)

    def row(values):
        writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                         else v for v in values])

    if detailed:
        row(["房间", "手数", "结算时间UTC", "底池", "公共牌", "入座编号", "玩家ID", "昵称", "本手盈亏", "累计盈亏", "本手获池"])
        entries = {p["id"]: p["entry"] for p in summary["players"]}
        for hand in summary["hands"]:
            for result in hand["results"]:
                row([summary["code"], hand["number"], iso(hand["ended_at"]), hand["pot"],
                     " ".join(hand["board"]), entries[result["id"]], result["id"],
                     result["name"], result["delta"], result["net"], int(result["won"])])
    else:
        row(["房间", "账单状态", "开始时间UTC", "结束时间UTC", "时长秒（含暂停）", "统计起始手", "历史不完整", "已结算手数", "最大底池", "最大底池手数",
             "入座编号", "玩家ID", "昵称", "玩家状态", "累计买入", "桌上筹码", "离桌带出", "净盈亏", "参与手数", "获胜手数"])
        for player in summary["players"]:
            row([summary["code"], "最终" if summary["ended_at"] is not None else "进行中快照", iso(summary["started_at"]),
                 iso(summary["ended_at"]), summary["elapsed_seconds"], summary["first_hand"], int(summary["legacy"]),
                 summary["completed_hands"], summary["largest_pot"]["pot"] if summary["largest_pot"] else "",
                 summary["largest_pot"]["number"] if summary["largest_pot"] else "",
                 player["entry"], player["id"], player["name"], player["status"], player["buyin"],
                 player["stack"], player["cashout"], player["net"], player["hands_played"], player["hands_won"]])
    return "\ufeff" + out.getvalue()
