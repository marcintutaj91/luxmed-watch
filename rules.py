"""Filtry godzin i dni oraz formatowanie zgrupowanych powiadomień (czysta logika, bez sieci)."""
from datetime import date, datetime

DNI = ["pon", "wt", "śr", "czw", "pt", "sob", "ndz"]

_DAY_ALIASES = {
    0: ("pon", "poniedzialek", "poniedziałek"),
    1: ("wt", "wtorek"),
    2: ("sr", "śr", "sroda", "środa"),
    3: ("czw", "czwartek"),
    4: ("pt", "piatek", "piątek"),
    5: ("sob", "sobota"),
    6: ("ndz", "niedz", "niedziela"),
}
_DAY_LOOKUP = {alias: idx for idx, names in _DAY_ALIASES.items() for alias in names}


class ConfigError(ValueError):
    pass


def _parse_time(value, field, label):
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        raise ConfigError(f"[{label}] {field} = {value!r}: oczekiwany format 'GG:MM', np. '16:30'") from None


def _parse_days(values, label):
    out = set()
    for v in values:
        idx = _DAY_LOOKUP.get(str(v).strip().lower())
        if idx is None:
            raise ConfigError(f"[{label}] days: nieznany dzień {v!r} (użyj: pon, wt, śr, czw, pt, sob, ndz)")
        out.add(idx)
    return out


def parse_filter(w):
    """Z bloku [[watch]] robi filtr: hours_from / hours_to ('GG:MM', włącznie) i days (lista dni)."""
    label = w.get("label", w.get("service_id", "watch"))
    if not isinstance(w.get("enabled", True), bool):
        raise ConfigError(f"[{label}] enabled = {w['enabled']!r}: użyj true albo false (bez cudzysłowu)")
    return {
        "from": _parse_time(w["hours_from"], "hours_from", label) if "hours_from" in w else None,
        "to": _parse_time(w["hours_to"], "hours_to", label) if "hours_to" in w else None,
        "days": _parse_days(w["days"], label) if "days" in w else None,
    }


def allowed(flt, start):
    """Czy termin zaczynający się o `start` (datetime) mieści się w filtrze."""
    if flt["days"] is not None and start.weekday() not in flt["days"]:
        return False
    t = start.time()
    if flt["from"] is not None and t < flt["from"]:
        return False
    if flt["to"] is not None and t > flt["to"]:
        return False
    return True


def _plural(n):
    if n == 1:
        return "nowy termin"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "nowe terminy"
    return "nowych terminów"


def short_clinic(clinic):
    """'LX Stomatologia Warszawa - Modlińska 58' -> 'Modlińska 58'."""
    return (clinic or "").split(" - ")[-1].strip()


def _when(t, today):
    start = datetime.fromisoformat(t["dateTimeFrom"])
    s = f"{DNI[start.weekday()]} {start:%d.%m}, {start:%H:%M}"
    days = (start.date() - today).days
    rel = {0: "DZIŚ", 1: "jutro"}.get(days, f"za {days} dni" if days > 1 else "")
    return s, rel


def format_group(name, terms, today=None):
    """Treść wiadomości dla jednego lekarza i etykiety przycisków (po jednym na termin)."""
    today = today or date.today()
    terms = sorted(terms, key=lambda t: t["dateTimeFrom"])
    lines, buttons = [f"{name.title()}: {len(terms)} {_plural(len(terms))}"], []
    for t in terms:
        s, rel = _when(t, today)
        place = short_clinic(t.get("clinic"))
        parts = [s] + ([rel] if rel else []) + ([place] if place else [])
        if t.get("isTelemedicine"):
            parts.append("TELEPORADA")
        lines.append("• " + " · ".join(parts))
        buttons.append("Zarezerwuj " + " · ".join([s] + ([place] if place else [])))
    return "\n".join(lines), buttons
