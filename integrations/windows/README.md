# Windows

Everything Windows needs installs with Redtooth (`pip install -e .`): WinRT Bluetooth discovery and pairing,
Wi-Fi Direct, Accept/Decline toasts, and switching the audio output (Core Audio through pycaw). No
PowerShell modules, no admin rights.

The first `redtooth serve` also adds:
- **Send To › Redtooth** (`%APPDATA%\Microsoft\Windows\SendTo\Redtooth.cmd`)
- **Send with Redtooth** in the file context menu (HKCU)

Both run `redtooth share <file>`, which opens the panel with the file selected. Skip them with
`redtooth serve --no-integrations`, or remove them later with `redtooth install-integrations --remove`.

Contacts: if Outlook is installed, its contacts are read automatically. Otherwise use **Add contacts** in the
panel: it offers any contacts export already in Downloads, takes a dropped `.vcf`/`.csv` (Google, iCloud,
Outlook, Android), or lets you add people by hand.

No "receive mode": `redtooth serve` listens all the time and asks with a toast when a file arrives.
