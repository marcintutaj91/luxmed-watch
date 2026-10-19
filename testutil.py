"""Pomocnik testów: kontekst konta z plikami stanu w bieżącym katalogu (sprzątane po teście)."""
from pathlib import Path

import luxmed_watch as lw


def make_ctx(test, name="marcin", multi=False, client=None, blocks=None, booking=True, others=()):
    """Ctx konta `name` z atrapą klienta; pliki stanu: <test>.<konto>.<rodzaj>.json."""
    accounts_cfg = {name: {"login": "l", "password": "p", "booking": booking}}
    accounts_cfg.update({o: {"login": "l", "password": "p"} for o in others})
    watch = [dict(b, account=name) if "account" not in b else b for b in (blocks or [])]
    cfg = {"accounts": accounts_cfg, "watch": watch}
    ctx = lw.Ctx(name, cfg, multi or bool(others), client=client)
    ctx.files = {k: Path(f"{test._testMethodName}.{name}.{k}.json") for k in ("state", "pending", "runtime")}
    for f in ctx.files.values():
        test.addCleanup(lambda f=f: f.unlink(missing_ok=True))
    ctx.seen = set()
    return ctx
