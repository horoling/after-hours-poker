"""Run with: python -m unittest discover -s tests -v."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
