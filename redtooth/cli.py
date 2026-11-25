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
import time
import urllib.request
from typing import Optional
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
    def rank(ip: str) -> tuple:
        o = [int(x) for x in ip.split(".")] if ip.count(".") == 3 else [0, 0, 0, 0]
        private = o[0] == 192 and o[1] == 168 or o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31)
        return (not private, o[0] != 192, ip)  # home Wi-Fi (192.168.x) first, VPN/odd ranges last

    return sorted(addrs, key=rank)


def open_panel(url: str, view: Optional[str] = None) -> None:
    from .desktop import open_panel as _open

    _open(url, HOME, view)


def post(port: int, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "X-Redtooth": "1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _add_routes(app, routes):
    app.add_routes(routes)
    return app


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


async def cmd_serve(args, set_status=None) -> None:
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
    from .phones import country_for_phone, detect_country, set_country

    set_country(cfg.get("country") or country_for_phone(cfg["phone"], prefer=detect_country()) or detect_country())
    # File-menu entry points are part of the product: add them once, quietly (no admin needed).
    if not args.no_integrations and cfg.get("integrations") not in ("installed", "removed"):
        try:
            cmd_install(args)
            cfg["integrations"] = "installed"
            save_config(cfg)
        except Exception as e:
            logging.warning("couldn't add file-menu entries: %s", e)
    extra = None
    hosted: Optional[InMemoryDirectory] = None
    if cfg.get("host_directory") or args.host_directory:
        # This computer is the group's directory: same process, its own LAN port, kept on disk.
        from aiohttp import web as _web

        hosted = InMemoryDirectory(HOME / "directory.json")
        if not await hosted.lookup([cfg["phone"]]):
            await hosted.register(cfg["phone"], cfg["name"])
        directory = hosted
        dport = int(cfg.get("directory_port", 8790))
        drunner = _web.AppRunner(_add_routes(_web.Application(), directory_routes(hosted, allow_open_register=False)))
        await drunner.setup()
        try:
            await _web.TCPSite(drunner, "0.0.0.0", dport).start()
        except OSError as e:
            logging.warning("couldn't open the directory on port %s (%s); is `redtooth directory` already running?", dport, e)
    else:
        directory = HTTPDirectory(cfg["directory"], cfg.get("token"))

    from .contacts import ContactStore
    from .server import ContactsService

    extra_contacts = cfg.get("contacts")
    extra_contacts = [extra_contacts] if isinstance(extra_contacts, str) else (extra_contacts or [])
    book = ContactBook()
    me = Person(Contact("c_me", cfg["name"], [cfg["phone"]]), is_self=True)
    roots = [Path(r).expanduser() for r in cfg.get("roots", [])] or default_roots()
    index = FileIndex(HOME / "index.db", load_embedder(args.embedder))
    print(f"Search: {index.embedder.name}", flush=True)
    store = Store(HOME / "state.db")
    agent = Agent(me, book, directory, detect_platform(), index, store)
    agent.shared_devices = [Device.from_dict(d) for d in cfg.get("shared_devices", [])]
    contacts = ContactsService(agent, ContactStore(HOME / "contacts.json"), extra_contacts)

    def save_country(iso: str) -> None:
        cfg["country"] = iso
        save_config(cfg)

    contacts.on_country = save_country
    await asyncio.get_running_loop().run_in_executor(None, contacts.load)
    print(f"Contacts: {len(agent.book.contacts)}" + ("" if agent.book.contacts else " (add them in the panel)"), flush=True)

    def persist_shared() -> None:
        cfg["shared_devices"] = [d.to_dict() for d in agent.shared_devices]
        save_config(cfg)

    agent.persist_shared = persist_shared

    async def publish_devices() -> None:
        try:
            await directory.set_devices(cfg["phone"], [this] + agent.shared_devices)
        except Exception as e:
            logging.warning("couldn't update the directory: %s", e)

    agent.publish_devices = publish_devices

    # Always listening for incoming files; accept from a notification or the panel.
    broker = AcceptBroker(native=True, auto_accept=lambda o: o.sender_phone == cfg["phone"])
    this = Device(cfg.setdefault("device_id", f"dev_{socket.gethostname()}"), cfg.get("device_name") or socket.gethostname(), cfg.get("device_kind", "laptop"), has_app=True, wifi_direct=agent.bt.wifi_direct)
    rx = Receiver(this, Path(cfg.get("inbox", Path.home() / "Downloads")).expanduser(), broker.ask, wire_incoming(agent, contacts))
    port = await rx.start("0.0.0.0", args.transfer_port)
    this.endpoints = [Endpoint("lan", ip, port) for ip in lan_addresses()]
    save_config(cfg)

    async def heartbeat() -> None:
        while True:
            try:
                this.endpoints = [Endpoint("lan", ip, port) for ip in lan_addresses()]
                await directory.set_devices(cfg["phone"], [this] + agent.shared_devices)
                await agent.refresh_presence(scan=False)  # no background Bluetooth inquiry: it makes audio stutter
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

    if hosted is not None:
        def show_code(phone: str, code: str) -> None:
            text = f"Verification code for {phone}: {code}. Type it on the device that's being set up."
            logging.warning(text)
            loop.call_soon_threadsafe(agent.bus.publish, "reply", {"ok": True, "text": text, "timings": {}})

        hosted.code_sink = show_code

    def app_info() -> dict:
        ip = (lan_addresses() or ["127.0.0.1"])[0]
        return {"directory_hosted": hosted is not None,
                "directory_url": f"http://{ip}:{cfg.get('directory_port', 8790)}" if hosted is not None else cfg.get("directory"),
                "name": cfg.get("name"), "phone": cfg.get("phone"), "version": "0.2"}

    async def tray_status() -> None:
        """Keep the tray tooltip/menu line current: what's playing, what's happening."""
        while set_status:
            try:
                out = await agent.bt.audio_output()
                busy = agent.busy if getattr(agent, "busy", "") in ("Connecting", "Searching", "Scanning") else ""
                set_status(busy or f"Playing through {out.name}", "blue" if busy else ("green" if out.transport == "bluetooth" else None))
            except Exception:
                pass
            await asyncio.sleep(4)

    asyncio.create_task(heartbeat())
    asyncio.create_task(tray_status())
    await _run_app(make_app(agent, broker=broker, extra_routes=extra, contacts=contacts, app_info=app_info), args.port, args.open and not set_status)


async def cmd_directory(args) -> None:
    from aiohttp import web

    from .directory import InMemoryDirectory, directory_routes

    app = web.Application()
    data = Path(args.data).expanduser() if args.data else HOME / "directory.json"
    app.add_routes(directory_routes(InMemoryDirectory(data), allow_open_register=args.open_register))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    print(f"Directory on http://{args.host}:{args.port} · registrations kept in {data}\nVerification codes appear in this window (SMS isn't set up).", flush=True)
    await asyncio.Event().wait()


async def cmd_setup(args) -> None:
    from .directory import HTTPDirectory, InMemoryDirectory
    from .phones import BY_ISO, country_for_phone, detect_country, flag, parse_country, set_country
    from .util import normalize_phone

    cfg = load_config()
    print("Redtooth setup. Press Enter to accept the suggestion in [brackets].\n")
    prev = cfg.get("name", "")
    cfg["name"] = args.name or input(f"Your name [{prev}]: " if prev else "Your name: ").strip() or prev
    guess = cfg.get("country") or detect_country()
    country = parse_country(args.country, guess) if args.country else None
    explicit = country is not None
    while not country:
        c = BY_ISO[guess]
        answer = input(f"Country for phone numbers [{c.name} (+{c.cc})]: ")
        country = parse_country(answer, guess)
        explicit = bool(answer.strip())
        if not country:
            print("  Didn't recognize that. Try a name (Germany), a code (DE) or a calling code (+49).")
    set_country(country)
    raw = args.phone or input(f"Your phone number ({flag(country)} +{BY_ISO[country].cc}, or include + for another country): ").strip()
    cfg["phone"] = normalize_phone(raw)
    if not explicit and raw.strip().startswith(("+", "00")):
        country = country_for_phone(cfg["phone"], prefer=country) or country
    cfg["country"] = country
    set_country(country)

    # The directory lets people find each other by phone number. One computer in the group hosts it.
    directory_url = args.directory
    if directory_url is None and not args.host:
        print("\nThe directory is how your contacts' devices find yours. One computer in your household/group hosts it.")
        answer = input("Address of an existing directory (like http://192.168.1.20:8790), or press Enter to host it on this computer: ").strip()
        directory_url = answer or None
    if directory_url and not directory_url.startswith("http"):
        directory_url = "http://" + directory_url
    if directory_url and ":" not in directory_url.split("//", 1)[1]:
        directory_url += ":8790"

    if not directory_url:
        state = InMemoryDirectory(HOME / "directory.json")
        cfg["token"] = await state.register(cfg["phone"], cfg["name"])
        cfg["host_directory"] = True
        ip = (lan_addresses() or ["127.0.0.1"])[0]
        cfg["directory"] = f"http://{ip}:{cfg.get('directory_port', 8790)}"
        print(f"\nThis computer hosts the directory at {cfg['directory']}.")
        print("To add another device, run setup there and enter that address; the code it asks for appears in Redtooth here.")
    else:
        cfg["host_directory"] = False
        cfg["directory"] = directory_url.rstrip("/")
        d = HTTPDirectory(cfg["directory"])
        try:
            await d.start_verification(cfg["phone"])
        except Exception as e:
            sys.exit(f"Couldn't reach the directory at {cfg['directory']} ({e}). Is Redtooth running on that computer, and are you on the same network?")
        print("A verification code was sent. It appears in Redtooth on the computer that hosts the directory.")
        code = args.code or input("Verification code: ").strip()
        cfg["token"] = await d.verify(cfg["phone"], code, cfg["name"])
    cfg.pop("contacts", None)  # an old setup could point at a file that no longer exists
    save_config(cfg)
    print(f"\nRegistered {cfg['name']} ({cfg['phone']}). Numbers without a country code are read as {BY_ISO[country].name} (+{BY_ISO[country].cc}).")
    if args.contacts:
        _import_contacts_cli(args.contacts)
    print("\nStart Redtooth with:  redtooth start   (it lives in the system tray; add contacts from its Settings)")


def _import_contacts_cli(path: str) -> None:
    from .contacts import ContactBook, ContactStore

    p = Path(path).expanduser()
    if not p.is_file():
        print(f"Contacts file not found: {p}\nYou can add contacts later in the panel (Add contacts).")
        return
    try:
        book = ContactBook.from_file(p)
    except Exception as e:
        print(f"Couldn't read {p.name}: {e}")
        return
    store = ContactStore(HOME / "contacts.json")
    own = store.load()
    added = own.merge(book)
    store.save(own)
    print(f"Imported {added} new contacts from {p.name} ({len(own.contacts)} total).")


def format_doctor(report: dict) -> str:
    lines = [f"Bluetooth radio: {report.get('radio')}"]
    if report.get("packages"):
        lines.append(f"Packages: {report['packages']}")
    lines.append("\nPaired devices:")
    for d in report.get("paired", []) or []:
        state = "connected" if d.get("connected") else "not connected"
        lines.append(f"  {d['name']:<30} {d.get('kind', ''):<11} {state:<14} {d.get('address') or ''}")
    if not report.get("paired"):
        lines.append("  (none)")
    eps = report.get("audio_endpoints")
    if eps is not None:
        lines.append("\nAudio outputs Windows knows about:")
        if isinstance(eps, str):
            lines.append("  " + eps)
        for e in eps if isinstance(eps, list) else []:
            lines.append(f"  {e['name']:<42} {'active' if e.get('active') else 'not active':<11} {'bluetooth' if e.get('bluetooth') else ''} {e.get('address') or ''}")
    out = report.get("audio_output") or {}
    lines.append(f"\nPlaying through: {out.get('name')} ({out.get('transport')})")
    lines.append("\nNearby (pairing mode / discoverable):")
    for d in report.get("nearby", []) or []:
        lines.append(f"  {d['name']:<30} {d.get('kind', ''):<11} signal {d.get('rssi')}")
    if not report.get("nearby"):
        lines.append("  (nothing found)")
    return "\n".join(lines)


async def cmd_doctor(args) -> None:
    """Paste this output when something about Bluetooth or audio doesn't work."""
    import platform

    from .bt.base import detect_platform

    logging.getLogger().setLevel(logging.INFO)
    bt = detect_platform()
    print(f"Redtooth doctor · {platform.platform()} · Python {platform.python_version()} · backend {bt.name}", flush=True)
    print("Scanning for 8 seconds (put a pair of headphones in pairing mode to see it appear)...", flush=True)
    report = await bt.diagnose()
    print(json.dumps(report, indent=1, default=str) if args.json else "\n" + format_doctor(report))


def cmd_contacts(args) -> None:
    from .contacts import ContactStore
    from .phones import detect_country, set_country

    set_country(load_config().get("country") or detect_country())

    if args.action == "import":
        for f in args.files:
            _import_contacts_cli(f)
    elif args.action == "add":
        from .contacts import ContactBook
        from .models import Contact
        from .util import new_id, normalize_phone

        store = ContactStore(HOME / "contacts.json")
        own = store.load()
        own.merge(ContactBook([Contact(new_id("c_"), args.files[0], [normalize_phone(args.files[1])])]))
        store.save(own)
        print(f"Added {args.files[0]}.")
    else:
        for c in ContactStore(HOME / "contacts.json").load().contacts:
            print(f"{c.name}  {', '.join(c.phones)}")
    if args.action != "list":
        print("If Redtooth is running, restart it to pick this up (imports in the panel apply immediately).")


def cmd_share(args) -> None:
    from . import desktop

    path = str(Path(args.file).resolve())
    if not desktop.is_running(args.port):  # right-clicked a file while Redtooth wasn't running: start it
        desktop.start_detached(desktop.background_command())
        for _ in range(60):
            time.sleep(0.5)
            if desktop.is_running(args.port):
                break
    try:
        post(args.port, "/api/select", {"path": path})
        open_panel(f"http://127.0.0.1:{args.port}/")
    except OSError:
        sys.exit("Redtooth isn't running. Start it with `redtooth start`.")


def cmd_ask(args) -> None:
    r = post(args.port, "/api/ask", {"text": " ".join(args.text)})
    print(r["text"])
    if r.get("question"):
        for i, o in enumerate(r["question"]["options"], 1):
            print(f"  {i}. {o['label']}  {o['detail']}")


def _install_windows(remove: bool = False) -> None:
    """Start menu entry, Send To, and the right-click menu: all per-user, no admin, no console windows."""
    import winreg

    from .desktop import background_command, tray_icon_image

    appdata = Path(os.environ["APPDATA"])
    start_lnk = appdata / "Microsoft/Windows/Start Menu/Programs/Redtooth.lnk"
    sendto_lnk = appdata / "Microsoft/Windows/SendTo/Redtooth.lnk"
    key = r"Software\Classes\*\shell\Redtooth"
    if remove:
        for f in (start_lnk, sendto_lnk, appdata / "Microsoft/Windows/SendTo/Redtooth.cmd"):
            f.unlink(missing_ok=True)
        for k in (key + r"\command", key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass
        return
    HOME.mkdir(parents=True, exist_ok=True)
    ico = HOME / "redtooth.ico"
    try:
        tray_icon_image(256).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    except Exception as e:
        logging.info("icon: %s", e)
    pyw = background_command()[0]
    (appdata / "Microsoft/Windows/SendTo/Redtooth.cmd").unlink(missing_ok=True)  # replaced by a shortcut (no console flash)

    def shortcut(path: Path, args: str, desc: str) -> None:
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');$s.TargetPath='{pyw}';"
              f"$s.Arguments='{args}';$s.IconLocation='{ico}';$s.Description='{desc}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], check=False, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    shortcut(start_lnk, "-m redtooth.cli start", "Redtooth")
    shortcut(sendto_lnk, "-m redtooth.cli share", "Send with Redtooth")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "Send with Redtooth")
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, str(ico))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'"{pyw}" -m redtooth.cli share "%1"')


def cmd_install(args) -> None:
    exe = shutil.which("redtooth") or f'"{sys.executable}" -m redtooth.cli'
    if getattr(args, "remove", False):
        if sys.platform.startswith("win"):
            _install_windows(remove=True)
        else:
            for f in (Path.home() / ".local/share/applications/redtooth-share.desktop", Path.home() / ".local/share/applications/redtooth.desktop",
                      Path.home() / ".local/share/nautilus/scripts/Send with Redtooth"):
                f.unlink(missing_ok=True)
        cfg = load_config()
        cfg["integrations"] = "removed"
        save_config(cfg)
        print("Removed Redtooth from the file menus.")
        return
    if sys.platform.startswith("win"):
        _install_windows()
        print("Added Redtooth to the Start menu, Send To › Redtooth, and 'Send with Redtooth' in the file right-click menu.")
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


def _port_busy_message(port: int) -> str:
    from . import desktop

    who = desktop.pids_on_port(port)
    detail = "".join(f"\n  PID {pid}: {cmd[:100]}" for pid, cmd in who)
    return (f"Port {port} is already in use by another program{detail or ''}.\n"
            "It's usually an older Redtooth window that's still open: press Ctrl+C in it (or run `redtooth stop`), then try again.")


def run_serve(args) -> None:
    from . import desktop

    url = f"http://127.0.0.1:{args.port}/"
    state = desktop.port_state(args.port)
    if state == "ours":
        print(f"Redtooth is already running: {url}")
        open_panel(url)
        return
    if state == "other":
        msg = _port_busy_message(args.port)
        if args.background:
            desktop.message_box("Redtooth can't start", msg)
        sys.exit(msg)
    log_path = HOME / "redtooth.log"
    if args.no_tray or not desktop.tray_available():
        asyncio.run(cmd_serve(args))
        return
    if not args.background:
        print(f"Redtooth panel: {url}\nThere's also a Redtooth icon in the system tray. Keep this window open, or use `redtooth start` to run without it.", flush=True)
    desktop.run_with_tray(lambda set_status: asyncio.run(cmd_serve(args, set_status)), url, HOME, open_at_start=args.open, log_path=log_path)


def cmd_start(args) -> None:
    """Run Redtooth with no console window, and wait until it actually answers (or say why it didn't)."""
    from . import desktop

    url = f"http://127.0.0.1:{args.port}/"
    state = desktop.port_state(args.port)
    if state == "ours":
        print(f"Redtooth is already running. Opening {url}")
        open_panel(url)
        return
    if state == "other":
        sys.exit(_port_busy_message(args.port))
    if not load_config().get("phone"):
        sys.exit("Run `redtooth setup` first.")
    log_path = HOME / "redtooth.log"
    HOME.mkdir(parents=True, exist_ok=True)
    start = log_path.stat().st_size if log_path.exists() else 0
    proc = desktop.start_detached(desktop.background_command(("--open", "--port", str(args.port))), log_path)
    print("Starting Redtooth", end="", flush=True)
    for i in range(180):  # first start downloads the search model, so allow a few minutes
        time.sleep(1)
        if desktop.port_state(args.port) == "ours":
            print(f"\nRedtooth is running: {url} (opening in your browser). It also has an icon in the system tray.")
            return
        if proc.poll() is not None:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[start:].strip().splitlines()[-25:] if log_path.exists() else []
            print("\nRedtooth stopped while starting. Last lines of its log:\n  " + "\n  ".join(tail or ["(empty)"]))
            print(f"Full log: {log_path}\nTo see it run in this window instead: redtooth serve --open")
            sys.exit(1)
        if i % 3 == 2:
            print(".", end="", flush=True)
        if i == 20:
            print(f" (still starting; the first run downloads the search model. Progress: {log_path})", end="", flush=True)
    print(f"\nIt's taking unusually long. Check {log_path}, or run `redtooth serve --open` to watch it start.")


def cmd_stop(args) -> None:
    from . import desktop

    state = desktop.port_state(args.port)
    if state == "ours":
        try:
            post(args.port, "/api/quit", {})
        except Exception:
            pass
        print("Redtooth stopped.")
        return
    stopped = False
    for pid, cmd in desktop.pids_on_port(args.port):
        if "redtooth" in cmd.lower():
            import psutil

            psutil.Process(pid).terminate()
            print(f"Stopped an older Redtooth (PID {pid}).")
            stopped = True
    if not stopped:
        print("Redtooth isn't running." if state == "free" else _port_busy_message(args.port))


def main(argv=None) -> None:
    if "--background" in (sys.argv[1:] if argv is None else argv) or sys.stdout is None or sys.stderr is None:
        # No console (pythonw): everything goes to the log file so nothing fails silently.
        HOME.mkdir(parents=True, exist_ok=True)
        _log = open(HOME / "redtooth.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = _log
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    p.add_argument("--no-integrations", action="store_true", help="don't add Send with Redtooth to the file menus")
    p.add_argument("--open", action="store_true")
    p.add_argument("--no-tray", action="store_true", help="run in this terminal without a tray icon")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("start", help="start Redtooth in the background (tray icon) and open its panel")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("stop", help="stop a running Redtooth (including an older version holding the port)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p = sub.add_parser("setup")
    p.add_argument("--name")
    p.add_argument("--phone")
    p.add_argument("--code")
    p.add_argument("--contacts", help="optional: a contacts export (.vcf, .csv or .json) to import")
    p.add_argument("--country", help="country for numbers without a country code (US, GB, +44, Germany…); default: this computer's region")
    p.add_argument("--directory", default=os.environ.get("REDTOOTH_DIRECTORY"), help="address of an existing directory (default: ask)")
    p.add_argument("--host", action="store_true", help="host the directory on this computer without asking")
    p = sub.add_parser("contacts", help="import, add or list contacts (the panel does the same)")
    p.add_argument("action", choices=["import", "add", "list"])
    p.add_argument("files", nargs="*", help="import: files (.vcf/.csv/.json); add: NAME PHONE")
    p = sub.add_parser("doctor", help="show what Redtooth sees: Bluetooth radio, paired devices, audio outputs, a short scan")
    p.add_argument("--json", action="store_true")
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
    p.add_argument("--data", help="where registrations are kept (default ~/.redtooth/directory.json)")
    p = sub.add_parser("install-integrations", help="add Send with Redtooth to the file menus (done automatically on first serve)")
    p.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "demo":
        asyncio.run(cmd_demo(args))
    elif args.cmd == "serve":
        run_serve(args)
    elif args.cmd == "start":
        cmd_start(args)
    elif args.cmd == "stop":
        cmd_stop(args)
    elif args.cmd == "setup":
        asyncio.run(cmd_setup(args))
    elif args.cmd == "directory":
        asyncio.run(cmd_directory(args))
    elif args.cmd == "panel":
        open_panel(f"http://127.0.0.1:{args.port}/")
    elif args.cmd == "doctor":
        asyncio.run(cmd_doctor(args))
    elif args.cmd == "contacts":
        cmd_contacts(args)
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
