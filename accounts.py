"""Wybór konta (Twoje / żony): dane logowania w jednym pliku, aktywne konto wskazuje zmienna.

config.toml:
    active_account = "marcin"

    [accounts.marcin]
    login = '...'
    password = '...'

    [accounts.zona]
    login = '...'
    password = '...'
    booking = false     # opcjonalnie: wyłącza przyciski rezerwacji dla tego konta

    [[watch]]
    account = "zona"    # blok dotyczy tylko tego konta (lista: ["marcin", "zona"]); brak = wszystkie konta

Które konto jest aktywne (od najważniejszego): --account, zmienna LUXMED_ACCOUNT,
komenda /konto z Telegrama (selected_account.json), active_account z configu, jedyne konto.
Stary zapis [auth] nadal działa jako konto "default".
"""
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).parent
SELECTED_FILE = HERE / "selected_account.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class AccountError(ValueError):
    pass


def list_accounts(cfg):
    """{nazwa: słownik z loginem/hasłem/opcjami}. Stary [auth] to konto 'default'."""
    accounts = cfg.get("accounts")
    if accounts:
        if not isinstance(accounts, dict):
            raise AccountError("accounts musi być tabelą: [accounts.nazwa]")
        for name, acc in accounts.items():
            if not _NAME_RE.match(name):
                raise AccountError(f"Nazwa konta {name!r}: dozwolone litery bez polskich znaków, cyfry, _ i -")
            if not isinstance(acc, dict):
                raise AccountError(f"[accounts.{name}] musi być tabelą z login i password")
        return dict(accounts)
    if cfg.get("auth"):
        return {"default": cfg["auth"]}
    return {}


def _match(names, wanted):
    for n in names:
        if n.lower() == str(wanted).strip().lower():
            return n
    raise AccountError(f"Nieznane konto {wanted!r}. Dostępne: {', '.join(names) or 'brak'}")


def read_selected():
    try:
        return json.loads(SELECTED_FILE.read_text()).get("account")
    except (FileNotFoundError, ValueError, AttributeError):
        return None


def set_selected(name):
    SELECTED_FILE.write_text(json.dumps({"account": name}))
    SELECTED_FILE.chmod(0o600)


def selected_name(cfg, cli=None, environ=None):
    """Nazwa aktywnego konta według kolejności pierwszeństwa opisanej na górze pliku."""
    environ = os.environ if environ is None else environ
    names = list(list_accounts(cfg))
    if not names:
        raise AccountError("Brak kont: dodaj [accounts.nazwa] z login i password w config.toml.")
    wanted = cli or environ.get("LUXMED_ACCOUNT") or read_selected() or cfg.get("active_account")
    if not wanted:
        if len(names) == 1:
            return names[0]
        raise AccountError(f"Jest kilka kont ({', '.join(names)}): ustaw active_account w config.toml.")
    return _match(names, wanted)


def credentials(cfg, name):
    acc = list_accounts(cfg)[name]
    login, password = acc.get("login"), acc.get("password")
    if not login or not password:
        where = "[auth]" if name == "default" else f"[accounts.{name}]"
        raise AccountError(f"Brak login lub password w {where} w config.toml.")
    return login, password


def booking_allowed(cfg, name):
    return bool(list_accounts(cfg)[name].get("booking", True))


def blocks_for(cfg, name):
    """Bloki [[watch]] dla konta; literówka w polu account to błąd (inaczej blok cicho nic by nie robił)."""
    names = list(list_accounts(cfg))
    out = []
    for w in cfg.get("watch", []):
        if "account" not in w:
            out.append(w)
            continue
        wanted = w["account"] if isinstance(w["account"], list) else [w["account"]]
        resolved = []
        for x in wanted:
            try:
                resolved.append(_match(names, x))
            except AccountError:
                label = w.get("label", w.get("service_id"))
                raise AccountError(f"Blok [{label}]: account = {x!r}, a konta to: {', '.join(names)}") from None
        if name in resolved:
            out.append(w)
    return out


def paths(name):
    """Pliki stanu danego konta. Konto 'default' zachowuje dawne nazwy plików."""
    if not _NAME_RE.match(name):
        raise AccountError(f"Niedozwolona nazwa konta: {name!r}")
    suffix = "" if name == "default" else f"-{name}"
    return {
        "state": HERE / f"state{suffix}.json",
        "pending": HERE / f"pending{suffix}.json",
        "runtime": HERE / f"runtime{suffix}.json",
    }
