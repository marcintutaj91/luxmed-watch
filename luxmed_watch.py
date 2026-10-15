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
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

import booking
import rules
from rules import DNI

BASE = "https://portalpacjenta.luxmed.pl/PatientPortal"
STATE_FILE = Path(__file__).with_name("state.json")
PENDING_FILE = Path(__file__).with_name("pending.json")
PENDING_LOCK = threading.Lock()
PENDING_TTL = 24 * 3600
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
        self.lock = threading.RLock()  # klient jest współdzielony z wątkiem Telegrama
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

    def _json(self, r, allow_empty=False):
        dbg(r.request.method, r.url.split("PatientPortal")[-1], r.status_code, r.text[:500])
        r.raise_for_status()
        if allow_empty and not r.text.strip():
            return {}  # np. releaseterm: HTTP 200 bez treści oznacza sukces
        try:
            return r.json()
        except ValueError:
            kind = r.headers.get("Content-Type", "?")
            raise RuntimeError(f"Odpowiedź nie jest JSON-em (HTTP {r.status_code}, {kind}): "
                               f"{r.text[:120]!r}") from None

    def get(self, path, **params):
        with self.lock:
            r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
            if r.status_code == 401 or "json" not in r.headers.get("Content-Type", ""):
                dbg("sesja wygasła lub zła odpowiedź, loguję ponownie", r.status_code)
                self.authenticate()
                r = self.s.get(f"{BASE}{path}", params=params, timeout=30)
            return self._json(r)

    def post(self, path, body, params=None):
        """POST z tokenem CSRF (wymagany przez operacje zapisu w portalu)."""
        with self.lock:
            for attempt in (1, 2):
                token = self.get("/NewPortal/security/getforgerytoken")["token"]
                r = self.s.post(f"{BASE}{path}", json=body, params=params, timeout=30, headers={
                    "xsrf-token": token, "x-requested-with": "XMLHttpRequest",
                    "Content-Type": "application/json"})
                if r.status_code != 401 or attempt == 2:
                    return self._json(r, allow_empty=True)
                self.authenticate()

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
        tfs = data.get("termsForService") or {}
        extra = tfs.get("additionalData") or {}
        out = []
        for day in tfs.get("termsForDays") or []:
            for t in day.get("terms") or []:
                t["_additional"] = {"isPreparationRequired": extra.get("isPreparationRequired"),
                                    "preparationItems": extra.get("preparationItems")}
                out.append(t)
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


def notify(title, msg, buttons=None):
    """buttons: lista (tekst, book_id); każdy przycisk w osobnym wierszu."""
    print(f"\n*** {title}\n{msg}\n")
    try:  # powiadomienie macOS
        subprocess.run(["osascript",
                        "-e", "on run argv",
                        "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "Glass"',
                        "-e", "end run", msg, title], check=False)
    except FileNotFoundError:
        pass
    if TG.get("bot_token") and TG.get("chat_id"):
        data = {"chat_id": TG["chat_id"], "text": f"{title}\n{msg}"}
        if buttons:
            data["reply_markup"] = json.dumps({"inline_keyboard": [
                [{"text": text, "callback_data": f"book:{bid}"}] for text, bid in buttons]})
        tg_api("sendMessage", **data)


def tg_api(method, **data):
    """Wywołanie API Telegrama; zwraca JSON albo None (błędy tylko logujemy)."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG['bot_token']}/{method}", data=data, timeout=15)
        if not r.ok:
            print(f"Telegram {method}: błąd {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except requests.RequestException as e:
        print(f"Telegram {method}: brak połączenia:", type(e).__name__, file=sys.stderr)
        return None


def load_pending():
    try:
        return json.loads(PENDING_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_pending(pending):
    now = time.time()
    pending = {k: v for k, v in pending.items() if now - v["ts"] < PENDING_TTL}
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False))
    PENDING_FILE.chmod(0o600)


def add_pending(key, term, name, label):
    book_id = hashlib.sha1(key.encode()).hexdigest()[:12]
    with PENDING_LOCK:
        pending = load_pending()
        pending[book_id] = {"term": term, "name": name, "label": label, "ts": time.time()}
        save_pending(pending)
    return book_id


def handle_callback(client, cb):
    """Kliknięcie przycisku 'Zarezerwuj' na Telegramie."""
    if str(cb.get("from", {}).get("id")) != str(TG.get("chat_id")):
        return  # tylko właściciel bota
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
    if not data.startswith("book:"):
        return
    with PENDING_LOCK:  # jedno kliknięcie = jedna próba
        pending = load_pending()
        entry = pending.pop(data[5:], None)
        save_pending(pending)
    if not entry:
        tg_api("answerCallbackQuery", callback_query_id=cb["id"], text="Ta oferta wygasła lub już obsłużona.")
        return
    tg_api("answerCallbackQuery", callback_query_id=cb["id"], text="Rezerwuję…")
    summary = format_term(entry["term"], entry["name"])
    try:
        booking.book(client, entry["term"])
        result = f"ZAREZERWOWANO\n{summary}"
    except booking.BookingError as e:
        result = f"NIE ZAREZERWOWANO\n{summary}\n{e}"
    except (requests.RequestException, RuntimeError, KeyError, ValueError) as e:
        result = f"BŁĄD przy rezerwacji ({type(e).__name__}); sprawdź portal.\n{summary}"
    print("\n***", result, "\n")
    if chat and mid:  # zdejmij przycisk i pokaż wynik w tej samej wiadomości
        tg_api("editMessageText", chat_id=chat, message_id=mid, text=f"Lux Med: {entry['label']}\n{result}")
    else:
        tg_api("sendMessage", chat_id=TG["chat_id"], text=result)


def telegram_listener(client):
    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["callback_query"])}
            if offset:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{TG['bot_token']}/getUpdates",
                             params=params, timeout=40)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    handle_callback(client, u["callback_query"])
        except Exception as e:  # wątek nie może paść
            print("Telegram listener:", type(e).__name__, file=sys.stderr)
            time.sleep(10)


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


def format_term(t, name):
    """Czytelny opis terminu: 'czw 15.10, 17:20 (40 min) · jutro'."""
    start = datetime.fromisoformat(t["dateTimeFrom"])
    when = f"{DNI[start.weekday()]} {start:%d.%m.%Y}, {start:%H:%M}"
    try:
        mins = int((datetime.fromisoformat(t["dateTimeTo"]) - start).total_seconds() // 60)
        when += f" ({mins} min)"
    except (KeyError, ValueError):
        pass
    days = (start.date() - date.today()).days
    rel = {0: "DZIŚ", 1: "jutro"}.get(days, f"za {days} dni" if days > 1 else "")
    if rel:
        when += f" · {rel}"
    lines = [when, name.title(), t.get("clinic", "")]
    if t.get("isTelemedicine"):
        lines.append("TELEPORADA")
    return "\n".join(x for x in lines if x)


def find_terms(client, cfg):
    """Wolne terminy pasujące do bloków [[watch]]: (blok, termin, nazwisko lekarza, klucz)."""
    horizon = cfg.get("horizon_days", 60)
    d_from = date.today().isoformat()
    d_to = (date.today() + timedelta(days=horizon)).isoformat()
    for w in cfg["watch"]:
        if not w.get("enabled", True):
            continue  # enabled = false wyłącza blok bez usuwania go z configu
        doctors = [d.lower() for d in w.get("doctors", [])]
        clinics = {str(c) for c in w.get("clinic_ids", [])}
        flt = rules.parse_filter(w)
        terms = client.terms(w["city_id"], w.get("city_name", ""), w["service_id"], d_from, d_to)
        for t in terms:
            doc = t.get("doctor") or {}
            name = f"{doc.get('firstName', '')} {doc.get('lastName', '')}".strip()
            if doctors and not any(x in name.lower() for x in doctors):
                continue
            if clinics and str(t.get("clinicGroupId", t.get("clinicId"))) not in clinics:
                continue
            if not rules.allowed(flt, datetime.fromisoformat(t["dateTimeFrom"])):
                continue
            key = f"{w['service_id']}|{t.get('dateTimeFrom')}|{doc.get('id')}|{t.get('clinicId')}"
            yield w, t, name, key


MAX_PER_MESSAGE = 10  # reszta trafi do kolejnej wiadomości w następnej rundzie


def check(client, cfg, seen):
    can_book = bool(cfg.get("booking", {}).get("enabled")) and bool(TG.get("bot_token") and TG.get("chat_id"))
    groups = {}  # (blok, lekarz) -> [(termin, klucz)]
    claimed = set()  # ten sam termin może pasować do kilku bloków: wygrywa pierwszy z configu
    for w, t, name, key in find_terms(client, cfg):
        if key not in seen and key not in claimed:
            claimed.add(key)
            groups.setdefault((id(w), w.get("label", w["service_id"]), name), []).append((t, key))
    for (_, label, name), items in groups.items():
        items.sort(key=lambda x: x[0]["dateTimeFrom"])
        items = items[:MAX_PER_MESSAGE]
        body, labels = rules.format_group(name, [t for t, _ in items])
        buttons = [(text, add_pending(key, t, name, label))
                   for text, (t, key) in zip(labels, items)] if can_book else None
        for _, key in items:
            seen.add(key)
        notify(f"Lux Med: {label}", body, buttons)
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
    sub.add_parser("book-test", help="test rezerwacji bez potwierdzenia: zablokuj pierwszy pasujący termin i zwolnij")
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

    try:
        for w_ in cfg["watch"]:
            rules.parse_filter(w_)
    except rules.ConfigError as ex:
        raise SystemExit(f"Błąd w config.toml: {ex}")

    if a.cmd == "book-test":
        for w, t, name, key in find_terms(client, cfg):
            print("Testuję na terminie:\n" + format_term(t, name))
            try:
                print("Wynik:", booking.book(client, t, dry_run=True))
            except booking.BookingError as e:
                print("Odmowa:", e)
            return
        raise SystemExit("Brak wolnych terminów pasujących do configu: nie ma na czym testować.")

    if cfg.get("booking", {}).get("enabled") and TG.get("bot_token") and TG.get("chat_id") and not a.once:
        threading.Thread(target=telegram_listener, args=(client,), daemon=True).start()
        print("Rezerwacja przyciskiem na Telegramie: WŁĄCZONA")
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
