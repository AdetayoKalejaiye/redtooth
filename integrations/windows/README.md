# Windows entry points

`redtooth install-integrations` adds:
- **Send To › Redtooth** (`%APPDATA%\Microsoft\Windows\SendTo\Redtooth.cmd`)
- **Send with Redtooth** in the file context menu (HKCU, no admin needed)

Both run `redtooth share <file>`, which opens the panel with the file selected.

Optional components: `pip install redtooth[windows]` (WinRT discovery/pairing, Wi-Fi Direct, toast Accept/Decline) and
`Install-Module AudioDeviceCmdlets` (switching the default output device).

No "receive mode": `redtooth serve` listens all the time and asks with a toast when a file arrives.
