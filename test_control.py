"""Testy komend Telegrama i sterowania w trakcie działania (bez sieci).

Uruchom: python -m unittest test_control -v
"""
import time
import unittest
from pathlib import Path
from unittest import mock

import control
import luxmed_watch as lw

CFG = {
    "interval_minutes": 5,
    "booking": {"enabled": True},
    "watch": [
        {"label": "Stomatolog", "city_id": 1, "service_id": 6621, "doctors": ["MONIKA HORVATH SKAPPER"]},
        {"label": "Konsultacja dermatologiczna", "city_id": 1, "service_id": 4448, "doctors": ["ADRIAN SZYMAŃSKI"]},
        {"label": "Dermatolog (dowolny)", "enabled": False, "city_id": 1, "service_id": 4448},
    ],
}


def msg(text, uid=42, age=0):
    return {"from": {"id": uid}, "text": text, "date": time.time() - age}


class Base(unittest.TestCase):
    def setUp(self):
        lw.TG.update({"bot_token": "T", "chat_id": "42"})
        patches = [mock.patch.object(control, "RUNTIME_FILE", Path(self._testMethodName + ".runtime.json")),
                   mock.patch.object(lw, "PENDING_FILE", Path(self._testMethodName + ".pending.json")),
                   mock.patch.object(lw, "STATE_FILE", Path(self._testMethodName + ".state.json"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for f in (control.RUNTIME_FILE, lw.PENDING_FILE, lw.STATE_FILE):
            self.addCleanup(lambda f=f: f.unlink(missing_ok=True))
        lw.WAKE.clear()

    def say(self, text, **kw):
        with mock.patch.object(lw, "tg_api") as tg:
            lw.handle_message(object(), CFG, msg(text, **kw))
        return [c.kwargs.get("text", "") for c in tg.call_args_list if c.args[0] == "sendMessage"]


class ControlTests(Base):
    def test_parse_command(self):
        self.assertEqual(control.parse_command("/status"), ("status", ""))
        self.assertEqual(control.parse_command("/Wlacz@mybot  derm "), ("wlacz", "derm"))
        self.assertEqual(control.parse_command("/włącz derm"), ("wlacz", "derm"))
        self.assertIsNone(control.parse_command("cześć"))
        self.assertIsNone(control.parse_command(None))

    def test_find_blocks_exact_beats_substring_and_ignores_diacritics(self):
        self.assertEqual(len(control.find_blocks(CFG, "derm")), 2)
        self.assertEqual([control.label_of(w) for w in control.find_blocks(CFG, "dowolny")],
                         ["Dermatolog (dowolny)"])
        self.assertEqual(len(control.find_blocks(CFG, "STOMATOLOG")), 1)
        self.assertEqual(control.find_blocks(CFG, ""), [])

    def test_runtime_override_wins_over_config(self):
        derm_all = CFG["watch"][2]
        self.assertFalse(control.block_enabled(derm_all))
        control.set_block(derm_all, True)
        self.assertTrue(control.block_enabled(derm_all))
        control.set_block(CFG["watch"][0], False)
        self.assertFalse(control.block_enabled(CFG["watch"][0]))
        control.reset()
        self.assertFalse(control.block_enabled(derm_all))
        self.assertTrue(control.block_enabled(CFG["watch"][0]))


class MessageTests(Base):
    def test_foreign_user_is_ignored(self):
        self.assertEqual(self.say("/pauza", uid=999), [])
        self.assertFalse(control.is_paused())

    def test_stale_command_is_ignored(self):
        self.assertEqual(self.say("/pauza", age=lw.COMMAND_MAX_AGE + 5), [])
        self.assertFalse(control.is_paused())

    def test_plain_text_is_ignored(self):
        self.assertEqual(self.say("hej"), [])

    def test_enable_one_block_and_wake_the_loop(self):
        out = self.say("/wlacz dowolny")
        self.assertIn("WŁĄCZONY", out[0])
        self.assertTrue(control.block_enabled(CFG["watch"][2]))
        self.assertTrue(lw.WAKE.is_set())

    def test_disable_block(self):
        self.say("/wylacz stomatolog")
        self.assertFalse(control.block_enabled(CFG["watch"][0]))

    def test_ambiguous_name_changes_nothing(self):
        out = self.say("/wlacz derm")
        self.assertIn("kilku bloków", out[0])
        self.assertFalse(control.block_enabled(CFG["watch"][2]))

    def test_unknown_or_empty_name_changes_nothing(self):
        self.assertIn("Nie znalazłem", self.say("/wlacz xyz")[0])
        self.assertIn("Podaj nazwę", self.say("/wlacz")[0])

    def test_pause_stops_checks_and_resume_restores(self):
        self.say("/pauza")
        client = mock.Mock()
        lw.check(client, CFG, set())
        client.terms.assert_not_called()
        self.say("/wznow")
        self.assertFalse(control.is_paused())
        self.assertTrue(lw.WAKE.is_set())

    def test_reset_clears_pause_and_overrides(self):
        self.say("/pauza")
        self.say("/wlacz dowolny")
        self.say("/reset")
        self.assertFalse(control.is_paused())
        self.assertFalse(control.block_enabled(CFG["watch"][2]))

    def test_status_reports_state(self):
        lw.STATUS.update(last_ok=time.time() - 120, fails=0, last_error=None)
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
        with mock.patch.object(lw, "notify") as n:
            lw.handle_message(client, CFG, msg("/terminy"))
        self.assertEqual(n.call_count, 1)
        self.assertEqual(len(n.call_args.args[2]), 1)  # przycisk Zarezerwuj

    def test_menu_commands_are_telegram_valid(self):
        for cmd, desc in control.COMMANDS:
            self.assertRegex(cmd, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(1 <= len(desc) <= 256)


if __name__ == "__main__":
    unittest.main()
