"""Private poker rooms with 2–11 seats. SQLite is shared by all workers.

Only a shuffled deck and accepted commands are persisted; PokerKit replays them
to reconstruct each hand. No client receives the deck or another player's cards.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from flask import Blueprint, current_app, jsonify, request
from pokerkit import Automation, Card, Deck, NoLimitTexasHoldem

rooms = Blueprint("private_rooms", __name__)
AUTOMATIONS = tuple(a for a in Automation if a not in (
    Automation.HOLE_DEALING, Automation.BOARD_DEALING,
))
STREETS = ["翻牌前", "翻牌", "转牌", "河牌"]
MAX_PLAYERS = 11


def hand_positions(hand):
    """Names follow the hand's actual players, not the room's seat limit."""
    if not hand:
        return {}
    count = len(hand["seats"])
    labels = {
        2: ["BB", "D / SB"],
        3: ["SB", "BB", "D"],
        4: ["SB", "BB", "UTG", "D"],
        5: ["SB", "BB", "UTG", "CO", "D"],
        6: ["SB", "BB", "UTG", "HJ", "CO", "D"],
        7: ["SB", "BB", "UTG", "UTG+1", "HJ", "CO", "D"],
        8: ["SB", "BB", "UTG", "UTG+1", "MP", "HJ", "CO", "D"],
        9: ["SB", "BB", "UTG", "UTG+1", "MP", "LJ", "HJ", "CO", "D"],
        10: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "LJ", "HJ", "CO", "D"],
        11: ["SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "MP+1", "LJ", "HJ", "CO", "D"],
    }[count]
    return dict(zip(hand["seats"], labels))


def betting_options(state):
    """Pot raises use the pot AFTER calling and include the player's street bet.

    Example: 1/2 blinds, first player facing 2 into a pot of 3: call first,
    then raise by half of 5 (rounded up) -> total 5; a pot raise -> total 7.
    Presets clamp to the legal range, including a short all-in maximum.
    """
    actor = state.actor_index
    call = state.checking_or_calling_amount
    can_raise = state.can_complete_bet_or_raise_to()
    minimum = state.min_completion_betting_or_raising_to_amount
    maximum = state.max_completion_betting_or_raising_to_amount
    half = pot = all_in = None
    if can_raise:
        base = state.bets[actor] + call
        called_pot = state.total_pot_amount + call
        half = max(minimum, min(maximum, base + (called_pot + 1) // 2))
        pot = max(minimum, min(maximum, base + called_pot))
        all_in = dict(kind="raise", amount=maximum)
    elif call > 0 and call == state.stacks[actor]:
        all_in = dict(kind="call", amount=None)
    return dict(call=call, fold=state.can_fold(), min_raise=minimum,
                max_raise=maximum, can_raise=can_raise,
                half_pot_to=half, pot_to=pot, all_in=all_in)


class RoomError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


@rooms.errorhandler(RoomError)
def room_error(error):
    response = jsonify(error=error.message)
    response.status_code = error.status
    return response


@rooms.after_request
def private_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def database():
    path = Path(current_app.config.get("ROOMS_DATABASE") or
                os.environ.get("ROOMS_DATABASE", Path(__file__).parent / "data/rooms.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS rooms (code TEXT PRIMARY KEY, doc TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, start REAL, count INTEGER)")
    return conn


@contextmanager
def transaction(code=None):
    conn = database()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = None
        if code:
            row = conn.execute("SELECT doc FROM rooms WHERE code=?", (code,)).fetchone()
            if row is None:
                raise RoomError("房间不存在，请检查邀请链接。", 404)
            room = json.loads(row[0])
            room["settings"].setdefault("max_players", 6)
        yield conn, room
        if room is not None:
            conn.execute("UPDATE rooms SET doc=? WHERE code=?", (json.dumps(room), code))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise RoomError("请发送有效的 JSON 请求。")
    return data


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise RoomError(f"{label}须为 {low}–{high} 的整数。")
    return value


def nickname(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 16:
        raise RoomError("昵称须为 1–16 个字符。")
    if any(ord(c) < 32 for c in value):
        raise RoomError("昵称不能包含控制字符。")
    return value.strip()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def member(room):
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    hashed = digest(token)
    for p in room["players"]:
        if secrets.compare_digest(p["token_hash"], hashed):
            p["seen"] = time.time()
            return p
    raise RoomError("请使用邀请链接加入，或回到原来的浏览器恢复座位。", 401)


def host(room, player):
    if room["owner"] != player["id"]:
        raise RoomError("只有房主可以进行此操作。", 403)


def rate_limit(conn, kind, limit):
    # Shared across workers. Do not trust arbitrary forwarded IP headers.
    key = kind + ":" + digest(request.remote_addr or "local")
    now = time.time()
    conn.execute("DELETE FROM rate_limits WHERE start < ?", (now - 3600,))
    row = conn.execute("SELECT start,count FROM rate_limits WHERE key=?", (key,)).fetchone()
    start, count = row if row and row[0] > now - 600 else (now, 0)
    if count >= limit:
        raise RoomError("操作过于频繁，请稍后再试。", 429)
    conn.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,?,?)", (key, start, count + 1))


def new_player(name, seat, stack):
    token = secrets.token_urlsafe(32)
    return dict(id=secrets.token_hex(8), name=nickname(name), seat=seat,
                stack=stack, buyin=stack, token_hash=digest(token), seen=time.time()), token


def settle_board(state):
    while state.status and state.can_deal_board():
        state.deal_board()


def build_state(hand):
    state = NoLimitTexasHoldem.create_state(
        AUTOMATIONS, True, 0, (hand["sb"], hand["bb"]), hand["bb"],
        hand["starting_stacks"], len(hand["seats"]),
    )
    state.deck_cards = deque(Card.parse("".join(hand["deck"])))
    while state.can_deal_hole():
        state.deal_hole()
    settle_board(state)
    for action in hand["actions"]:
        apply_action(state, action["kind"], action.get("amount"))
    return state


def apply_action(state, kind, amount=None):
    if kind == "fold" and state.can_fold():
        state.fold()
    elif kind == "call" and state.can_check_or_call():
        state.check_or_call()
    elif kind == "raise" and type(amount) is int and state.can_complete_bet_or_raise_to(amount):
        state.complete_bet_or_raise_to(amount)
    else:
        raise RoomError("此操作不合法，牌局已刷新，请重新选择。", 409)
    settle_board(state)


def finish_if_needed(room, state):
    hand = room["hand"]
    if state.status or hand.get("settled"):
        return
    hand["settled"] = True
    hand["deadline"] = None
    by_seat = {p["seat"]: p for p in room["players"]}
    for i, seat in enumerate(hand["seats"]):
        by_seat[seat]["stack"] = state.stacks[i]
    room["history"].append(dict(
        number=hand["number"], board=[repr(c) for c in state.get_board_cards(0)],
        results=[dict(name=by_seat[seat]["name"], delta=state.payoffs[i])
                 for i, seat in enumerate(hand["seats"])],
    ))
    room["history"] = room["history"][-50:]


def set_deadline(room):
    seconds = room["settings"]["turn_seconds"]
    room["hand"]["deadline"] = time.time() + seconds if seconds else None


def record_action(room, state, kind, amount=None, automatic=False):
    hand = room["hand"]
    actor = state.actor_index
    seat = hand["seats"][actor]
    street = state.street_index
    call = state.checking_or_calling_amount
    apply_action(state, kind, amount)
    hand["actions"].append(dict(kind=kind, amount=amount, seat=seat,
                                street=street, automatic=automatic,
                                label=("弃牌" if kind == "fold" else
                                       f"加注至 {amount}" if kind == "raise" else
                                       f"跟注 {call}" if call else "过牌")))
    # Replaying includes all forced board cards, pot collection and payouts.
    room["version"] += 1
    set_deadline(room)
    finish_if_needed(room, state)


def tick(room):
    hand = room["hand"]
    if hand is None:
        return None
    state = build_state(hand)
    if (state.status and not room["paused"] and hand["deadline"] is not None
            and time.time() >= hand["deadline"]):
        kind = "call" if state.checking_or_calling_amount == 0 else "fold"
        record_action(room, state, kind, automatic=True)
    return state


def view(room, player, state=None):
    hand = room["hand"]
    if hand is not None and state is None:
        state = build_state(hand)
    seat_to_index = {seat: i for i, seat in enumerate(hand["seats"])} if hand else {}
    positions = hand_positions(hand)
    seats = []
    for p in sorted(room["players"], key=lambda x: x["seat"]):
        item = {k: p[k] for k in ("id", "name", "seat", "stack", "buyin")}
        item.update(online=time.time() - p["seen"] < 20, cards=[], bet=0,
                    status="等待入局", delta=None, position=None)
        i = seat_to_index.get(p["seat"])
        if hand and hand["player_ids"].get(str(p["seat"])) != p["id"]:
            i = None
        if i is not None:
            item["position"] = positions[p["seat"]]
            # Own cards are retained even after folding; other cards only become
            # public if PokerKit explicitly showed them at showdown.
            own = p["id"] == player["id"]
            shown = bool(state.hole_cards[i]) and all(state.hole_card_statuses[i])
            item["cards"] = [repr(c) for c in state.hole_cards[i]] if own or shown else []
            if own and not item["cards"]:
                item["cards"] = hand["holes"][str(p["seat"])]
            item["bet"] = state.bets[i]
            item["stack"] = state.stacks[i] if state.status else p["stack"]
            item["status"] = ("已结算" if not state.status else "已弃牌" if not state.statuses[i]
                              else "全下" if state.stacks[i] == 0 else "对局中")
            item["delta"] = state.payoffs[i] if not state.status else None
        seats.append(item)
    legal = None
    actor_seat = hand["seats"][state.actor_index] if state and state.actor_index is not None else None
    if state and state.status and actor_seat == player["seat"] and not room["paused"]:
        legal = betting_options(state)
    return dict(code=room["code"], invite=room["invite"], me=player["id"],
                owner=room["owner"], settings=room["settings"], players=seats,
                version=room["version"], paused=room["paused"],
                phase="lobby" if not state else "playing" if state.status else "finished",
                hand_number=hand["number"] if hand else 0,
                dealer=room["dealer"], actor_seat=actor_seat,
                board=[repr(c) for c in state.get_board_cards(0)] if state else [],
                pot=state.total_pot_amount if state else 0,
                street=STREETS[state.street_index] if state and state.street_index is not None else "等待开局" if not state else "本手结束",
                deadline=hand["deadline"] if hand else None,
                server_time=time.time(), legal=legal,
                actions=hand["actions"] if hand else [], history=room["history"])


@rooms.get("/friends")
@rooms.get("/rooms/<code>")
def room_page(code=None):
    return current_app.send_static_file("friends.html")


@rooms.get("/friends-assets/<filename>")
def room_asset(filename):
    if filename not in ("friends.css", "friends.js"):
        raise RoomError("文件不存在。", 404)
    return current_app.send_static_file(filename)


@rooms.post("/api/rooms")
def create_room():
    data = body()
    capacity = integer(data.get("max_players", 6), "座位数", 2, MAX_PLAYERS)
    player, token = new_player(data.get("name"), 0, 1000)
    code = secrets.token_hex(5).upper()
    room = dict(code=code, invite=secrets.token_urlsafe(24), owner=player["id"],
                players=[player], settings=dict(sb=1, bb=2, starting_stack=1000, turn_seconds=0,
                                                max_players=capacity),
                version=1, hand=None, dealer=-1, paused=False, history=[])
    with transaction() as (conn, _):
        rate_limit(conn, "create", 20)
        conn.execute("INSERT INTO rooms VALUES (?,?)", (code, json.dumps(room)))
    return jsonify(token=token, room=view(room, player)), 201


@rooms.post("/api/rooms/<code>/join")
def join_room(code):
    data = body()
    with transaction() as (conn, _):
        rate_limit(conn, "join", 120)
    with transaction(code) as (conn, room):
        invite = data.get("invite", "")
        if not isinstance(invite, str) or not secrets.compare_digest(invite, room["invite"]):
            raise RoomError("邀请链接无效，请向朋友索取完整链接。", 403)
        capacity = room["settings"]["max_players"]
        if len(room["players"]) >= capacity:
            raise RoomError(f"{capacity} 个座位已满。已有座位请使用原浏览器进入。", 409)
        seat = next(i for i in range(capacity) if not any(p["seat"] == i for p in room["players"]))
        player, token = new_player(data.get("name"), seat, room["settings"]["starting_stack"])
        room["players"].append(player)
        if room["owner"] is None:
            room["owner"] = player["id"]
        room["version"] += 1
        result = view(room, player, tick(room))
    return jsonify(token=token, room=result)


@rooms.get("/api/rooms/<code>")
def room_state(code):
    with transaction(code) as (_, room):
        player = member(room)
        result = view(room, player, tick(room))
    return jsonify(result)


@rooms.post("/api/rooms/<code>/<command>")
def room_command(code, command):
    data = body()
    with transaction(code) as (_, room):
        player = member(room)
        state = tick(room)
        active = state is not None and state.status
        if command == "act":
            if room["paused"]:
                raise RoomError("房主已暂停牌局。", 409)
            if not active or room["hand"]["seats"][state.actor_index] != player["seat"]:
                raise RoomError("还没轮到你行动。", 409)
            if data.get("version") != room["version"]:
                raise RoomError("牌局已更新，请重新选择操作。", 409)
            record_action(room, state, data.get("kind"), data.get("amount"))
        elif command == "start":
            host(room, player)
            if active:
                raise RoomError("本手尚未结束。", 409)
            participants = sorted((p for p in room["players"] if p["stack"] > 0), key=lambda p: p["seat"])
            if len(participants) < 2:
                raise RoomError("至少需要两名有筹码的玩家。", 409)
            # Physical button advances to the next funded seat. PokerKit uses
            # SB=0, BB=1 for 3+ players, BB=0, button/SB=1 for heads-up.
            dealer = next((p["seat"] for p in participants if p["seat"] > room["dealer"]), participants[0]["seat"])
            previous = room["hand"]
            if len(participants) == 2 and previous and len(previous["seats"]) > 2:
                # Entering heads-up: advance the BB, rather than making the
                # previous BB post it twice. The other player is button/SB.
                old_bb = previous["seats"][1]
                next_bb = next((p["seat"] for p in participants if p["seat"] > old_bb), participants[0]["seat"])
                dealer = next(p["seat"] for p in participants if p["seat"] != next_bb)
            room["dealer"] = dealer
            ordered = [p for p in participants if p["seat"] > dealer] + [p for p in participants if p["seat"] <= dealer]
            deck = [repr(c) for c in Deck.STANDARD]
            secrets.SystemRandom().shuffle(deck)
            hand = dict(number=room["hand"]["number"] + 1 if room["hand"] else 1,
                        seats=[p["seat"] for p in ordered], starting_stacks=[p["stack"] for p in ordered],
                        deck=deck, sb=room["settings"]["sb"], bb=room["settings"]["bb"],
                        actions=[], settled=False,
                        holes={str(p["seat"]): [deck[i], deck[len(ordered) + i]]
                               for i, p in enumerate(ordered)}, deadline=None,
                        player_ids={str(p["seat"]): p["id"] for p in ordered})
            room["hand"] = hand
            room["paused"] = False
            state = build_state(hand)
            set_deadline(room)
            finish_if_needed(room, state)
            room["version"] += 1
        elif command == "settings":
            host(room, player)
            if active:
                raise RoomError("请在两手之间修改规则。", 409)
            sb = integer(data.get("sb"), "小盲", 1, 100000)
            bb = integer(data.get("bb"), "大盲", sb + 1, 200000)
            stack = integer(data.get("starting_stack"), "买入筹码", bb, 10000000)
            seconds = integer(data.get("turn_seconds"), "行动时间", 0, 600)
            capacity = integer(data.get("max_players", room["settings"]["max_players"]), "座位数", 2, MAX_PLAYERS)
            if any(p["seat"] >= capacity for p in room["players"]):
                raise RoomError(f"请先让 {capacity + 1} 号及之后座位的玩家离桌，再缩小牌桌。", 409)
            room["settings"] = dict(sb=sb, bb=bb, starting_stack=stack, turn_seconds=seconds,
                                    max_players=capacity)
            # Before the first hand, settings update everyone's initial buy-in.
            if room["hand"] is None:
                for p in room["players"]:
                    p["stack"] = p["buyin"] = stack
            room["version"] += 1
        elif command == "pause":
            host(room, player)
            if not active:
                raise RoomError("当前没有进行中的牌局。", 409)
            hand = room["hand"]
            if room["paused"]:
                hand["deadline"] = time.time() + hand.pop("remaining") if hand.get("remaining") is not None else None
            else:
                hand["remaining"] = max(0, hand["deadline"] - time.time()) if hand["deadline"] else None
                hand["deadline"] = None
            room["paused"] = not room["paused"]
            room["version"] += 1
        elif command == "rename":
            player["name"] = nickname(data.get("name"))
            room["version"] += 1
        elif command == "rebuy":
            if active:
                raise RoomError("请在本手结束后补码。", 409)
            amount = room["settings"]["starting_stack"]
            if player["stack"] + amount > 100000000:
                raise RoomError("筹码已达到上限。")
            player["stack"] += amount
            player["buyin"] += amount
            # An ended state's stacks are historical. Do not project them over
            # the newly topped-up stack in the room view.
            room["version"] += 1
        elif command == "leave":
            if active:
                raise RoomError("请在本手结束后离桌；断线仍可重连。", 409)
            room["players"].remove(player)
            if player["id"] == room["owner"]:
                room["owner"] = room["players"][0]["id"] if room["players"] else None
            room["version"] += 1
            return jsonify(left=True)
        else:
            raise RoomError("未知操作。", 404)
        result = view(room, player, state)
    return jsonify(result)
