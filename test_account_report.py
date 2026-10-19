"""Testy account_report (bez sieci). Uruchom: python -m unittest test_account_report -v"""
import copy
import json
import unittest
from datetime import datetime

import account_report as ar

CATALOG = [{"id": 1, "name": "Grupa", "children": [{"id": 6621, "name": "Stomatolog"},
                                                    {"id": 4448, "name": "Konsultacja dermatologiczna"}]}]


class FakeClient:
    """Atrapa portalu; zapisuje wszystkie wywołania, żeby sprawdzić, że są to tylko odczyty."""
    def __init__(self, extra_service=False, free=True, terms=None):
        self.calls = []
        self.extra_service = extra_service
        self.free = free
        self.terms = terms if terms is not None else [
            {"doctor": {"id": 7, "firstName": "MONIKA", "lastName": "HORVATH"}, "dateTimeFrom": "2026-10-20T14:00:00",
             "clinicId": 3}]

    def get(self, path, **params):
        self.calls.append(path)
        if path.endswith("GetLimits"):
            return {"canReserve": True, "maxReservedVisitsCount": 0}
        if path.endswith("serviceVariantsGroups"):
            extra = [{"id": 9999, "name": "Usługa tylko w pakiecie"}] if self.extra_service else []
            return CATALOG + extra
        if path.endswith("serviceVariantAvailabilityDetails"):
            return {"isNotAvailableInContract": False, "isServiceForFree": self.free}
        if path.endswith("facilitiesAndDoctors"):
            return {"doctors": [{"id": 7, "firstName": "MONIKA", "lastName": "HORVATH"}], "facilities": [{"id": 3, "name": "X"}]}
        if path.endswith("terms/index"):
            return {"termsForService": {"termsForDays": [{"terms": self.terms}]}}
        raise AssertionError(f"nieoczekiwane wywołanie {path}")


def report(name="a", at="2026-10-19T10:00:00", **kw):
    return ar.build_report(FakeClient(**kw), name, 1, [6621], 60, now=datetime.fromisoformat(at))


class BuildTests(unittest.TestCase):
    def test_report_has_expected_shape_and_no_secrets(self):
        r = report()
        self.assertEqual(r["catalog"]["6621"], "Stomatolog")
        s = r["services"]["6621"]
        self.assertEqual(s["doctors"], ["7"])
        self.assertEqual(s["terms"], ["7|2026-10-20T14:00:00|3"])
        self.assertEqual(r["doctor_names"]["7"], "MONIKA HORVATH")
        blob = json.dumps(r).lower()
        for word in ("password", "token", "login"):
            self.assertNotIn(word, blob)

    def test_only_read_endpoints_are_called(self):
        c = FakeClient()
        ar.build_report(c, "a", 1, [6621], 60)
        self.assertEqual(len(c.calls), 5)
        self.assertFalse(any(x in p.lower() for p in c.calls for x in ("lockterm", "confirm", "release", "delete")))


class CompareTests(unittest.TestCase):
    def test_identical_reports_say_the_same(self):
        text = "\n".join(ar.compare_reports(report("a"), report("b")))
        self.assertIn("oba konta widzą to samo", text)
        self.assertIn("identyczna", text)

    def test_extra_service_in_one_account_is_reported(self):
        text = "\n".join(ar.compare_reports(report("a", extra_service=True), report("b")))
        self.assertIn("Tylko a", text)
        self.assertIn("9999 Usługa tylko w pakiecie", text)
        self.assertIn("konta widzą różne dane", text)

    def test_availability_flag_difference_is_reported(self):
        text = "\n".join(ar.compare_reports(report("a", free=True), report("b", free=False)))
        self.assertIn("isServiceForFree: a=True | b=False", text)

    def test_term_difference_lists_examples(self):
        extra = [{"doctor": {"id": 7, "firstName": "MONIKA", "lastName": "HORVATH"},
                  "dateTimeFrom": "2026-10-21T15:20:00", "clinicId": 3}]
        a = report("a", terms=FakeClient().terms + extra)
        text = "\n".join(ar.compare_reports(a, report("b")))
        self.assertIn("tylko a: 2026-10-21 15:20 Monika Horvath", text)

    def test_far_apart_reports_trigger_a_warning(self):
        text = "\n".join(ar.compare_reports(report("a", at="2026-10-19T10:00:00"), report("b", at="2026-10-19T12:00:00")))
        self.assertIn("min od siebie", text)

    def test_close_reports_do_not_warn(self):
        text = "\n".join(ar.compare_reports(report("a", at="2026-10-19T10:00:00"), report("b", at="2026-10-19T10:05:00")))
        self.assertNotIn("min od siebie", text)

    def test_different_city_is_flagged(self):
        b = copy.deepcopy(report("b"))
        b["city"] = 3
        self.assertIn("Różne parametry", "\n".join(ar.compare_reports(report("a"), b)))


if __name__ == "__main__":
    unittest.main()
