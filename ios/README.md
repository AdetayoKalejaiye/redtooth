# Redtooth for iPhone (`ios` branch)

The iPhone app: plain-language requests, phone-number profiles, file search, direct transfers that are
wire-compatible with the desktop and Android, accept-from-notification, and a headphones flow built around
what iOS actually allows.

> **Status: written, not compiled.** The workspace this was built in has no Swift toolchain. Every file
> parses cleanly (tree-sitter Swift grammar), and the core is a line-for-line port of the Kotlin core,
> which *is* tested: 85-case parser parity, transfers both ways with the Python desktop, and the directory
> client. Expect a round of compile fixes on first open in Xcode, mostly in `App/WifiAware.swift`
> (iOS 26 APIs written from documentation).

## Layout

```
ios/
  project.yml                 XcodeGen spec: app + share extension + Control Center control
  Packages/RedtoothCore/      Swift package, no UI (iOS 17 / macOS 14)
    Sources/RedtoothCore/
      Models.swift            people, devices, profiles, replies (same JSON fields as the desktop)
      Wire.swift              frames: 4-byte length + JSON header + payload; NWConnection channel
      Transfer.swift          Receiver (resume journal, SHA-256 verify), LAN listener + Bonjour, Sender
      Directory.swift         phone-number directory client (OTP verify, hashed lookup, nudges)
      Intent.swift            request parser (port of redtooth/intent.py)
      People.swift            contact + nearby + profile resolution
      Agent.swift             send / find / “to his laptop instead” / headphones guidance
    Tests/RedtoothCoreTests/  parser parity fixtures, transfers, agent flows, Python interop (macOS)
  App/                        SwiftUI panel, onboarding, file index, notifications, App Intents, Wi-Fi Aware
  ShareExtension/             Share sheet → pick a person → sent from the extension
  Controls/                   Control Center / Action button control (iOS 18+)
```

## Build

```sh
cd ios/Packages/RedtoothCore && swift test      # on a Mac; interop tests use python3 from the repo root
brew install xcodegen && cd ios && xcodegen      # generates Redtooth.xcodeproj
open Redtooth.xcodeproj                          # set your team; App Group group.app.redtooth
```

Run the directory from the desktop repo (`redtooth directory --open-register` for testing), then set its
URL on the onboarding screen. A phone and a computer on the same Wi-Fi can send to each other as soon as both
have registered.

## What's different on iPhone, and why

| Feature | Desktop / Android | iPhone |
|---|---|---|
| Connect to Daniel's headphones | Redtooth pairs and connects them | Redtooth works out *which* headphones (“Sony XM5s”), notifies Daniel to put them in pairing mode, and opens the system audio picker with that name. Pairing a new device only happens in Settings › Bluetooth. After the first time, Redtooth remembers the output name and just points at it. |
| Search files | Whole drive / shared storage | Only folders the user adds (Files app picker) plus anything shared into Redtooth. |
| Receive in background | Always listening | Listens while the app is open or briefly woken. A push-to-accept relay (APNs) is the planned fix; the device token is already stored with the device record. |
| Phone ↔ phone without Wi-Fi | Wi-Fi Aware (Android) | Wi-Fi Aware on iOS 26 (iPhone 12+), after a one-time system pairing per pair of phones. |
| System entry points | Tray, context menu, tile | Share sheet, Siri/Shortcuts (App Intents), Action button, Control Center control. |

## Needs a real device to check

1. Local-network permission prompt and Bonjour advertising (`NSBonjourServices`).
2. Accept/Decline from a time-sensitive notification while the panel is closed.
3. Route picker flow and `AVAudioSession.routeChangeNotification` remembering the chosen headphones.
4. Wi-Fi Aware: entitlement approval from Apple, DevicePicker pairing, then a transfer with no shared network.
5. Share extension memory limit with large files (it streams in 1 MB chunks, so it should stay small).
6. APNs spike: directory sends a push on OFFER; the app wakes, restarts the listener, re-registers its endpoint.
