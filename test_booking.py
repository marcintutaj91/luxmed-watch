"""Testy logiki rezerwacji na atrapie klienta (bez sieci, bez prawdziwych rezerwacji).

Uruchom: python -m unittest test_booking -v
"""
import json
import threading
import unittest
from unittest import mock

import booking
import luxmed_watch as lw

TERM = {
    "dateTimeFrom": "2026-10-15T17:20:00", "dateTimeTo": "2026-10-15T18:00:00",
    "doctor": {"id": 17277, "firstName": "MONIKA", "lastName": "HORVATH SKAPPER"},
    "clinicId": 2548, "clinic": "LX Stomatologia Warszawa - Modlińska 58",
    "roomId": 13883, "serviceId": 6621, "scheduleId": 12430304,
    "isImpediment": False, "impedimentText": "", "isAdditional": False, "isTelemedicine": False,
    "_additional": {"isPreparationRequired": True, "preparationItems": [{"header": None, "text": "x"}]},
}
VAL = {"payerId": 760, "contractId": 40306, "price": 0.0, "isReferralRequired": False,
       "requireReferralForPP": False, "valuationType": 1}


def lock_ok(**over):
    value = {"temporaryReservationId": 555, "valuations": [dict(VAL)], "conflictedVisit": None,
             "askForReferral": False}
    value.update(over)
    return {"value": value, "errors": [], "hasErrors": False}


class FakeClient:
    def __init__(self, lock_resp, confirm_resp=None):
        self.lock = threading.RLock()
        self.calls = []
        self.responses = {"lockterm": lock_resp,
                          "confirm": confirm_resp or {"value": {"ok": True}, "errors": [], "hasErrors": False},
                          "releaseterm": {}}

    def post(self, path, body, params=None):
        self.calls.append((path.rsplit("/", 1)[-1], body, params))
        return self.responses[path.rsplit("/", 1)[-1]]

    @property
    def steps(self):
        return [c[0] for c in self.calls]


class BookTests(unittest.TestCase):
    def test_happy_path_lock_then_confirm(self):
        c = FakeClient(lock_ok())
        booking.book(c, TERM)
        self.assertEqual(c.steps, ["lockterm", "confirm"])
        confirm = c.calls[1][1]
        self.assertEqual(confirm["temporaryReservationId"], 555)
        self.assertEqual(confirm["valuation"], VAL)
        self.assertEqual(confirm["timeFrom"], "17:20")

    def test_lock_body_fields(self):
        b = booking.lock_body(TERM)
        self.assertEqual((b["doctorId"], b["facilityId"], b["roomId"], b["scheduleId"], b["serviceVariantId"]),
                         (17277, 2548, 13883, 12430304, 6621))
        self.assertEqual((b["timeFrom"], b["timeTo"]), ("17:20", "18:00"))
        self.assertTrue(b["isPreparationRequired"])
        # 17:20 czasu warszawskiego w październiku (CEST) = 15:20 UTC
        self.assertEqual(b["date"], "2026-10-15T15:20:00.000Z")

    def test_date_uses_winter_time_after_dst_ends(self):
        t = dict(TERM, dateTimeFrom="2026-11-02T18:40:00", dateTimeTo="2026-11-02T19:20:00")
        self.assertEqual(booking.lock_body(t)["date"], "2026-11-02T17:40:00.000Z")  # CET = UTC+1

    def test_dry_run_locks_and_releases_without_confirm(self):
        c = FakeClient(lock_ok())
        out = booking.book(c, TERM, dry_run=True)
        self.assertEqual(c.steps, ["lockterm", "releaseterm"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(c.calls[1][2], {"reservationId": 555})

    def test_lock_error_means_term_taken_and_nothing_else_is_sent(self):
        c = FakeClient({"value": None, "errors": [{"message": "Termin zajęty"}], "hasErrors": True})
        with self.assertRaisesRegex(booking.BookingError, "Termin zajęty"):
            booking.book(c, TERM)
        self.assertEqual(c.steps, ["lockterm"])

    def test_guardrails_release_and_never_confirm(self):
        cases = {
            "conflict": lock_ok(conflictedVisit={"id": 1}),
            "askReferral": lock_ok(askForReferral=True),
            "referral": lock_ok(valuations=[dict(VAL, isReferralRequired=True)]),
            "paid": lock_ok(valuations=[dict(VAL, price=150.0)]),
            "noValuation": lock_ok(valuations=[]),
        }
        for name, resp in cases.items():
            with self.subTest(name):
                c = FakeClient(resp)
                with self.assertRaises(booking.BookingError):
                    booking.book(c, TERM)
                self.assertEqual(c.steps, ["lockterm", "releaseterm"])

    def test_confirm_error_releases_lock(self):
        c = FakeClient(lock_ok(), {"errors": [{"message": "Limit wizyt"}], "hasErrors": True})
        with self.assertRaisesRegex(booking.BookingError, "Limit wizyt"):
            booking.book(c, TERM)
        self.assertEqual(c.steps, ["lockterm", "confirm", "releaseterm"])


class ClientPostTests(unittest.TestCase):
    def _client(self, text, status=200):
        c = lw.Luxmed.__new__(lw.Luxmed)
        c.lock = threading.RLock()
        resp = mock.Mock(status_code=status, text=text, headers={"Content-Type": "application/json"})
        resp.request.method, resp.url = "POST", "x/PatientPortal/y"
        resp.json.side_effect = lambda: json.loads(text)
        resp.raise_for_status = lambda: None
        c.get = lambda *a, **k: {"token": "t"}
        c.s = mock.Mock(post=mock.Mock(return_value=resp))
        return c

    def test_empty_200_body_is_success_for_post(self):
        self.assertEqual(self._client("").post("/p", {}), {})

    def test_json_body_still_parsed(self):
        self.assertEqual(self._client('{"a": 1}').post("/p", {}), {"a": 1})

    def test_garbage_body_still_raises(self):
        with self.assertRaises(RuntimeError):
            self._client("<html>").post("/p", {})


class CallbackTests(unittest.TestCase):
    def setUp(self):
        lw.TG.update({"bot_token": "T", "chat_id": "42"})
        self.tmp = mock.patch.object(lw, "PENDING_FILE", lw.Path(self._testMethodName + ".pending.json"))
        self.tmp.start()
        self.addCleanup(self.tmp.stop)
        self.addCleanup(lambda: lw.PENDING_FILE.unlink(missing_ok=True))
        self.book_id = lw.add_pending("k1", TERM, "MONIKA HORVATH SKAPPER", "Stomatolog")

    def cb(self, uid=42, data=None):
        return {"id": "c1", "from": {"id": uid}, "data": data or f"book:{self.book_id}",
                "message": {"chat": {"id": 42}, "message_id": 7}}

    def test_foreign_user_cannot_book(self):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(booking, "book") as bk:
            lw.handle_callback(object(), self.cb(uid=999))
        bk.assert_not_called()
        tg.assert_not_called()
        self.assertIn(self.book_id, lw.load_pending())  # oferta nietknięta

    def test_click_books_once_and_second_click_is_expired(self):
        with mock.patch.object(lw, "tg_api") as tg, mock.patch.object(booking, "book") as bk:
            lw.handle_callback(object(), self.cb())
            lw.handle_callback(object(), self.cb())
        self.assertEqual(bk.call_count, 1)
        texts = [c.kwargs.get("text", "") for c in tg.call_args_list]
        self.assertTrue(any("ZAREZERWOWANO" in t for t in texts))
        self.assertTrue(any("wygasła" in t for t in texts))

    def test_booking_error_is_reported_not_raised(self):
        with mock.patch.object(lw, "tg_api") as tg, \
                mock.patch.object(booking, "book", side_effect=booking.BookingError("Termin zajęty")):
            lw.handle_callback(object(), self.cb())
        texts = " ".join(c.kwargs.get("text", "") for c in tg.call_args_list)
        self.assertIn("NIE ZAREZERWOWANO", texts)
        self.assertIn("Termin zajęty", texts)

    def test_button_payload_fits_telegram_limit(self):
        self.assertLessEqual(len(f"book:{self.book_id}".encode()), 64)


if __name__ == "__main__":
    unittest.main()
