"""Sterowanie nasłuchem w trakcie działania (komendy z Telegrama): pauza i włączanie bloków [[watch]].

Zmiany trafiają do runtime.json, a nie do config.toml, więc nie dotykamy pliku z hasłem.
Ustawienie z Telegrama ma pierwszeństwo przed `enabled` z configu.
"""
import json
import threading
import unicodedata
from pathlib import Path

RUNTIME_FILE = Path(__file__).with_name("runtime.json")
_LOCK = threading.Lock()

HELP = """Komendy:
/status - czy działa i kiedy było ostatnie sprawdzenie
/terminy [konto] - aktualne wolne terminy (z przyciskami)
/bloki - lista obserwowanych bloków i ich stan
/wlacz [konto] <nazwa> - włącz blok, np. /wlacz zona derm
/wylacz [konto] <nazwa> - wyłącz blok
/pauza [konto] - wstrzymaj powiadomienia (bez konta: wszystkie)
/wznow [konto] - wznów powiadomienia
/konto - pokaż aktywne konta; /konto zona, /konto wszystkie
/reset [konto] - wróć do ustawień z config.toml

Przy kilku kontach nazwę konta podajesz przed frazą, np. /wlacz zona derm."""

COMMANDS = [  # do menu Telegrama (tylko małe litery łacińskie, cyfry i _)
    ("status", "Czy działa i kiedy ostatnie sprawdzenie"),
    ("terminy", "Aktualne wolne terminy"),
    ("bloki", "Obserwowane bloki i ich stan"),
    ("wlacz", "Włącz blok: /wlacz nazwa"),
    ("wylacz", "Wyłącz blok: /wylacz nazwa"),
    ("pauza", "Wstrzymaj powiadomienia"),
    ("wznow", "Wznów powiadomienia"),
    ("konto", "Pokaż lub przełącz konto"),
    ("reset", "Wróć do ustawień z configu"),
    ("pomoc", "Lista komend"),
]


def _ascii(text):
    text = text.lower().replace("ł", "l")  # ł nie rozkłada się przy NFD
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _path(path):
    """Plik stanu danego konta; bez argumentu: RUNTIME_FILE (jedno konto / testy)."""
    return Path(path) if path else RUNTIME_FILE


def _load(path=None):
    try:
        return json.loads(_path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _save(data, path=None):
    f = _path(path)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    f.chmod(0o600)


def label_of(w):
    return str(w.get("label", w.get("service_id")))


def is_paused(path=None):
    return bool(_load(path).get("paused"))


def set_paused(value, path=None):
    with _LOCK:
        data = _load(path)
        data["paused"] = bool(value)
        _save(data, path)


def block_enabled(w, path=None):
    override = _load(path).get("blocks", {}).get(label_of(w))
    return bool(w.get("enabled", True)) if override is None else bool(override)


def set_block(w, value, path=None):
    with _LOCK:
        data = _load(path)
        data.setdefault("blocks", {})[label_of(w)] = bool(value)
        _save(data, path)


def reset(path=None):
    with _LOCK:
        _save({}, path)


def find_blocks(blocks, query):
    """Bloki pasujące do frazy: dokładne dopasowanie etykiety wygrywa, inaczej podciąg (bez polskich znaków)."""
    q = _ascii(query.strip())
    if not q:
        return []
    exact = [w for w in blocks if _ascii(label_of(w)) == q]
    return exact or [w for w in blocks if q in _ascii(label_of(w))]


def describe_blocks(blocks, path=None):
    lines = []
    overrides = _load(path).get("blocks", {})
    for w in blocks:
        mark = "WŁĄCZONY" if block_enabled(w, path) else "wyłączony"
        note = " (ustawione z Telegrama)" if label_of(w) in overrides else ""
        docs = ", ".join(d.title() for d in w.get("doctors", [])) or "dowolny lekarz"
        lines.append(f"• {label_of(w)}: {mark}{note}\n   {docs}")
    return "\n".join(lines) or "Brak bloków."


def parse_command(text):
    """'/Wlacz@bot derm' -> ('wlacz', 'derm'); zwykły tekst -> None. Polskie znaki w komendzie są OK."""
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, arg = text[1:].partition(" ")
    return _ascii(head.split("@")[0]), arg.strip()
