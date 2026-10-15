"""Rezerwacja terminu w portalu pacjenta Lux Med.

Przebieg: token CSRF -> reservation/lockterm -> reservation/confirm
(przy błędzie lub wątpliwościach: reservation/releaseterm).

Format zapytań wzorowany na projektach dyrkin/luxmed-bot i crible/luxmed_bot.
To NIEOFICJALNE API, może się zmienić.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


class BookingError(Exception):
    """Rezerwacja nie doszła do skutku (komunikat nadaje się do pokazania użytkownikowi)."""


def _utc_date(local_iso):
    local = datetime.fromisoformat(local_iso).replace(tzinfo=WARSAW)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00.000Z")


def _hhmm(local_iso):
    return datetime.fromisoformat(local_iso).strftime("%H:%M")


def lock_body(t):
    extra = t.get("_additional") or {}
    return {
        "date": _utc_date(t["dateTimeFrom"]),
        "doctor": t["doctor"],
        "doctorId": t["doctor"]["id"],
        "eReferralId": None,
        "facilityId": t["clinicId"],
        "impedimentText": t.get("impedimentText"),
        "isAdditional": bool(t.get("isAdditional")),
        "isImpediment": bool(t.get("isImpediment")),
        "isPreparationRequired": bool(extra.get("isPreparationRequired")),
        "isTelemedicine": bool(t.get("isTelemedicine")),
        "parentReservationId": None,
        "preparationItems": extra.get("preparationItems") or [],
        "referralId": None,
        "referralTypeId": None,
        "roomId": t["roomId"],
        "scheduleId": t["scheduleId"],
        "serviceVariantId": t["serviceId"],
        "timeFrom": _hhmm(t["dateTimeFrom"]),
        "timeTo": _hhmm(t["dateTimeTo"]),
    }


def confirm_body(t, temporary_id, valuation):
    return {
        "date": _utc_date(t["dateTimeFrom"]),
        "doctorId": t["doctor"]["id"],
        "facilityId": t["clinicId"],
        "roomId": t["roomId"],
        "scheduleId": t["scheduleId"],
        "serviceVariantId": t["serviceId"],
        "temporaryReservationId": temporary_id,
        "timeFrom": _hhmm(t["dateTimeFrom"]),
        "valuation": valuation,
    }


def _errors(resp):
    out = []
    for e in resp.get("errors") or []:
        out.append(e.get("message") if isinstance(e, dict) else str(e))
    return "; ".join(x for x in out if x) or "nieznany błąd portalu"


def _release(client, temporary_id):
    try:
        client.post("/NewPortal/reservation/releaseterm", {}, params={"reservationId": temporary_id})
    except Exception as e:  # blokada i tak wygaśnie sama
        print("Nie udało się zwolnić blokady:", type(e).__name__, e)


def book(client, t, dry_run=False):
    """Zarezerwuj termin t (słownik z terms/index). Zwraca odpowiedź portalu.

    dry_run=True: tylko zablokuj termin i od razu zwolnij, bez potwierdzania.
    Rzuca BookingError z czytelnym powodem.
    """
    with client.lock:  # blokada -> potwierdzenie bez przeplatania z innymi zapytaniami
        resp = client.post("/NewPortal/reservation/lockterm", lock_body(t))
        if resp.get("hasErrors") or not resp.get("value"):
            raise BookingError("Termin niedostępny: " + _errors(resp))
        value = resp["value"]
        temp_id = value.get("temporaryReservationId")
        if not temp_id:
            raise BookingError("Portal nie zwrócił numeru blokady terminu.")

        problem = None
        valuations = value.get("valuations") or []
        if value.get("conflictedVisit"):
            problem = "masz już w tym czasie inną wizytę"
        elif value.get("askForReferral"):
            problem = "portal wymaga skierowania"
        elif not valuations:
            problem = "brak informacji o rozliczeniu wizyty"
        else:
            v = valuations[0]
            if v.get("isReferralRequired") or v.get("requireReferralForPP"):
                problem = "wizyta wymaga skierowania"
            elif v.get("price") not in (None, 0, 0.0):
                problem = f"wizyta jest płatna ({v.get('price')} zł)"
        if problem:
            _release(client, temp_id)
            raise BookingError(f"Nie rezerwuję automatycznie: {problem}. Zarezerwuj ręcznie w portalu.")

        if dry_run:
            _release(client, temp_id)
            return {"dry_run": True, "temporaryReservationId": temp_id}

        resp = client.post("/NewPortal/reservation/confirm", confirm_body(t, temp_id, valuations[0]))
        if resp.get("hasErrors"):
            _release(client, temp_id)
            raise BookingError("Portal odrzucił rezerwację: " + _errors(resp))
        return resp
