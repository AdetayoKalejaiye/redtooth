# Redtooth

Agentic Bluetooth. Ask for what you want ("connect to my friend Daniel's headphones", "send Daniel the deck I edited this morning") and Redtooth finds the person, the device and the file, asks only a specific question when it has to, and does the rest. Nobody has to put a device into send or receive mode.

```
pip install -e .            # Python 3.11+
redtooth demo --open        # simulated Bluetooth, real transfers -> http://127.0.0.1:8787
pytest -q                   # 31 tests: scenarios, search, indexing, speed-up index, eval set
redtooth eval               # score file search on the labeled test set
```

## Try these in the demo

| Request | What happens |
|---|---|
| Hey, connect to my friend Daniel's headphones | Resolves *Daniel Ortiz* (friend, nearby, uses the app) over *Daniel Kim*, finds the XM5s he registered, pairs, connects, and confirms they became the audio output. |
| Same, with **No app** selected in the demo controls | Searches first, then asks: "Are Daniel's the Sony XM5s?" Yes → pairs through the OS and remembers the association. |
| Same, with **Not pairing** selected | Sends Daniel a nudge to enter pairing mode and keeps scanning. Flip the pairing switch and it connects by itself. |
| Send Daniel the deck I edited this morning | Two decks fit: "Which deck? Two were edited this morning." Routes to Daniel's devices are checked while you choose. Daniel accepts on his phone (simulated on the right), and you see Waiting for Daniel → Sending → Saved to Daniel's iPhone 16. |
| Send it to his laptop instead | Keeps the same file and person and sends to his MacBook Air. |
| Find the deck about customer churn | Surfaces *Retention Plan.pptx*, whose slide says "Keeping more customers". |
| Interrupt (demo controls) during a big send | The transfer resumes from the last verified byte. Select *Product walkthrough.mp4* (48 MB) to see it. |

## How it works

```
redtooth/
  intent.py        request -> action, person, device kind, file query (type, topic, time window)
  timeparse.py     "this morning", "yesterday", "last week" -> [since, until)
  contacts.py      contacts (JSON, vCard, macOS Contacts) and name resolution ranked by relation, app, proximity
  directory.py     phone-number profiles with OTP verification; registered devices; hashed contact discovery; nudges
  devices.py       search first: remembered -> registered -> paired -> nearby scan; connect + verify the audio output
  bt/              base interface + simulator, macOS (blueutil, SwitchAudioSource), Windows (WinRT, BluetoothSetServiceState,
                   AudioDeviceCmdlets), Linux (bluetoothctl, pactl, wpa_cli P2P)
  files/           text extraction (pptx/docx/pdf/xlsx/odf/iWork/text), SQLite FTS5 + sqlite-vec storage,
                   multilingual-E5 (ONNX), RRF fusion; vecindex.py is the speed-up index
  search_eval/     labeled test set (37-file corpus, 52 queries), scoring and calibration
  transfer/        framed TCP protocol, per-chunk BLAKE2b, full-file SHA-256, resume journal, route probing
  agent.py         the orchestrator: parallel resolution, specific questions, carry-forward context
  server.py        local API + server-sent events; web/panel.html is the panel
  notify.py        accept from a notification (libnotify actions, alerter/osascript, Windows toasts)
```

**Understanding requests.** A deterministic parser fed with your contact names handles connect, send, find, follow-ups ("instead", "his", "it"), yes/no, and picks like "the second one" or "the board one". Conversation context carries the selected file, person and device forward.

**Connecting to the device someone means.** Evidence is gathered cheapest first. A remembered or registered device that is already paired connects without waiting for a scan. Otherwise one scan decides between *known* (connect), *plausible* (a specific yes/no question, or a short list) and *nothing* (ask the owner for pairing mode, nudge them if they use the app, keep scanning). After connecting, Redtooth polls the system output and switches it if the OS didn't, so "Connected" means sound is actually playing there.

**Finding files.** Every file gets a filename chunk plus content chunks ("Slide 2", "Page 3"). FTS5 with bm25 (filename weighted 10×) handles exact names and project codes like `PRJ-2041`. multilingual-E5 embeddings (`query:` / `passage:` prefixes) handle topics phrased differently. The two rankings are merged with reciprocal-rank fusion. Type and time filters apply inside both searches, so a filtered match can't be crowded out by unfiltered ones. Redtooth asks when the second result is close to the first, or when type and time alone leave more than one candidate.

**Telling files apart.** Three things handle ambiguity on top of the keyword + meaning search:
- *Usage:* files Windows lists as recently opened (the Recent Items shortcuts; `recently-used.xbel` on Linux), files you sent or picked in Redtooth, and which file you picked for a request before ("term paper" → `HIST 201 Term Paper.docx`), all fading with a two-week half-life.
- *Re-ranking:* a multilingual cross-encoder (`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, 8-bit ONNX, ~120 MB, downloaded in the background on first start) reads the request with each of the top 20 candidates and scores how well they match. Until it's downloaded, search works as before. `redtooth eval --rerank` scores it on the test set.
- *Questions:* Redtooth asks only when the re-ranker is unsure (top two close, or nothing clearly relevant), and the question names what differs: "the final (edited Nov 23) or the draft (Oct 6)?", "the Word doc or the PDF?", "the one in Work or the one in Downloads?". Your answer is remembered for that request.

**Indexing a whole device** (`files/indexer.py`). `redtooth serve` indexes in a background thread:
- **Scope:** your folders (Documents, Desktop and Downloads by default), skipping system, build and cache folders and file types it can't search. It lists directories with `os.scandir`, and finds changed files with one bulk comparison, not a lookup per file.
- **Order:** files edited in the last 14 days are indexed first, newest first, and become searchable before the older backlog starts. Measured on a 20,000-file library: recent files were searchable after 0.2 s, and the full first run took 92 s with the fallback embedder (226 s before this rework).
- **Limits:** extraction runs in low-priority worker processes, with batched embedding and batched writes. Files over 150 MB (20 MB for plain text) are indexed by name only. On battery it slows down.
- **No silent cap:** a 1,000,000-file runaway guard keeps the most recently edited files and reports what it left out.
- **Safe deletes:** files are removed from the index only when they're proven gone. Folders it can't read are kept as-is and reported, with the macOS permission hint in the panel.
- **After the first run:** it watches for changes (FSEvents, inotify, or ReadDirectoryChangesW, via watchdog), with a full safety pass every 30 minutes. A pass with nothing changed takes 0.08 s for 20,000 files.
- **Panel:** shows progress ("Indexing · 745 of 20,009 files · recent files ready"), and replies say when older files may be missing.
- **While the first run is incomplete:** a request first asks the OS's own index (Spotlight on macOS, Windows Search on Windows) for likely files, and indexes those on the spot.

The Spotlight, Windows Search and macOS/Windows file watching paths are written to each platform's documented interfaces but haven't been run on those systems.

**Speed-up index.** Vectors are stored in sqlite-vec inside the same SQLite file, but searched through a derived, memory-mapped, cluster-sorted matrix (`files/vecindex.py`):
- **Up to 200k chunks:** search is exact.
- **Above 200k:** k-means clusters the vectors and each query scores only the nearest clusters. The number of clusters probed is tuned on your own library, using short word queries, so that recall@10 against exact search stays at or above 95%.
- **Filters:** type and time filters are array masks. When they leave few rows, search is exact over just those rows.
- **Updates:** files indexed since the last build sit in a small in-memory delta, and deletions in a tombstone set, until a rebuild (seconds).

Measured on a 120k-chunk library:

| | sqlite-vec scan (before) | new index, exact | new index, clustered |
|---|---|---|---|
| vector step | 164 ms | 20 ms | 5.5 ms |
| whole search, median | ~195 ms | 62 ms | 41 ms |
| deck-filtered search | 50 ms | 3.6 ms | 3.3 ms |
| build | — | 1.4 s | 6.3 s incl. tuning |

What didn't work, measured with `tools/ann_bench.py`:
- **sqlite-vec's own search:** its 1-bit and 8-bit searches are slower than its float search.
- **sqlite-vec partition keys:** they make clustered probing possible, but scanning every partition is 2.6× slower than an unpartitioned scan. Even with blocks sized to the clusters, partitioned search lost to NumPy.

The clustered index agreed with exact search on the top result for 59 of 64 queries with the fallback embeddings. Those cluster poorly; E5 should do better, and the self-tuning measures it either way.

**Sending fast.** File search and recipient resolution run in parallel. As soon as the recipient is known, every one of their devices is pinged (the PONG must carry the right device id), with Wi-Fi Direct preferred over the LAN. The sender hashes the full file while it waits for acceptance and streams 1 MiB chunks the moment ACCEPT arrives. The receiver verifies each chunk, journals verified offsets, checks the full SHA-256, fsyncs, then sends SAVED. Only then does the sender show **Saved**.

**Measured separately** (panel → Timing, `GET /api/metrics`): time to find a match, acceptance → first byte, first byte → saved, plus route check, time to find the device, and connect time. Loopback numbers in the demo: match ~2 ms, acceptance → first byte ~1–4 ms, 40 MB at ~150–180 MB/s.

## Search quality

`redtooth eval` indexes a 37-file corpus built to be hard, and scores 52 labeled queries by category:
- **Paraphrase-only matches:** files whose titles avoid the query's words.
- **Near-miss distractors:** the HR "Retention Bonus Policy" against the customer Retention Plan.
- **Other languages:** Spanish, German, French and Japanese documents.
- **Project codes and type/time requests.**
- **Queries that should find nothing.**

Baseline with the fallback embedder (no model download):

| category | n | hit@1 | hit@3 |
|---|---|---|---|
| exact name | 7 | 100% | 100% |
| project code | 5 | 100% | 100% |
| type + time | 3 | 100% | 100% |
| near-miss | 5 | 80% | 80% |
| vague | 4 | 75% | 75% |
| paraphrase | 18 | 39% | 67% |
| other language | 7 | 29% | 29% |
| should find nothing | 3 | 67% | — |

The fallback's non-keyword rows move by several points when its hashing changes. They're noise, not signal.

E5 is the default: the first `redtooth serve`, `demo` or `eval` downloads it (~120 MB, once). The fallback above is only used if that download fails, and is a keyword engine with typo tolerance; paraphrase and cross-language search need E5. To tune ranking for E5:

```
redtooth eval --calibrate --save      # tunes the similarity floor, band and keyword/meaning weights
redtooth eval --root ~/Documents --queries my_queries.json   # your files, your labels
```

Calibration reports a held-out score (tune on half the queries, score the other half, both ways). It saves only if tuning beats the defaults on queries it didn't see; on the fallback it doesn't, so nothing is saved.

## Running it for real

```
pip install -e .            # everything, including Windows Bluetooth/audio and the tray icon
redtooth setup              # name, country, phone number; press Enter to host the directory on this computer
redtooth serve --open       # runs in this window, opens the panel in your browser, adds a tray icon
redtooth start              # same, but in the background with no window (waits until it's up, or shows why not)
redtooth stop               # stops it (also an older version still holding the port)
redtooth doctor             # what Redtooth sees: Bluetooth radio, paired devices, audio outputs, a scan
```

- **One process.** The panel is a web page (http://127.0.0.1:8787). The tray icon is an extra way in:
  left-click opens the panel, right-click has Add a device, Your devices, Contacts, Start when I sign in,
  and Quit. `--no-tray` skips the icon. Running in the background logs to `~/.redtooth/redtooth.log`, and any
  failure to start is printed by `redtooth start` (and shown in a message box), never silent.
- **The directory** (who uses Redtooth, by phone number) is hosted by one computer in the group, inside that
  computer's Redtooth on port 8790. Other devices run `redtooth setup`, enter its address, and type the code
  that appears in the host's panel. `redtooth directory` still runs one on its own.
- **Bluetooth.** The panel shows each paired headphone/speaker with its real state (Connected / Not
  connected) and a Connect, Use or Disconnect button. **Add a device** scans for devices in pairing mode and
  pairs them in place. Redtooth never scans in the background (Bluetooth inquiry makes audio stutter).
- **Your devices** (Settings) is where you mark headphones and speakers as yours and name them, so contacts
  who use Redtooth can ask for "Rob's headphones".
- **Contacts** (Settings) takes a `.vcf`/`.csv` export from iPhone/iCloud, Android, Google or Outlook, or
  people added by hand; macOS Contacts and Windows Outlook are read automatically.
- On first start Redtooth downloads the search model (~120 MB, once) and adds Redtooth to the Start menu,
  Send To, and the file right-click menu (`--no-integrations` to skip).

Config lives in `~/.redtooth/config.json`; the log (when running from the tray) in `~/.redtooth/redtooth.log`.

## Platform status

The same features on every platform, as far as each OS allows:

| | Windows | macOS | Linux | Android | iOS |
|---|---|---|---|---|---|
| Always-there entry point | Tray icon | Menu-bar icon | Tray icon (AppIndicator) | Notification + long-press shortcuts | Control Center + Shortcuts/Siri |
| Start at sign-in / boot | Run key | LaunchAgent | XDG autostart | Boot receiver (toggle) | n/a (iOS wakes the app for pushes) |
| Real "Connected" state | Win32 Bluetooth API | blueutil | bluetoothctl | A2DP + headset profiles | Current audio route only |
| Pair new device | In the panel (inquiry scan + pair) | In the panel (blueutil) | In the panel (bluetoothctl) | In the app (discovery + pair) | Settings only (Apple doesn't allow apps to pair); the app says where |
| Your devices (claim + name friends use) | Settings › Your devices | same | same | Your devices sheet | Mark the output playing now |
| Audio checked before saying it moved | Core Audio | SwitchAudioSource | pactl | Android 13+ media route | AVAudioSession route |
| Send to your own phone without Redtooth on it | OBEX Object Push | Bluetooth File Exchange hand-off | obexd | System Bluetooth share | Not possible (iPhones don't accept Bluetooth files); AirDrop |
| Search: hybrid + re-ranker + usage + "which one" | Full | Full | Full | Keyword + coverage, recency, usage, same questions | Keyword + coverage, recency, usage, same questions |

What's verified: Python tests (79), the Kotlin core suite (23 checks, including live Kotlin↔Python transfers and directory), and shared fixtures that pin phone numbers, intents, ranking and the "which one" questions to the same answers in all three languages. What isn't: none of the Bluetooth backends have run on real hardware except your Windows logs; the Android app hasn't been built with the Android SDK, and the Swift code hasn't been compiled (no Swift toolchain on the build machine). `redtooth doctor` shows what the desktop sees.

## Security notes

**Phone numbers aren't verified yet.** The shared directory lets the first person who registers a number keep it: nobody can register it again, and your other devices join with a link code (`redtooth link`, or Settings › Link another device). Until texted codes are turned on, someone could register your number before you do, so set Redtooth up early. A directory can text codes instead with Twilio Verify (see `render.yaml`).

The local API binds to 127.0.0.1 and requires an `X-Redtooth: 1` header on every POST, so websites can't drive it. Contact discovery sends SHA-256 hashes of phone numbers. Incoming files always need acceptance unless they come from your own number. Received names are sanitized, and partial files stay in a hidden folder until they verify.
