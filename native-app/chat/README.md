# Quickbot Chat

Native macOS chat UI for the Quickbot server. A fork of
[Enchanted](https://github.com/gluonfield/enchanted) (forked at upstream
commit `dc9bff8`) adapted to Quickbot:

- Talks to the Quickbot server's **OpenAI-compatible API** (the original app
  spoke the Ollama API).
- **Auto-configures** itself: on launch (and whenever the server becomes
  unreachable) it asks the server component for the endpoint and model via
  `serverctl status --json`, so there is nothing to set up.
- **Invocation mode** on **⌘;** — a global hotkey that summons a floating
  prompt panel from any app (Spotlight-style). Pressing it with text selected
  offers completions (fix grammar, summarize, …) typed back into the app you
  came from. The shortcut is configurable in the completions editor.

## Installation

```bash
./scripts/build.sh
open ~/Applications/Quickbot\ Chat.app
```

The app expects the server component at
`~/Devland/_experimental/quickbot/server`; set `QUICKBOT_SERVER_DIR` to
override. Start/stop the server with the Quickbot menu bar app or
`serverctl` — Quickbot Chat is only a client.

Completions (typing into other apps) ask for Accessibility permission on
first use; plain chat needs no permissions.

## Usage

The app starts **silently**: no Dock icon, no window — it just arms the
⌘; hotkey (~44 MB in standby). The chat window appears only on explicit
request:

- **⌘;** anywhere → floating prompt panel (Esc closes it); submitting a
  prompt from the panel opens the chat window with the conversation
- **⌘⇧;** anywhere → toggles the chat window (shows it hidden, hides it
  visible); also via **"Show chat"** in the Quickbot menu bar app
- The chat window shows like the panel does — no Dock icon, ever; closing
  it just hides it
- Settings (⌘,): endpoint (auto-filled), system prompt, default model

## Build notes (no Xcode required)

The upstream Xcode project was replaced with SwiftPM (`Package.swift`)
so the app builds with Command Line Tools only:

- The build pins the macOS 26.5 SDK: newer SDKs turn SwiftUI property
  wrappers (`@State`, …) into compiler macros whose plugins ship only with
  Xcode.
- SwiftData was replaced with plain `@Observable` models plus a JSON store
  (`~/Library/Application Support/Quickbot Chat/store.json`) for the same
  reason (`@Model` needs Xcode's `SwiftDataMacros`).
- `KeyboardShortcuts` is vendored in `Vendor/` with its `#Preview` blocks
  stripped (previews need Xcode's `PreviewsMacros`).
- Asset catalogs were replaced with code-defined colors
  (`QuickbotChat/Helpers/AssetColors.swift`) and loose PNGs in `icons/`
  (no `actool` outside Xcode).

## Structure

```
Package.swift                   SwiftPM manifest (deps + target)
QuickbotChat/                   app sources (fork of Enchanted's sources)
  Application/QuickbotChatApp   entry point, ⌘; hotkey definition
  Services/QuickbotService      OpenAI-compatible client + serverctl auto-config
  Services/SwiftDataService     JSON-backed persistence (same API as before)
  UI/macOS/PromptPanel/         floating panel (invocation mode)
Vendor/KeyboardShortcuts        vendored dependency (previews stripped)
icons/                          app icon + logo (lucide bot, Quickbot green)
scripts/build.sh                builds and installs the .app
```

License: [Apache 2.0](LICENSE) (from upstream Enchanted).
Icon: [lucide](https://lucide.dev) (ISC).
