#!/usr/bin/env python3
"""Nasłuch wolnych terminów w portalu pacjenta Lux Med. Tylko powiadamia, nie rezerwuje.

UWAGA: endpointy to NIEOFICJALNE API portalu (wzorowane na projekcie LuxmedSniper).
Mogą się zmienić. Przy pierwszym uruchomieniu użyj --debug, żeby zobaczyć surowe odpowiedzi.

Użycie:
  python luxmed_watch.py discover cities
  python luxmed_watch.py discover services stomat
  python luxmed_watch.py discover doctors --city 1 --service 4502
  python luxmed_watch.py watch            # pętla
  python luxmed_watch.py watch --once     # jedno sprawdzenie
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
import tomllib
import uuid
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = "https://portalpacjenta.luxmed.pl/PatientPortal"
STATE_FILE = Path(__file__).with_name("state.json")
DEBUG = False
TG = {}  # {"bot_token": ..., "chat_id": ...}


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, file=sys.stderr)


class Luxmed:
    def __init__(self, login, password):
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "Accept-Language": "pl"})
        self.login, self.password = login, password
        self.authenticate()

    def authenticate(self):
        r = self.s.post(f"{BASE}/Account/LogIn", json={"login": self.login, "password": self.password},
                        timeout=30)
        body = re.sub(r'("token"\s*:\s*")[^"]+', r'\1<redacted>', r.text)
        dbg("auth", r.status_code, r.headers.get("Content-Type"), body[:400])
        try:
            token = r.json()["token"]
        except (ValueError, KeyError, TypeError):
            raise SystemExit(
                f"Logowanie nieudane (HTTP {r.status_code}). Odpowiedź nie zawiera tokenu: "
                "błędne dane, wymagane MFA albo zmiana API. Uruchom z --debug.")
        for k, v in self.s.cookies.items():
            self.s.headers[k] = v
        self.s.headers["authorization-token"] = f"Bearer {token}"

    def get(self, path, **params):
        r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        if r.status_code == 401 or "json" not in r.headers.get("Content-Type", ""):
            dbg("sesja wygasła lub zła odpowiedź, loguję ponownie", r.status_code)
            self.authenticate()
            r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        dbg(path, params, r.status_code, r.text[:500])
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            kind = r.headers.get("Content-Type", "?")
            raise RuntimeError(f"Odpowiedź nie jest JSON-em (HTTP {r.status_code}, {kind}): "
                               f"{r.text[:120]!r}") from None

    def cities(self):
        return self.get("/NewPortal/Dictionary/cities")

    def services(self):
        return self.get("/NewPortal/Dictionary/serviceVariantsGroups")

    def doctors(self, city_id, service_id):
        return self.get("/NewPortal/Dictionary/facilitiesAndDoctors",
                        cityId=city_id, serviceVariantId=service_id)

    def terms(self, city_id, city_name, service_id, date_from, date_to):
        data = self.get(
            "/NewPortal/terms/index",
            **{"searchPlace.id": city_id, "searchPlace.type": 0,
               "serviceVariantId": service_id, "languageId": 10,
               "searchDateFrom": date_from, "searchDateTo": date_to,
               "searchDatePreset": (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days,
               "delocalized": "false", "processId": str(uuid.uuid4())})
        out = []
        for day in (data.get("termsForService") or {}).get("termsForDays") or []:
            out.extend(day.get("terms") or [])
        return out


def flatten(node, acc=None):
    """Spłaszcza drzewo słownika usług do listy {id, name}."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        if "id" in node and "name" in node:
            acc.append({"id": node["id"], "name": node["name"]})
        for v in node.values():
            flatten(v, acc)
    elif isinstance(node, list):
        for v in node:
            flatten(v, acc)
    return acc


def notify(title, msg):
    print(f"\n*** {title}\n{msg}\n")
    try:  # powiadomienie macOS
        subprocess.run(["osascript",
                        "-e", "on run argv",
                        "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                        "-e", "end run", msg, title], check=False)
    except FileNotFoundError:
        pass
    if TG.get("bot_token") and TG.get("chat_id"):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG['bot_token']}/sendMessage",
                              data={"chat_id": TG["chat_id"], "text": f"{title}\n{msg}"}, timeout=15)
            if not r.ok:
                print(f"Telegram: błąd {r.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print("Telegram: brak połączenia:", type(e).__name__, file=sys.stderr)


def telegram_setup():
    """Znajdź chat_id po wiadomości do bota i wyślij wiadomość testowa."""
    tok = TG.get("bot_token")
    if not tok:
        raise SystemExit("Brak bot_token w [telegram] w config.toml.")
    chat = TG.get("chat_id")
    if not chat:
        r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15)
        if not r.ok:
            raise SystemExit(f"Telegram odrzucił token (HTTP {r.status_code}). Sprawdź bot_token.")
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
                 for u in r.json().get("result", []) if "message" in u}
        if not chats:
            raise SystemExit("Brak wiadomości do bota. Otwórz bota w Telegramie, wyślij mu /start "
                             "i uruchom to polecenie ponownie.")
        for cid, name in chats.items():
            print(f"chat_id = {cid}   ({name})")
        chat = next(iter(chats))
        print("Wpisz chat_id do [telegram] w config.toml. Wysyłam test na pierwszy z nich.")
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": "Lux Med watcher: test powiadomień OK"}, timeout=15)
    print("Wysłano." if r.ok else f"Błąd wysyłki: HTTP {r.status_code}")


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def check(client, cfg, seen):
    horizon = cfg.get("horizon_days", 60)
    d_from = date.today().isoformat()
    d_to = (date.today() + timedelta(days=horizon)).isoformat()
    for w in cfg["watch"]:
        doctors = [d.lower() for d in w.get("doctors", [])]
        clinics = {str(c) for c in w.get("clinic_ids", [])}
        terms = client.terms(w["city_id"], w.get("city_name", ""), w["service_id"], d_from, d_to)
        for t in terms:
            doc = t.get("doctor") or {}
            name = f"{doc.get('firstName', '')} {doc.get('lastName', '')}".strip()
            if doctors and not any(x in name.lower() for x in doctors):
                continue
            if clinics and str(t.get("clinicGroupId", t.get("clinicId"))) not in clinics:
                continue
            key = f"{w['service_id']}|{t.get('dateTimeFrom')}|{doc.get('id')}|{t.get('clinicId')}"
            if key in seen:
                continue
            seen.add(key)
            notify(f"Lux Med: {w.get('label', w['service_id'])}",
                   f"{t.get('dateTimeFrom')}  {name}\n{t.get('clinic', '')}")
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("what", choices=["cities", "services", "doctors"])
    d.add_argument("filter", nargs="?", default="")
    d.add_argument("--city")
    d.add_argument("--service")
    e = sub.add_parser("export", help="zapisz katalog usług i lekarzy do catalog.json (dla picker.html)")
    e.add_argument("--city", required=True, help="ID miasta (z: discover cities)")
    e.add_argument("--filter", action="append", required=True,
                   help="fragment nazwy usługi, można podać wielokrotnie, np. --filter stomat --filter dermat")
    sub.add_parser("telegram", help="znajdź chat_id i wyślij wiadomość testową")
    w = sub.add_parser("watch")
    w.add_argument("--once", action="store_true")
    a = ap.parse_args()
    DEBUG = a.debug

    cfg_path = Path(a.config)
    try:
        cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    except tomllib.TOMLDecodeError as ex:
        raise SystemExit(f"Błąd w {cfg_path.name}: {ex}\n"
                         "Wartości tekstowe muszą być w apostrofach lub cudzysłowie, "
                         "np. login = 'abc'.")
    auth = cfg.get("auth", {})
    TG.update({"bot_token": cfg.get("telegram", {}).get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"),
               "chat_id": cfg.get("telegram", {}).get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")})
    if a.cmd == "telegram":
        telegram_setup()
        return
    login = auth.get("login") or os.getenv("LUXMED_LOGIN")
    pw = auth.get("password") or os.getenv("LUXMED_PASSWORD")
    if not login or not pw:
        raise SystemExit("Brak danych logowania: uzupełnij sekcję [auth] w config.toml "
                         "(albo ustaw LUXMED_LOGIN i LUXMED_PASSWORD).")
    client = Luxmed(login, pw)

    if a.cmd == "discover":
        if a.what == "doctors":
            data = client.doctors(a.city, a.service)
            for doc in data.get("doctors", []):
                print(doc.get("id"), doc.get("firstName"), doc.get("lastName"))
        else:
            data = client.cities() if a.what == "cities" else client.services()
            for x in flatten(data):
                if a.filter.lower() in str(x["name"]).lower():
                    print(x["id"], x["name"])
        return

    if a.cmd == "export":
        city = next((c for c in flatten(client.cities()) if str(c["id"]) == str(a.city)), None)
        if not city:
            raise SystemExit(f"Nie znaleziono miasta o ID {a.city}.")
        wanted = [f.lower() for f in a.filter]
        services, seen_ids = [], set()
        for s in flatten(client.services()):
            if s["id"] not in seen_ids and any(f in str(s["name"]).lower() for f in wanted):
                seen_ids.add(s["id"])
                services.append(s)
        doctors = {}
        for s in services:
            time.sleep(random.uniform(1, 2))  # nie obciążaj portalu
            try:
                data = client.doctors(a.city, s["id"])
            except requests.RequestException as ex:
                print(f"Pomijam usługę {s['id']}: {ex}", file=sys.stderr)
                continue
            doctors[str(s["id"])] = [
                {"id": d.get("id"), "name": f"{d.get('firstName', '')} {d.get('lastName', '')}".strip()}
                for d in data.get("doctors", [])]
        out = Path(__file__).with_name("catalog.json")
        out.write_text(json.dumps({"city": city, "services": services, "doctors": doctors},
                                  ensure_ascii=False, indent=1))
        print(f"Zapisano {out.name}: {len(services)} usług, "
              f"{sum(len(v) for v in doctors.values())} pozycji lekarzy.")
        return

    if "watch" not in cfg:
        raise SystemExit("Brak bloków [[watch]] w config.toml.")
    seen = load_state()
    interval = cfg.get("interval_minutes", 5)
    fails, alerted = 0, False
    while True:
        try:
            check(client, cfg, seen)
            if alerted:
                notify("Lux Med watcher: działa ponownie", "Nasłuch wrócił do normy.")
            fails, alerted = 0, False
        except (requests.RequestException, SystemExit, ValueError, KeyError, RuntimeError) as e:
            fails += 1
            print("Błąd nasłuchu:", type(e).__name__, e, file=sys.stderr)
            if fails >= 3 and not alerted:
                alerted = True
                notify("Lux Med watcher: PROBLEM",
                       f"{fails} nieudane sprawdzenia z rzędu ({type(e).__name__}). "
                       "Nasłuch może nie działać.")
        if a.once:
            break
        time.sleep(interval * 60 * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    main()
