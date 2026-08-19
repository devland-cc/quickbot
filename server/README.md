# Quickbot server

The server component of Quickbot: owns the whole inference server lifecycle.
Every other component (the menu bar app, the CLI) drives the server exclusively
through `serverctl` — none of them talk to the process, the port or the PID
directly.

Everything in Quickbot runs on MLX. The two **engines** are serving
processes, not competing runtimes:

- **`mlx_vlm`** (default): `mlx_vlm.server` with the local models in
  `../models/`, MTP speculative decoding and APC prompt caching.
- **`ollama`**: `ollama serve` **always** on Ollama's MLX runner
  (`OLLAMA_LLM_LIBRARY=mlx`, registry tags that contain `mlx`, e.g.
  `qwen3.8:27b-mlx`). GGUF / llama.cpp / Metal is out of scope. Seed an
  `ollama:` address in `catalog_seed.list` (or use the leftover
  `ollamaModel` in `config.json`).

There is no `backend: mlx | ollama` compute split. `serverctl backend` is a
deprecated alias of `serverctl engine`; the old spelling `mlx` still means
`mlx_vlm`.

Engine differences to keep in mind:

- MTP speculative decoding and APC prompt caching are mlx_vlm-only; the ollama
  engine pays the full prefill on later search rounds (a same-shape query
  took ~56s on ollama vs ~34s on mlx_vlm).
- Through Ollama the model always reasons before answering — its OpenAI
  endpoint ignores `enable_thinking` (and `think`, and Qwen's `/no_think`),
  so the app's Thinking toggle only has effect on the mlx_vlm engine. The proxy
  normalizes Ollama's `delta.reasoning` to `delta.reasoning_content` so
  clients see one contract.

A model catalog (see below) picks which model to serve from the machine's RAM
tier. Without the catalog files — or before `setup` installs PyYAML —
`serverctl` falls back to `config.json` and never crashes.

## Layout

```
serverctl              entry point (bash shim → serverctl.py)
serverctl.py           start/stop/adopt/health/status (stdlib only)
engines.py             engine façade (mlx_vlm / ollama)
catalog.py             catalog resolve / sync / measure (stdlib at import)
catalog_seed.list      user-managed model addresses
catalog_tiers.yml      user-managed RAM tiers + overrides
catalog.json           machine-managed measured facts (do not edit)
catalog_settings.yml   hybrid: CLI-managed index + per-model config
config.json            infra + legacy fallback (created on first run)
profile.json           machine profile (created by setup / `serverctl profile`)
requirements.lock      exact package versions (uv pip freeze)
.venv/                 Python 3.12 venv with mlx-vlm (not versioned)
.server.pid            PID of a server started by serverctl (not versioned)
```

The models live next door, in `../models/`.

Installed builds have no `.venv`: `release.sh` embeds a relocatable CPython
(python-build-standalone) as `python-runtime.tar.gz` next to these files,
the shim unpacks it into `~/Library/Application Support/Quickbot/runtime`
on first run (bash + tar only, so a Mac without Python works), and
`quickbot setup` pip-installs `requirements.lock` straight into that
runtime — no venv, nothing pointing back into the app bundle, no Python on
the user's system. A legacy 0.1.0 `venv/` in the data dir keeps working and
takes precedence; delete it and re-run setup to switch to the runtime.

## Usage

```bash
./serverctl start                 # start the server (loading the model takes ~40s)
./serverctl stop                  # SIGTERM, SIGKILL after 20s, port cleanup
./serverctl toggle
./serverctl engine                # print the active engine (mlx_vlm or ollama)
./serverctl engine ollama         # switch serving process; restarts if it was up
./serverctl backend               # deprecated alias of `engine` (`mlx` → mlx_vlm)
./serverctl catalog list
./serverctl catalog sync          # apply catalog_seed.list
./serverctl catalog validate
./serverctl catalog measure <id>
./serverctl profile               # re-detect chip / RAM / disk
./serverctl status                # human-readable state
./serverctl status --json         # machine-readable (what the menu bar app polls)
./serverctl health                # exit 0 if /health responds
./serverctl log                   # tail -f the server log
```

States: `running` (port listening and `/health` responding), `starting`
(process alive but not healthy yet — the model is loading and the port may not
even be bound), `stopped`.

If a server is already listening on the port — started from a terminal, for
example — `serverctl` adopts it instead of starting a second one (`status`
marks it as external).

## Configuration

### Model catalog

Four files live next to `config.json` (repo checkout: `server/`; installed
builds: `~/Library/Application Support/Quickbot/`):

| File | Who writes it | Role |
|---|---|---|
| `catalog_seed.list` | you | Addresses, one per line (`hf:` default, or `ollama:`). Draft/MTP models are **not** seed entries — they hang off the settings block's `decoder`. |
| `catalog_tiers.yml` | you | RAM tiers (8/16/32 GB), `headroomGB`, and overrides that apply **last**. |
| `catalog.json` | `serverctl` | Measured facts (weights, peak GB, tok/s, capabilities). Read-only. |
| `catalog_settings.yml` | hybrid | The `index:` region between the `QUICKBOT-INDEX` markers is rewritten by `catalog sync`. Everything under `models:` is yours. A leading `*` in the index pins that model for auto-selection. |

**Precedence** (later wins): engine defaults < `catalog.json` facts < the
per-model settings block < the tier's `overrides`. `catalog validate` warns
on every clamp. Duplicate pins in a tier fail gracefully: the first `*` wins
and the rest warn (like conflicting CSS `!important`).

Resolution runs on every `start` / `status` (no cache). The largest tier at
or below the machine's RAM is chosen; if that tier has no pin, it falls
through to the next smaller one (32→16→8). If every tier is empty, behaviour
is the legacy `config.json` path. A single trace line is appended to the
server log:

```
resolved: tier=32 pin=hf:mlx-community/Qwen3.8-27B-4bit engine=mlx_vlm ctx=30720
```

Shipped pins (Qwen3.8 has no size below 27B; smaller tiers use the previous Qwen3.5 generation, same `qwen3_5` architecture, MLX 4-bit + MTP):

| RAM tier | Auto-selected model | Weights |
|---|---|---|
| 32 GB | `Qwen3.8-27B-4bit` + MTP | ~16 GB |
| 16 GB | `Qwen3.5-9B-MLX-4bit` + MTP | ~6 GB |
| 8 GB | `Qwen3.5-4B-MLX-4bit` + MTP | ~3 GB |

Unmeasured models (added by `sync`, not yet `measure`d) get
`estPeak = weightsGB * 1.2 + 2.0` from the Hugging Face API and are inserted
into every tier where that estimate plus headroom still fits.

`catalog measure` / `validate --live` / a measuring `sync` offload the stack
first (`DATA_DIR/.catalog-operation.json`). The menu bar shows **"Quickbot
unavailable during catalog operations"** and disables the switch until the
command finishes (or, after a `kill -9`, until the next `status` notices the
dead pid and drops the stale marker).

### `config.json`

Created with defaults on first run. **While the catalog is active** these keys
are ignored (the catalog supplies them): `model`, `draftModel`, `executable`,
`backend` (leftover alias of the engine id), `envVars`, `extraArgs`. These **stay live**:

- `port` / `proxyPort` / `host` / `webSearch` / `logFile` / `stopServerOnQuit`
- `ollamaExecutable` / `ollamaPort` / `ollamaEnvVars`

```json
{
  "backend": "mlx_vlm",
  "executable": ".../server/.venv/bin/mlx_vlm.server",
  "model": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-4bit",
  "draftModel": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-MTP-4bit",
  "port": 8080,
  "ollamaExecutable": "/opt/homebrew/bin/ollama",
  "ollamaModel": "qwen3.8:27b-mlx",
  "ollamaPort": 11434,
  "ollamaEnvVars": { "OLLAMA_KEEP_ALIVE": "1h", "OLLAMA_FLASH_ATTENTION": "1" },
  "host": "127.0.0.1",
  "extraArgs": [],
  "envVars": { "APC_ENABLED": "1" },
  "healthPath": "/health",
  "logFile": "~/Library/Logs/Quickbot/server.log",
  "stopServerOnQuit": false
}
```

- `backend`: leftover alias of the engine id (`mlx_vlm` or `ollama`). Old
  files may still say `"mlx"`; that means `mlx_vlm`. Switch with
  `serverctl engine <name>` (`backend` is the same command). With the catalog
  active the engine is set per model in `catalog_settings.yml`; this command
  only updates the leftover fallback. Both values are MLX serving processes.
- `extraArgs`: additional `mlx_vlm.server` flags, e.g. `["--max-tokens", "4096"]`
  (legacy path only; catalog models use the settings block's `extraArgs`).
- `envVars`: extra environment variables for `mlx_vlm.server`. `APC_ENABLED=1`
  enables prompt caching — without it the server redoes the full system prompt
  prefill on every request (~110s for 13k tokens).
- `ollamaEnvVars`: environment for `ollama serve`. `OLLAMA_KEEP_ALIVE=1h`
  keeps the model loaded between requests; Ollama's default (`5m`) frees
  ~18 GB when idle but costs a ~30s reload on the next prompt.
- `stopServerOnQuit`: if `true`, quitting the menu bar app also stops the
  server (default `false`, so the model isn't taken down by accident).

`mlx_vlm.server` accepts `--max-kv-size` (KV cache in tokens). When the
catalog is active, `contextLength` is passed through that flag; on ollama it
maps to `OLLAMA_CONTEXT_LENGTH`. On the legacy path the flag is omitted, so
start/stop behaviour is unchanged.

## Connecting a client

The server exposes an OpenAI-compatible API at `http://127.0.0.1:8080/v1`
(no API key). Two things to keep in mind when pointing a client at it:

1. **The model name must be the full path** (mlx_vlm engine). `mlx_vlm.server`
   uses that field as the load path; a short name like `Qwen3.8-27B-4bit`
   makes it try to download from HuggingFace and fail with a 401. Use
   `/Users/danieldrehmer/Devland/_experimental/quickbot/models/Qwen3.8-27B-4bit`.
   On the ollama engine the model name is the MLX registry tag instead
   (`qwen3.8:27b-mlx`) — clients should always take the name from
   `GET /v1/models` rather than hardcode it.
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
