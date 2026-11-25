# Windows

`pip install -e .` installs everything Windows needs: WinRT (Bluetooth devices, pairing, radio state, Wi-Fi
Direct), Core Audio through pycaw/comtypes (audio outputs, reconnecting Bluetooth headsets, switching the
default output), win11toast (Accept/Decline notifications), and pystray (tray icon). No PowerShell modules,
no admin rights.

- `redtooth setup` once, then `redtooth start`. Redtooth lives in the notification area; left-click for the
  panel (an Edge app window docked above the taskbar), right-click for the menu.
- Start when I sign in: tray menu or Settings. It adds `pythonw -m redtooth.cli serve --background` to
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- The first start adds Redtooth to the Start menu, **Send To › Redtooth**, and **Send with Redtooth** in the
  file right-click menu (all per-user). Remove with `redtooth install-integrations --remove`.
- `redtooth doctor` prints the Bluetooth radio state, paired devices with their real connection state, every
  audio output Windows knows (and which are Bluetooth), the current output, and an 8-second scan.

How the Bluetooth parts work: paired devices and their state come from WinRT association endpoints
(`System.Devices.Aep.IsConnected`); scanning uses an association-endpoint watcher with Bluetooth inquiry;
pairing uses `DeviceInformationCustomPairing` (confirm-only or PIN 0000); connecting an already-paired headset
sends the Bluetooth audio driver's one-shot reconnect (`KSPROPSETID_BtAudio`) through `IKsControl`, falling
back to restarting the device's audio services, and is only reported as connected once Windows says so.
