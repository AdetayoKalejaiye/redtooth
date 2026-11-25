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
redtooth directory --port 8790                      # one per group/household; verification codes are logged until SMS is wired up
redtooth setup --directory http://HOST:8790         # name + phone number, verify with the code
redtooth models fetch                               # optional: pre-download the search model (otherwise the first start does it)
redtooth serve --open                               # panel, always-on receiver on :47800, reindex every 2 min
redtooth install-integrations                       # Send To / context menu (Windows), Open With + Files script (Linux)
redtooth ask "send Daniel the deck I edited this morning"
```

Config lives in `~/.redtooth/config.json` (contacts file, folders to index, inbox, shared devices). To make your headphones findable by friends, tap **Share** beside them in the panel. That is Daniel's step in the main flow.

## Platform status

| | Status in this build |
|---|---|
| Agent, search, contacts, directory, transfers, resume, panel | Implemented and covered by tests |
| Simulated Bluetooth | Implemented, drives the demo and tests |
| Linux / macOS / Windows Bluetooth + audio routing | Implemented against each OS's tools/APIs, **not yet run on hardware** |
| Wi-Fi Direct | Route preference implemented; group formation for Linux (`wpa_cli`) and Windows (WinRT) not yet tested on hardware. macOS has no public API, so Macs use the LAN |
| E5 embeddings | Default; downloaded automatically on first start (falls back to keyword search if offline, retries next start; opt out with `REDTOOTH_EMBEDDER=hash`). Not yet scored: the model host was unreachable from the build machine. Run `redtooth eval --calibrate --save` once it's downloaded |
| Phones | The panel is responsive and the wire protocol is documented in `docs/PROTOCOL.md`. iOS/Android need native clients for Bluetooth, the share sheet and background receiving |
| Quick controls | The panel opens as a compact app window (`redtooth panel`); share entry via `redtooth share FILE`. A native menu-bar / Quick Settings tile is the next step |

## Security notes

The local API binds to 127.0.0.1 and requires an `X-Redtooth: 1` header on every POST, so websites can't drive it. Contact discovery sends SHA-256 hashes of phone numbers. Incoming files always need acceptance unless they come from your own number. Received names are sanitized, and partial files stay in a hidden folder until they verify.
