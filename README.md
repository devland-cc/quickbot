# Quickbot

A fully local LLM assistant for Apple silicon Macs. Quickbot runs
[Qwen3.8-27B](https://huggingface.co/mlx-community/Qwen3.8-27B-4bit) on-device
with [MLX](https://github.com/ml-explore/mlx) and MTP speculative decoding,
wrapped in a native menu bar app and a chat interface that is one keystroke
away.

- **Menu bar app** — one switch turns the whole stack on and off (server,
  models, chat app). Shows live startup progress while the model loads.
- **Quickbot Chat** — native chat UI (a fork of
  [Enchanted](https://github.com/gluonfield/enchanted)). Runs silently in the
  background: no Dock icon, no windows until you ask.
- **Server** — `mlx_vlm.server` behind `serverctl`, exposing an
  OpenAI-compatible API at `http://127.0.0.1:8080/v1` for any other client.

Everything runs offline. Nothing leaves the machine.

## Shortcuts

| Shortcut | Action |
| --- | --- |
| ⌘; | Open/close the prompt panel |
| ⌘⇧; | Show/hide the chat window |

When Quickbot is off, both shortcuts open the menu bar menu instead.

## Install (Homebrew)

```sh
brew tap devland-cc/tap
brew install --cask --no-quarantine quickbot
quickbot setup   # installs the Python environment and downloads the models (~16 GB)
```

`--no-quarantine` is needed because the apps are ad-hoc signed, not notarized.

`quickbot setup` downloads the models from their original Hugging Face
repositories ([mlx-community/Qwen3.8-27B-4bit](https://huggingface.co/mlx-community/Qwen3.8-27B-4bit)
and [mlx-community/Qwen3.8-27B-MTP-4bit](https://huggingface.co/mlx-community/Qwen3.8-27B-MTP-4bit))
into `~/Library/Application Support/Quickbot/models`, next to the Python
environment and the server config. Interrupted downloads resume on re-run.

Then launch **Quickbot** from `/Applications` and flip the switch in the menu
bar.

### Requirements

- Apple silicon Mac with enough unified memory for the model (~16 GB of
  weights; 24 GB+ of RAM recommended)
- macOS 14 (Sonoma) or newer

## Repository layout

| Path | Component |
| --- | --- |
| `server/` | `serverctl` + `mlx_vlm.server` lifecycle management |
| `native-app/menu-bar/` | Quickbot menu bar app |
| `native-app/chat/` | Quickbot Chat (Enchanted fork) |
| `models/` | Model weights (git-ignored; downloaded by `quickbot setup`) |
| `scripts/release.sh` | Builds the distributable tarball |

## Development

Each app builds with its own `scripts/build.sh` (SwiftPM/`swiftc` only — no
Xcode required; see `native-app/chat/README.md` for the SDK notes). In a repo
checkout, `serverctl` keeps its state in the repo (`server/.venv`, `models/`,
`server/config.json`); installed builds use
`~/Library/Application Support/Quickbot` instead. `QUICKBOT_SERVER_DIR` and
`QUICKBOT_DATA_DIR` override the component and data locations.

Cutting a release:

```sh
scripts/release.sh <version>
gh release create v<version> build/release/quickbot-<version>.tar.gz
```

Then update `version` and `sha256` in the cask at
[devland-cc/homebrew-tap](https://github.com/devland-cc/homebrew-tap).
