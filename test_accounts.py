"""Testy wyboru konta (Twoje / żony). Uruchom: python -m unittest test_accounts -v"""
import time
import unittest
from pathlib import Path
from unittest import mock

import accounts
import control
import luxmed_watch as lw

CFG = {
    "active_account": "marcin",
    "accounts": {"marcin": {"login": "m", "password": "p1"},
                 "zona": {"login": "z", "password": "p2", "booking": False}},
    "watch": [
        {"label": "Wspólny", "service_id": 1},
        {"label": "Stomatolog", "account": "marcin", "service_id": 6621},
        {"label": "Derm żony", "account": "zona", "service_id": 4448},
        {"label": "Oboje", "account": ["marcin", "zona"], "service_id": 2},
    ],
}


class Tmp(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(accounts, "SELECTED_FILE", Path(self._testMethodName + ".selected.json"))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: accounts.SELECTED_FILE.unlink(missing_ok=True))


class SelectionTests(Tmp):
    def test_config_variable_is_used(self):
        self.assertEqual(accounts.selected_name(CFG, environ={}), "marcin")

    def test_precedence_cli_env_selected_config(self):
        accounts.set_selected("zona")
        self.assertEqual(accounts.selected_name(CFG, environ={}), "zona")            # plik z /konto > config
        self.assertEqual(accounts.selected_name(CFG, environ={"LUXMED_ACCOUNT": "marcin"}), "marcin")  # env > plik
        self.assertEqual(accounts.selected_name(CFG, cli="zona", environ={"LUXMED_ACCOUNT": "marcin"}), "zona")

    def test_match_is_case_insensitive_and_unknown_is_error(self):
        self.assertEqual(accounts.selected_name(CFG, cli="ZONA", environ={}), "zona")
        with self.assertRaisesRegex(accounts.AccountError, "Dostępne: marcin, zona"):
            accounts.selected_name(CFG, cli="babcia", environ={})

    def test_several_accounts_without_choice_is_error(self):
        cfg = {k: v for k, v in CFG.items() if k != "active_account"}
        with self.assertRaisesRegex(accounts.AccountError, "active_account"):
            accounts.selected_name(cfg, environ={})

    def test_single_account_needs_no_choice(self):
        self.assertEqual(accounts.selected_name({"accounts": {"jedyne": {"login": "a", "password": "b"}}}, environ={}), "jedyne")

    def test_legacy_auth_is_account_default_with_legacy_files(self):
        cfg = {"auth": {"login": "a", "password": "b"}}
        self.assertEqual(accounts.selected_name(cfg, environ={}), "default")
        self.assertEqual(accounts.paths("default")["state"].name, "state.json")
        self.assertEqual(accounts.paths("zona")["state"].name, "state-zona.json")

    def test_no_accounts_is_error(self):
        with self.assertRaisesRegex(accounts.AccountError, "Brak kont"):
            accounts.selected_name({}, environ={})

    def test_bad_account_names_are_rejected(self):
        with self.assertRaises(accounts.AccountError):
            accounts.list_accounts({"accounts": {"../zona": {"login": "a", "password": "b"}}})
        with self.assertRaises(accounts.AccountError):
            accounts.paths("../x")


class CredentialsAndBlocksTests(Tmp):
    def test_credentials_per_account(self):
        self.assertEqual(accounts.credentials(CFG, "zona"), ("z", "p2"))

    def test_missing_credentials_name_the_section(self):
        cfg = {"accounts": {"zona": {"login": "", "password": ""}}}
        with self.assertRaisesRegex(accounts.AccountError, r"\[accounts.zona\]"):
            accounts.credentials(cfg, "zona")

    def test_blocks_are_filtered_per_account(self):
        labels = lambda name: [w["label"] for w in accounts.blocks_for(CFG, name)]
        self.assertEqual(labels("marcin"), ["Wspólny", "Stomatolog", "Oboje"])
        self.assertEqual(labels("zona"), ["Wspólny", "Derm żony", "Oboje"])

    def test_typo_in_block_account_is_an_error_not_silence(self):
        cfg = dict(CFG, watch=[{"label": "X", "account": "zonaa", "service_id": 1}])
        with self.assertRaisesRegex(accounts.AccountError, "zonaa"):
            accounts.blocks_for(cfg, "marcin")

    def test_booking_flag_per_account(self):
        self.assertTrue(accounts.booking_allowed(CFG, "marcin"))
        self.assertFalse(accounts.booking_allowed(CFG, "zona"))

    def test_accounts_never_share_state_files(self):
        a, b = accounts.paths("marcin"), accounts.paths("zona")
        for key in ("state", "pending", "runtime"):
            self.assertNotEqual(a[key], b[key])


def msg(text):
    return {"from": {"id": 42}, "text": text, "date": time.time()}


class TelegramTests(Tmp):
    def setUp(self):
        super().setUp()
        lw.TG.update({"bot_token": "T", "chat_id": "42"})
        p = mock.patch.dict(lw.ACCOUNT, {"name": "marcin", "multi": True, "booking": True})
        p.start()
        self.addCleanup(p.stop)

    def say(self, text):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.threading, "Timer") as timer:
            lw.handle_message(object(), CFG, msg(text))
        self.timer = timer
        return [c.kwargs.get("text", "") for c in tg.call_args_list if c.args[0] == "sendMessage"]

    def test_konto_without_arg_shows_current_and_options(self):
        out = self.say("/konto")[0]
        self.assertIn("Aktywne konto: marcin", out)
        self.assertIn("zona", out)

    def test_switching_saves_selection_and_schedules_restart(self):
        out = self.say("/konto zona")[0]
        self.assertIn("Przełączam na konto zona", out)
        self.assertEqual(accounts.read_selected(), "zona")
        self.timer.assert_called_once()
        self.assertIs(self.timer.call_args.args[1], lw.os._exit)

    def test_same_or_unknown_account_does_not_restart(self):
        self.assertIn("już aktywne", self.say("/konto marcin")[0])
        self.assertIn("Nieznane konto", self.say("/konto babcia")[0])
        self.timer.assert_not_called()
        self.assertIsNone(accounts.read_selected())

    def test_no_switch_to_account_without_credentials(self):
        cfg = dict(CFG, accounts={"marcin": CFG["accounts"]["marcin"], "zona": {"login": "", "password": ""}})
        with mock.patch.object(lw, "tg_api") as tg:
            lw.handle_message(object(), cfg, msg("/konto zona"))
        self.assertIn("Nie przełączam", tg.call_args.kwargs["text"])
        self.assertIsNone(accounts.read_selected())

    def test_notifications_carry_account_tag_only_with_multiple_accounts(self):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.subprocess, "run"):
            lw.notify("Lux Med: Derm", "treść")
            self.assertTrue(tg.call_args.kwargs["text"].startswith("[marcin] Lux Med: Derm"))
            with mock.patch.dict(lw.ACCOUNT, {"multi": False}):
                lw.notify("Lux Med: Derm", "treść")
            self.assertTrue(tg.call_args.kwargs["text"].startswith("Lux Med: Derm"))

    def test_booking_buttons_follow_account_setting(self):
        cfg = {"booking": {"enabled": True}}
        self.assertTrue(lw.can_book_now(cfg))
        with mock.patch.dict(lw.ACCOUNT, {"booking": False}):
            self.assertFalse(lw.can_book_now(cfg))


if __name__ == "__main__":
    unittest.main()
