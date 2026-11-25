"""redtooth command line.

  redtooth demo                      simulated world, real transfers (open the printed URL)
  redtooth setup                     register your profile (phone number) with a directory
  redtooth serve                     run on this computer with its real Bluetooth
  redtooth panel                     open the compact panel window
  redtooth share FILE                open the panel with FILE selected (share-sheet / Send To hook)
  redtooth ask "connect to Daniel's headphones"     one-shot request to a running instance
  redtooth models fetch              download multilingual-E5 for semantic search
  redtooth eval [--calibrate --save] score file search on the labeled test set (or your own)
  redtooth directory --port 8790     host a profile directory for your group
  redtooth install-integrations      add Send To / context-menu / .desktop entries
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

HOME = Path(os.environ.get("REDTOOTH_HOME", Path.home() / ".redtooth"))
CONFIG = HOME / "config.json"
DEFAULT_PORT = 8787
TRANSFER_PORT = 47800


def load_config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG, 0o600)


def lan_addresses() -> list[str]:
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packets sent; picks the outbound interface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except OSError:
        pass
    return sorted(addrs)


def open_panel(url: str) -> None:
    """A compact app window when Chrome/Edge is present, else the default browser."""
    for exe in ("google-chrome", "chromium", "microsoft-edge", "msedge", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        path = shutil.which(exe) or (exe if os.path.exists(exe) else None)
        if path:
            subprocess.Popen([path, f"--app={url}", "--window-size=420,760"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


def post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Redtooth": "1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def _run_app(app, port: int, open_ui: bool) -> None:
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Redtooth panel: {url}", flush=True)
    if open_ui:
        open_panel(url)
    await asyncio.Event().wait()


async def cmd_demo(args) -> None:
    from .demo import build_demo
    from .server import make_app

    demo = await build_demo(args.root, embedder=args.embedder)
    print(f"Demo files: {demo.root}\nSearch: {demo.agent.index.embedder.name}", flush=True)
    await _run_app(make_app(demo.agent, demo=demo), args.port, args.open)


async def cmd_serve(args) -> None:
    from .agent import Agent
    from .bt.base import detect_platform
    from .contacts import ContactBook
    from .directory import HTTPDirectory, InMemoryDirectory, directory_routes
    from .files.embed import load_embedder
    from .files.index import FileIndex, default_roots
    from .models import Contact, Device, Endpoint, Person
    from .notify import AcceptBroker
    from .server import make_app, wire_incoming
    from .store import Store
    from .transfer.receiver import Receiver

    cfg = load_config()
    if not cfg.get("phone"):
        sys.exit("Run `redtooth setup` first.")
    extra = None
    if args.host_directory:
        state = InMemoryDirectory()
        directory = state
        extra = directory_routes(state, allow_open_register=False)
        await state.register(cfg["phone"], cfg["name"])
    else:
        directory = HTTPDirectory(cfg["directory"], cfg.get("token"))

    book = ContactBook()
    if cfg.get("contacts"):
        p = Path(cfg["contacts"]).expanduser()
        book = ContactBook.from_vcard(p) if p.suffix == ".vcf" else ContactBook.from_json(p)
    elif sys.platform == "darwin":
        book = ContactBook.from_macos()
    me = Person(Contact("c_me", cfg["name"], [cfg["phone"]]), is_self=True)
    roots = [Path(r).expanduser() for r in cfg.get("roots", [])] or default_roots()
    index = FileIndex(HOME / "index.db", load_embedder(args.embedder))
    print(f"Search: {index.embedder.name}", flush=True)
    store = Store(HOME / "state.db")
    agent = Agent(me, book, directory, detect_platform(), index, store)
    agent.shared_devices = [Device.from_dict(d) for d in cfg.get("shared_devices", [])]

    def persist_shared() -> None:
        cfg["shared_devices"] = [d.to_dict() for d in agent.shared_devices]
        save_config(cfg)

    agent.persist_shared = persist_shared

    # Always listening for incoming files; accept from a notification or the panel.
    broker = AcceptBroker(native=True, auto_accept=lambda o: o.sender_phone == cfg["phone"])
    this = Device(cfg.setdefault("device_id", f"dev_{socket.gethostname()}"), cfg.get("device_name") or socket.gethostname(), cfg.get("device_kind", "laptop"), has_app=True, wifi_direct=agent.bt.wifi_direct)
    rx = Receiver(this, Path(cfg.get("inbox", Path.home() / "Downloads")).expanduser(), broker.ask, wire_incoming(agent))
    port = await rx.start("0.0.0.0", args.transfer_port)
    this.endpoints = [Endpoint("lan", ip, port) for ip in lan_addresses()]
    save_config(cfg)

    async def heartbeat() -> None:
        while True:
            try:
                this.endpoints = [Endpoint("lan", ip, port) for ip in lan_addresses()]
                await directory.set_devices(cfg["phone"], [this] + agent.shared_devices)
                await agent.refresh_presence(scan=True)
                if isinstance(directory, HTTPDirectory):
                    for msg in await directory.inbox():
                        agent.bus.publish("nudge", msg)
            except Exception as e:
                logging.warning("heartbeat: %s", e)
            await asyncio.sleep(20)

    # Indexing runs in its own thread: recent files first, then the rest, then it watches for changes.
    from .files.indexer import Indexer

    loop = asyncio.get_running_loop()
    indexer = Indexer(index, roots, on_status=lambda s: loop.call_soon_threadsafe(agent.bus.publish, "index", s))
    agent.indexer = indexer
    indexer.start()

    asyncio.create_task(heartbeat())
    await _run_app(make_app(agent, broker=broker, extra_routes=extra), args.port, args.open)


async def cmd_directory(args) -> None:
    from aiohttp import web

    from .directory import InMemoryDirectory, directory_routes

    app = web.Application()
    app.add_routes(directory_routes(InMemoryDirectory(), allow_open_register=args.open_register))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    print(f"Directory on http://{args.host}:{args.port}  (codes are logged here until SMS is configured)", flush=True)
    await asyncio.Event().wait()


async def cmd_setup(args) -> None:
    from .directory import HTTPDirectory
    from .util import normalize_phone

    cfg = load_config()
    cfg["name"] = args.name or input("Your name: ").strip()
    cfg["phone"] = normalize_phone(args.phone or input("Your phone number: ").strip())
    cfg["directory"] = args.directory
    d = HTTPDirectory(args.directory)
    await d.start_verification(cfg["phone"])
    code = args.code or input("Verification code: ").strip()
    cfg["token"] = await d.verify(cfg["phone"], code, cfg["name"])
    if args.contacts:
        cfg["contacts"] = args.contacts
    save_config(cfg)
    print(f"Registered {cfg['name']} ({cfg['phone']}).")


def cmd_share(args) -> None:
    path = str(Path(args.file).resolve())
    try:
        post(args.port, "/api/select", {"path": path})
        open_panel(f"http://127.0.0.1:{args.port}/")
    except OSError:
        sys.exit("Redtooth isn't running. Start it with `redtooth serve`.")


def cmd_ask(args) -> None:
    r = post(args.port, "/api/ask", {"text": " ".join(args.text)})
    print(r["text"])
    if r.get("question"):
        for i, o in enumerate(r["question"]["options"], 1):
            print(f"  {i}. {o['label']}  {o['detail']}")


def cmd_install(args) -> None:
    exe = shutil.which("redtooth") or f'"{sys.executable}" -m redtooth.cli'
    if sys.platform.startswith("win"):
        sendto = Path(os.environ["APPDATA"]) / "Microsoft/Windows/SendTo/Redtooth.cmd"
        sendto.write_text(f'@echo off\r\n{exe} share %1\r\n')
        reg = HOME / "redtooth-context-menu.reg"
        cmd = exe.replace("\\", "\\\\").replace('"', '\\"')
        reg.write_text(
            "Windows Registry Editor Version 5.00\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\Classes\\*\\shell\\Redtooth]\r\n@=\"Send with Redtooth\"\r\n\r\n"
            f"[HKEY_CURRENT_USER\\Software\\Classes\\*\\shell\\Redtooth\\command]\r\n@=\"{cmd} share \\\"%1\\\"\"\r\n"
        )
        subprocess.run(["reg", "import", str(reg)], check=False)
        print(f"Added Send To › Redtooth and a 'Send with Redtooth' context-menu entry.")
    elif sys.platform == "darwin":
        print("Add a Quick Action: open Shortcuts › New Shortcut › 'Receive Files from Quick Actions / Share Sheet', "
              f"then 'Run Shell Script' with:  {exe} share \"$@\"  (pass input as arguments). See integrations/macos/README.md.")
    else:
        apps = Path.home() / ".local/share/applications"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "redtooth-share.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=Send with Redtooth\nExec={exe} share %f\nMimeType=application/octet-stream;application/pdf;"
            "application/vnd.openxmlformats-officedocument.presentationml.presentation;application/vnd.openxmlformats-officedocument.wordprocessingml.document;image/png;image/jpeg;video/mp4;text/plain;\n"
            "NoDisplay=true\nIcon=bluetooth\n"
        )
        (apps / "redtooth.desktop").write_text(f"[Desktop Entry]\nType=Application\nName=Redtooth\nExec={exe} panel\nIcon=bluetooth\nCategories=Utility;\n")
        nscripts = Path.home() / ".local/share/nautilus/scripts"
        nscripts.mkdir(parents=True, exist_ok=True)
        s = nscripts / "Send with Redtooth"
        s.write_text(f'#!/bin/sh\nfor f in "$@"; do {exe} share "$f"; done\n')
        s.chmod(0o755)
        print("Added 'Send with Redtooth' to Open With and the Files (Nautilus) Scripts menu, plus a Redtooth app launcher.")


def main(argv=None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["eval"]:
        from .search_eval.run import run

        run(argv[1:])
        return
    ap = argparse.ArgumentParser(prog="redtooth", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--root")
    p.add_argument("--embedder", default=None, choices=["auto", "e5", "hash"])
    p.add_argument("--open", action="store_true")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--transfer-port", type=int, default=TRANSFER_PORT)
    p.add_argument("--host-directory", action="store_true")
    p.add_argument("--embedder", default=None, choices=["auto", "e5", "hash"], help="search model (default: E5, downloaded on first run)")
    p.add_argument("--open", action="store_true")
    p = sub.add_parser("setup")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.add_argument("--code")
    p.add_argument("--contacts", help="contacts .json or .vcf")
    p.add_argument("--directory", default=os.environ.get("REDTOOTH_DIRECTORY", "http://127.0.0.1:8790"))
    p = sub.add_parser("panel")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("share")
    p.add_argument("file")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("ask")
    p.add_argument("text", nargs="+")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("models")
    p.add_argument("action", choices=["fetch"])
    p = sub.add_parser("directory")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--open-register", action="store_true", help="skip phone verification (testing only)")
    sub.add_parser("install-integrations")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        asyncio.run(cmd_demo(args))
    elif args.cmd == "serve":
        asyncio.run(cmd_serve(args))
    elif args.cmd == "setup":
        asyncio.run(cmd_setup(args))
    elif args.cmd == "directory":
        asyncio.run(cmd_directory(args))
    elif args.cmd == "panel":
        open_panel(f"http://127.0.0.1:{args.port}/")
    elif args.cmd == "share":
        cmd_share(args)
    elif args.cmd == "ask":
        cmd_ask(args)
    elif args.cmd == "models":
        from .files.embed import fetch_model

        print(f"Model ready in {fetch_model()}")
    elif args.cmd == "install-integrations":
        cmd_install(args)


if __name__ == "__main__":
    main()
