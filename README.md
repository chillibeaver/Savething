# Savething

Say goodbye to manually copy-pasting save locations into Syncthing on every device. [中文](README_ch.md)

Savething uses [Ludusavi](https://github.com/mtkennerly/ludusavi) to find each game's save folder and [Syncthing](https://syncthing.net) to sync it, across Windows and SteamOS. An always-on server relays the saves, so your devices don't need to be on at the same time.

Share a game once on one device, accept it once on the others, and Syncthing handles the rest.

---

## Requirements

- **An always-on server** running Syncthing.
- **Windows** / **SteamOS** gaming devices with Syncthing installed. See [SteamOS](#steamos) for what is supported there.
- **Ludusavi** on devices that share games. Devices that only receive saves don't need it.

---

## Setup

### 1. Server

In the server's Syncthing web GUI, open **Actions → Settings → GUI**:

- Set **GUI Listen Address** to `0.0.0.0:8384`, so your devices can reach the API.
- Copy the **API Key**.

Then create a folder for saves that Syncthing can write to, e.g. `/srv/media/game/savething`.

### 2. Pair devices

In Syncthing, add every gaming device and the server to each other, and add the gaming devices to each other. Wait until they all show **Connected**.

If Auto Accept is on, turn it off: **Sharing** tab → uncheck **Auto Accept**. Otherwise, unshared games can come back on their own.

Devices are shown by their Syncthing name. To hide some devices, add name patterns to `exclude_device_patterns` in `config.json`.

### 3. Initialize each gaming device

Double-click `savething.exe`, choose `6. init`, and enter the server's save folder, address (e.g. `http://192.168.1.10:8384`), and API key. 

Or set everything in one command, with no prompts:

```
savething.exe init --server-url http://<server IP>:8384 --server-key <API key> --server-root <save folder>
```

Leave out `--server-root` if the save folder is the default `/srv/media/game/savething`.

You're done when you see `Init complete.` Run `5. status` to check the connection, then `2. preview` to see what would be synced.

---

## SteamOS

Supported: **Windows games run with Proton**, either Steam games or games added to Steam as non-Steam games. Native Linux games are not supported. In Desktop Mode, open Konsole and run:

```
curl -OL https://raw.githubusercontent.com/chillibeaver/Savething/refs/heads/main/savething.py
python3 savething.py
```

- **Install** Ludusavi and SyncThingy (Syncthing) from Discover. Both are found automatically.
- **Start the game once** before `accept`, so Proton creates its prefix.
- **Steam games** use their own Proton prefix automatically.
- **Shortcut names don't matter.** For non-Steam games, Savething finds which shortcut runs the game; if it can't tell, it asks once and remembers.
- Only saves on `C:` can be mapped. Saves on other drives are skipped.

---

## Usage

Double-click `savething.exe` to open the menu, or run the commands directly:

| Command | Menu | What it does |
|---|---|---|
| `share` | 1 | Pick games and devices to sync |
| `share --dry-run` | 2 | Preview only, changes nothing |
| `accept` | 3 | Accept saves shared to this device |
| `unshare` | 4 | Stop syncing a game on all devices (files are kept) |
| `status` | 5 | Show sync status |
| `init` | 6 | Set up this device |

**`share` options**

- `--all`: also show excluded games (e.g. games with no save files). In the menu, type `showall` at the game prompt instead.
- `--devices <name>`: choose target devices without being asked.
- `--yes`: skip confirmations. Settings files are not synced unless you add `--include-config`.
- `--include-config` / `--exclude-config`: sync or skip settings files without being asked.

**`unshare` options**

- `--game <ID or name>`: choose the game without the list.
- `--yes`: skip confirmation.

---

## How it works

`share` asks Ludusavi where the game keeps its saves. Savething creates the share on this device and on the server, and records the save path in the `savething-registry` folder relative to the home folder, e.g. `<home>/Saved Games/Hades II`. On another device, `accept` fills in its own home folder. After that, Syncthing does all the syncing.

On SteamOS, a Proton prefix's `drive_c/users/steamuser` is the home folder, so the same path works there:

```
Windows:    C:/Users/you/Saved Games/Hades II
SteamOS:    .../compatdata/<appid>/pfx/drive_c/users/steamuser/Saved Games/Hades II
```

---

## Good to know

- **Excluded games:** games that only save to the Windows registry, and games whose saves are only inside Steam or install folders (e.g. Steam Cloud's `userdata`; those are left to Steam Cloud). Steam games are treated like any other game.
- **`[ignored in Ludusavi]`** means the game is unchecked in Ludusavi's Backup tab. It's only a note: the game can still be shared and synced normally.
- **Settings files:** graphics and control settings are not synced by default, so each device can keep its own. You're asked for each game during `share`.
- **Games with saves in several places** get one synced folder per location.
- **Existing saves:** if a device already has saves for a game, the newest file wins. The older one is kept as `*.sync-conflict-*`.
- **Backups:** the server keeps the last 10 versions of every save file in `.stversions` inside the game's folder.
- **Unshare** keeps all save files. Other devices stop syncing the next time they run `accept`.
- **Don't delete the `savething-registry` folder.** `accept` needs it to know where each game's saves go.

---

## Troubleshooting

**`accept` finds nothing, but Syncthing shows an invitation**
Wait a few seconds for the registry to sync from the server, then run `accept` again.

**"Cannot reach server Syncthing"**
Open `http://<server IP>:8384` in a browser on this device. If it doesn't load, check the server's GUI Listen Address and firewall.

**"The server Syncthing does not know this device yet" / "has not added the server device yet"**
The device and the server aren't paired in Syncthing.

**A device is missing from the list in `share`**
It isn't added in this device's Syncthing, or its name matches `exclude_device_patterns`.

**"Cannot find ludusavi"**
Set the full path in the `"ludusavi"` entry of `%APPDATA%\savething\config.json`, e.g. `"C:\\Tools\\ludusavi.exe"`.

**"Cannot find the local Syncthing config.xml"**
Run savething with `--st-home <Syncthing config folder>`.

**Windows SmartScreen blocks savething.exe**
The exe is unsigned. Click **More info → Run anyway**, or use `python savething.py` instead.

**Changing the server address or API key**
Run `init` again, or edit `%APPDATA%\savething\config.json`.
