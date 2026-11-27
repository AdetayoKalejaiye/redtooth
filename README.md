# Redtooth

**Agentic Bluetooth. Ask for what you want.**

<!-- Launch video: on GitHub, edit this file and drag redtooth_launch.mp4 here; GitHub turns it into a player. -->

Type what you mean, and Redtooth works out the person, the device and the file:

- **"Connect to Sam's speaker."** No asking what it's called, no guessing between three `WH-1000XM5`s and an `Unknown Device`. If Sam uses Redtooth and has marked the speaker as theirs, Redtooth knows exactly which one you mean, connects, and checks that your sound actually moved before it says so.
- **"Send Daniel the deck I edited this morning."** You don't need the file name. Redtooth searches by what's in your files, what you use, and when you edited them. If two files fit, it asks the one question that tells them apart ("the Q4 board update or the sales kickoff?") and remembers your answer.
- **"Send it to his laptop instead."** It keeps the file and the person and just changes the device.

> **Beta.** Windows is the main platform for now. macOS and Linux run from Python. The Android and iOS apps aren't released yet. See [Limits](#limits) before you rely on it.

## Get it

### Windows

1. Download **Redtooth-Setup-x.y.z.exe** from the [latest release](https://github.com/AdetayoKalejaiye/redtooth/releases/latest).
2. Run it. Windows will say **"Windows protected your PC"** because the installer isn't signed yet: click **More info › Run anyway**. It installs for your user only and doesn't need admin rights.
3. Open **Redtooth** from the Start menu. The first time, a short setup asks your name, your country and your phone number. Press **Enter** to use the shared directory.
4. Redtooth opens in your browser and adds an icon to the system tray (next to the clock). Left-click the icon to open Redtooth any time; right-click it for Pair new device, Your devices, Contacts, Start when I sign in, and Quit.

### macOS and Linux

Python 3.11 or newer:

```
pip install git+https://github.com/AdetayoKalejaiye/redtooth
redtooth setup
redtooth start
```

- **macOS** also needs `brew install blueutil switchaudio-osx` for Bluetooth and audio switching.
- **Linux** uses `bluetoothctl` (BlueZ) and `pactl` (PulseAudio or PipeWire).

### Just try it (no Bluetooth needed)

```
pip install git+https://github.com/AdetayoKalejaiye/redtooth
redtooth demo --open
```

The demo has a pretend Bluetooth radio, a pretend friend (Daniel) with headphones, a phone and a laptop, and a folder of sample files. Transfers between you and Daniel are real. Try:

| Type this | What happens |
|---|---|
| Connect to my friend Daniel's headphones | Finds the XM5s Daniel registered, connects, and confirms they're now playing your audio. |
| Send Daniel the deck I edited this morning | Two decks fit, so it asks which. Daniel accepts on his phone (on the right), and you see Waiting → Sending → Saved. |
| Send it to his laptop instead | Same file, same person, now his MacBook Air. |
| Find the deck about customer churn | Finds *Retention Plan.pptx*, whose slides say "keeping more customers" but never "churn". |

## Using it

- **Your contacts.** Redtooth finds people by the phone numbers in your contacts. Outlook (Windows) and Contacts (macOS) are read automatically. Otherwise go to **Settings › Contacts** and drop in an export from your phone, iCloud, Google or Outlook (`.vcf` or `.csv`), or add people by hand.
- **Your devices.** In **Settings › Your devices**, mark the headphones, speakers and phone that are yours and give them the name friends use. That's what lets a friend ask for "Sam's speaker". Nothing is shared until you add it there.
- **Pair new device.** Pair headphones and speakers right in Redtooth: put them in pairing mode and click **Pair new device**. For a phone, open its Bluetooth settings screen so it can be found.
- **Sending to people.** The other person needs Redtooth too, and they always tap **Accept** before anything arrives. Files go straight from your device to theirs over your Wi-Fi network or Bluetooth, so you need to be near each other or on the same network.
- **Sending to your own phone.** "Send my resume to my phone" works even if the phone doesn't have Redtooth: Android phones get it over Bluetooth with an Accept prompt. iPhones don't accept Bluetooth files; use AirDrop for those.
- **More than one computer.** On a computer where Redtooth is set up, go to **Settings › Link another device** (or run `redtooth link`) for a one-time code, then run setup on the new computer with the same phone number and enter the code.

Useful commands: `redtooth start` / `redtooth stop` (background, with the tray icon), `redtooth doctor` (what Redtooth can see: Bluetooth, paired devices, audio outputs), `redtooth link`.

## Limits

Honest list, so you know what you're getting:

- **Phone numbers aren't verified yet.** The first person to register a number keeps it, and nobody else can register it after that. But until texted codes are turned on, someone could register *your* number before you do, so set Redtooth up early.
- **The installer isn't signed**, hence the Windows warning above.
- **The shared directory runs on a free plan.** If nobody has used it for a while, the first request can take up to a minute while it wakes up.
- **The Android and iOS apps** are in development in this repo and haven't been released or tested on phones yet.
- **iOS can't pair Bluetooth devices from an app** (Apple only allows it in Settings), and iPhones can't receive files over Bluetooth.
- Bluetooth support was tested on real Windows hardware. The macOS and Linux Bluetooth code follows each system's documented tools but has had less real-world testing. `redtooth doctor` shows what it sees on your machine; please paste it into an issue if something's off.

## Privacy

What stays on your computer:

- **Your files and their contents.** Search runs locally, including the search models (downloaded once, ~240 MB in total). Nothing about your files is uploaded.
- **Your contacts.** They're stored in `~/.redtooth`. When Redtooth checks which of your contacts use it, it sends SHA-256 hashes of their phone numbers, not the numbers. Hashing keeps the numbers out of plain view, but phone numbers are short enough that a hash could be reversed by trying every number, so treat it as a light measure, not a guarantee.
- **What you send.** Files go device to device and never pass through the directory.

What the shared directory stores (a server on Render, with its data in a Neon Postgres database):

- **Your name and phone number.**
- **The devices you've published:** this computer's name and its address on your local network (so friends nearby can reach it), and any headphones, speakers or phones you've marked as yours, with their names and Bluetooth addresses.
- **A sign-in token** for each of your devices, and **short messages** between Redtooth users, like a request to put your headphones in pairing mode.

It doesn't store your contacts, your files, or what you send to whom. There's no list of users to browse: a lookup needs a phone number. There's no self-serve way to delete your registration yet: open an issue and it will be removed by hand.

The local part of Redtooth only listens on your own computer (`127.0.0.1`), and websites can't control it.

## Host your own directory

The directory is a small server: `redtooth directory --open-register` runs one, and `render.yaml` deploys it to Render's free plan with its data in Postgres (`DATABASE_URL`). Point setup at it by typing its address when asked. To text verification codes, drop `--open-register` and set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_VERIFY_SID`.

## Under the hood

```
redtooth/
  intent.py        request -> action, person, device kind, file query (type, topic, time window)
  timeparse.py     "this morning", "yesterday", "last week" -> [since, until)
  contacts.py      contacts (JSON, vCard, CSV, macOS Contacts, Outlook) and name resolution
  directory.py     phone-number profiles, link codes, hashed contact lookup, nudges; JSON file or Postgres
  devices.py       search first: remembered -> registered -> paired -> nearby scan; connect, then verify the audio output
  bt/              simulator, Windows (Win32 Bluetooth API, Core Audio, OBEX), macOS (blueutil, SwitchAudioSource),
                   Linux (bluetoothctl, pactl, obexd)
  files/           text extraction (pptx/docx/pdf/xlsx/odf/iWork/text), SQLite FTS5 + vectors,
                   multilingual-E5 embeddings, cross-encoder re-ranking, usage signals
  clarify.py       when to ask "which one?", and asking about what actually differs
  transfer/        framed TCP protocol, per-chunk BLAKE2b, full-file SHA-256, resume journal; OBEX Object Push
  agent.py         the orchestrator: parallel resolution, specific questions, carry-forward context
  server.py        local API + server-sent events; web/panel.html is the panel
  desktop.py       tray icon, start at sign-in, background mode
```

**Understanding requests.** A deterministic parser, fed with your contact names, handles connect, send and find, follow-ups ("instead", "his", "it"), yes/no, and picks like "the board one". The selected file, person and device carry forward between requests.

**Connecting to the device someone means.** Evidence is gathered cheapest first: a remembered or registered device that's already paired connects without waiting for a scan. Otherwise one scan decides between *known* (connect), *plausible* (a specific yes/no question) and *nothing* (ask the owner to turn on pairing mode, and nudge them if they use Redtooth). "Connected" comes from the operating system's real state, and Redtooth only says your audio moved after reading the actual output device.

**Finding files.** Every file gets a filename chunk plus content chunks ("Slide 2", "Page 3"). Keyword search (SQLite FTS5, filename weighted) handles exact names and codes like `PRJ-2041`. Multilingual E5 embeddings handle topics phrased differently, in any of the languages E5 covers. The two are merged with reciprocal-rank fusion, then adjusted for:

- **Coverage:** how much of your request a file matches.
- **Usage:** files you recently opened, sent or picked, fading over two weeks.
- **Recency:** newer files win ties.

A multilingual cross-encoder re-reads the request against the top 20 candidates. Redtooth asks "which one?" only when that's still unsure, and the question names the difference: version, file type, folder, or date. Indexing runs in the background, recent files first, and watches for changes afterwards.

**Sending.** File search and finding the person run in parallel. As soon as the person is known, all of their devices are checked for a route. The sender hashes the file while waiting for Accept, then streams 1 MiB chunks. The receiver verifies every chunk, can resume after an interruption, checks the whole file's SHA-256, and only then reports **Saved**.

**Search quality.** `redtooth eval` scores search on a labeled 37-file corpus built to be hard: paraphrases, near-miss distractors, other languages, project codes, and queries that should find nothing. `redtooth eval --rerank` includes the re-ranker.

## Platform status

| | Windows | macOS | Linux | Android | iOS |
|---|---|---|---|---|---|
| Always-there entry point | Tray icon | Menu-bar icon | Tray icon | Notification + shortcuts | Control Center + Shortcuts |
| Start at sign-in | Yes | Yes | Yes | Start when the phone starts | n/a |
| Real "Connected" state | Win32 Bluetooth API | blueutil | bluetoothctl | A2DP + headset profiles | Current audio route only |
| Pair new device | In Redtooth | In Redtooth | In Redtooth | In the app | Settings only (an Apple rule) |
| Audio checked before saying it moved | Core Audio | SwitchAudioSource | pactl | Android 13+ | Audio route |
| Send to your own phone without Redtooth on it | Bluetooth (OBEX) | Bluetooth File Exchange | obexd | System Bluetooth share | Not possible; use AirDrop |
| Search | Full | Full | Full | Keyword + usage + recency | Keyword + usage + recency |
| Status | **Beta, installer** | From Python | From Python | In development | In development |

## Development

```
pip install -e ".[dev]"
pytest -q                  # Python tests
redtooth demo --open
```

The `android` and `ios` branches hold the phone apps (in development). GitHub Actions builds the Windows installer (main), the Android APK and Kotlin tests (android), and the Swift tests and iOS app (ios). Shared fixtures keep phone-number handling, request parsing and search ranking identical in Python, Kotlin and Swift.
