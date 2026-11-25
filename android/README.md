# Redtooth for Android

```
android/
  core/   Kotlin core, no dependencies: wire protocol, sender/receiver with resume, directory client,
          request parser, people and device resolution, the agent. Compiled and tested on this branch.
  app/    The Android app: Compose panel, always-on receiver service, Bluetooth, Wi-Fi Aware,
          folder-grant file search, contacts, share target, Quick Settings tile. Not yet built or run.
```

## Build and test

Requires Android Studio (Ladybug or newer) / Android SDK 35, JDK 17.

```
cd android
gradle wrapper --gradle-version 8.11.1     # once, to create ./gradlew
./gradlew :core:test                       # 15 checks, including interop with the Python desktop (run from this repo)
./gradlew :app:installDebug
python -m redtooth.cli directory --port 8790 --open-register   # a directory for testing (on your computer)
```

The app's default directory is `http://10.0.2.2:8790` (the host machine, from the emulator). On a phone, enter your computer's LAN address during onboarding.

## What the core tests prove (run in the build workspace)

- The request parser matches the desktop's on 85 requests (fixtures generated from `redtooth/intent.py` by `tools/make_intent_fixtures.py`).
- Phone normalization and contact hashes match the desktop's.
- Agent flows against fake Bluetooth and files: connect to registered headphones, "Are Daniel's the Sony XM5s?" without the app, pairing-mode nudge then auto-connect, "Send Daniel the deck I edited this morning" → which deck → delivered to his phone, "Send it to his laptop instead", find the churn deck, then "send it to Daniel".
- A transfer dropped at 10 MB resumes and arrives byte-identical. Declines are reported.
- Kotlin ↔ Python desktop transfers in both directions (the desktop switches to SHA-256 chunks for the phone), and the Kotlin directory client against the Python directory server.

## How the app maps to the platform

| Feature | Android API | Notes |
|---|---|---|
| Find headphones nearby | `BluetoothAdapter.startDiscovery` | needs BLUETOOTH_SCAN |
| Pair | `BluetoothDevice.createBond` | system shows its pairing confirmation |
| Connect audio | waits for Android's auto-connect after bonding, then tries `BluetoothA2dp.connect` by reflection | no public API; if both fail, the panel offers Bluetooth settings |
| Verify output | `AudioManager.getDevices(GET_DEVICES_OUTPUTS)` | media follows the connected A2DP device; it can't be forced |
| Receive any time | foreground service (`connectedDevice`) on port 47800 | Accept / Decline in the notification; files land in Downloads/Redtooth |
| Phone ↔ phone without Wi-Fi | Wi-Fi Aware publish/subscribe + `WifiAwareNetworkSpecifier` (PSK, port) | Android ↔ Android; iPhones reach Android over the LAN |
| File search | folders granted with `ACTION_OPEN_DOCUMENT_TREE`, MediaStore photos/videos, SQLite FTS4 | text from docx/pptx/xlsx/txt/md/csv; PDFs by name; no meaning-based (E5) search yet |
| Share sheet | `ACTION_SEND` activity | the file arrives in the panel already selected |
| Quick control | Quick Settings tile | shows the audio output; tap opens the panel |

## Needs a real device to verify

1. Bluetooth discovery, bonding and the A2DP connect fallback on a few phone brands (the hidden `connect` is blocked on some).
2. Wi-Fi Aware data path between two phones (publisher/subscriber timing, PSK, IPv6 socket).
3. Foreground-service survival under battery optimization (Doze); share-sheet URI grants for very large files.
4. Folder indexing speed on a large Drive/Documents tree.

## Known gaps

- Transfers are integrity-checked but not encrypted on the LAN (same as the desktop today). Adding TLS with pinned per-device keys exchanged through the directory is the next protocol step.
- No E5 semantic search on Android yet (needs ONNX Runtime Mobile and the XLM-R tokenizer).
