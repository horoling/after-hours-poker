"""Agent integration: permissions, private information and atomic actions."""
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

from multiplayer_app import create_app
from private_rooms import agent_task, build_state, transaction
from room_agents import choose_action, context


class RoomAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(TESTING=True, ROOMS_DATABASE=str(Path(self.temp.name) / "rooms.db"))
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        result = self.client.post("/api/rooms", json=dict(name="房主", turn_seconds=0)).get_json()
        self.code, self.invite = result["room"]["code"], result["room"]["invite"]
        self.tokens = [result["token"]]

    def req(self, command=None, player=0, client=None, **data):
        client = client or self.client
        url = f"/api/rooms/{self.code}" + (f"/{command}" if command else "")
        headers = {"Authorization": "Bearer " + self.tokens[player]}
        readonly = command is None or command == "summary" or command.startswith("export/")
        return client.get(url, headers=headers) if readonly else client.post(url, json=data, headers=headers)

    def join(self):
        result = self.client.post(f"/api/rooms/{self.code}/join", json=dict(name="朋友", invite=self.invite))
        self.assertEqual(result.status_code, 200)
        self.tokens.append(result.get_json()["token"])

    def add(self, count=1):
        state = self.req().get_json()
        response = self.req("add_agent", count=count, version=state["version"])
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def due(self):
        with self.app.app_context(), transaction(self.code) as (_, room):
            room["hand"]["agent_due"] = 0

    def to_agent(self):
        state = self.req("start").get_json()
        if state["legal"]:
            state = self.req("act", kind="call", version=state["version"]).get_json()
        self.assertTrue(next(p for p in state["players"] if p["seat"] == state["actor_seat"])["is_agent"])
        return state

    def test_permissions_capacity_and_duplicate_add(self):
        self.join()
        state = self.req().get_json()
        self.assertEqual(self.req("add_agent", player=1, count=1, version=state["version"]).status_code, 403)
        self.assertEqual(self.req("add_agent", count=True, version=state["version"]).status_code, 400)
        self.assertEqual(self.req("add_agent", count=5, version=state["version"]).status_code, 409)
        response = self.req("add_agent", count=4, version=state["version"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.req("add_agent", count=4, version=state["version"]).status_code, 409)
        state = response.get_json()
        agents = [p for p in state["players"] if p["is_agent"]]
        self.assertEqual(len(agents), 4)
        self.assertTrue(all(p["online"] and p["stack"] == 1000 for p in agents))
        self.assertEqual(len({p["seat"] for p in state["players"]}), 6)
        self.assertEqual(self.client.post(f"/api/rooms/{self.code}/join", json=dict(name="满了", invite=self.invite)).status_code, 409)

    def test_no_agent_credentials_or_impersonation(self):
        state = self.add()
        text = json.dumps(state)
        self.assertNotIn("token_hash", text)
        self.assertNotIn("token", state)
        # Even a known credential cannot control an AI seat.
        from private_rooms import digest
        with self.app.app_context(), transaction(self.code) as (_, room):
            room["players"][1]["token_hash"] = digest("test-agent-credential")
        self.assertEqual(self.client.get(f"/api/rooms/{self.code}", headers={"Authorization": "Bearer test-agent-credential"}).status_code, 401)

    def test_management_only_between_hands_even_when_paused(self):
        state = self.add()
        agent = state["players"][1]
        human = state["players"][0]
        self.assertEqual(self.req("remove_agent", agent_id=human["id"], version=state["version"]).status_code, 404)
        self.req("start")
        state = self.req("pause").get_json()
        for command in ("add_agent", "remove_agent", "rebuy_agent"):
            self.assertEqual(self.req(command, agent_id=agent["id"], count=1, version=state["version"]).status_code, 409)

    def test_remove_rebuy_rejoin_and_csv_keep_agent_accounts(self):
        state = self.add()
        agent_id = state["players"][1]["id"]
        state = self.req("rebuy_agent", agent_id=agent_id, version=state["version"]).get_json()
        self.assertEqual(state["players"][1]["stack"], 2000)
        self.assertEqual(self.req("remove_agent", agent_id=agent_id, version=state["version"]).status_code, 200)
        state = self.add()
        self.assertNotEqual(state["players"][1]["id"], agent_id)
        report = self.req("summary").get_json()
        old = next(p for p in report["players"] if p["id"] == agent_id)
        self.assertTrue(old["is_agent"])
        self.assertEqual((old["cashout"], old["buyin"], old["net"]), (2000, 2000, 0))
        self.assertEqual(report["net_total"], 0)
        self.assertEqual(report["total_buyin"], report["total_chips"])
        response = self.req("export/summary")
        rows = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))
        self.assertEqual([r["玩家类型"] for r in rows].count("Agent"), 2)
        state = self.req("end").get_json()
        self.assertEqual(self.req("rebuy_agent", agent_id=state["players"][1]["id"], version=state["version"]).status_code, 409)

    def test_host_transfer_skips_agents_and_can_be_reclaimed(self):
        state = self.add()
        self.join()
        self.req("leave")
        state = self.req(player=1).get_json()
        self.assertEqual(state["owner"], state["me"])
        self.req("leave", player=1)
        self.join()
        state = self.req(player=2).get_json()
        self.assertEqual(state["owner"], state["me"])
        self.assertEqual(len(state["players"]), 2)

    def test_thinking_pause_restart_and_single_action_per_poll(self):
        self.add(2)
        state = self.to_agent()
        before = len(state["actions"])
        # Save a long remaining delay, pause, and prove that restart preserves it.
        with self.app.app_context(), transaction(self.code) as (_, room):
            room["hand"]["agent_due"] = time.time() + 50
        state = self.req("pause").get_json()
        self.due()
        other = create_app(self.config).test_client()
        self.assertEqual(len(self.req(client=other).get_json()["actions"]), before)
        self.req("pause")
        self.assertEqual(len(self.req(client=other).get_json()["actions"]), before)
        self.due()
        with patch("private_rooms.choose_action", return_value=dict(kind="call", amount=None)) as decide:
            state = self.req(client=other).get_json()
            self.assertEqual(decide.call_count, 1)
        self.assertEqual(len(state["actions"]), before + 1)
        self.assertTrue(state["actions"][-1]["agent"])
        self.assertFalse(state["actions"][-1]["automatic"])
        self.assertEqual(len(self.req().get_json()["actions"]), before + 1)

    def test_concurrent_polls_compute_outside_write_lock_commit_once(self):
        self.add(2)
        before = self.to_agent()
        self.due()
        barrier = Barrier(2)

        def decide(*args):
            barrier.wait(timeout=5)
            return dict(kind="call", amount=None)

        with patch("private_rooms.choose_action", side_effect=decide):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: self.req(client=self.app.test_client()), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertTrue(all(len(r.get_json()["actions"]) == len(before["actions"]) + 1 for r in responses))

    def test_pause_during_calculation_discards_stale_decision(self):
        self.add()
        before = self.to_agent()
        self.due()
        started, release = Event(), Event()

        def decide(*args):
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("strategy held database lock")
            return dict(kind="call", amount=None)

        with patch("private_rooms.choose_action", side_effect=decide), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: self.req(client=self.app.test_client()))
            self.assertTrue(started.wait(timeout=5))
            try:
                self.assertEqual(self.req("pause").status_code, 200)
            finally:
                release.set()
            state = future.result().get_json()
        self.assertTrue(state["paused"])
        self.assertEqual(len(state["actions"]), len(before["actions"]))

    def test_observation_contains_only_own_cards_and_public_information(self):
        self.add(2)
        self.to_agent()
        self.due()
        with self.app.app_context(), transaction(self.code) as (_, room):
            state = build_state(room["hand"])
            task = agent_task(room, state)
            own = [repr(c) for c in state.hole_cards[state.actor_index]]
            self.assertEqual(task["observation"]["hand"], own)
            text = json.dumps(task)
            for key in ("deck", "holes", "token_hash", "invite", "player_ids"):
                self.assertNotIn(key, text)
            for i, cards in enumerate(state.hole_cards):
                if i != state.actor_index:
                    for card in cards:
                        self.assertNotIn(repr(card), text)
        response = self.req().get_json()
        self.assertEqual(len(response["players"][0]["cards"]), 2)
        if response["phase"] == "playing":
            self.assertTrue(all(not p["cards"] for p in response["players"] if p["is_agent"]))

    def test_strategy_failure_uses_check_fold_not_a_paid_call(self):
        self.add()
        self.to_agent()
        self.due()
        with patch("private_rooms.choose_action", side_effect=RuntimeError("test failure")), self.assertLogs(self.app.logger, level="ERROR"):
            state = self.req().get_json()
        self.assertEqual(state["actions"][-1]["kind"], "call")  # BB can check free.
        # Start another hand, and have the human face the AI with a paid bet.
        with patch("private_rooms.choose_action", return_value=dict(kind="call", amount=None)):
            while state["phase"] == "playing":
                self.due()
                state = (self.req("act", kind="call", version=state["version"]) if state["legal"] else self.req()).get_json()
        state = self.req("start").get_json()
        self.due()
        with patch("private_rooms.choose_action", return_value=dict(kind="call", amount=None)):
            state = self.req().get_json()
        self.assertIsNotNone(state["legal"])
        self.req("act", kind="raise", amount=100, version=state["version"])
        self.due()
        with patch("private_rooms.choose_action", side_effect=RuntimeError("test failure")), self.assertLogs(self.app.logger, level="ERROR"):
            state = self.req().get_json()
        self.assertEqual(state["actions"][-1]["kind"], "fold")

    def test_full_mixed_hands_for_2_7_9_11_seats(self):
        for capacity in (2, 7, 9, 11):
            with self.subTest(capacity=capacity):
                result = self.client.post("/api/rooms", json=dict(name="房主", max_players=capacity, turn_seconds=0)).get_json()
                self.code, self.invite, self.tokens = result["room"]["code"], result["room"]["invite"], [result["token"]]
                self.add(capacity - 1)
                state = self.req("start").get_json()
                count = 0
                while state["phase"] == "playing":
                    if state["legal"]:
                        response = self.req("act", kind="call", version=state["version"])
                    else:
                        self.due()
                        response = self.req()
                    self.assertEqual(response.status_code, 200, response.get_json())
                    state = response.get_json()
                    count += 1
                    self.assertLess(count, 150)
                report = self.req("summary").get_json()
                self.assertEqual(report["completed_hands"], 1)
                self.assertEqual(sum(p["stack"] for p in state["players"]), capacity * 1000)
                self.assertEqual(report["net_total"], 0)
                self.assertTrue(all(p["hands_played"] == 1 for p in report["players"]))
                self.assertTrue(any(a["agent"] for a in state["actions"]))
                rows = list(csv.DictReader(io.StringIO(self.req("export/hands").data.decode("utf-8-sig"))))
                self.assertEqual([r["玩家类型"] for r in rows].count("Agent"), capacity - 1)

    def test_legal_sizing_and_deterministic_strategy_from_observation(self):
        self.add(2)
        self.to_agent()
        self.due()
        with self.app.app_context(), transaction(self.code) as (_, room):
            task = agent_task(room, build_state(room["hand"]))
        ctx, moves = context(task["observation"])
        self.assertEqual(len(ctx["hand"]), 2)
        self.assertEqual(choose_action(task["observation"], task["seed"]), choose_action(task["observation"], task["seed"]))
        for move in moves.values():
            if move["kind"] == "raise":
                self.assertGreaterEqual(move["amount"], task["observation"]["options"]["min_raise"])
                self.assertLessEqual(move["amount"], task["observation"]["options"]["max_raise"])

    def test_real_postflop_policy_and_short_all_in_mapping(self):
        self.add(2)
        state = self.req("start").get_json()
        with patch("private_rooms.choose_action", return_value=dict(kind="call", amount=None)):
            while not state["board"]:
                self.due()
                state = (self.req("act", kind="call", version=state["version"]) if state["legal"] else self.req()).get_json()
        self.due()
        with self.app.app_context(), transaction(self.code) as (_, room):
            task = agent_task(room, build_state(room["hand"]))
        self.assertIsNotNone(task)
        ctx, moves = context(task["observation"])
        self.assertEqual(len(ctx["board"]), 3)
        self.assertIn(choose_action(task["observation"], task["seed"]), moves.values())
        # Insufficient chips to call is legal all-in CALL, never a raise.
        obs = dict(task["observation"], hand=["As", "Ah"], board=[], stack=1,
                   options=dict(call=1, fold=True, can_raise=False, min_raise=None,
                                max_raise=None, half_pot_to=None, pot_to=None,
                                all_in=dict(kind="call", amount=None)))
        self.assertEqual(choose_action(obs, 42), dict(kind="call", amount=None))

    def test_agents_all_in_unequal_stacks_settle_side_pots(self):
        self.add(2)
        with self.app.app_context(), transaction(self.code) as (_, room):
            for p, amount in zip(room["players"], (1000, 70, 180)):
                p["stack"] = p["buyin"] = amount
        state = self.req("start").get_json()
        def all_in(obs, seed):
            return obs["options"]["all_in"] or dict(kind="call", amount=None)
        with patch("private_rooms.choose_action", side_effect=all_in):
            while state["phase"] == "playing":
                self.due()
                response = self.req("act", kind="call", version=state["version"]) if state["legal"] else self.req()
                self.assertEqual(response.status_code, 200)
                state = response.get_json()
        self.assertEqual(sum(p["stack"] for p in state["players"]), 1250)
        self.assertEqual(self.req("summary").get_json()["net_total"], 0)

    def test_friends_can_join_mixed_table_and_act_their_own_seats(self):
        self.join()
        self.add(2)
        state = self.req("start").get_json()
        for _ in range(120):
            if state["phase"] != "playing":
                break
            seat = state["actor_seat"]
            if seat < 2:
                state = self.req(player=seat).get_json()
                response = self.req("act", player=seat, kind="call", version=state["version"])
            else:
                self.due()
                response = self.req()
            self.assertEqual(response.status_code, 200)
            state = response.get_json()
        self.assertEqual(state["phase"], "finished")
        self.assertEqual(sum(p["stack"] for p in state["players"]), 4000)
        report = self.req("summary", player=1).get_json()
        self.assertEqual(len([p for p in report["players"] if not p["is_agent"]]), 2)
        self.assertEqual(report["net_total"], 0)


if __name__ == "__main__":
    unittest.main()
