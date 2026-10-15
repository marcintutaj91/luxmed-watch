"""Testy filtrów godzin/dni, grupowania i formatowania powiadomień (bez sieci).

Uruchom: python -m unittest test_rules -v
"""
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import luxmed_watch as lw
import rules


def term(dt, doc_id=1, first="MONIKA", last="HORVATH SKAPPER", clinic="LX Stomatologia Warszawa - Modlińska 58"):
    return {"dateTimeFrom": dt, "dateTimeTo": dt, "doctor": {"id": doc_id, "firstName": first, "lastName": last},
            "clinicId": 1, "clinic": clinic, "roomId": 1, "serviceId": 6621, "scheduleId": 1}


class FilterTests(unittest.TestCase):
    def test_no_filter_allows_everything(self):
        f = rules.parse_filter({"label": "x"})
        self.assertTrue(rules.allowed(f, datetime(2026, 10, 15, 3, 0)))

    def test_hours_are_inclusive(self):
        f = rules.parse_filter({"hours_from": "16:00", "hours_to": "20:00"})
        ok = lambda h, m: rules.allowed(f, datetime(2026, 10, 15, h, m))
        self.assertFalse(ok(15, 59))
        self.assertTrue(ok(16, 0))
        self.assertTrue(ok(20, 0))
        self.assertFalse(ok(20, 1))

    def test_only_from_or_only_to(self):
        self.assertFalse(rules.allowed(rules.parse_filter({"hours_from": "16:00"}), datetime(2026, 10, 15, 9, 0)))
        self.assertFalse(rules.allowed(rules.parse_filter({"hours_to": "12:00"}), datetime(2026, 10, 15, 13, 0)))

    def test_days_with_aliases(self):
        f = rules.parse_filter({"days": ["pon", "Śr", "piątek"]})
        mon, tue, wed, fri = (datetime(2026, 10, d, 12, 0) for d in (12, 13, 14, 16))
        self.assertTrue(rules.allowed(f, mon))
        self.assertFalse(rules.allowed(f, tue))
        self.assertTrue(rules.allowed(f, wed))
        self.assertTrue(rules.allowed(f, fri))

    def test_bad_config_gives_clear_errors(self):
        with self.assertRaisesRegex(rules.ConfigError, "GG:MM"):
            rules.parse_filter({"label": "Derm", "hours_from": "4pm"})
        with self.assertRaisesRegex(rules.ConfigError, "nieznany dzień"):
            rules.parse_filter({"label": "Derm", "days": ["pon", "xyz"]})


class FormatTests(unittest.TestCase):
    def test_polish_plural(self):
        self.assertEqual([rules._plural(n) for n in (1, 2, 4, 5, 12, 22, 25)],
                         ["nowy termin", "nowe terminy", "nowe terminy", "nowych terminów",
                          "nowych terminów", "nowe terminy", "nowych terminów"])

    def test_group_is_sorted_and_buttons_match_lines(self):
        ts = [term("2026-10-16T09:00:00"), term("2026-10-15T17:20:00")]
        body, buttons = rules.format_group("MONIKA HORVATH SKAPPER", ts, today=date(2026, 10, 15))
        lines = body.split("\n")
        self.assertEqual(lines[0], "Monika Horvath Skapper: 2 nowe terminy")
        self.assertIn("czw 15.10, 17:20", lines[1])
        self.assertIn("DZIŚ", lines[1])
        self.assertIn("jutro", lines[2])
        self.assertIn("Modlińska 58", lines[1])
        self.assertEqual(len(buttons), 2)
        self.assertTrue(buttons[0].startswith("Zarezerwuj czw 15.10, 17:20"))

    def test_button_fits_telegram_callback_limit(self):
        _, buttons = rules.format_group("A B", [term("2026-10-15T17:20:00")], today=date(2026, 10, 15))
        self.assertLess(len(buttons[0]), 64)


class FakeClient:
    def __init__(self, terms_by_service):
        self.by_service = terms_by_service

    def terms(self, city_id, city_name, service_id, d_from, d_to):
        return [dict(t) for t in self.by_service[service_id]]


class CheckTests(unittest.TestCase):
    def setUp(self):
        lw.TG.update({"bot_token": "T", "chat_id": "42"})
        p = mock.patch.object(lw, "STATE_FILE", Path(self._testMethodName + ".state.json"))
        q = mock.patch.object(lw, "PENDING_FILE", Path(self._testMethodName + ".pending.json"))
        p.start(), q.start()
        self.addCleanup(p.stop), self.addCleanup(q.stop)
        for f in (lw.STATE_FILE, lw.PENDING_FILE):
            self.addCleanup(lambda f=f: f.unlink(missing_ok=True))

    def cfg(self, **watch):
        return {"booking": {"enabled": True},
                "watch": [dict({"label": "Stomatolog", "city_id": 1, "service_id": 6621}, **watch)]}

    def run_check(self, cfg, terms, seen=None):
        seen = set() if seen is None else seen
        with mock.patch.object(lw, "notify") as n:
            lw.check(FakeClient({6621: terms}), cfg, seen)
        return n, seen

    def test_one_message_per_doctor_with_button_per_term(self):
        terms = [term("2026-10-27T14:40:00"), term("2026-10-26T14:40:00"),
                 term("2026-10-28T15:20:00", doc_id=2, first="ADRIAN", last="SZYMAŃSKI")]
        n, seen = self.run_check(self.cfg(), terms)
        self.assertEqual(n.call_count, 2)  # dwóch lekarzy -> dwie wiadomości
        monika = next(c for c in n.call_args_list if "Monika" in c.args[1])
        self.assertEqual(len(monika.args[2]), 2)  # przycisk przy każdym terminie
        self.assertEqual(len(seen), 3)

    def test_already_seen_terms_are_not_repeated(self):
        terms = [term("2026-10-26T14:40:00")]
        _, seen = self.run_check(self.cfg(), terms)
        n, _ = self.run_check(self.cfg(), terms + [term("2026-10-27T14:40:00")], seen)
        self.assertEqual(n.call_count, 1)
        self.assertIn("1 nowy termin", n.call_args.args[1])

    def test_hour_filter_drops_terms_and_does_not_mark_them_seen(self):
        terms = [term("2026-10-26T09:00:00"), term("2026-10-26T17:20:00")]
        n, seen = self.run_check(self.cfg(hours_from="16:00"), terms)
        self.assertEqual(n.call_count, 1)
        self.assertIn("17:20", n.call_args.args[1])
        self.assertNotIn("09:00", n.call_args.args[1])
        self.assertEqual(len(seen), 1)

    def test_nothing_new_means_no_message(self):
        n, _ = self.run_check(self.cfg(days=["sob"]), [term("2026-10-26T09:00:00")])  # poniedziałek
        n.assert_not_called()

    def test_no_buttons_when_booking_disabled(self):
        cfg = self.cfg()
        cfg["booking"]["enabled"] = False
        n, _ = self.run_check(cfg, [term("2026-10-26T14:40:00")])
        self.assertIsNone(n.call_args.args[2])

    def test_cap_per_message_leaves_rest_for_next_round(self):
        terms = [term(f"2026-10-{d:02d}T10:00:00") for d in range(10, 25)]  # 15 terminów
        n, seen = self.run_check(self.cfg(), terms)
        self.assertEqual(len(n.call_args.args[2]), lw.MAX_PER_MESSAGE)
        self.assertEqual(len(seen), lw.MAX_PER_MESSAGE)
        n2, _ = self.run_check(self.cfg(), terms, seen)
        self.assertEqual(n2.call_count, 1)  # reszta w następnej rundzie


if __name__ == "__main__":
    unittest.main()
