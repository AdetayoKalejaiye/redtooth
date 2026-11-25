"""Let the recipient accept from a notification where the OS supports actions.

Linux: libnotify actions (`notify-send --action … --wait`).
macOS: `alerter` (brew install vjeantet/tap/alerter) for actionable
notifications; otherwise a small dialog via osascript.
Windows: `win11toast` toast buttons if installed.
The panel's incoming card always offers Accept / Decline too; whichever
answer comes first wins.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from typing import Callable, Optional

from .bt.base import run
from .transfer.receiver import Offer
from .util import human_bytes


class AcceptBroker:
    def __init__(self, native: bool = True, auto_accept: Optional[Callable[[Offer], bool]] = None):
        self.native = native
        self.auto_accept = auto_accept or (lambda o: False)
        self.pending: dict[str, asyncio.Future] = {}

    def resolve(self, offer_id: str, accept: bool) -> bool:
        fut = self.pending.get(offer_id)
        if fut and not fut.done():
            fut.set_result(accept)
            return True
        return False

    async def ask(self, offer: Offer) -> bool:
        if self.auto_accept(offer):
            return True
        fut = asyncio.get_running_loop().create_future()
        self.pending[offer.id] = fut
        native = asyncio.create_task(self._native(offer)) if self.native else None
        if native:

            def _done(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception() is None and t.result() is not None and not fut.done():
                    fut.set_result(t.result())

            native.add_done_callback(_done)
        try:
            return await fut
        finally:
            self.pending.pop(offer.id, None)
            if native and not native.done():
                native.cancel()

    async def _native(self, offer: Offer) -> Optional[bool]:
        title = f"{offer.sender_name} wants to send you a file"
        body = f"{offer.name} · {human_bytes(offer.size)}"
        try:
            if sys.platform.startswith("linux") and shutil.which("notify-send"):
                _, out = await run("notify-send", "-a", "Redtooth", "--action=accept=Accept", "--action=decline=Decline", "--wait", title, body, timeout=120)
                return {"accept": True, "decline": False}.get(out.strip())
            if sys.platform == "darwin":
                if shutil.which("alerter"):
                    _, out = await run("alerter", "-title", title, "-message", body, "-actions", "Accept", "-closeLabel", "Decline", "-timeout", "120", timeout=125)
                    return {"Accept": True, "Decline": False, "@CLOSED": False}.get(out.strip())
                script = f'display dialog "{body}" with title "{title}" buttons {{"Decline", "Accept"}} default button "Accept" giving up after 120'
                _, out = await run("osascript", "-e", script, timeout=125)
                return True if "Accept" in out else (False if "Decline" in out else None)
            if sys.platform.startswith("win"):
                try:
                    from win11toast import toast_async  # type: ignore

                    res = await toast_async(title, body, buttons=["Accept", "Decline"], app_id="Redtooth")
                    arg = (res or {}).get("arguments", "")
                    return True if "Accept" in arg else (False if "Decline" in arg else None)
                except ImportError:
                    return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return None
