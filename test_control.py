"""Testy komend Telegrama i sterowania w trakcie działania, także przy dwóch kontach naraz (bez sieci).

Uruchom: python -m unittest test_control -v
"""
import time
import unittest
from unittest import mock

import control
import luxmed_watch as lw
from testutil import make_ctx

BLOCKS = [
    {"label": "Stomatolog", "city_id": 1, "service_id": 6621, "doctors": ["MONIKA HORVATH SKAPPER"]},
    {"label": "Konsultacja dermatologiczna", "city_id": 1, "service_id": 4448, "doctors": ["ADRIAN SZYMAŃSKI"]},
    {"label": "Dermatolog (dowolny)", "enabled": False, "city_id": 1, "service_id": 4448},
]
ZONA_BLOCKS = [
    {"label": "Konsultacja dermatologiczna", "city_id": 1, "service_id": 4448, "doctors": ["ADRIAN SZYMAŃSKI"]},
]
CFG = {"interval_minutes": 5, "booking": {"enabled": True},
       "accounts": {"marcin": {"login": "m", "password": "p"}, "zona": {"login": "z", "password": "p"}}}


def msg(text, uid=42, age=0):
    return {"from": {"id": uid}, "text": text, "date": time.time() - age}


class Base(unittest.TestCase):
    def setUp(self):
        lw.TG.update({"bot_token": "T", "chat_id": "42"})
        lw.WAKE.clear()
        self.ctx = make_ctx(self, blocks=BLOCKS)

    def say(self, text, ctxs=None, **kw):
        with mock.patch.object(lw, "tg_api") as tg:
            lw.handle_message(ctxs or [self.ctx], CFG, msg(text, **kw))
        return [c.kwargs.get("text", "") for c in tg.call_args_list if c.args[0] == "sendMessage"]


class ControlTests(Base):
    def test_parse_command(self):
        self.assertEqual(control.parse_command("/status"), ("status", ""))
        self.assertEqual(control.parse_command("/Wlacz@mybot  derm "), ("wlacz", "derm"))
        self.assertEqual(control.parse_command("/włącz derm"), ("wlacz", "derm"))
        self.assertIsNone(control.parse_command("cześć"))
        self.assertIsNone(control.parse_command(None))

    def test_find_blocks_exact_beats_substring_and_ignores_diacritics(self):
        self.assertEqual(len(control.find_blocks(BLOCKS, "derm")), 2)
        self.assertEqual([control.label_of(w) for w in control.find_blocks(BLOCKS, "dowolny")],
                         ["Dermatolog (dowolny)"])
        self.assertEqual(len(control.find_blocks(BLOCKS, "STOMATOLOG")), 1)
        self.assertEqual(control.find_blocks(BLOCKS, ""), [])

    def test_runtime_override_wins_over_config_and_is_per_file(self):
        f1, f2 = self.ctx.runtime, make_ctx(self, name="zona").runtime
        control.set_block(BLOCKS[2], True, f1)
        self.assertTrue(control.block_enabled(BLOCKS[2], f1))
        self.assertFalse(control.block_enabled(BLOCKS[2], f2))  # drugie konto nietknięte
        control.set_block(BLOCKS[0], False, f1)
        self.assertFalse(control.block_enabled(BLOCKS[0], f1))
        control.reset(f1)
        self.assertFalse(control.block_enabled(BLOCKS[2], f1))
        self.assertTrue(control.block_enabled(BLOCKS[0], f1))


class SingleAccountMessageTests(Base):
    def test_foreign_user_is_ignored(self):
        self.assertEqual(self.say("/pauza", uid=999), [])
        self.assertFalse(control.is_paused(self.ctx.runtime))

    def test_stale_command_is_ignored(self):
        self.assertEqual(self.say("/pauza", age=lw.COMMAND_MAX_AGE + 5), [])
        self.assertFalse(control.is_paused(self.ctx.runtime))

    def test_plain_text_is_ignored(self):
        self.assertEqual(self.say("hej"), [])

    def test_enable_one_block_and_wake_the_loop(self):
        self.assertIn("WŁĄCZONY", self.say("/wlacz dowolny")[0])
        self.assertTrue(control.block_enabled(BLOCKS[2], self.ctx.runtime))
        self.assertTrue(lw.WAKE.is_set())

    def test_disable_block(self):
        self.say("/wylacz stomatolog")
        self.assertFalse(control.block_enabled(BLOCKS[0], self.ctx.runtime))

    def test_ambiguous_name_changes_nothing(self):
        self.assertIn("kilku bloków", self.say("/wlacz derm")[0])
        self.assertFalse(control.block_enabled(BLOCKS[2], self.ctx.runtime))

    def test_unknown_or_empty_name_changes_nothing(self):
        self.assertIn("Nie znalazłem", self.say("/wlacz xyz")[0])
        self.assertIn("Podaj nazwę", self.say("/wlacz")[0])

    def test_pause_stops_checks_and_resume_restores(self):
        self.say("/pauza")
        self.ctx._client = mock.Mock()
        lw.check(self.ctx, CFG)
        self.ctx._client.terms.assert_not_called()
        self.say("/wznow")
        self.assertFalse(control.is_paused(self.ctx.runtime))
        self.assertTrue(lw.WAKE.is_set())

    def test_reset_clears_pause_and_overrides(self):
        self.say("/pauza")
        self.say("/wlacz dowolny")
        self.say("/reset")
        self.assertFalse(control.is_paused(self.ctx.runtime))
        self.assertFalse(control.block_enabled(BLOCKS[2], self.ctx.runtime))

    def test_status_reports_state(self):
        self.ctx.status.update(last_ok=time.time() - 120, fails=0, last_error=None)
        out = self.say("/status")[0]
        self.assertIn("Nasłuch działa", out)
        self.assertIn("2 min temu", out)
        self.assertIn("Dermatolog (dowolny): wyłączony", out)
        self.say("/pauza")
        self.assertIn("WSTRZYMANE", self.say("/status")[0])

    def test_help_and_unknown(self):
        self.assertIn("/status", self.say("/pomoc")[0])
        self.assertIn("Nieznana komenda", self.say("/foo")[0])

    def test_terminy_sends_groups_with_buttons_even_when_paused(self):
        self.say("/pauza")
        t = {"dateTimeFrom": "2026-10-26T14:40:00", "dateTimeTo": "2026-10-26T15:20:00",
             "doctor": {"id": 1, "firstName": "MONIKA", "lastName": "HORVATH SKAPPER"}, "clinicId": 1,
             "clinic": "LX - Domaniewska 39B", "roomId": 1, "serviceId": 6621, "scheduleId": 1}
        client = mock.Mock()
        client.terms.side_effect = lambda c, n, svc, a, b: [dict(t)] if svc == 6621 else []
        self.ctx._client = client
        with mock.patch.object(lw, "notify") as n:
            lw.handle_message([self.ctx], CFG, msg("/terminy"))
        self.assertEqual(n.call_count, 1)
        self.assertEqual(len(n.call_args.args[2]), 1)  # przycisk Zarezerwuj

    def test_menu_commands_are_telegram_valid(self):
        for cmd, desc in control.COMMANDS:
            self.assertRegex(cmd, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(desc) <= 256)


class TwoAccountsMessageTests(Base):
    def setUp(self):
        super().setUp()
        self.marcin = make_ctx(self, name="marcin", multi=True, blocks=BLOCKS)
        self.zona = make_ctx(self, name="zona", multi=True, blocks=ZONA_BLOCKS)
        self.both = [self.marcin, self.zona]

    def test_pause_without_account_pauses_all_and_with_account_only_that_one(self):
        self.say("/pauza zona", self.both)
        self.assertTrue(control.is_paused(self.zona.runtime))
        self.assertFalse(control.is_paused(self.marcin.runtime))
        self.say("/wznow", self.both)
        self.say("/pauza", self.both)
        self.assertTrue(control.is_paused(self.zona.runtime) and control.is_paused(self.marcin.runtime))
        self.say("/reset marcin", self.both)
        self.assertFalse(control.is_paused(self.marcin.runtime))
        self.assertTrue(control.is_paused(self.zona.runtime))

    def test_unknown_account_in_pause_is_rejected(self):
        self.assertIn("Nieznane konto", self.say("/pauza babcia", self.both)[0])
        self.assertFalse(control.is_paused(self.marcin.runtime) or control.is_paused(self.zona.runtime))

    def test_same_block_name_in_both_accounts_needs_the_account_prefix(self):
        out = self.say("/wylacz konsultacja", self.both)[0]
        self.assertIn("kilku bloków", out)
        self.assertIn("[marcin]", out)
        self.assertIn("[zona]", out)
        self.assertTrue(control.block_enabled(ZONA_BLOCKS[0], self.zona.runtime))  # nic nie zmieniono
        self.assertIn("[zona]", self.say("/wylacz zona konsultacja", self.both)[0])
        self.assertFalse(control.block_enabled(ZONA_BLOCKS[0], self.zona.runtime))
        self.assertTrue(control.block_enabled(BLOCKS[1], self.marcin.runtime))  # Twoje konto nietknięte

    def test_unique_block_is_found_across_accounts_without_prefix(self):
        self.assertIn("WŁĄCZONY", self.say("/wlacz dowolny", self.both)[0])
        self.assertTrue(control.block_enabled(BLOCKS[2], self.marcin.runtime))

    def test_status_and_blocks_cover_both_accounts(self):
        status = self.say("/status", self.both)[0]
        self.assertIn("Aktywne konta: marcin, zona", status)
        self.assertIn("[marcin]", status)
        self.assertIn("[zona]", status)
        blocks = self.say("/bloki", self.both)[0]
        self.assertIn("[marcin]", blocks)
        self.assertIn("[zona]", blocks)

    def test_one_account_failing_does_not_stop_the_other(self):
        broken, healthy = mock.Mock(), mock.Mock()
        broken.terms.side_effect = lw.requests.ConnectionError("x")
        healthy.terms.return_value = []
        self.marcin._client, self.zona._client = broken, healthy
        with mock.patch.object(lw, "notify"):
            for _ in range(3):
                for c in self.both:
                    lw.run_check(c, CFG)
        self.assertEqual(self.marcin.status["fails"], 3)
        self.assertTrue(self.marcin.alerted)
        self.assertEqual(self.zona.status["fails"], 0)
        self.assertFalse(self.zona.alerted)

    def test_problem_alert_is_tagged_with_the_failing_account_and_recovery_clears_it(self):
        bad = mock.Mock()
        bad.terms.side_effect = lw.requests.ConnectionError("x")
        self.zona._client = bad
        with mock.patch.object(lw, "notify") as n:
            for _ in range(3):
                lw.run_check(self.zona, CFG)
            self.assertEqual(n.call_args.kwargs["tag"], "[zona] ")
            self.assertIn("PROBLEM", n.call_args.args[0])
            bad.terms.side_effect = None
            bad.terms.return_value = []
            lw.run_check(self.zona, CFG)
        self.assertIn("działa ponownie", n.call_args.args[0])
        self.assertFalse(self.zona.alerted)

    def test_terminy_per_account_uses_that_accounts_client_and_tag(self):
        t = {"dateTimeFrom": "2026-10-26T14:40:00", "dateTimeTo": "2026-10-26T15:20:00",
             "doctor": {"id": 9, "firstName": "ADRIAN", "lastName": "SZYMAŃSKI"}, "clinicId": 1,
             "clinic": "LX - Domaniewska 39B", "roomId": 1, "serviceId": 4448, "scheduleId": 1}
        zc, mc = mock.Mock(), mock.Mock()
        zc.terms.return_value = [dict(t)]
        mc.terms.return_value = []
        self.zona._client, self.marcin._client = zc, mc
        with mock.patch.object(lw, "notify") as n:
            lw.handle_message(self.both, CFG, msg("/terminy zona"))
        self.assertEqual(n.call_count, 1)
        self.assertEqual(n.call_args.kwargs["tag"], "[zona] ")
        mc.terms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
