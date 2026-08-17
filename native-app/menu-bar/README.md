# Quickbot menu bar app

macOS menu bar app to start and stop the Quickbot server with one click.

This is a thin UI over the server component in [`../../server`](../../server):
all process lifecycle logic (start, stop, adoption, health checks) lives there,
in `serverctl`. The app just invokes it and renders `serverctl status --json`,
polling every 3s.

The menu bar icon comes from [lucide](https://lucide.dev):

| State | Icon |
|---|---|
| Server on | [`bot`](https://lucide.dev/icons/bot) |
| Server off / starting / failed | [`bot-off`](https://lucide.dev/icons/bot-off) |

The icons are vector PDFs marked as *template*, so they automatically follow
light/dark mode and Retina displays.

## Installation

```bash
./scripts/build.sh
open ~/Applications/Quickbot.app
```

No system permissions required (no accessibility, no screen recording).

To launch together with your Mac, use the **"Start Quickbot at login"** menu item.

The app expects the server component at
`~/Devland/_experimental/quickbot/server`; set `QUICKBOT_SERVER_DIR` to
override.

## Usage

Click the menu bar icon to open the menu:

- **Quickbot switch** — first item, an actual switch with a green track when
  on. Flipping it keeps the menu open, so you can watch the state change;
  flipping it off while the model is loading cancels the startup
- **Show chat** (⌘⇧;) — brings up [Quickbot Chat](../chat)'s main window
  (launching the app if it is not running)
- **Copy API endpoint** — copies `http://127.0.0.1:8080/v1`
- **Start Quickbot at login**
- **Quit Quickbot**

Server state details (uptime, PID, log, `config.json`) live in the server
component: `quickbot status` / `quickbot log` (see below).

### States

| State | Meaning |
|---|---|
| Off | No server on the port |
| Starting… | Process is up, loading the model (takes ~40s) |
| On | `/health` responding |
| Stopping… | SIGTERM sent |
| Failed | Startup error; details in the menu and in the log |

The icon only switches to `bot` when the server actually responds to the health check.

## Behavior

- **Adopts external servers**: if the server was started outside the app (via
  `serverctl` or the CLI), the app detects it within 3s and shows `bot`.
- **Single instance**: opening the app twice does not create two menu bar icons.
- **Quitting does not take the server down** (unless `stopServerOnQuit` is
  `true` in the server's `config.json`).
- If the server dies externally, the icon reverts to `bot-off` on its own.

## Command line (optional)

`scripts/quickbot` is a thin wrapper over the server's `serverctl` — the app
does not need to be running:

```bash
./scripts/quickbot on       # start
./scripts/quickbot off      # stop
./scripts/quickbot toggle   # toggle
./scripts/quickbot status   # app + server state
./scripts/quickbot health   # exit 0 if /health responds
./scripts/quickbot log      # follow the log
```

Useful for keyboard shortcuts (Shortcuts app → "Run Shell Script").

To use it from anywhere:

```bash
ln -sf ~/Devland/_experimental/quickbot/native-app/menu-bar/scripts/quickbot /usr/local/bin/quickbot
```

## Configuration and log

Both belong to the server component:

- Config: `~/Devland/_experimental/quickbot/server/config.json`
- Log: `~/Library/Logs/Quickbot/server.log`

See [`../../server/README.md`](../../server/README.md) for the options.

## Structure

```
Sources/Icons.swift             lucide bot / bot-off icons
Sources/ServerController.swift  thin client over serverctl + UI state machine
Sources/main.swift              menu bar and menu
icons/                          original lucide SVGs + generated PDFs
scripts/build.sh                builds and installs the .app
scripts/quickbot                optional CLI (wraps serverctl)
```

Icon license: [lucide](https://github.com/lucide-icons/lucide/blob/main/LICENSE) (ISC).
