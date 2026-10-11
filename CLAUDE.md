# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PyQt6 desktop app (Linux) to control the BatMobile Zumo robot (`../robot_chenillette`) over classic Bluetooth (SPP/RFCOMM). Port of the React Native Android app (`../robot_chenillette_telecommande`): same UI layout, same command protocol. Sends single ASCII characters to the robot's SoftwareSerial Bluetooth module.

## Run

```bash
./launchRemoteControl.sh   # cd to its own folder, checks PyQt6, then exec python3 batmobile.py
```

GUI dependency: PyQt6 (`requirements.txt`). The RFCOMM link uses the stdlib `socket` module (`AF_BLUETOOTH` / `BTPROTO_RFCOMM`), so no PyBluez. Pairing (name `BatMobile`, PIN `0000`, address `00:18:A1:12:16:C5`) is automated by `bt_pairing.py` (needs `python3-dbus` + `python3-gi`).

Offscreen smoke test (no display, no robot): `QT_QPA_PLATFORM=offscreen`, instantiate `Window`, replace `w.link.send` with a recorder, call `w.set_connected(True)`, then emit button `pressed`/`released` signals or call `keyPressEvent`/`keyReleaseEvent` with `QKeyEvent`s.

## Architecture

Single file — all logic lives in `batmobile.py`:

- **`paired_devices()`** — parses `bluetoothctl devices Paired` (fallback: `bluetoothctl paired-devices` for older BlueZ) into `(address, name)` pairs.
- **`Link`** (`QObject`) — RFCOMM socket on channel `RFCOMM_CHANNEL = 1` (confirmed working with the real robot). `connect_to()` connects in a background thread, sends the current speed digit, then emits `connected` / `failed`. A watcher thread blocks on `recv()` (the robot never sends anything) and emits `lost` when the link closes. `send()` emits `lost` on write error. `disconnect()` sends `' '` before closing. Signals are emitted from background threads; Qt queues them to the GUI thread.
- **`Window`** — header (device combo box, editable for a manual address; refresh; connect button), vertical speed `QSlider` (0–9, top = fast) left of the d-pad, claw row, head row, keyboard hint.
  - **`dir_button(label, cmd)`** — `pressed` sends `cmd`, `released` sends `' '`; `cmd=None` is the stop button (press only).
  - **Keyboard** — `HOLD_KEYS` maps `int` key codes → commands (Z/S/Q/D + arrows, V/C, W/X). Auto-repeat is ignored. Space = stop, digits move the slider (which sends the digit). The stop on release is sent only if the released key is the last one pressed (`held_key`). `focusOutEvent` sends a stop if a key is held.
  - Every button and the slider use `NoFocus` so the window keeps keyboard focus (otherwise Space would click the focused button).

Use `int` key codes (`e.key()`, `Qt.Key.X.value`), not `Qt.Key(e.key())`: the enum constructor raises on unknown key codes.

### `bt_pairing.py`

Standalone script, also run by the GUI through `QProcess` (kept out of process so the GLib main loop needed by dbus-python never runs alongside Qt's). BlueZ over D-Bus: powers on the adapter, finds the device named `BatMobile` (already known to BlueZ, else `StartDiscovery` with a `bredr` filter, watching `InterfacesAdded` / `PropertiesChanged` for the name), registers an `org.bluez.Agent1` whose `RequestPinCode` returns `0000` (the Bluetooth Bee uses legacy PIN pairing), calls `Pair()`, then sets `Trusted`. Full discovery + PIN pairing confirmed working with the real robot (`--repair` run). Already paired → only sets `Trusted`. `--repair` removes the device first; `--timeout` bounds discovery. Progress goes to stdout; the last line on success is `ADDRESS=xx:xx:…` (parsed by the GUI); errors go to stderr with exit code 1.

In the GUI, when `paired_devices()` has no `BatMobile`, `refresh_devices()` inserts a "BatMobile — appairage automatique" item with `None` data at index 0; **Connecter** on that item calls `start_pairing()`, which shows the script's last stdout line in `self.status`, then refreshes the list and connects to the returned address.

Do not run `bt_pairing.py --repair` casually when testing: it deletes the existing pairing.

## Command Protocol

Same as the Android app and the firmware:

| Char | Action |
|------|--------|
| `z` / `s` | Avance / Recule (maintenir) |
| `q` / `d` | Pivote à gauche / droite (maintenir) |
| `v` / `c` | Ferme / Ouvre la pince (maintenir) |
| `w` / `x` | Tête ▲ / ▼ (maintenir) |
| `0` … `9` | Vitesse `(n + 1) × 40` — ne stoppe pas le robot |
| ` ` (espace) | Stop |

The character is sent on press and `' '` on release, **with no repeat while held**: the firmware has no keep-alive (`COMMAND_TIMEOUT_MS = 0`). `SPEED_LEVELS` / `SPEED_PER_LEVEL` / `SPEED_LEVEL_DEFAULT` must stay in sync with the firmware and `App.tsx`. Any change to the command flow must keep the safety stops (disconnect, close, focus loss, link loss) in place.

The robot's Bluetooth module accepts only one connection at a time: the phone app must be disconnected first.
