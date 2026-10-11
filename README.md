# Play Along

Watch or listen together, in sync, on several VLC players. Play Along keeps play/pause,
timeline position and playback speed aligned across devices through a small central
server, with two ways to decide who is in charge:

- **Anarchy** — anyone in the session can pause, resume, seek or change the speed.
- **Monarchy** — only the device that started the session controls playback; everyone
  else follows.

No accounts, no cloud: one Python script on a machine everyone can reach, plus a Lua
extension in each VLC.

```
extension/play_along.lua      VLC 3.x extension (Lua), works with the Qt desktop interface
extension/tests/              headless integration test (runs a real VLC without a GUI)
server/play_along_server.py   server, Python 3.8+ standard library only
server/play_along.service     systemd unit for running the server as a service
server/sim_client.py          simulated devices: server test and reference client
```

## How it works

- One device **starts a session** and picks the policy. The server stores the session
  state (playing, position, rate) and the moment it was set, so the expected position at
  any time is `position + (now - at) * rate`.
- Every VLC in the session reports its state about once per second. At most once every
  `--check-interval` seconds per device (default 10) the server compares the report with
  the expected position:
  - within `--tolerance` (default 5 s): nothing;
  - ahead by up to `tolerance + max-pause`: the device is **paused for up to
    `--max-pause` seconds** (default 2) and resumed;
  - any other deviation: the device is told to **seek** to the expected position.
- Local actions (pause, play, seek, rate) are detected by the extension and sent to the
  server, which applies the policy: accept and broadcast to the others, or reject and
  send the device back to the session state.
- Command delivery uses long polling: the extension asks the server, which holds the
  answer for up to ~1 s until it has something to say. A pause by the monarch reaches
  the other devices in about a second.

## Quick start

### 1. Run the server

```
cd server
python play_along_server.py --port 8765
```

Any machine with Python 3.8+ works. Nothing to install. Open `http://<server>:8765/` in a
browser for a live dashboard of sessions, devices, positions and measured drift.

| option | default | meaning |
|---|---|---|
| `--host` | `0.0.0.0` | listen address (`127.0.0.1` to accept local connections only) |
| `--port` | `8765` | TCP port |
| `--tolerance` | `5` | accepted drift, in seconds |
| `--max-pause` | `2` | longest pause used as a correction |
| `--check-interval` | `10` | minimum time between drift checks per device |
| `--cooldown` | `3` | after sending a command, time without drift checks (avoids double corrections) |
| `--stale` | `30` | seconds without a report before a device is dropped from the session |
| `-v` | | verbose log |

On Windows, the first run triggers a firewall prompt for Python. Allow it if other devices
on the network will use the server. There is no authentication and no TLS: run it on a
LAN or behind a VPN/tunnel.

#### Run as a service (Linux, systemd)

`server/play_along.service` runs the server as a dedicated `playalong` user, restarts it on
failure and applies systemd hardening (read-only filesystem, no access to home directories).
As root:

```
useradd --system --no-create-home --shell /usr/sbin/nologin playalong
mkdir -p /opt/play_along && cp play_along_server.py /opt/play_along/
cp play_along.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now play_along
journalctl -u play_along -f        # follow the log
```

Port and tuning parameters live on the `ExecStart` line. Open the port in the machine's
firewall (for example `ufw allow 8765/tcp`).

### 2. Install the extension (VLC 3.x)

Copy `extension/play_along.lua` **and the `extension/lang/` folder** to your VLC extensions
folder (the `lang/` folder holds the translations; without it the UI is in English):

| system | folder |
|---|---|
| Windows | `%APPDATA%\vlc\lua\extensions\` |
| Linux | `~/.local/share/vlc/lua/extensions/` |
| macOS | `~/Library/Application Support/org.videolan.vlc/lua/extensions/` |

Then in VLC: Tools › Plugins and extensions › *Active Extensions* tab › **Reload extensions**
(or restart VLC). Enable it under **View › Play Along**.

### 3. Use it

1. Open the media, start playback and pause it at the beginning. The extension needs a
   media loaded; if you open the window without one, it tells you so.
2. In the *Play Along* window, enter the server and your name. The host alone is enough
   (`192.168.0.10`, `myserver.example`); port 8765 is assumed. Use `host:port` for another port.
3. To start a session, choose the policy and click **Start session**. The 4-letter code
   appears in the *Session code* field.
4. On the other devices, open the same media, type the code and click **Join**. VLC jumps
   to the session position and follows from there.
5. **Leave session** leaves; **Reconnect** resumes syncing after network failures.

The window shows the session, your role (monarch / follower / participant), every device's
position and drift, and the last event (accepted or rejected actions, corrections received).

Server, name and language are remembered in `play_along.json` inside VLC's user data folder.

### Languages

The UI follows VLC's own language: the extension reads strings VLC translates itself (the
names of the playlist root nodes, "Playlist" and "Media Library", which exist even with no
media open) and picks the matching file in `lang/`. English is built in; `lang/pt.json`
adds Portuguese. To add a language, copy `pt.json` to `lang/<code>.json` (`<code>` is the
two-letter code, e.g. `es`), translate the values and add VLC's translations of "Playlist"
and "Media Library" to `NODE_TO_LANG` in `play_along.lua` (they are in VLC's `po/<code>.po`). The keys are the English texts used in the code,
read through the `T("English text")` helper. Set the environment variable
`PLAY_ALONG_LANG` (for example `en` or `pt`) to force a language.

## Good to know

- Everyone must open the **same media** (same length). Play Along syncs the position, not
  the file.
- VLC Lua extensions have no timer. The sync loop is chained through a trick (see below)
  and **only runs while a media is playing or paused**. With VLC stopped, the device drops
  out of the session after `--stale` seconds and comes back when the media is reopened.
- Each cycle blocks the extension for about a second (the long poll). Clicks in the window
  can take up to a second to react. If the server hangs without closing the connection,
  after 10 s VLC shows "Extension not responding — kill it?". Disabling the extension in
  the View menu also stops it.
- After 3 consecutive network failures the extension stops trying; click **Reconectar** or
  press play/pause to resume.
- The default device name is your computer or user name; it is sent to the server and
  shown to the other participants. Change it in the window if you prefer.
- Built and tested on VLC 3.0.21 (Windows, Qt interface). VLC for Android and iOS do not
  support Lua extensions. VLC 4 changes the Lua API and is not supported.

## Under the hood

Everything the extension relies on was checked against the VLC 3.0.x source code rather
than the (partly outdated) Lua README:

- `vlc.net` (TCP sockets) is available to extensions; `vlc.misc` (`mdate`/`mwait`) is not.
  Every callback runs on a single per-extension thread with a 10 s watchdog.
- To wake up each cycle without a timer, the extension changes a harmless item meta
  (`item:set_meta("setting", ...)`). VLC propagates that (playlist → Qt → extensions
  manager) and calls `meta_changed()` back; that callback runs the next cycle. The Qt
  interface delivers **two** `meta_changed` per change, so the cycle is only rescheduled
  every second event (`S.since`); the extra event becomes a quick cycle with no wait.
- Local action detection: each cycle compares the current player state with the previous
  one using the server clock (`server_now` from every response). A position jump above
  2.5 s becomes a "seek", a state change a "play"/"pause", a rate change a "rate". After
  applying a server command there is a 3 s grace period so the extension does not echo
  its own correction.
- Tunables at the top of `play_along.lua`: `TICK_MS`, `MAX_FAILURES`, `GRACE_S`,
  `JUMP_THRESHOLD`, `SEEK_THRESHOLD`.

## Protocol

HTTP/1.0 with JSON bodies. Every API response is HTTP 200 with `ok` (true/false) and
`server_now` (the server's monotonic clock, in seconds).

| route | body | response |
|---|---|---|
| `POST /api/create` | `device_id, device_name, policy, state, media` | `session`, `commands` |
| `POST /api/join` | `code, device_id, device_name, state?, media?` | `session`, `commands` (initial sync) |
| `POST /api/leave` | `code, device_id` | |
| `POST /api/end` | `code, device_id` (owner only) | |
| `POST /api/sync` | `code, device_id, state, media?, wait_ms?, action?` | `commands`, `session`, `accepted?` |
| `GET /api/sessions` | | session list |
| `GET /` | | HTML dashboard |

`policy` is `"anarchy"` or `"monarchy"`. `state` is `{playing, position, rate}` (position
in seconds). `action` is `{type: play|pause|seek|rate, position, rate, playing}`.

Commands from the server to a device:

- `{"type":"set_state","playing":bool,"position":sec|null,"rate":x,"at":t,"reason":"..."}` —
  target position is `position + (server_now - at) * rate` while playing; `null` means
  "do not seek, only change play/pause/rate" (used by the short-pause correction).
  `reason` is a short English text for logs and the window ("initial sync",
  "pause by Ana", "correction: seek (drift +6.0 s)", ...);
- `{"type":"session_ended"}`.

Errors come as `{"ok": false, "error": "<code>"}` with codes `session_not_found`,
`device_id_required`, `invalid_policy`, `owner_only`, `unknown_route`, `invalid_json`,
`internal_error`. The server itself is English only; the extension translates the error
codes it shows.

`server/sim_client.py` is a complete reference client in ~150 lines, handy as a starting
point for other players or platforms.

## Tests

- `python server/sim_client.py --server 127.0.0.1:8765` — simulated devices covering both
  policies, rejected actions, short-pause and seek corrections, and session end. Start the
  server with `--check-interval 1 --cooldown 1` to make it fast.
- `python extension/tests/headless_test.py` — starts the server, runs a real VLC **without
  a GUI** (`-I luaintf`) executing the actual extension logic, and checks pause/play/seek/rate
  in both directions, monarchy rejections and session end against a Python device. Needs VLC
  installed; pass `--vlc` for a custom executable path.

## Contributing

Issues and pull requests are welcome. Useful directions: UI translations, an Android or
iOS client built on libVLC speaking the same protocol, and testing on Linux and macOS.
