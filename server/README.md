# Quickbot server

The server component of Quickbot: owns the whole `mlx_vlm.server` lifecycle.
Every other component (the menu bar app, the CLI) drives the server exclusively
through `serverctl` — none of them talk to the process, the port or the PID
directly.

## Layout

```
serverctl           entry point (bash shim → serverctl.py)
serverctl.py        start/stop/adopt/health/status logic (stdlib only)
config.json         server configuration (created on first run)
requirements.lock   exact package versions (uv pip freeze)
.venv/              Python 3.12 venv with mlx-vlm (not versioned)
.server.pid         PID of a server started by serverctl (not versioned)
```

The models live next door, in `../models/`.

## Usage

```bash
./serverctl start           # start the server (loading the model takes ~40s)
./serverctl stop            # SIGTERM, SIGKILL after 20s, port cleanup
./serverctl toggle
./serverctl status          # human-readable state
./serverctl status --json   # machine-readable (what the menu bar app polls)
./serverctl health          # exit 0 if /health responds
./serverctl log             # tail -f the server log
```

States: `running` (port listening and `/health` responding), `starting`
(process alive but not healthy yet — the model is loading and the port may not
even be bound), `stopped`.

If a server is already listening on the port — started from a terminal, for
example — `serverctl` adopts it instead of starting a second one (`status`
marks it as external).

## Configuration

`config.json`, created with defaults on first run:

```json
{
  "executable": ".../server/.venv/bin/mlx_vlm.server",
  "model": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-4bit",
  "draftModel": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-MTP-4bit",
  "port": 8080,
  "host": "127.0.0.1",
  "extraArgs": [],
  "envVars": { "APC_ENABLED": "1" },
  "healthPath": "/health",
  "logFile": "~/Library/Logs/Quickbot/server.log",
  "stopServerOnQuit": false
}
```

- `extraArgs`: additional flags, e.g. `["--max-tokens", "4096"]`
- `envVars`: extra environment variables. `APC_ENABLED=1` enables prompt
  caching — without it the server redoes the full system prompt prefill on
  every request (~110s for 13k tokens).
- `stopServerOnQuit`: if `true`, quitting the menu bar app also stops the
  server (default `false`, so the model isn't taken down by accident).

## Connecting a client

The server exposes an OpenAI-compatible API at `http://127.0.0.1:8080/v1`
(no API key). Two things to keep in mind when pointing a client at it:

1. **The model name must be the full path.** `mlx_vlm.server` uses that field
   as the load path; a short name like `Qwen3.8-27B-4bit` makes it try to
   download from HuggingFace and fail with a 401. Use
   `/Users/danieldrehmer/Devland/_experimental/quickbot/models/Qwen3.8-27B-4bit`.
2. **Maximum context window of 32768 on this machine.** The model accepts
   262144, but the KV cache would be 68.7 GB (the model already uses ~25 GB of
   the 32 GB). Tested: 30k works with a 27.8 GB peak; above that the GPU fails
   with `Insufficient Memory`.

### Performance

- **Generation:** ~17.7 tok/s on predictable text (MTP/speculative decoding
  working), dropping to 7-10 tok/s on free-form prose, where the draft model
  guesses less accurately.
- **Prefill:** a large system prompt (~13k tokens) takes ~100s on the first
  call. With `APC_ENABLED=1` subsequent turns of the same conversation drop to
  ~1s, because the already-processed prefix is reused. Prompts whose prefix
  changes (e.g. a system prompt embedding the current date/time) pay the
  prefill again.
- APC costs memory (the peak went from ~21 to ~25 GB). To turn it off, remove
  `APC_ENABLED` from `envVars` in `config.json` and restart the server.

One request at a time: two simultaneous 13k-token generations blow past the
GPU's memory.

## Recreating the venv

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.lock
```
