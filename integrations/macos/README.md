# macOS entry points

## Share from Finder or any app (Quick Action)

1. Open **Shortcuts** → **New Shortcut** → name it *Send with Redtooth*.
2. In the shortcut's details, turn on **Use as Quick Action** (Finder, Services menu) and **Show in Share Sheet**; set it to receive **Files**.
3. Add **Run Shell Script**, set *Pass input* to **as arguments**, and paste:

   ```sh
   for f in "$@"; do /usr/local/bin/redtooth share "$f"; done
   ```
   (Use `which redtooth` to get the right path.)

The open file arrives in the panel already selected; tap a person or say who.

## Quick control

`redtooth panel` opens the compact panel as its own window. To pin it to a key, add a Shortcut that runs `redtooth panel` and assign a keyboard shortcut in its details.

## Tools used for Bluetooth and audio

```sh
brew install blueutil switchaudio-osx
brew install vjeantet/tap/alerter     # optional: Accept/Decline right in the notification
```
Grant Terminal (or your Python) **Bluetooth** and **Contacts** access in System Settings → Privacy & Security.
