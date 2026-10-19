#!/usr/bin/env python3
"""Nasłuch wolnych terminów w portalu pacjenta Lux Med (jedno lub kilka kont naraz).

Powiadamia na macOS i Telegramie, a rezerwuje tylko na Twoje kliknięcie (przycisk "Zarezerwuj").
Konta, bloki [[watch]], Telegram i rezerwacja: patrz config.example.toml.

UWAGA: endpointy to NIEOFICJALNE API portalu (wzorowane na projektach LuxmedSniper i luxmed-bot).
Mogą się zmienić. Regulamin Lux Med zabrania korzystania z portalu przez aplikacje spoza Lux Med.
Przy problemach użyj --debug, żeby zobaczyć surowe odpowiedzi.

Użycie:
  python luxmed_watch.py watch                      # aktywne konta z configu (active_account)
  python luxmed_watch.py --account zona watch       # tylko to konto; też "marcin,zona" albo "wszystkie"
  python luxmed_watch.py watch --once               # jedno sprawdzenie
  python luxmed_watch.py --account zona discover cities
  python luxmed_watch.py discover services stomat
  python luxmed_watch.py book-test                  # test blokady terminu bez rezerwacji
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

import accounts
import booking
import control
import rules
from rules import DNI

BASE = "https://portalpacjenta.luxmed.pl/PatientPortal"
PENDING_LOCK = threading.Lock()
PENDING_TTL = 24 * 3600
WAKE = threading.Event()  # przerywa oczekiwanie między sprawdzeniami (np. po /wlacz)
COMMAND_MAX_AGE = 120  # ignoruj komendy starsze niż tyle sekund (np. zaległe po restarcie)
MAX_PER_MESSAGE = 10  # reszta trafi do kolejnej wiadomości w następnej rundzie
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


def load_state(path):
    try:
        return set(json.loads(Path(path).read_text()))
    except (FileNotFoundError, ValueError):
        return set()


class Ctx:
    """Jedno konto: leniwie tworzony klient, jego bloki [[watch]], własne pliki stanu i status działania."""

    def __init__(self, name, cfg, multi, client=None):
        self.name, self.cfg, self.multi = name, cfg, multi
        self.blocks = accounts.blocks_for(cfg, name)
        self.files = accounts.paths(name)
        self.booking = accounts.booking_allowed(cfg, name)
        self.seen = load_state(self.files["state"])
        self.status = {"last_ok": None, "fails": 0, "last_error": None}
        self.alerted = False
        self._client = client
        self._lock = threading.Lock()

    @property
    def tag(self):
        """'[zona] ' gdy skonfigurowano kilka kont, żeby było wiadomo, czyje to powiadomienie."""
        return f"[{self.name}] " if self.multi else ""

    @property
    def runtime(self):
        return self.files["runtime"]

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                self._client = Luxmed(*accounts.credentials(self.cfg, self.name))
            return self._client


def notify(title, msg, buttons=None, tag=""):
    """buttons: lista (tekst, callback_data); każdy przycisk w osobnym wierszu."""
    title = tag + title
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
                [{"text": text, "callback_data": cb}] for text, cb in buttons]})
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


def reply(text):
    tg_api("sendMessage", chat_id=TG["chat_id"], text=text)


def load_pending(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_pending(path, pending):
    now = time.time()
    pending = {k: v for k, v in pending.items() if now - v["ts"] < PENDING_TTL}
    Path(path).write_text(json.dumps(pending, ensure_ascii=False))
    Path(path).chmod(0o600)


def add_pending(ctx, key, term, name, label):
    """Zapisuje ofertę rezerwacji konta i zwraca callback_data przycisku: book:<konto>:<id>."""
    book_id = hashlib.sha1(f"{ctx.name}|{key}".encode()).hexdigest()[:12]
    with PENDING_LOCK:
        pending = load_pending(ctx.files["pending"])
        pending[book_id] = {"term": term, "name": name, "label": label, "ts": time.time()}
        save_pending(ctx.files["pending"], pending)
    return f"book:{ctx.name}:{book_id}"


def can_book_now(cfg, ctx):
    return (bool(cfg.get("booking", {}).get("enabled")) and ctx.booking
            and bool(TG.get("bot_token") and TG.get("chat_id")))


def find_ctx(ctxs, name):
    return next((c for c in ctxs if c.name.lower() == str(name).strip().lower()), None)


def handle_callback(ctxs, cfg, cb):
    """Kliknięcie przycisku 'Zarezerwuj': rezerwuje na koncie, do którego należy oferta."""
    if str(cb.get("from", {}).get("id")) != str(TG.get("chat_id")):
        return  # tylko właściciel bota
    parts = cb.get("data", "").split(":")
    msg = cb.get("message") or {}
    chat, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
    if parts[0] != "book" or len(parts) not in (2, 3):
        return
    if len(parts) == 3:
        ctx, book_id = find_ctx(ctxs, parts[1]), parts[2]
    else:  # stary format book:<id> z wiadomości sprzed trybu wielu kont
        book_id = parts[1]
        ctx = next((c for c in ctxs if book_id in load_pending(c.files["pending"])), None)
    if ctx is None:
        tg_api("answerCallbackQuery", callback_query_id=cb["id"],
               text="Oferta wygasła albo dotyczy konta, które nie jest teraz aktywne.")
        return
    if not can_book_now(cfg, ctx):
        tg_api("answerCallbackQuery", callback_query_id=cb["id"],
               text=f"Rezerwacja przyciskiem jest wyłączona dla konta {ctx.name}.")
        return
    with PENDING_LOCK:  # jedno kliknięcie = jedna próba
        pending = load_pending(ctx.files["pending"])
        entry = pending.pop(book_id, None)
        save_pending(ctx.files["pending"], pending)
    if not entry:
        tg_api("answerCallbackQuery", callback_query_id=cb["id"], text="Oferta wygasła lub była już obsłużona.")
        return
    tg_api("answerCallbackQuery", callback_query_id=cb["id"], text=f"Rezerwuję ({ctx.name})…")
    summary = format_term(entry["term"], entry["name"])
    try:
        booking.book(ctx.client, entry["term"])
        result = f"ZAREZERWOWANO\n{summary}"
    except booking.BookingError as e:
        result = f"NIE ZAREZERWOWANO\n{summary}\n{e}"
    except (requests.RequestException, RuntimeError, KeyError, ValueError, SystemExit) as e:
        result = f"BŁĄD przy rezerwacji ({type(e).__name__}); sprawdź portal.\n{summary}"
    print("\n***", ctx.tag, result, "\n")
    if chat and mid:  # zdejmij przyciski i pokaż wynik w tej samej wiadomości
        tg_api("editMessageText", chat_id=chat, message_id=mid,
               text=f"{ctx.tag}Lux Med: {entry['label']}\n{result}")
    else:
        tg_api("sendMessage", chat_id=TG["chat_id"], text=f"{ctx.tag}{result}")


def telegram_listener(ctxs, cfg):
    tg_api("setMyCommands", commands=json.dumps(
        [{"command": c, "description": d} for c, d in control.COMMANDS], ensure_ascii=False))
    offset = None
    while True:
        try:
            params = {"timeout": 25, "allowed_updates": json.dumps(["callback_query", "message"])}
            if offset:
                params["offset"] = offset
            r = requests.get(f"https://api.telegram.org/bot{TG['bot_token']}/getUpdates",
                             params=params, timeout=40)
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                if u.get("callback_query"):
                    handle_callback(ctxs, cfg, u["callback_query"])
                elif u.get("message"):
                    handle_message(ctxs, cfg, u["message"])
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


def find_terms(ctx, cfg):
    """Wolne terminy pasujące do bloków [[watch]] konta: (blok, termin, nazwisko lekarza, klucz)."""
    horizon = cfg.get("horizon_days", 60)
    d_from = date.today().isoformat()
    d_to = (date.today() + timedelta(days=horizon)).isoformat()
    for w in ctx.blocks:
        if not control.block_enabled(w, ctx.runtime):
            continue  # enabled = false w configu albo /wylacz z Telegrama
        doctors = [d.lower() for d in w.get("doctors", [])]
        clinics = {str(c) for c in w.get("clinic_ids", [])}
        flt = rules.parse_filter(w)
        terms = ctx.client.terms(w["city_id"], w.get("city_name", ""), w["service_id"], d_from, d_to)
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


def collect_groups(ctx, cfg, seen=None):
    seen = ctx.seen if seen is None else seen
    groups = {}  # (blok, lekarz) -> [(termin, klucz)]
    claimed = set()  # ten sam termin może pasować do kilku bloków: wygrywa pierwszy z configu
    for w, t, name, key in find_terms(ctx, cfg):
        if key not in seen and key not in claimed:
            claimed.add(key)
            groups.setdefault((id(w), w.get("label", w["service_id"]), name), []).append((t, key))
    return groups


def send_groups(ctx, cfg, groups, seen=None, limit=None):
    can_book = can_book_now(cfg, ctx)
    for n, ((_, label, name), items) in enumerate(groups.items()):
        if limit is not None and n >= limit:
            break
        items.sort(key=lambda x: x[0]["dateTimeFrom"])
        items = items[:MAX_PER_MESSAGE]
        body, labels = rules.format_group(name, [t for t, _ in items])
        buttons = [(text, add_pending(ctx, key, t, name, label))
                   for text, (t, key) in zip(labels, items)] if can_book else None
        if seen is not None:
            seen.update(key for _, key in items)
        notify(f"Lux Med: {label}", body, buttons, tag=ctx.tag)


def check(ctx, cfg):
    if control.is_paused(ctx.runtime):
        return
    send_groups(ctx, cfg, collect_groups(ctx, cfg), ctx.seen)
    ctx.files["state"].write_text(json.dumps(sorted(ctx.seen)))


def run_check(ctx, cfg):
    """Jedno sprawdzenie konta; błąd jednego konta nie zatrzymuje pozostałych."""
    try:
        check(ctx, cfg)
        ctx.status.update(last_ok=time.time(), fails=0, last_error=None)
        if ctx.alerted:
            notify("Lux Med watcher: działa ponownie", "Nasłuch wrócił do normy.", tag=ctx.tag)
        ctx.alerted = False
    except (requests.RequestException, SystemExit, ValueError, KeyError, RuntimeError) as e:
        fails = ctx.status["fails"] + 1
        ctx.status.update(fails=fails, last_error=type(e).__name__)
        print(f"Błąd nasłuchu{' ' + ctx.name if ctx.multi else ''}:", type(e).__name__, e, file=sys.stderr)
        if fails >= 3 and not ctx.alerted:
            ctx.alerted = True
            notify("Lux Med watcher: PROBLEM",
                   f"{fails} nieudane sprawdzenia z rzędu ({type(e).__name__}). Nasłuch może nie działać.",
                   tag=ctx.tag)


def pick_targets(ctxs, arg):
    """Opcjonalna nazwa konta na początku argumentu: (konta, reszta, czy_wskazano_konto)."""
    first, _, rest = arg.partition(" ")
    ctx = find_ctx(ctxs, first) if first else None
    return ([ctx], rest.strip(), True) if ctx else (ctxs, arg, False)


def blocks_text(ctxs):
    multi = len(ctxs) > 1
    return "\n\n".join((f"{c.tag.strip()}\n" if multi else "") + control.describe_blocks(c.blocks, c.runtime)
                        for c in ctxs)


def status_text(ctxs, cfg):
    lines = [f"Nasłuch działa. Aktywne konta: {', '.join(c.name for c in ctxs)}. "
             f"Sprawdzam co ok. {cfg.get('interval_minutes', 5)} min."]
    for c in ctxs:
        lines.append("")
        if len(ctxs) > 1:
            lines.append(f"[{c.name}]")
        st = c.status
        if st["last_ok"]:
            ago = int((time.time() - st["last_ok"]) // 60)
            lines.append(f"Ostatnie udane sprawdzenie: {datetime.fromtimestamp(st['last_ok']):%H:%M:%S} ({ago} min temu)")
        else:
            lines.append("Od startu nie było jeszcze udanego sprawdzenia.")
        if st["fails"]:
            lines.append(f"Nieudane sprawdzenia z rzędu: {st['fails']} ({st['last_error']})")
        lines.append("Powiadomienia: WSTRZYMANE (/wznow)" if control.is_paused(c.runtime) else "Powiadomienia: włączone")
        lines.append("Rezerwacja przyciskiem: " + ("włączona" if can_book_now(cfg, c) else "wyłączona"))
        lines.append(control.describe_blocks(c.blocks, c.runtime))
    return "\n".join(lines)


def toggle_block(ctxs, arg, value):
    targets, phrase, _ = pick_targets(ctxs, arg)
    if not phrase:
        return "Podaj nazwę bloku, np. /wlacz derm\n\n" + blocks_text(targets)
    found = [(c, w) for c in targets for w in control.find_blocks(c.blocks, phrase)]
    if not found:
        return f"Nie znalazłem bloku pasującego do „{phrase}”.\n\n" + blocks_text(targets)
    if len(found) > 1:
        names = "\n".join(f"• {c.tag}{control.label_of(w)}" for c, w in found)
        hint = "\nPrzy kilku kontach dodaj nazwę konta, np. /wlacz zona derm." if len(ctxs) > 1 else ""
        return f"„{phrase}” pasuje do kilku bloków, doprecyzuj:\n{names}{hint}"
    c, w = found[0]
    control.set_block(w, value, c.runtime)
    WAKE.set()  # sprawdź od razu, nie czekaj na koniec interwału
    state = "WŁĄCZONY (sprawdzę terminy od razu)" if value else "wyłączony"
    return f"{c.tag}{control.label_of(w)}: {state}"


def switch_account(ctxs, cfg, arg):
    defined = list(accounts.list_accounts(cfg))
    active = [c.name for c in ctxs]
    status = f"Aktywne konta: {', '.join(active)}. Dostępne: {', '.join(defined)}."
    if not arg:
        return status + ("\nZmiana: /konto <nazwa> albo /konto wszystkie" if len(defined) > 1 else "")
    try:
        target = accounts.selected_names(cfg, cli=arg, environ={})
        for name in target:
            accounts.credentials(cfg, name)
            accounts.blocks_for(cfg, name)
    except accounts.AccountError as e:
        return f"Nie przełączam: {e}"
    if target == active:
        return f"Aktywne już są: {', '.join(active)}."
    accounts.set_selected("all" if target == defined and len(defined) > 1 else target[0] if len(target) == 1 else target)
    threading.Timer(1.5, os._exit, args=(0,)).start()  # launchd uruchomi nasłuch ponownie (ok. 30 s)
    return (f"Przełączam na: {', '.join(target)}. Nasłuch uruchomi się ponownie (ok. 30 s). "
            "Jeśli nie działa jako usługa, uruchom go ręcznie.")


def handle_message(ctxs, cfg, msg):
    """Komendy z Telegrama; tylko od właściciela bota."""
    if str(msg.get("from", {}).get("id")) != str(TG.get("chat_id")):
        return
    if time.time() - msg.get("date", 0) > COMMAND_MAX_AGE:
        return
    parsed = control.parse_command(msg.get("text"))
    if not parsed:
        return
    cmd, arg = parsed
    targets, rest, picked = pick_targets(ctxs, arg)
    if cmd in ("pauza", "wznow", "reset", "terminy") and arg and not picked:
        reply(f"Nieznane konto „{arg}”. Aktywne: {', '.join(c.name for c in ctxs)}.")
        return
    names = ", ".join(c.name for c in targets) if len(ctxs) > 1 else ""
    suffix = f" ({names})" if names else ""
    if cmd in ("start", "pomoc", "help"):
        reply(control.HELP)
    elif cmd == "status":
        reply(status_text(ctxs, cfg))
    elif cmd == "bloki":
        reply(blocks_text(ctxs))
    elif cmd == "konto":
        reply(switch_account(ctxs, cfg, arg))
    elif cmd in ("wlacz", "wylacz"):
        reply(toggle_block(ctxs, arg, cmd == "wlacz"))
    elif cmd == "pauza":
        for c in targets:
            control.set_paused(True, c.runtime)
        reply(f"Powiadomienia wstrzymane{suffix}. Wznowisz komendą /wznow. /terminy nadal działa.")
    elif cmd == "wznow":
        for c in targets:
            control.set_paused(False, c.runtime)
        WAKE.set()
        reply(f"Powiadomienia wznowione{suffix}, sprawdzam terminy.")
    elif cmd == "reset":
        for c in targets:
            control.reset(c.runtime)
        WAKE.set()
        reply(f"Przywrócono ustawienia z config.toml{suffix} (koniec pauzy i ręcznych przełączeń).")
    elif cmd == "terminy":
        sent = False
        for c in targets:
            try:
                groups = collect_groups(c, cfg, set())
            except (requests.RequestException, RuntimeError, KeyError, ValueError, SystemExit) as e:
                reply(f"{c.tag}Nie udało się pobrać terminów ({type(e).__name__}).")
                continue
            if groups:
                sent = True
                send_groups(c, cfg, groups, None, limit=6)
                if len(groups) > 6:
                    reply(f"{c.tag}…i {len(groups) - 6} kolejnych lekarzy. Zawęź listę blokami (/bloki).")
        if not sent:
            reply("Brak wolnych terminów dla włączonych bloków.")
    else:
        reply("Nieznana komenda. /pomoc pokaże listę.")


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    ap.add_argument("--account", help="konto z [accounts.*], kilka po przecinku albo 'wszystkie' "
                                      "(domyślnie: active_account z configu)")
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
    TG.update({"bot_token": cfg.get("telegram", {}).get("bot_token") or os.getenv("TELEGRAM_BOT_TOKEN"),
               "chat_id": cfg.get("telegram", {}).get("chat_id") or os.getenv("TELEGRAM_CHAT_ID")})
    if a.cmd == "telegram":
        telegram_setup()
        return
    single = a.cmd != "watch"  # discover, export i book-test działają na jednym koncie
    try:
        if not accounts.list_accounts(cfg) and os.getenv("LUXMED_LOGIN") and os.getenv("LUXMED_PASSWORD"):
            cfg["auth"] = {"login": os.getenv("LUXMED_LOGIN"), "password": os.getenv("LUXMED_PASSWORD")}
        names = [accounts.selected_name(cfg, cli=a.account)] if single else accounts.selected_names(cfg, cli=a.account)
        multi = len(accounts.list_accounts(cfg)) > 1
        ctxs = [Ctx(n, cfg, multi) for n in names]
        for c in ctxs:
            accounts.credentials(cfg, c.name)  # brak danych = błąd od razu, nie po godzinie
    except accounts.AccountError as ex:
        raise SystemExit(f"Konta: {ex}")
    if multi:
        print(f"Aktywne konta: {', '.join(names)}")

    if a.cmd == "discover":
        client = ctxs[0].client
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
        client = ctxs[0].client
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

    if not any(c.blocks for c in ctxs):
        raise SystemExit(f"Brak bloków [[watch]] dla aktywnych kont ({', '.join(names)}). "
                         "Dodaj blok, np. z account = \"nazwa\".")
    try:
        for c in ctxs:
            for w_ in c.blocks:
                rules.parse_filter(w_)
    except rules.ConfigError as ex:
        raise SystemExit(f"Błąd w config.toml: {ex}")

    if a.cmd == "book-test":
        ctx = ctxs[0]
        for w, t, name, key in find_terms(ctx, cfg):
            print("Testuję na terminie:\n" + format_term(t, name))
            try:
                print("Wynik:", booking.book(ctx.client, t, dry_run=True))
            except booking.BookingError as e:
                print("Odmowa:", e)
            return
        raise SystemExit("Brak wolnych terminów pasujących do configu: nie ma na czym testować.")

    if TG.get("bot_token") and TG.get("chat_id") and not a.once:
        if cfg.get("booking", {}).get("enabled"):
            print("Rezerwacja przyciskiem na Telegramie: WŁĄCZONA (dla kont bez booking = false)")
        threading.Thread(target=telegram_listener, args=(ctxs, cfg), daemon=True).start()
        print("Komendy z Telegrama: WŁĄCZONE (/pomoc)")
    interval = cfg.get("interval_minutes", 5)
    while True:
        for i, ctx in enumerate(ctxs):
            if i:
                time.sleep(random.uniform(2, 5))  # nie wysyłaj zapytań obu kont naraz
            run_check(ctx, cfg)
        if a.once:
            break
        WAKE.wait(interval * 60 * random.uniform(0.8, 1.2))  # budzik: /wlacz, /wznow, /reset
        WAKE.clear()


if __name__ == "__main__":
    main()
