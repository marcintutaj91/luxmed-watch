#!/usr/bin/env python3
"""Porównanie widoczności usług i terminów między dwoma kontami Lux Med. TYLKO ODCZYT.

Skrypt jest samodzielny (wymaga tylko Pythona 3.11+ i pakietu `requests`). Wysyła wyłącznie zapytania
odczytujące (słowniki, dostępność usługi, lista lekarzy, terminy). Niczego nie rezerwuje i niczego nie zmienia.

Raport zawiera tylko dane publiczne dla każdego pacjenta (ID i nazwy usług, lekarze, terminy, flagi
dostępności usługi w pakiecie). NIE zawiera loginu, hasła, tokenów ani danych osobowych.

Użycie:
  python account_report.py report --name zona            # login i hasło zapyta w terminalu
  python account_report.py report --name marcin --config config.toml   # login z [auth] w configu
  python account_report.py compare report-marcin.json report-zona.json

Uwaga: korzystanie z portalu przez aplikacje innych dostawców niż Lux Med jest niezgodne z jego regulaminem.
Używaj tylko na własnym koncie i z umiarem (skrypt robi kilkanaście zapytań z przerwami).
"""
import argparse
import getpass
import json
import os
import random
import sys
import time
import tomllib
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = "https://portalpacjenta.luxmed.pl/PatientPortal"
MAX_REPORT_GAP_MIN = 10  # terminy zmieniają się w minutach: raporty z dalszych godzin nie są porównywalne


# ---------------------------------------------------------------- klient (tylko GET)

class Client:
    def __init__(self, login, password):
        import requests  # import tu, żeby `compare` działał bez requests
        self.requests = requests
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Accept-Language": "pl"})
        self.login, self.password = login, password
        self.authenticate()

    def authenticate(self):
        r = self.s.post(f"{BASE}/Account/LogIn", json={"login": self.login, "password": self.password}, timeout=30)
        try:
            token = r.json()["token"]
        except (ValueError, KeyError, TypeError):
            raise SystemExit(f"Logowanie nieudane (HTTP {r.status_code}): błędne dane, wymagane MFA albo zmiana API.")
        for k, v in self.s.cookies.items():
            self.s.headers[k] = v
        self.s.headers["authorization-token"] = f"Bearer {token}"

    def get(self, path, **params):
        time.sleep(random.uniform(0.8, 1.8))  # nie obciążaj portalu
        r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        if r.status_code == 401 or "json" not in r.headers.get("Content-Type", ""):
            self.authenticate()
            r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- raport

def flatten(node, acc=None):
    acc = {} if acc is None else acc
    if isinstance(node, dict):
        if "id" in node and "name" in node:
            acc[str(node["id"])] = node["name"]
        for v in node.values():
            flatten(v, acc)
    elif isinstance(node, list):
        for v in node:
            flatten(v, acc)
    return acc


def build_report(client, name, city, services, days, now=None):
    today = date.today()
    report = {
        "version": 1, "name": name, "taken_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "city": city, "days": days, "limits": {}, "catalog": {}, "services": {}, "doctor_names": {},
    }
    try:
        report["limits"] = client.get("/NewPortal/Reservation/GetLimits")
    except Exception as e:  # nieobowiązkowe
        report["limits"] = {"error": type(e).__name__}
    report["catalog"] = flatten(client.get("/NewPortal/Dictionary/serviceVariantsGroups"))
    for sid in services:
        entry = {"name": report["catalog"].get(str(sid), "?")}
        entry["availability"] = client.get("/NewPortal/Reservation/serviceVariantAvailabilityDetails",
                                           serviceVariantId=sid, cityId=city, reservationId="")
        fd = client.get("/NewPortal/Dictionary/facilitiesAndDoctors", cityId=city, serviceVariantId=sid)
        entry["doctors"] = sorted(str(d["id"]) for d in fd.get("doctors", []))
        entry["facilities"] = sorted(str(f["id"]) for f in fd.get("facilities", []))
        for d in fd.get("doctors", []):
            report["doctor_names"][str(d["id"])] = f"{d.get('firstName', '')} {d.get('lastName', '')}".strip()
        terms = client.get("/NewPortal/terms/index", **{
            "searchPlace.id": city, "searchPlace.type": 0, "serviceVariantId": sid, "languageId": 10,
            "searchDateFrom": today.isoformat(), "searchDateTo": (today + timedelta(days=days)).isoformat(),
            "searchDatePreset": days, "delocalized": "false", "processId": str(uuid.uuid4())})
        keys = []
        for day in (terms.get("termsForService") or {}).get("termsForDays") or []:
            for t in day.get("terms") or []:
                doc = t.get("doctor") or {}
                report["doctor_names"].setdefault(str(doc.get("id")),
                                                  f"{doc.get('firstName', '')} {doc.get('lastName', '')}".strip())
                keys.append(f"{doc.get('id')}|{t.get('dateTimeFrom')}|{t.get('clinicId')}")
        entry["terms"] = sorted(keys)
        report["services"][str(sid)] = entry
    return report


# ---------------------------------------------------------------- porównanie

def _names(report, ids, limit=8):
    ids = sorted(ids)
    shown = [f"{i} {report['catalog'].get(i, '?')}" for i in ids[:limit]]
    return shown + ([f"… i {len(ids) - limit} więcej"] if len(ids) > limit else [])


def _term_label(report, key):
    doc, when, _clinic = key.split("|")
    return f"{when.replace('T', ' ')[:16]} {report['doctor_names'].get(doc, doc).title()}"


def compare_reports(a, b):
    """Zwraca listę linii z raportem różnic (pusta lista nie oznacza zgodności: patrz ostatnia linia)."""
    out = []
    A, B = a["name"], b["name"]
    gap = abs((datetime.fromisoformat(a["taken_at"]) - datetime.fromisoformat(b["taken_at"])).total_seconds()) / 60
    out.append(f"Porównanie: {A} ({a['taken_at']}) vs {B} ({b['taken_at']})")
    if (a["city"], a["days"]) != (b["city"], b["days"]):
        out.append(f"! Różne parametry (miasto/dni): {a['city']}/{a['days']} vs {b['city']}/{b['days']}")
    if gap > MAX_REPORT_GAP_MIN:
        out.append(f"! Raporty wykonano {gap:.0f} min od siebie: różnice w terminach mogą wynikać z upływu czasu. "
                   f"Porównuj oba raporty wykonane w ciągu {MAX_REPORT_GAP_MIN} min.")
    out.append(f"Limity rezerwacji: {A} {a.get('limits')} | {B} {b.get('limits')}")

    only_a = set(a["catalog"]) - set(b["catalog"])
    only_b = set(b["catalog"]) - set(a["catalog"])
    out.append(f"\nKatalog usług: {A} widzi {len(a['catalog'])}, {B} widzi {len(b['catalog'])}")
    for who, ids, rep in ((A, only_a, a), (B, only_b, b)):
        if ids:
            out.append(f"  Tylko {who} ({len(ids)}):")
            out += [f"    {x}" for x in _names(rep, ids)]

    differences = bool(only_a or only_b)
    for sid in sorted(set(a["services"]) & set(b["services"])):
        sa, sb = a["services"][sid], b["services"][sid]
        lines = []
        for flag in sorted(set(sa["availability"]) | set(sb["availability"])):
            va, vb = sa["availability"].get(flag), sb["availability"].get(flag)
            if va != vb:
                lines.append(f"    dostępność: {flag}: {A}={va} | {B}={vb}")
        da, db = set(sa["doctors"]), set(sb["doctors"])
        if da != db:
            lines.append(f"    lekarze: {A}={len(da)}, {B}={len(db)} (tylko {A}: {len(da - db)}, tylko {B}: {len(db - da)})")
        fa, fb = set(sa["facilities"]), set(sb["facilities"])
        if fa != fb:
            lines.append(f"    placówki: {A}={len(fa)}, {B}={len(fb)}")
        ta, tb = set(sa["terms"]), set(sb["terms"])
        if ta != tb:
            lines.append(f"    terminy: {A}={len(ta)}, {B}={len(tb)} (tylko {A}: {len(ta - tb)}, tylko {B}: {len(tb - ta)})")
            for who, keys, rep in ((A, sorted(ta - tb), a), (B, sorted(tb - ta), b)):
                lines += [f"      tylko {who}: {_term_label(rep, k)}" for k in keys[:5]]
        out.append(f"\nUsługa {sid} {sa['name']}: " + ("identyczna" if not lines else "RÓŻNICE"))
        out += lines
        differences |= bool(lines)
    out.append("\nWniosek: " + ("konta widzą różne dane (patrz wyżej)." if differences
                               else "oba konta widzą to samo w sprawdzonym zakresie."))
    return out


# ---------------------------------------------------------------- CLI

def _credentials(config_path, account=None):
    if config_path:
        cfg = tomllib.loads(Path(config_path).read_text())
        accounts = cfg.get("accounts")
        if accounts:  # format z kilkoma kontami: [accounts.nazwa]
            names = list(accounts)
            wanted = account or cfg.get("active_account")
            if isinstance(wanted, list) or str(wanted or "").lower() in ("all", "wszystkie", "*") or "," in str(wanted or ""):
                wanted = None  # raport robi się dla jednego konta naraz
            wanted = wanted or (names[0] if len(names) == 1 else None)
            match = next((n for n in names if wanted and n.lower() == str(wanted).lower()), None)
            if not match:
                raise SystemExit(f"Podaj --account. Dostępne konta: {', '.join(names)}")
            auth = accounts[match]
        else:
            auth = cfg.get("auth", {})
        if auth.get("login") and auth.get("password"):
            return auth["login"], auth["password"]
        raise SystemExit(f"Brak login/password dla wybranego konta w pliku {config_path}.")
    login = os.getenv("LUXMED_LOGIN") or input("Login do Portalu Pacjenta: ").strip()
    password = os.getenv("LUXMED_PASSWORD") or getpass.getpass("Hasło (nie będzie widoczne): ")
    return login, password


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="zapisz raport z tego konta (tylko odczyt)")
    r.add_argument("--name", required=True, help="etykieta raportu, np. imię (trafia do nazwy pliku)")
    r.add_argument("--city", type=int, default=1, help="ID miasta (domyślnie 1 = Warszawa)")
    r.add_argument("--services", default="6621,4448",
                   help="ID usług po przecinku (domyślnie 6621 Stomatolog, 4448 Konsultacja dermatologiczna)")
    r.add_argument("--days", type=int, default=60)
    r.add_argument("--config", help="opcjonalnie: config.toml ([auth] lub [accounts.*]) zamiast pytania w terminalu")
    r.add_argument("--account", help="konto z [accounts.*] w configu (domyślnie active_account)")
    c = sub.add_parser("compare", help="porównaj dwa raporty")
    c.add_argument("a")
    c.add_argument("b")
    args = ap.parse_args()

    if args.cmd == "compare":
        reports = [json.loads(Path(p).read_text()) for p in (args.a, args.b)]
        print("\n".join(compare_reports(*reports)))
        return

    login, password = _credentials(args.config, args.account)
    services = [int(x) for x in args.services.split(",") if x.strip()]
    print("Pobieram dane (tylko odczyt, kilkanaście zapytań z przerwami)…")
    report = build_report(Client(login, password), args.name, args.city, services, args.days)
    out = Path(f"report-{args.name}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"Zapisano {out} ({len(report['catalog'])} usług w katalogu, "
          + ", ".join(f"{s['name']}: {len(s['terms'])} terminów" for s in report["services"].values()) + ").")
    print("Plik nie zawiera loginu, hasła ani danych osobowych. Możesz go bezpiecznie przesłać.")


if __name__ == "__main__":
    sys.exit(main())
