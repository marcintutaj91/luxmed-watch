"""Testy wyboru konta (Twoje / żony). Uruchom: python -m unittest test_accounts -v"""
import time
import unittest
from pathlib import Path
from unittest import mock

import accounts
import luxmed_watch as lw
from testutil import make_ctx

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

    def test_all_and_lists_select_several_accounts_in_config_order(self):
        for wanted in ("wszystkie", "ALL", "*", "zona,marcin", ["zona", "marcin"], "marcin; zona"):
            with self.subTest(wanted=wanted):
                self.assertEqual(accounts.selected_names(CFG, cli=wanted, environ={}), ["marcin", "zona"])
        self.assertEqual(accounts.selected_names(CFG, cli="zona", environ={}), ["zona"])

    def test_config_variable_can_be_all_or_a_list(self):
        self.assertEqual(accounts.selected_names(dict(CFG, active_account="all"), environ={}), ["marcin", "zona"])
        self.assertEqual(accounts.selected_names(dict(CFG, active_account=["zona"]), environ={}), ["zona"])

    def test_selected_file_may_store_all(self):
        accounts.set_selected("all")
        self.assertEqual(accounts.selected_names(CFG, environ={}), ["marcin", "zona"])

    def test_single_account_commands_refuse_several_accounts(self):
        with self.assertRaisesRegex(accounts.AccountError, "jednym koncie"):
            accounts.selected_name(CFG, cli="all", environ={})
        self.assertEqual(accounts.selected_name(CFG, cli="zona", environ={}), "zona")

    def test_unknown_name_in_a_list_is_an_error(self):
        with self.assertRaisesRegex(accounts.AccountError, "babcia"):
            accounts.selected_names(CFG, cli="marcin,babcia", environ={})

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
        self.marcin = make_ctx(self, name="marcin", multi=True, blocks=[{"label": "A", "service_id": 1}])
        self.zona = make_ctx(self, name="zona", multi=True, booking=False, blocks=[{"label": "B", "service_id": 2}])

    def say(self, text, ctxs=None):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.threading, "Timer") as timer:
            lw.handle_message(ctxs or [self.marcin], CFG, msg(text))
        self.timer = timer
        return [c.kwargs.get("text", "") for c in tg.call_args_list if c.args[0] == "sendMessage"]

    def test_konto_without_arg_shows_active_and_options(self):
        out = self.say("/konto")[0]
        self.assertIn("Aktywne konta: marcin", out)
        self.assertIn("zona", out)

    def test_switching_to_one_account_saves_selection_and_schedules_restart(self):
        out = self.say("/konto zona")[0]
        self.assertIn("Przełączam na: zona", out)
        self.assertEqual(accounts.read_selected(), "zona")
        self.timer.assert_called_once()
        self.assertIs(self.timer.call_args.args[1], lw.os._exit)

    def test_switching_to_all_accounts(self):
        self.say("/konto wszystkie")
        self.assertEqual(accounts.read_selected(), "all")
        self.timer.assert_called_once()

    def test_already_active_set_or_unknown_account_does_not_restart(self):
        self.assertIn("Aktywne już są", self.say("/konto marcin")[0])
        self.assertIn("Aktywne już są", self.say("/konto wszystkie", [self.marcin, self.zona])[0])
        self.assertIn("Nie przełączam", self.say("/konto babcia")[0])
        self.timer.assert_not_called()
        self.assertIsNone(accounts.read_selected())

    def test_no_switch_to_account_without_credentials(self):
        cfg = dict(CFG, accounts={"marcin": CFG["accounts"]["marcin"], "zona": {"login": "", "password": ""}})
        with mock.patch.object(lw, "tg_api") as tg:
            lw.handle_message([self.marcin], cfg, msg("/konto zona"))
        self.assertIn("Nie przełączam", tg.call_args.kwargs["text"])
        self.assertIsNone(accounts.read_selected())

    def test_notifications_carry_account_tag_only_with_multiple_accounts(self):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.subprocess, "run"):
            lw.notify("Lux Med: Derm", "treść", tag=self.zona.tag)
            self.assertTrue(tg.call_args.kwargs["text"].startswith("[zona] Lux Med: Derm"))
            single = make_ctx(self, name="solo")
            lw.notify("Lux Med: Derm", "treść", tag=single.tag)
            self.assertTrue(tg.call_args.kwargs["text"].startswith("Lux Med: Derm"))

    def test_booking_buttons_follow_account_setting(self):
        cfg = {"booking": {"enabled": True}}
        self.assertTrue(lw.can_book_now(cfg, self.marcin))
        self.assertFalse(lw.can_book_now(cfg, self.zona))  # booking = false na koncie

    def test_click_on_other_accounts_offer_uses_that_accounts_client(self):
        mc, zc = object(), object()
        self.marcin._client, self.zona._client = mc, zc
        self.zona.booking = True
        data = lw.add_pending(self.zona, "k", {"dateTimeFrom": "2026-10-26T14:40:00", "dateTimeTo": "2026-10-26T15:20:00"},
                              "ADRIAN SZYMAŃSKI", "Derm")
        cb = {"id": "c", "from": {"id": 42}, "data": data, "message": {"chat": {"id": 42}, "message_id": 1}}
        with mock.patch.object(lw, "tg_api"), mock.patch.object(lw.booking, "book") as bk:
            lw.handle_callback([self.marcin, self.zona], dict(CFG, booking={"enabled": True}), cb)
        self.assertIs(bk.call_args.args[0], zc)  # nigdy klient Twojego konta
        self.assertEqual(lw.load_pending(self.marcin.files["pending"]), {})

    def test_offer_of_inactive_account_is_not_booked_on_the_wrong_one(self):
        data = lw.add_pending(self.zona, "k", {"dateTimeFrom": "2026-10-26T14:40:00"}, "A B", "Derm")
        cb = {"id": "c", "from": {"id": 42}, "data": data, "message": {"chat": {"id": 42}, "message_id": 1}}
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.booking, "book") as bk:
            lw.handle_callback([self.marcin], CFG, cb)  # aktywne tylko marcin
        bk.assert_not_called()
        self.assertIn("nie jest teraz aktywne", tg.call_args.kwargs["text"])

    def test_account_with_booking_off_refuses_even_a_leftover_button(self):
        data = lw.add_pending(self.zona, "k", {"dateTimeFrom": "2026-10-26T14:40:00"}, "A B", "Derm")
        cb = {"id": "c", "from": {"id": 42}, "data": data, "message": {"chat": {"id": 42}, "message_id": 1}}
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(lw.booking, "book") as bk:
            lw.handle_callback([self.marcin, self.zona], {"booking": {"enabled": True}}, cb)
        bk.assert_not_called()
        self.assertIn("wyłączona dla konta zona", tg.call_args.kwargs["text"])
        self.assertEqual(len(lw.load_pending(self.zona.files["pending"])), 1)  # oferta nadal czeka


if __name__ == "__main__":
    unittest.main()
