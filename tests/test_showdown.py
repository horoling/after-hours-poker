"""Public showdown, best hands and actual pot awards (including zero-net wins)."""
import unittest

from pokerkit import StandardHighHand
from multiplayer_app import create_app
from private_rooms import best_hand, transaction
import test_private_rooms as fixtures


class ShowdownTests(unittest.TestCase):
    def setUp(self):
        self.g = fixtures.PrivateRoomTests()
        self.g.setUp()
        self.addCleanup(self.g.tearDown)

    def pairs(self):
        self.g.join(2)
        self.g.start()
        self.g.rig_deck("As Ks Qs Ah Kh Qh 4c 2c 3d 7h 5c 9s 6c Jc".split())

    def test_mucked_losers_are_public_to_every_player_at_showdown(self):
        self.pairs()
        self.g.play_calls()
        for viewer in range(3):
            state = self.g.req(player=viewer).get_json()
            self.assertTrue(state["result"]["showdown"])
            self.assertTrue(all(len(p["cards"]) == 2 and p["hand_rank"] for p in state["players"]))
            self.assertTrue(all(p["hand_rank"]["category"] == "一对" for p in state["players"]))
            self.assertEqual([p["seat"] for p in state["players"] if p["won"]], [1])
            self.assertEqual([p["awarded"] for p in state["players"]], [0, 30, 0])
            self.assertNotIn("deck", state)

    def test_no_hand_rank_or_opponent_cards_before_final_call(self):
        self.pairs()
        saw_river = False
        while True:
            state = self.g.req().get_json()
            if state["phase"] != "playing":
                break
            saw_river |= len(state["board"]) == 5
            self.assertIsNone(state["result"])
            for viewer in range(3):
                own = self.g.req(player=viewer).get_json()
                for p in own["players"]:
                    self.assertIsNone(p["hand_rank"])
                    self.assertEqual(len(p["cards"]), 2 if p["seat"] == viewer else 0)
                    self.assertFalse(p["won"])
            self.g.action("call")
        self.assertTrue(saw_river)

    def test_folded_hand_stays_private_even_when_other_players_show(self):
        self.pairs()
        self.g.action("fold")  # Physical seat zero folds preflop.
        self.g.play_calls()
        for viewer in range(3):
            state = self.g.req(player=viewer).get_json()
            folded = state["players"][0]
            self.assertEqual(folded["status"], "已弃牌")
            self.assertIsNone(folded["hand_rank"])
            self.assertEqual(len(folded["cards"]), 2 if viewer == 0 else 0)
            public_folded = next(p for p in state["result"]["players"] if p["seat"] == 0)
            self.assertEqual(public_folded["cards"], [])
            self.assertIsNone(public_folded["hand_rank"])

    def test_winning_by_folds_does_not_force_a_reveal(self):
        self.g.join(1)
        state = self.g.start()
        folded_seat = state["actor_seat"]
        self.g.action("fold")
        for viewer in range(2):
            state = self.g.req(player=viewer).get_json()
            result = state["result"]
            self.assertFalse(result["showdown"])
            self.assertEqual(len([p for p in result["players"] if p["won"]]), 1)
            self.assertFalse(next(p for p in result["players"] if p["seat"] == folded_seat)["won"])
            self.assertTrue(all(not p["cards"] and p["hand_rank"] is None for p in result["players"]))
            self.assertTrue(all(len(p["cards"]) == (2 if p["seat"] == viewer else 0) for p in state["players"]))

    def test_board_only_royal_flush_split_has_three_zero_net_winners(self):
        self.g.join(2)
        self.g.start()
        self.g.rig_deck("2h 3h 4h 2d 3d 4d 5c Tc Jc Qc 6c Kc 7c Ac".split())
        state = self.g.play_calls()
        result = state["result"]
        self.assertTrue(result["split"])
        self.assertEqual(len(result["pots"][0]["winners"]), 3)
        self.assertTrue(all(p["won"] and p["delta"] == 0 and p["awarded"] == 10 for p in result["players"]))
        for p in result["players"]:
            self.assertEqual(p["hand_rank"]["category"], "皇家同花顺")
            self.assertEqual(set(p["hand_rank"]["cards"]), set(state["board"]))

    def test_side_pot_winner_can_have_zero_net_and_refund_is_not_an_award(self):
        self.g.join(2)
        self.g.stacks([300,100,200])
        self.g.start()
        self.g.rig_deck("As Ks Qs Ah Kh Qh 4c 2c 3d 7h 5c 9s 6c Jc".split())
        self.g.action("raise", 300)
        state = self.g.play_calls()
        result = state["result"]
        self.assertEqual(result["pot"], 500)
        self.assertEqual([p["amount"] for p in result["pots"]], [300,200])
        by_seat = {p["seat"]:p for p in result["players"]}
        self.assertEqual((by_seat[1]["awarded"],by_seat[1]["delta"]), (300,200))
        self.assertTrue(by_seat[2]["won"])
        self.assertEqual((by_seat[2]["awarded"],by_seat[2]["delta"]), (200,0))
        self.assertFalse(by_seat[0]["won"])
        self.assertEqual(sum(p["awarded"] for p in result["players"]), result["pot"])

    def test_large_uncalled_raise_is_returned_not_celebrated_as_winnings(self):
        self.g.join(1)
        self.g.start()
        self.g.action("call")
        self.g.action("call")
        self.g.action("raise", 500)
        self.g.action("fold")
        result = self.g.req().get_json()["result"]
        self.assertEqual(result["pot"],20)
        self.assertEqual(sum(p["awarded"] for p in result["players"]),20)
        self.assertFalse(result["showdown"])

    def test_reconnect_and_rebuy_keep_result_new_hand_clears_it(self):
        self.pairs()
        final = self.g.play_calls()
        restored = self.g.req(client=create_app(self.g.config).test_client()).get_json()
        self.assertEqual(restored["result"],final["result"])
        self.g.req("rebuy")
        self.assertEqual(self.g.req().get_json()["result"],final["result"])
        # Legacy completed rooms without cached result get the same filtered data.
        with self.g.app.app_context(), transaction(self.g.code) as (_, room):
            room["hand"].pop("result")
        self.assertEqual(self.g.req().get_json()["result"],final["result"])
        new = self.g.start()
        self.assertIsNone(new["result"])
        self.assertTrue(all(p["hand_rank"] is None and not p["won"] for p in new["players"]))

    def test_best_five_categories_and_ace_low_straight(self):
        cases = [
            ("AsKs", "QsJsTs2c3d", "皇家同花顺"),
            ("9s8s", "7s6s5s2c3d", "同花顺"),
            ("AsAh", "AdAcKs2c3d", "四条"),
            ("AsAh", "AdKsKh2c3d", "葫芦"),
            ("As7s", "KsJs2s3d4h", "同花"),
            ("As2s", "3c4d5h9sKh", "顺子"),
            ("AsAh", "AdKsQh2c3d", "三条"),
            ("AsAh", "KsKhQh2c3d", "两对"),
            ("AsAh", "KsQh9c2c3d", "一对"),
            ("As7h", "KsJh9c2c3d", "高牌"),
        ]
        for hole, board, category in cases:
            with self.subTest(category=category):
                cards = lambda text: [text[i:i+2] for i in range(0,len(text),2)]
                rank = best_hand(cards(hole),cards(board))
                self.assertEqual(rank["category"],category)
                self.assertEqual(len(rank["cards"]),5)
                self.assertEqual(StandardHighHand("".join(rank["cards"])),StandardHighHand.from_game(hole,board))
                if hole == "As2s":
                    self.assertEqual(rank["label"],"顺子 · 5 高")
                    self.assertEqual(rank["cards"][-1],"As")
