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

Które konta są aktywne (od najważniejszego): --account, zmienna LUXMED_ACCOUNT,
komenda /konto z Telegrama (selected_account.json), active_account z configu, jedyne konto.
Wartość: nazwa konta ("zona"), kilka nazw ("marcin,zona" albo lista w configu) albo "wszystkie" / "all".
Aktywne konta są obsługiwane jednocześnie, każde z własnymi plikami stanu.
Stary zapis [auth] nadal działa jako konto "default".
"""
import json
import os
import re
import tempfile
import tomllib
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


ALL_WORDS = {"all", "wszystkie", "wszystko", "*"}


def read_selected():
    try:
        return json.loads(SELECTED_FILE.read_text()).get("account")
    except (FileNotFoundError, ValueError, AttributeError):
        return None


def set_selected(value):
    """value: nazwa konta, lista nazw albo "all"."""
    SELECTED_FILE.write_text(json.dumps({"account": value}))
    SELECTED_FILE.chmod(0o600)


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [p for p in str(value).replace(";", ",").split(",") if p.strip()]


def selected_names(cfg, cli=None, environ=None):
    """Lista aktywnych kont (w kolejności z configu), według pierwszeństwa opisanej na górze pliku."""
    environ = os.environ if environ is None else environ
    names = list(list_accounts(cfg))
    if not names:
        raise AccountError("Brak kont: dodaj [accounts.nazwa] z login i password w config.toml.")
    wanted = cli or environ.get("LUXMED_ACCOUNT") or read_selected() or cfg.get("active_account")
    if not wanted:
        if len(names) == 1:
            return names
        raise AccountError(f"Jest kilka kont ({', '.join(names)}): ustaw active_account w config.toml "
                           "(nazwa, lista nazw albo \"wszystkie\").")
    parts = _as_list(wanted)
    if any(p.strip().lower() in ALL_WORDS for p in parts):
        return names
    chosen = {_match(names, p) for p in parts}
    return [n for n in names if n in chosen]


def selected_name(cfg, cli=None, environ=None):
    """Jedno aktywne konto (dla poleceń działających na jednym koncie: discover, export, book-test)."""
    chosen = selected_names(cfg, cli=cli, environ=environ)
    if len(chosen) > 1:
        raise AccountError(f"To polecenie działa na jednym koncie, a aktywne są: {', '.join(chosen)}. "
                           "Podaj --account nazwa.")
    return chosen[0]


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


def _toml_str(value):
    """Zapis tekstu w TOML: apostrofy (tekst dosłowny), a przy apostrofie w treści cudzysłów z escapowaniem."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value)  # poprawny TOML basic string


def set_credentials(config_path, name, login, password):
    """Wpisuje login i hasło do [accounts.<name>] w config.toml (tworzy sekcję, gdy jej nie ma).

    Reszta pliku zostaje bez zmian. Wynik jest sprawdzany parserem TOML przed zapisem.
    """
    if not _NAME_RE.match(name):
        raise AccountError(f"Niedozwolona nazwa konta: {name!r} (litery bez polskich znaków, cyfry, _ i -)")
    if not login or not password:
        raise AccountError("Login i hasło nie mogą być puste.")
    path = Path(config_path)
    lines = path.read_text().split("\n") if path.exists() else []
    header = f"[accounts.{name}]"
    new = {"login": f"login = {_toml_str(login)}", "password": f"password = {_toml_str(password)}"}
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        block = [header, new["login"], new["password"], ""]
        anchor = next((i for i, l in enumerate(lines) if l.strip() in ("[telegram]", "[booking]", "[[watch]]")),
                      len(lines))
        lines[anchor:anchor] = block
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        for key in ("login", "password"):
            idx = next((i for i in range(start + 1, end) if re.match(rf"\s*{key}\s*=", lines[i])), None)
            if idx is None:
                lines.insert(start + 1, new[key])
                end += 1
            else:
                lines[idx] = new[key]
    text = "\n".join(lines)
    try:
        parsed = tomllib.loads(text)
        got = parsed["accounts"][name]
        assert got["login"] == login and got["password"] == password
    except (tomllib.TOMLDecodeError, KeyError, AssertionError):
        raise AccountError("Nie udało się bezpiecznie zapisać danych (wynik nie przeszedł kontroli). Config bez zmian.") from None
    fd, tmp = tempfile.mkstemp(dir=path.parent if path.exists() else ".", prefix=".config-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
