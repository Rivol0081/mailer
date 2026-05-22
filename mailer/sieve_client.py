"""ManageSieve client wrapper.

We use sievelib (synchronous), wrapped in asyncio.to_thread so callers stay
async-friendly. ManageSieve is not universally supported (Gmail/Outlook
don't expose it); the bot falls back to client-side filtering when this
module raises.
"""
from __future__ import annotations

import asyncio


SCRIPT_NAME = "mailer_keywords"


def _build_script(keywords: list[str], action: str) -> str:
    """Build a Sieve script that handles the keyword list.

    action:
      "trash"  -> fileinto "Trash"
      "delete" -> discard
      "flag"   -> setflag "\\Flagged" (requires imap4flags extension)
    """
    if not keywords:
        return '# mailer: no keywords\nrequire [];\n'

    extensions = ["fileinto", "body"]
    if action == "flag":
        extensions.append("imap4flags")

    quoted = [k.replace("\\", "\\\\").replace('"', '\\"') for k in keywords]
    header_match = ",".join(f'"{k}"' for k in quoted)

    if action == "delete":
        action_block = "    discard;\n    stop;"
    elif action == "flag":
        action_block = '    setflag "\\\\Flagged";\n    stop;'
    else:  # trash
        action_block = '    fileinto "Trash";\n    stop;'

    ext_list = ", ".join('"' + e + '"' for e in extensions)
    return (
        f"require [{ext_list}];\n\n"
        f"if anyof (\n"
        f'    header :contains "Subject" [{header_match}],\n'
        f"    body :text :contains [{header_match}]\n"
        f") {{\n"
        f"{action_block}\n"
        f"}}\n"
    )


def _sync_install(host: str, port: int, user: str, password: str, script: str) -> None:
    from sievelib.managesieve import Client

    c = Client(host, port=port, debug=False)
    try:
        ok = c.connect(user, password, starttls=True, authmech="PLAIN")
        if not ok:
            # Try LOGIN mechanism
            ok = c.connect(user, password, starttls=True, authmech="LOGIN")
        if not ok:
            raise ConnectionError(f"ManageSieve auth failed for {user}@{host}")
        c.putscript(SCRIPT_NAME, script)
        c.setactive(SCRIPT_NAME)
    finally:
        try:
            c.logout()
        except Exception:
            pass


def _sync_remove(host: str, port: int, user: str, password: str) -> None:
    from sievelib.managesieve import Client

    c = Client(host, port=port, debug=False)
    try:
        ok = c.connect(user, password, starttls=True, authmech="PLAIN")
        if not ok:
            ok = c.connect(user, password, starttls=True, authmech="LOGIN")
        if not ok:
            raise ConnectionError(f"ManageSieve auth failed for {user}@{host}")
        try:
            c.setactive("")  # deactivate
        except Exception:
            pass
        try:
            c.deletescript(SCRIPT_NAME)
        except Exception:
            pass
    finally:
        try:
            c.logout()
        except Exception:
            pass


async def install_filter(
    host: str, port: int, user: str, password: str,
    keywords: list[str], action: str,
) -> None:
    script = _build_script(keywords, action)
    await asyncio.to_thread(_sync_install, host, port, user, password, script)


async def remove_filter(host: str, port: int, user: str, password: str) -> None:
    await asyncio.to_thread(_sync_remove, host, port, user, password)


async def probe(host: str, port: int, user: str, password: str) -> bool:
    """Return True if ManageSieve is reachable and we can authenticate."""
    def _check() -> bool:
        from sievelib.managesieve import Client
        c = Client(host, port=port, debug=False)
        try:
            return bool(c.connect(user, password, starttls=True, authmech="PLAIN"))
        except Exception:
            return False
        finally:
            try:
                c.logout()
            except Exception:
                pass
    return await asyncio.to_thread(_check)
