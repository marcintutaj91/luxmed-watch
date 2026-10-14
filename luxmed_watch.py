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


def dbg(*a):
    if DEBUG:
        print("[debug]", *a, file=sys.stderr)


class Luxmed:
    def __init__(self, login, password):
        self.s = requests.Session()
        dev = str(uuid.uuid4())
        self.s.headers.update({
            "User-Agent": f"Patient Portal; 4.19.0; {dev}; Android; 28; {dev}",
            "Accept": "application/json",
            "Accept-Language": "pl",
        })
        self.login, self.password = login, password
        self.authenticate()

    def authenticate(self):
        r = self.s.post(f"{BASE}/connect/token", data={
            "username": self.login, "password": self.password,
            "grant_type": "password", "client_id": "iPhone",
        }, timeout=30)
        dbg("auth", r.status_code, r.text[:300])
        if r.status_code != 200 or "access_token" not in r.text:
            raise SystemExit(f"Logowanie nieudane ({r.status_code}). Sprawdź dane lub endpoint (--debug).")
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path, **params):
        r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        if r.status_code == 401:
            self.authenticate()
            r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
        dbg(path, params, r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json()

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
            **{"searchPlace.id": city_id, "searchPlace.name": city_name,
               "serviceVariantId": service_id, "languageId": 10,
               "searchDateFrom": date_from, "searchDateTo": date_to,
               "delocalized": "false"})
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
        script = f'display notification {json.dumps(msg)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)
    except FileNotFoundError:
        pass
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if tok and chat:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": f"{title}\n{msg}"}, timeout=15)


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
            if clinics and str(t.get("clinicId")) not in clinics:
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
    w = sub.add_parser("watch")
    w.add_argument("--once", action="store_true")
    a = ap.parse_args()
    DEBUG = a.debug

    cfg_path = Path(a.config)
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    auth = cfg.get("auth", {})
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
    while True:
        try:
            check(client, cfg, seen)
        except requests.RequestException as e:
            print("Błąd sieci/API:", e, file=sys.stderr)
        if a.once:
            break
        time.sleep(interval * 60 * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    main()
