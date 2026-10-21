"""Run with: python -m unittest discover -s tests -v."""
from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from multiplayer_app import create_app
from private_rooms import build_state, transaction


class PrivateRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.config = {"TESTING": True, "ROOMS_DATABASE": str(Path(self.temp.name) / "rooms.db")}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        result = self.client.post("/api/rooms", json={"name": "房主"}).get_json()
        self.code = result["room"]["code"]
        self.invite = result["room"]["invite"]
        self.tokens = [result["token"]]
        # Existing rule fixtures use 5/10; new room defaults are tested separately.
        self.req("settings", sb=5, bb=10, starting_stack=1000, turn_seconds=0)

    def tearDown(self):
        self.temp.cleanup()

    def req(self, command=None, player=0, client=None, **data):
        client = client or self.client
        url = f"/api/rooms/{self.code}" + (f"/{command}" if command else "")
        headers = {"Authorization": "Bearer " + self.tokens[player]}
        if command:
            return client.post(url, json=data, headers=headers)
        return client.get(url, headers=headers)

    def join(self, count=5):
        for _ in range(count):
            response = self.client.post(f"/api/rooms/{self.code}/join", json={
                "name": f"玩家 {len(self.tokens)}", "invite": self.invite,
            })
            self.assertEqual(response.status_code, 200)
            self.tokens.append(response.get_json()["token"])

    def start(self):
        response = self.req("start")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def play_calls(self):
        state = self.req().get_json()
        count = 0
        while state["phase"] == "playing":
            actor = state["actor_seat"]
            state = self.req(player=actor).get_json()
            response = self.req("act", player=actor, kind="call", version=state["version"])
            self.assertEqual(response.status_code, 200)
            state = response.get_json()
            count += 1
            self.assertLess(count, 100)
        return state

    def test_invites_authentication_and_capacity(self):
        self.assertEqual(self.client.get(f"/api/rooms/{self.code}").status_code, 401)
        self.assertEqual(self.client.post(f"/api/rooms/{self.code}/join", json={
            "name": "陌生人", "invite": "wrong",
        }).status_code, 403)
        self.join()
        self.assertEqual(self.client.post(f"/api/rooms/{self.code}/join", json={
            "name": "第七人", "invite": self.invite,
        }).status_code, 409)

    def test_six_distinct_private_hands_and_no_reveal_override(self):
        self.join()
        self.start()
        all_cards = []
        for i in range(6):
            response = self.client.get(f"/api/rooms/{self.code}?reveal=1", headers={
                "Authorization": "Bearer " + self.tokens[i],
            })
            state = response.get_json()
            for p in state["players"]:
                self.assertEqual(len(p["cards"]), 2 if p["seat"] == i else 0)
                all_cards.extend(p["cards"])
            self.assertNotIn("deck", response.get_data(as_text=True))
            self.assertNotIn("token_hash", response.get_data(as_text=True))
        self.assertEqual(len(set(all_cards)), 12)

    def test_full_six_player_hand_and_continuous_stacks(self):
        self.join()
        self.start()
        final = self.play_calls()
        self.assertEqual(sum(p["stack"] for p in final["players"]), 6000)
        self.assertEqual(len(final["board"]), 5)
        self.assertEqual(len(final["history"]), 1)
        stacks = [p["stack"] for p in final["players"]]
        state = self.start()
        # At the new hand, the only deductions are the blinds.
        self.assertEqual(sum(p["stack"] + p["bet"] for p in state["players"]), sum(stacks))
        self.assertEqual(state["hand_number"], 2)
        self.assertEqual(state["dealer"], 1)

    def test_restart_restores_identical_state_and_seat(self):
        self.join()
        before = self.start()
        new_client = create_app(self.config).test_client()
        after = self.req(client=new_client).get_json()
        for key in ("me", "board", "version", "actor_seat", "hand_number"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["players"][0]["cards"], after["players"][0]["cards"])

    def test_host_authorization_and_action_validation(self):
        self.join(1)
        self.assertEqual(self.req("start", player=1).status_code, 403)
        state = self.start()
        actor = state["actor_seat"]
        self.assertEqual(self.req("act", player=1-actor, kind="fold", version=state["version"]).status_code, 409)
        self.assertEqual(self.req("act", player=actor, kind="raise", amount=1, version=state["version"]).status_code, 409)
        self.assertEqual(self.req("act", player=actor, kind="raise", amount=True, version=state["version"]).status_code, 409)
        self.assertEqual(self.req("act", player=actor, kind="call", version=-1).status_code, 409)
        self.assertEqual(self.req("settings", sb=5, bb=10, starting_stack=1000, turn_seconds=0).status_code, 409)

    def test_duplicate_concurrent_action_is_applied_once(self):
        self.join()
        state = self.start()
        def act(_):
            return self.req("act", client=self.app.test_client(), player=state["actor_seat"],
                            kind="call", version=state["version"]).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = sorted(pool.map(act, range(2)))
        self.assertEqual(statuses, [200, 409])
        self.assertEqual(len(self.req().get_json()["actions"]), 1)

    def test_pause_keeps_deadline_and_rejects_actions(self):
        self.join(1)
        self.req("settings", sb=5, bb=10, starting_stack=1000, turn_seconds=30)
        state = self.start()
        self.req("pause")
        state = self.req().get_json()
        self.assertTrue(state["paused"])
        self.assertIsNone(state["deadline"])
        self.assertEqual(self.req("act", player=state["actor_seat"], kind="call", version=state["version"]).status_code, 409)
        state = self.req("pause").get_json()
        self.assertFalse(state["paused"])
        self.assertGreater(state["deadline"], time.time() + 25)

    def test_expired_turn_folds_and_unlimited_turn_does_not(self):
        self.join()
        state = self.start()
        self.assertIsNone(state["deadline"])
        self.assertEqual(self.req().get_json()["version"], state["version"])
        with self.app.app_context(), transaction(self.code) as (_, room):
            room["hand"]["deadline"] = time.time() - 1
        state = self.req().get_json()
        self.assertTrue(state["actions"][-1]["automatic"])
        self.assertEqual(state["actions"][-1]["kind"], "fold")

    def test_rebuy_rename_and_new_seat_does_not_inherit_cards(self):
        self.join(1)
        state = self.start()
        self.play_calls()
        old = self.req(player=1).get_json()["players"][1]["cards"]
        self.assertEqual(len(old), 2)
        self.assertEqual(self.req("leave", player=1).status_code, 200)
        response = self.client.post(f"/api/rooms/{self.code}/join", json={"name":"新人", "invite":self.invite})
        newcomer = response.get_json()["room"]
        self.assertEqual(newcomer["players"][1]["cards"], [])
        before = self.req().get_json()["players"][0]["stack"]
        self.assertEqual(self.req("rebuy").get_json()["players"][0]["stack"], before + 1000)
        self.assertEqual(self.req("rename", name="新昵称").get_json()["players"][0]["name"], "新昵称")

    def rig_deck(self, prefix):
        with self.app.app_context(), transaction(self.code) as (_, room):
            hand = room["hand"]
            hand["deck"] = prefix + [c for c in hand["deck"] if c not in prefix]
            state = build_state(hand)
            hand["holes"] = {str(seat): [repr(c) for c in state.hole_cards[i]] for i, seat in enumerate(hand["seats"])}

    def test_three_way_allin_side_pots_and_uncalled_refund(self):
        self.join(2)
        with self.app.app_context(), transaction(self.code) as (_, room):
            for p, stack in zip(room["players"], [300, 100, 200]):
                p["stack"] = p["buyin"] = stack
        self.start()
        # PokerKit index 0=physical seat 1 (AA), 1=seat 2 (KK), 2=seat 0 (QQ).
        self.rig_deck("As Ks Qs Ah Kh Qh 4c 2c 3d 7h 5c 9s 6c Jc".split())
        state = self.req().get_json()
        self.assertEqual(state["actor_seat"], 0)
        state = self.req("act", kind="raise", amount=300, version=state["version"]).get_json()
        while state["phase"] == "playing":
            state = self.req("act", player=state["actor_seat"], kind="call", version=state["version"]).get_json()
        self.assertEqual([p["stack"] for p in state["players"]], [100, 300, 200])
        self.assertEqual(sum(p["stack"] for p in state["players"]), 600)

    def test_board_royal_flush_splits_pot(self):
        self.join(2)
        self.start()
        self.rig_deck("2h 3h 4h 2d 3d 4d 5c Tc Jc Qc 6c Kc 7c Ac".split())
        state = self.play_calls()
        self.assertEqual([p["stack"] for p in state["players"]], [1000,1000,1000])

    def test_bad_input_and_standalone_does_not_expose_training_records(self):
        self.assertEqual(self.client.post("/api/rooms", json=[]).status_code, 400)
        self.assertEqual(self.client.post("/api/rooms", json={"name":""}).status_code, 400)
        self.assertEqual(self.client.post("/api/rooms", json={"name":"a"*17}).status_code, 400)
        self.assertEqual(self.client.get("/api/hands").status_code, 404)
        with self.client.get("/friends-assets/friends.js") as asset:
            self.assertEqual(asset.status_code, 200)
        self.assertEqual(self.client.get("/healthz").get_json(), {"status":"ok"})

    def capacity(self, size):
        response = self.req("settings", sb=5, bb=10, starting_stack=1000,
                            turn_seconds=0, max_players=size)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def stacks(self, amounts):
        with self.app.app_context(), transaction(self.code) as (_, room):
            for p, stack in zip(room["players"], amounts):
                p["stack"] = p["buyin"] = stack

    def action(self, kind, amount=None):
        state = self.req().get_json()
        response = self.req("act", player=state["actor_seat"], kind=kind,
                            amount=amount, version=state["version"])
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_seven_players(self):
        self.check_full_table(7)

    def test_nine_players(self):
        self.check_full_table(9)

    def test_eleven_players(self):
        self.check_full_table(11)

    def check_full_table(self, size):
        self.capacity(size)
        self.join(size - 1)
        state = self.start()
        cards = []
        for i in range(size):
            own = self.req(player=i).get_json()
            for p in own["players"]:
                self.assertEqual(len(p["cards"]), 2 if p["seat"] == i else 0)
                cards.extend(p["cards"])
        self.assertEqual(len(set(cards)), 2 * size)
        final = self.play_calls()
        self.assertEqual(sum(p["stack"] for p in final["players"]), 1000 * size)
        self.assertEqual(len(set(cards + final["board"])), 2 * size + 5)
        self.assertEqual(len(final["actions"]), size * 4)
        self.assertEqual(self.client.post(f"/api/rooms/{self.code}/join", json={
            "name":"超员", "invite":self.invite,
        }).status_code, 409)
        state = self.start()
        self.assertEqual(state["dealer"], 1)

    def test_capacity_validation_migration_and_safe_shrink(self):
        with self.app.app_context(), transaction(self.code) as (_, room):
            del room["settings"]["max_players"]
        self.assertEqual(self.req().get_json()["settings"]["max_players"], 6)
        self.capacity(11)
        self.join(10)
        response = self.req("settings", sb=5, bb=10, starting_stack=1000,
                            turn_seconds=0, max_players=7)
        self.assertEqual(response.status_code, 409)
        for size in (1,12,True,"9"):
            self.assertEqual(self.client.post("/api/rooms", json={"name":"测试", "max_players":size}).status_code, 400)
        self.start()
        self.assertEqual(self.req("settings", sb=5, bb=10, starting_stack=1000,
                                  turn_seconds=0,max_players=9).status_code, 409)

    def test_create_eleven_seat_room(self):
        response = self.client.post("/api/rooms", json={"name":"新房主", "max_players":11})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["room"]["settings"]["max_players"], 11)

    def test_short_allin_does_not_reopen_betting(self):
        self.join(2)
        self.stacks([1000,25,1000])
        self.start()
        self.action("raise",20)  # seat 0, full raise of 10
        self.action("raise",25)  # seat 1, short all-in of 5
        self.action("call")      # seat 2 calls 25
        state = self.req().get_json()
        self.assertEqual(state["actor_seat"],0)
        self.assertFalse(state["legal"]["can_raise"])
        self.assertEqual(state["legal"]["call"],5)
        self.assertEqual(self.req("act",kind="raise",amount=40,version=state["version"]).status_code,409)
        self.action("call")

    def test_cumulative_short_allins_reopen_betting(self):
        self.join(3)
        self.stacks([25,30,1000,1000])
        self.start()
        self.action("raise",20)  # seat 3
        self.action("raise",25)  # seat 0: +5
        self.action("raise",30)  # seat 1: +5, cumulative full raise
        self.action("call")      # seat 2
        state = self.req(player=3).get_json()
        self.assertEqual(state["actor_seat"],3)
        self.assertTrue(state["legal"]["can_raise"])
        self.assertEqual(state["legal"]["min_raise"],40)
        self.action("raise",40)

    def test_heads_up_blinds_and_action_order(self):
        self.join(1)
        state=self.start()
        self.assertEqual(state["dealer"],0)
        self.assertEqual(state["actor_seat"],0)
        self.assertEqual([p["bet"] for p in state["players"]],[5,10])
        self.action("call")
        state=self.action("call")
        self.assertEqual(state["street"],"翻牌")
        self.assertEqual(state["actor_seat"],1)

    def test_entering_heads_up_does_not_repeat_big_blind(self):
        self.join(2)
        self.start()             # seat 0 button, 1 SB, 2 BB
        self.play_calls()
        with self.app.app_context(), transaction(self.code) as (_,room):
            room["players"][0]["stack"]=0
        state=self.start()       # seats 1 and 2 remain: 1 BB, 2 button/SB
        self.assertEqual(state["dealer"],2)
        self.assertEqual(state["actor_seat"],2)
        self.assertEqual(state["players"][1]["bet"],10)
        self.assertEqual(state["players"][2]["bet"],5)

    def test_odd_chip_goes_to_winner_left_of_button(self):
        self.join(2)
        self.start()
        self.rig_deck("2h 3h 4h 2d 3d 4d 5c Tc Jc Qc 6c Kc 7c Ac".split())
        self.action("call")      # seat 0 button
        self.action("fold")      # seat 1 leaves dead SB of 5
        state=self.play_calls()  # seats 2 and 0 tie on the board
        self.assertEqual([p["stack"] for p in state["players"]],[1002,995,1003])

    def test_blind_allins_retain_own_cards_and_settle(self):
        self.join(1)
        self.stacks([2,3])
        state=self.start()
        self.assertEqual(state["phase"],"finished")
        self.assertEqual(sum(p["stack"] for p in state["players"]),5)
        for i in range(2):
            own=self.req(player=i).get_json()
            self.assertEqual(len(own["players"][i]["cards"]),2)

    def test_new_room_defaults_are_one_two_without_ante(self):
        result=self.client.post("/api/rooms",json={"name":"默认规则"}).get_json()
        self.assertEqual(result["room"]["settings"]["sb"],1)
        self.assertEqual(result["room"]["settings"]["bb"],2)
        self.req("settings",sb=1,bb=2,starting_stack=1000,turn_seconds=0)
        self.join()
        state=self.start()
        self.assertEqual(state["pot"],3)
        self.assertEqual(sum(p["stack"]+p["bet"] for p in state["players"]),6000)

    def test_pot_presets_include_call_and_current_street_bet(self):
        self.req("settings",sb=1,bb=2,starting_stack=1000,turn_seconds=0)
        self.join(2)
        self.start()
        state=self.req().get_json()  # button acts first in a three-player hand
        self.assertEqual(state["actor_seat"],0)
        self.assertEqual(state["legal"]["half_pot_to"],5)  # 2 + ceil((3+2)/2)
        self.assertEqual(state["legal"]["pot_to"],7)       # 2 + (3+2)
        self.action("call")
        small_blind=self.req(player=1).get_json()["legal"]
        self.assertEqual(small_blind["half_pot_to"],5)    # existing SB 1 + call 1 + half 6
        self.assertEqual(small_blind["pot_to"],8)
        self.action("call")
        self.action("call")
        flop=self.req(player=1).get_json()["legal"]
        self.assertEqual(flop["call"],0)
        self.assertEqual(flop["half_pot_to"],3)
        self.assertEqual(flop["pot_to"],6)

    def test_pot_presets_clamp_to_min_raise_and_short_allin(self):
        self.join(2)
        self.start()
        self.action("raise",40)
        state=self.req(player=1).get_json()
        self.assertGreaterEqual(state["legal"]["half_pot_to"],state["legal"]["min_raise"])
        self.assertLessEqual(state["legal"]["pot_to"],state["legal"]["max_raise"])
        self.play_calls()
        self.stacks([12,1000,1000])
        self.start()
        # Locate the short player when they are the actor and check legal presets.
        while self.req().get_json()["actor_seat"] != 0:
            self.action("call")
        short=self.req().get_json()["legal"]
        self.assertTrue(short["can_raise"])
        self.assertEqual(short["half_pot_to"],12)
        self.assertEqual(short["pot_to"],12)
        self.action("raise",short["half_pot_to"])

    def test_allin_call_available_when_raise_not_allowed(self):
        self.join(2)
        self.stacks([1000,8,1000])
        self.start()
        self.action("call")
        short=self.req(player=1).get_json()["legal"]
        self.assertFalse(short["can_raise"])
        self.assertEqual(short["all_in"],{"kind":"call","amount":None})
        self.assertIsNone(short["half_pot_to"])
        self.action("call")

    def test_no_allin_raise_when_short_raise_does_not_reopen(self):
        self.join(2)
        self.stacks([1000,25,1000])
        self.start()
        self.action("raise",20)
        self.action("raise",25)
        self.action("call")
        legal=self.req().get_json()["legal"]
        self.assertFalse(legal["can_raise"])
        self.assertIsNone(legal["half_pot_to"])
        self.assertIsNone(legal["pot_to"])
        self.assertIsNone(legal["all_in"])

    def test_positions_follow_real_hand_and_button_rotation(self):
        self.capacity(11)
        self.join(5)
        state=self.start()  # an eleven-seat room with six participants
        self.assertEqual([p["position"] for p in state["players"]],["D","SB","BB","UTG","HJ","CO"])
        self.play_calls()
        state=self.start()
        self.assertEqual([p["position"] for p in state["players"]],["CO","D","SB","BB","UTG","HJ"])
        self.action("fold")
        state=self.req().get_json()
        self.assertEqual(state["players"][4]["position"],"UTG")
        joined=self.client.post(f"/api/rooms/{self.code}/join",json={"name":"中途加入","invite":self.invite}).get_json()
        self.assertIsNone(joined["room"]["players"][-1]["position"])

    def test_heads_up_position_combines_dealer_and_small_blind(self):
        self.join(1)
        state=self.start()
        self.assertEqual([p["position"] for p in state["players"]],["D / SB","BB"])

    def summary(self, client=None):
        return (client or self.client).get(
            f"/api/rooms/{self.code}/summary",
            headers={"Authorization": "Bearer " + self.tokens[0]}).get_json()

    def test_summary_counts_settled_hands_and_split_winners_once(self):
        self.join(2)
        before = self.summary()
        self.assertIsNone(before["started_at"])
        self.start()
        self.rig_deck("2h 3h 4h 2d 3d 4d 5c Tc Jc Qc 6c Kc 7c Ac".split())
        active = self.summary()
        self.assertTrue(active["active"])
        self.assertEqual(active["completed_hands"], 0)
        self.assertEqual(sum(p["net"] for p in active["players"]), 0)
        self.play_calls()
        result = self.summary()
        self.assertEqual(result["completed_hands"], 1)
        self.assertEqual(result["largest_pot"]["pot"], 30)
        self.assertTrue(all(p["hands_played"] == p["hands_won"] == 1 for p in result["players"]))
        self.assertEqual(result["net_total"], 0)
        self.assertEqual([p["net"] for p in result["players"]], [0,0,0])
        self.assertTrue(all(p["won"] for p in result["hands"][0]["results"]))
        # Repeated polls and replays must not duplicate accounting.
        self.req()
        self.assertEqual(self.summary()["completed_hands"], 1)

    def test_summary_side_pot_win_and_uncalled_refund(self):
        self.join(2)
        self.stacks([300,100,200])
        self.start()
        self.rig_deck("As Ks Qs Ah Kh Qh 4c 2c 3d 7h 5c 9s 6c Jc".split())
        self.action("raise",300)
        self.play_calls()
        result = self.summary()
        players = {p["name"]:p for p in result["players"]}
        self.assertEqual(result["largest_pot"]["pot"], 500)
        self.assertEqual(players["房主"]["hands_won"], 0)
        self.assertEqual(players["玩家 1"]["hands_won"], 1)
        self.assertEqual(players["玩家 2"]["hands_won"], 1)
        self.assertEqual(players["玩家 2"]["net"], 0)

    def test_fold_pot_excludes_unmatched_raise_and_winner_counts(self):
        self.join(2)
        self.start()
        self.action("raise",500)
        self.action("fold")
        self.action("fold")
        result = self.summary()
        self.assertEqual(result["largest_pot"]["pot"], 25)  # SB5 + BB10 + winner's matched10.
        winner = next(p for p in result["players"] if p["name"] == "房主")
        self.assertEqual(winner["hands_won"],1)
        self.assertEqual(winner["net"],15)

    def test_flop_fold_pot_does_not_include_uncalled_flop_bet(self):
        self.join(1)
        self.start()
        self.action("call")
        self.action("call")
        self.action("raise",500)
        self.action("fold")
        self.assertEqual(self.summary()["largest_pot"]["pot"],20)

    def test_rebuy_leave_and_same_name_rejoin_are_separate_balanced_entries(self):
        self.join(1)
        self.start()
        self.action("fold")
        before = self.summary()
        old = next(p for p in before["players"] if p["name"] == "玩家 1")
        self.req("rebuy", player=1)
        topped = next(p for p in self.summary()["players"] if p["id"] == old["id"])
        self.assertEqual(topped["buyin"],2000)
        self.assertEqual(topped["net"],old["net"])
        self.req("leave",player=1)
        after = self.summary()
        departed = next(p for p in after["players"] if p["id"] == old["id"])
        self.assertEqual(departed["stack"],0)
        self.assertEqual(departed["cashout"],topped["stack"])
        self.assertEqual(departed["status"],"已离桌")
        self.assertEqual(after["net_total"],0)
        new = self.client.post(f"/api/rooms/{self.code}/join",json={"name":"玩家 1","invite":self.invite}).get_json()
        report = self.summary()
        same_name = [p for p in report["players"] if p["name"] == "玩家 1"]
        self.assertEqual(len(same_name),2)
        fresh = next(p for p in same_name if p["id"] != old["id"])
        self.assertEqual((fresh["net"],fresh["hands_played"],fresh["hands_won"]),(0,0,0))
        self.assertEqual(report["net_total"],0)
        self.assertEqual(len({p["entry"] for p in report["players"]}),3)
        self.assertNotEqual(new["room"]["players"][1]["id"],old["id"])

    def test_late_join_only_counts_dealt_hands_and_renaming_preserves_identity(self):
        self.join(1)
        self.start()
        self.join(1)
        self.play_calls()
        result = self.summary()
        late = next(p for p in result["players"] if p["name"] == "玩家 2")
        self.assertEqual(late["hands_played"],0)
        self.req("rename",player=2,name="新的名字")
        renamed = next(p for p in self.summary()["players"] if p["id"] == late["id"])
        self.assertEqual(renamed["name"],"新的名字")
        self.start()
        self.play_calls()
        self.assertEqual(next(p for p in self.summary()["players"] if p["id"] == late["id"])["hands_played"],1)

    def test_end_session_requires_host_and_idle_freezes_bill_and_clock(self):
        self.join(1)
        self.start()
        self.assertEqual(self.req("end").status_code,409)
        self.action("fold")
        self.assertEqual(self.req("end",player=1).status_code,403)
        self.assertTrue(self.req("end").get_json()["session_ended"])
        report = self.summary()
        self.assertIsNotNone(report["ended_at"])
        for command in ("start","rebuy","rename","settings","end"):
            self.assertEqual(self.req(command,name="改名").status_code,409)
        self.assertEqual(self.client.post(f"/api/rooms/{self.code}/join",json={"name":"迟到","invite":self.invite}).status_code,409)
        with patch("session_stats.time.time",return_value=time.time()+7200):
            later=self.summary()
        self.assertEqual(report["elapsed_seconds"],later["elapsed_seconds"])
        self.req("leave",player=1)
        # Leaving after the session end doesn't rewrite the fixed final bill.
        self.assertEqual(self.summary()["players"],report["players"])

    def test_csv_auth_unicode_formulas_and_no_private_cards(self):
        self.join(1)
        self.req("rename",name="=HYPERLINK(1)")
        self.start()
        self.action("fold")
        for endpoint in ("summary","export/summary","export/hands"):
            self.assertEqual(self.client.get(f"/api/rooms/{self.code}/{endpoint}").status_code,401)
        headers={"Authorization":"Bearer "+self.tokens[0]}
        export=self.client.get(f"/api/rooms/{self.code}/export/summary",headers=headers)
        self.assertEqual(export.status_code,200)
        self.assertTrue(export.data.startswith(b"\xef\xbb\xbf"))
        text=export.data.decode("utf-8-sig")
        rows=list(csv.DictReader(io.StringIO(text)))
        self.assertIn("'=HYPERLINK(1)",[r["昵称"] for r in rows])
        self.assertEqual(sum(int(r["净盈亏"]) for r in rows),0)
        self.assertNotIn("token_hash",text)
        detail=self.client.get(f"/api/rooms/{self.code}/export/hands",headers=headers)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(detail.data.decode("utf-8-sig"))))),2)
        summary=self.summary()
        self.assertNotIn("deck",str(summary))
        self.assertNotIn("holes",str(summary))
        self.assertNotIn("token_hash",str(summary))
        self.assertEqual(export.headers["Cache-Control"],"no-store")

    def test_full_session_survives_restart_and_recent_history_limit(self):
        self.join(1)
        for _ in range(52):
            self.start()
            self.action("fold")
        self.assertEqual(len(self.req().get_json()["history"]),50)
        before=self.summary()
        self.assertEqual(before["completed_hands"],52)
        self.assertEqual(len(before["hands"]),52)
        self.assertEqual(sum(p["hands_played"] for p in before["players"]),104)
        new_client=create_app(self.config).test_client()
        after=self.summary(client=new_client)
        for key in ("players","hands","largest_pot","started_at"):
            self.assertEqual(before[key],after[key])

    def test_legacy_room_migration_marks_missing_history_without_inventing_it(self):
        self.join(1)
        self.start()
        self.action("fold")
        with self.app.app_context(),transaction(self.code) as (_,room):
            del room["session"]
        report=self.summary()
        self.assertTrue(report["legacy"])
        self.assertEqual(report["first_hand"],2)
        self.assertEqual(report["completed_hands"],0)
        self.assertEqual(report["net_total"],0)
        self.start()
        self.action("fold")
        report=self.summary()
        self.assertEqual(report["completed_hands"],1)
        self.assertTrue(all(p["hands_played"]==1 for p in report["players"]))

    def chat(self, text="你好，同桌！", player=0, message_id=None, **extra):
        self.chat_counter=getattr(self,"chat_counter",0)+1
        return self.client.post(f"/api/rooms/{self.code}/chat",json={
            "text":text,"client_id":message_id or f"test-message-{self.chat_counter:06d}",**extra,
        },headers={"Authorization":"Bearer "+self.tokens[player]})

    def chat_history(self, after=0, client=None):
        return (client or self.client).get(f"/api/rooms/{self.code}/chat?after={after}",
                                          headers={"Authorization":"Bearer "+self.tokens[0]})

    def test_chat_auth_room_isolation_and_authoritative_sender(self):
        self.join(1)
        url=f"/api/rooms/{self.code}/chat"
        self.assertEqual(self.client.get(url).status_code,401)
        self.assertEqual(self.client.post(url,json={"text":"陌生人"}).status_code,401)
        response=self.chat("中文、emoji 🃏\n第二行",player=1,name="伪装房主",player_id="fake",created_at=1)
        self.assertEqual(response.status_code,201)
        message=response.get_json()["message"]
        self.assertEqual(message["name"],"玩家 1")
        self.assertNotEqual(message["player_id"],"fake")
        self.assertGreater(message["created_at"],1)
        self.assertEqual(message["text"],"中文、emoji 🃏\n第二行")
        self.assertNotIn("client_id",message)
        self.assertNotIn("token_hash",str(response.get_json()))
        self.assertEqual(self.chat_history().get_json()["messages"],[message])
        other=self.client.post("/api/rooms",json={"name":"另一桌"}).get_json()
        foreign={"Authorization":"Bearer "+other["token"]}
        self.assertEqual(self.client.get(url,headers=foreign).status_code,401)
        self.assertEqual(self.client.post(url,json={"text":"跨房间","client_id":"test-foreign-123456"},headers=foreign).status_code,401)

    def test_chat_validation_unicode_and_literal_html(self):
        for text in ("", " \n\t ", "字"*301, True, 123, [], "含\x00控制符", "含\r回车符"):
            self.assertEqual(self.chat(text).status_code,400)
        for message_id in ("x",True,123,"!"*20,"中"*20):
            self.assertEqual(self.chat(message_id=message_id).status_code,400)
        html='<img src=x onerror="alert(1)"> & <script>test</script>'
        self.assertEqual(self.chat(html).get_json()["message"]["text"],html)
        self.join(1)
        response=self.chat("🃏"*300,player=1)
        self.assertEqual(response.status_code,201)
        self.assertEqual(len(response.get_json()["message"]["text"]),300)
        self.assertEqual(self.chat_history().get_json()["version"],2)

    def test_chat_retry_is_idempotent_and_rate_limit_is_per_player(self):
        self.join(1)
        first=self.chat(message_id="test-retry-0000001")
        retry=self.chat(message_id="test-retry-0000001")
        self.assertEqual(retry.status_code,200)
        self.assertEqual(first.get_json(),retry.get_json())
        self.assertEqual(self.chat("改成另一条",message_id="test-retry-0000001").status_code,409)
        self.assertEqual(self.chat("连续刷屏").status_code,429)
        self.assertEqual(self.chat("同一个 IP 的另一位",player=1).status_code,201)
        self.assertEqual(len(self.chat_history().get_json()["messages"]),2)
        with self.app.app_context(),transaction(self.code) as (_,room):
            room["players"][0]["last_chat_at"]-=2
        self.assertEqual(self.chat("稍后发送").status_code,201)

    def test_chat_concurrent_retry_and_ordered_incremental_sync(self):
        def post(_):
            return self.app.test_client().post(f"/api/rooms/{self.code}/chat",json={
                "text":"只出现一次","client_id":"test-concurrent-1234",
            },headers={"Authorization":"Bearer "+self.tokens[0]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(post,range(2)))
        self.assertEqual(sorted(r.status_code for r in responses),[200,201])
        self.join(1)
        self.chat("第二条",player=1)
        result=self.chat_history(after=1).get_json()
        self.assertEqual([m["id"] for m in result["messages"]],[2])
        self.assertEqual(self.chat_history(after=2).get_json()["messages"],[])
        for cursor in ("-1","no","1.1","9"*20,"一"):
            self.assertEqual(self.chat_history(after=cursor).status_code,400)

    def test_chat_does_not_change_action_version_deadline_or_accounting(self):
        self.join(1)
        state=self.start()
        before=self.summary()
        self.assertEqual(self.chat("下注前聊一句").status_code,201)
        after=self.req().get_json()
        self.assertEqual(after["version"],state["version"])
        self.assertEqual(after["deadline"],state["deadline"])
        self.assertEqual(after["actions"],state["actions"])
        self.assertEqual(after["chat_version"],1)
        self.assertNotIn("chat",after)
        self.assertEqual(self.summary()["players"],before["players"])
        response=self.req("act",player=state["actor_seat"],kind="call",version=state["version"])
        self.assertEqual(response.status_code,200)

    def test_chat_available_while_paused_and_after_session_ends(self):
        self.join(1)
        self.start()
        self.req("pause")
        self.assertEqual(self.chat("暂停聊聊").status_code,201)
        self.req("pause")
        self.play_calls()
        self.req("end")
        bill=self.summary()
        self.assertEqual(self.chat("下次再来！",player=1).status_code,201)
        after=self.summary()
        for key in ("version","players","hands","ended_at","elapsed_seconds"):
            self.assertEqual(bill[key],after[key])
        self.assertEqual(self.chat_history().get_json()["version"],2)

    def test_chat_retains_last_200_messages_and_survives_restart(self):
        for i in range(205):
            with self.app.app_context(),transaction(self.code) as (_,room):
                room["players"][0]["last_chat_at"]=0
            self.assertEqual(self.chat(f"消息 {i+1}").status_code,201)
        result=self.chat_history().get_json()
        self.assertEqual(result["version"],205)
        self.assertEqual(result["oldest_id"],6)
        self.assertEqual([m["id"] for m in result["messages"]],list(range(6,206)))
        client=create_app(self.config).test_client()
        self.assertEqual(self.chat_history(client=client).get_json(),result)
        self.assertEqual(self.chat_history().headers["Cache-Control"],"no-store")

    def test_chat_old_rooms_rename_and_new_occupant_identity(self):
        self.join(1)
        with self.app.app_context(),transaction(self.code) as (_,room):
            del room["chat"],room["chat_version"]
        self.assertEqual(self.chat_history().get_json()["messages"],[])
        old=self.chat("原来的我",player=1).get_json()["message"]
        self.req("rename",player=1,name="新昵称")
        self.assertEqual(self.chat_history().get_json()["messages"][0]["name"],"玩家 1")
        self.req("leave",player=1)
        self.assertEqual(self.chat("离桌后",player=1).status_code,401)
        joined=self.client.post(f"/api/rooms/{self.code}/join",json={"name":"新昵称","invite":self.invite}).get_json()
        self.tokens.append(joined["token"])
        fresh=self.chat("新的座位",player=2).get_json()["message"]
        self.assertNotEqual(fresh["player_id"],old["player_id"])
        self.assertEqual(len(self.chat_history().get_json()["messages"]),2)


if __name__ == "__main__":
    unittest.main()
