#!/usr/bin/env python3
"""Quickbot server controller.

Owns the whole inference-server lifecycle: start, stop, adoption of externally
started servers, health checks, status reporting, and the model catalog.
Every other component (menu bar app, CLI) drives the server exclusively
through this script.

Usage: serverctl {setup|start|stop|toggle|engine [mlx_vlm|ollama]|backend [mlx_vlm|ollama]|catalog {sync|validate|measure|list}|profile [--json]|status [--json]|health|log}
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import catalog
import engines

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))

# Layout: when this script runs from a repo checkout, everything lives in the
# repo (venv in server/.venv, models in ../models). When it runs from an
# installed location (inside an app bundle) — or QUICKBOT_DATA_DIR is set —
# mutable state moves to a per-user data directory so the install stays
# read-only and model downloads land somewhere unobtrusive.
INSTALLED = ".app/Contents/" in SERVER_DIR
DATA_DIR = os.environ.get("QUICKBOT_DATA_DIR") or (
    os.path.expanduser("~/Library/Application Support/Quickbot")
    if INSTALLED else SERVER_DIR
)
RELOCATED = INSTALLED or bool(os.environ.get("QUICKBOT_DATA_DIR"))

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
PID_FILE = os.path.join(DATA_DIR, ".server.pid")
PROXY_PID_FILE = os.path.join(DATA_DIR, ".proxy.pid")
VENV_DIR = (os.path.join(DATA_DIR, "venv") if RELOCATED
            else os.path.join(SERVER_DIR, ".venv"))
# Private Python runtime: installed copies bundle python-build-standalone as
# server/python-runtime.tar.gz; the serverctl shim (or _extract_runtime as a
# fallback) unpacks it into the data dir, and setup pip-installs straight
# into it — no venv, so nothing points back into the app bundle.
RUNTIME_DIR = os.path.join(DATA_DIR, "runtime")
RUNTIME_TARBALL = os.path.join(SERVER_DIR, "python-runtime.tar.gz")
MODELS_DIR = (os.path.join(DATA_DIR, "models") if RELOCATED
              else os.path.join(os.path.dirname(SERVER_DIR), "models"))


def env_bin_dir():
    """bin/ of the Python environment holding the server packages: the repo
    .venv (dev), the private runtime, or a legacy 0.1.0 venv in the data
    dir. Falls back to the path setup would create."""
    candidates = [os.path.join(SERVER_DIR, ".venv/bin"),
                  os.path.join(RUNTIME_DIR, "bin"),
                  os.path.join(DATA_DIR, "venv/bin")]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[1] if RELOCATED else candidates[0]

# Models served by Quickbot, downloaded by `setup` from their original
# Hugging Face repositories.
MODELS = [
    {"repo": "mlx-community/Qwen3.8-27B-4bit",
     "dir": "Qwen3.8-27B-4bit", "size": "~16 GB"},
    {"repo": "mlx-community/Qwen3.8-27B-MTP-4bit",
     "dir": "Qwen3.8-27B-MTP-4bit", "size": "~240 MB"},
]

CLI_NAME = os.environ.get("QUICKBOT_CLI_NAME", "serverctl")

DEFAULT_CONFIG = {
    # Serving process. Both engines are MLX: "mlx_vlm" is mlx_vlm.server,
    # "ollama" is ollama serve with the MLX runner. The leftover key
    # "backend" is an alias of the engine id; the old spelling "mlx"
    # still maps to mlx_vlm.
    "backend": "mlx_vlm",
    "executable": os.path.join(env_bin_dir(), "mlx_vlm.server"),
    "model": os.path.join(MODELS_DIR, MODELS[0]["dir"]),
    "draftModel": os.path.join(MODELS_DIR, MODELS[1]["dir"]),
    "port": 8080,
    "ollamaExecutable": "/opt/homebrew/bin/ollama",
    "ollamaModel": "qwen3.8:27b-mlx",
    "ollamaPort": 11434,
    # Keep the model loaded between requests; "5m" (Ollama's default) frees
    # ~18 GB when idle but costs a ~30s reload on the next prompt.
    "ollamaEnvVars": {"OLLAMA_KEEP_ALIVE": "1h", "OLLAMA_FLASH_ATTENTION": "1"},
    # Tool proxy: OpenAI-compatible front that gives the model web search.
    "proxyPort": 8081,
    "webSearch": True,
    "host": "127.0.0.1",
    "extraArgs": [],
    # Prompt caching: without APC the server redoes the full system prompt
    # prefill on every request (~110s for 13k tokens).
    "envVars": {"APC_ENABLED": "1"},
    "healthPath": "/health",
    "logFile": "~/Library/Logs/Quickbot/server.log",
    "stopServerOnQuit": False,
}


def expand(path):
    return os.path.expanduser(path)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")


def upstream_port(cfg):
    return engines.get_engine(cfg).upstream_port()


def health_path(cfg):
    return engines.get_engine(cfg).health_path()


def log_file(cfg):
    path = expand(cfg["logFile"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def endpoint(cfg):
    """Endpoint clients should use: the tool proxy when it is up,
    the inference server directly otherwise."""
    if proxy_alive(cfg):
        return proxy_endpoint(cfg)
    return upstream_endpoint(cfg)


def upstream_endpoint(cfg):
    return "http://{}:{}/v1".format(cfg["host"], upstream_port(cfg))


def proxy_endpoint(cfg):
    return "http://{}:{}/v1".format(cfg["host"], cfg.get("proxyPort", 8081))


def server_command(cfg):
    return engines.get_engine(cfg).server_command()


# --- Process inspection ---------------------------------------------------


def is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def listening_pid(port):
    """PID of the process listening on a TCP port (via lsof)."""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-iTCP:{}".format(port), "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    for line in out.split():
        try:
            return int(line)
        except ValueError:
            continue
    return None


def pidfile_pid():
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        return pid if is_alive(pid) else None
    except (OSError, ValueError):
        return None


def process_start_epoch(pid):
    try:
        raw = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not raw:
            return None
        parsed = time.strptime(" ".join(raw.split()), "%a %b %d %H:%M:%S %Y")
        return time.mktime(parsed)
    except Exception:
        return None


def health_ok(cfg):
    url = "http://{}:{}{}".format(cfg["host"], upstream_port(cfg), health_path(cfg))
    try:
        with urllib.request.urlopen(url, timeout=2.5) as resp:
            return 200 <= resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def current_state(cfg):
    """(state, pid, adopted) for the server right now.

    running  = port listening and /health responding
    starting = process alive but not healthy yet (model loading; the port may
               not even be bound yet)
    stopped  = nothing alive
    """
    lpid = listening_pid(upstream_port(cfg))
    own = pidfile_pid()
    if lpid is not None:
        adopted = own is None or own != lpid
        return ("running" if health_ok(cfg) else "starting", lpid, adopted)
    if own is not None:
        return ("starting", own, False)
    return ("stopped", None, False)


# --- Tool proxy lifecycle -------------------------------------------------


def proxy_alive(cfg):
    return listening_pid(cfg.get("proxyPort", 8081)) is not None


def start_proxy(cfg):
    if proxy_alive(cfg):
        return
    # Run the proxy on the interpreter serverctl itself runs under (the shim
    # already picked the right environment, whatever the backend). uvicorn
    # next to it marks a full environment — the proxy needs fastapi/httpx/
    # uvicorn, while serverctl runs on any Python.
    python = sys.executable
    script = os.path.join(SERVER_DIR, "toolproxy.py")
    uvicorn = os.path.join(os.path.dirname(python), "uvicorn")
    if not (os.path.exists(uvicorn) and os.path.exists(script)):
        return
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["QUICKBOT_UPSTREAM"] = "http://{}:{}".format(cfg["host"], upstream_port(cfg))
    env["QUICKBOT_UPSTREAM_HEALTH"] = health_path(cfg)
    env["QUICKBOT_PROXY_PORT"] = str(cfg.get("proxyPort", 8081))
    env["QUICKBOT_WEB_SEARCH"] = "1" if cfg.get("webSearch", True) else "0"
    with open(log_file(cfg), "ab") as lf:
        proc = subprocess.Popen(
            [python, script], stdout=lf, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    with open(PROXY_PID_FILE, "w") as f:
        f.write(str(proc.pid))


def stop_proxy(cfg):
    targets = set()
    try:
        with open(PROXY_PID_FILE) as f:
            targets.add(int(f.read().strip()))
    except (OSError, ValueError):
        pass
    lpid = listening_pid(cfg.get("proxyPort", 8081))
    if lpid is not None:
        targets.add(lpid)
    for pid in targets:
        _kill(pid, signal.SIGTERM)
    deadline = time.time() + 5
    while time.time() < deadline and any(is_alive(p) for p in targets):
        time.sleep(0.1)
    for pid in targets:
        if is_alive(pid):
            _kill(pid, signal.SIGKILL)
    try:
        os.remove(PROXY_PID_FILE)
    except OSError:
        pass


# --- Commands -------------------------------------------------------------


def cmd_start(cfg):
    try:
        cfg, trace = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    state, pid, _ = current_state(cfg)
    if state != "stopped":
        start_proxy(cfg)  # heal a missing proxy next to a live server
        print("Server already {} (PID {}).".format(state, pid))
        return 0

    eng = engines.get_engine(cfg)
    reason = eng.missing_reason()
    if reason:
        print(reason, file=sys.stderr)
        return 1

    cmd = eng.server_command()
    log = log_file(cfg)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(log, "a") as lf:
        lf.write("\n===== Quickbot start {} =====\n{}\n".format(stamp, " ".join(cmd)))
        if trace:
            lf.write("{}\n".format(trace))

    env = eng.env()

    with open(log, "ab") as lf:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    start_proxy(cfg)
    eng.post_start()
    print("Starting {} server (PID {}). Loading the model takes ~40s."
          .format(engines.engine_name(cfg), proc.pid))
    return 0


def cmd_stop(cfg):
    try:
        cfg, _ = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    stop_proxy(cfg)
    state, target, _ = current_state(cfg)
    if target is None:
        print("Server already stopped.")
        _remove_pidfile()
        return 0

    # SIGTERM first: uvicorn does a clean shutdown.
    _kill(target, signal.SIGTERM)
    deadline = time.time() + 20
    while time.time() < deadline and is_alive(target):
        time.sleep(0.2)

    if is_alive(target):
        _kill(target, signal.SIGKILL)
        deadline = time.time() + 5
        while time.time() < deadline and is_alive(target):
            time.sleep(0.1)

    # Make sure the port is actually free (orphaned uvicorn processes).
    leftover = listening_pid(upstream_port(cfg))
    if leftover is not None and leftover != target:
        _kill(leftover, signal.SIGTERM)
        time.sleep(1.0)
        if is_alive(leftover):
            _kill(leftover, signal.SIGKILL)

    _remove_pidfile()
    print("Server stopped.")
    return 0


def _kill(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _remove_pidfile():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def cmd_toggle(cfg):
    try:
        cfg, _ = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    state, _, _ = current_state(cfg)
    return cmd_stop(cfg) if state in ("running", "starting") else cmd_start(cfg)


def cmd_engine(cfg, args, via_backend=False):
    current_engine = engines.engine_name(cfg)
    if not args:
        print(current_engine)
        return 0
    choice = args[0]
    if choice == "mlx":
        choice = "mlx_vlm"
    if choice not in engines.ENGINES:
        print("usage: {} {} [{}]".format(
            CLI_NAME, "backend" if via_backend else "engine",
            "|".join(engines.ENGINES)),
              file=sys.stderr)
        return 2
    engine = choice
    if engine == current_engine:
        print("Engine already {}.".format(engine))
        return 0
    if catalog.catalog_active():
        print("Note: with the catalog active, the engine is set per model "
              "in catalog_settings.yml. Updating config.json for the legacy "
              "fallback only.")
    try:
        effective, _ = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    state, _, _ = current_state(effective)
    was_up = state != "stopped"
    if was_up:
        cmd_stop(effective)  # stop the old engine before the ports change meaning
    cfg["engine"] = engine
    cfg["backend"] = engine  # leftover key; same value as engine, both are MLX
    save_config(cfg)
    print("Engine set to {}.".format(engine))
    return cmd_start(load_config()) if was_up else 0


def cmd_backend(cfg, args):
    return cmd_engine(cfg, args, via_backend=True)


def cmd_status(cfg, as_json):
    try:
        cfg, _ = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    state, pid, adopted = current_state(cfg)
    started = process_start_epoch(pid) if pid is not None else None
    if engines.engine_name(cfg) == "ollama":
        model_name = model_path = cfg.get("ollamaModel") or cfg.get("model")
        draft = None
    else:
        model_path = expand(cfg["model"])
        model_name = os.path.basename(model_path.rstrip("/"))
        draft = cfg.get("draftModel")
    op = catalog.current_operation()
    engine = engines.engine_name(cfg)
    info = {
        "state": state,
        "pid": pid,
        "adopted": adopted,
        "engine": engine,
        "backend": engine,  # deprecated alias of engine; never "mlx" vs "ollama"
        "startedAtEpoch": started,
        "modelName": model_name,
        "modelPath": model_path,
        "draftModelPath": expand(draft) if draft else None,
        "endpoint": endpoint(cfg),
        "webSearch": bool(cfg.get("webSearch", True)) and proxy_alive(cfg),
        "configFile": CONFIG_FILE,
        "logFile": log_file(cfg),
        "stopServerOnQuit": bool(cfg.get("stopServerOnQuit", False)),
        "catalogOperation": (op or {}).get("label") if op else None,
        "tier": cfg.get("tier"),
        "displayName": cfg.get("displayName"),
    }
    if as_json:
        print(json.dumps(info))
        return 0

    print("State:    {}".format(state))
    if pid is not None:
        print("PID:      {}{}".format(pid, " (external)" if adopted else ""))
    if started is not None and state == "running":
        secs = int(time.time() - started)
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        up = "{}h {:02d}min".format(h, m) if h else ("{}min {:02d}s".format(m, s) if m else "{}s".format(s))
        print("Uptime:   {}".format(up))
    if info["catalogOperation"]:
        print("Catalog:  {}".format(info["catalogOperation"]))
    print("Engine:   {}".format(info["engine"]))
    print("Model:    {}".format(info["modelName"]))
    print("Endpoint: {}".format(info["endpoint"]))
    print("Config:   {}".format(info["configFile"]))
    print("Log:      {}".format(info["logFile"]))
    return 0


# --- Setup (installer) ----------------------------------------------------


def _find_python():
    """Interpreter used to create the venv. mlx needs a recent Python and
    the lock file was resolved under 3.12, so prefer that line."""
    override = os.environ.get("QUICKBOT_PYTHON")
    candidates = ([override] if override else []) + [
        "/opt/homebrew/opt/python@3.12/bin/python3.12",
        shutil.which("python3.12"),
        shutil.which("python3.13"),
        shutil.which("python3"),
    ]
    for path in candidates:
        if not path or not os.access(path, os.X_OK):
            continue
        try:
            out = subprocess.run([path, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                                 capture_output=True, text=True, timeout=10).stdout.split()
            if (int(out[0]), int(out[1])) >= (3, 10):
                return path
        except Exception:
            continue
    return None


def _extract_runtime():
    """Unpack the bundled Python runtime into RUNTIME_DIR. Normally the
    serverctl shim already did this; here as a fallback for when
    serverctl.py is invoked directly."""
    if os.access(os.path.join(RUNTIME_DIR, "bin/python3"), os.X_OK):
        return 0
    if not os.path.exists(RUNTIME_TARBALL):
        print("Bundled Python runtime not found:\n{}\n"
              "The app bundle is incomplete — reinstall the app "
              "(`brew reinstall --cask quickbot`).".format(RUNTIME_TARBALL),
              file=sys.stderr)
        return 1
    import tarfile
    import tempfile
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".runtime-extract.", dir=DATA_DIR)
    with tarfile.open(RUNTIME_TARBALL) as tf:
        tf.extractall(tmp)
    try:
        os.rename(os.path.join(tmp, "python"), RUNTIME_DIR)
    except OSError:
        pass  # lost a race against the shim: keep the winner
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _setup_python():
    bin_dir = env_bin_dir()
    if os.access(os.path.join(bin_dir, "mlx_vlm.server"), os.X_OK):
        legacy = bin_dir == os.path.join(DATA_DIR, "venv/bin")
        print("==> Python environment already present ({})"
              .format(os.path.dirname(bin_dir)))
        if legacy:
            print("    (legacy venv from 0.1.0 — still fine; delete it and "
                  "re-run `{} setup` to switch to the bundled runtime)"
                  .format(CLI_NAME))
        return 0

    if RELOCATED:
        code = _extract_runtime()
        if code != 0:
            return code
        python = os.path.join(RUNTIME_DIR, "bin/python3")
        lock = os.path.join(SERVER_DIR, "requirements.lock")
        print("==> Installing the inference server (mlx-vlm and friends)")
        print("    into {}".format(RUNTIME_DIR))
        if subprocess.call([python, "-m", "pip", "install", "--quiet",
                            "--upgrade", "pip"]) != 0:
            return 1
        if subprocess.call([python, "-m", "pip", "install", "-r", lock]) != 0:
            return 1
        return 0

    # Repo checkout: venv from a system interpreter (developer flow).
    python = _find_python()
    if python is None:
        print("Python >= 3.10 not found. Install it (e.g. `brew install python@3.12`)\n"
              "or point QUICKBOT_PYTHON at an interpreter, then re-run `{} setup`."
              .format(CLI_NAME), file=sys.stderr)
        return 1
    print("==> Creating Python environment in {}".format(VENV_DIR))
    print("    using {}".format(python))
    if subprocess.call([python, "-m", "venv", VENV_DIR]) != 0:
        return 1
    pip = os.path.join(VENV_DIR, "bin/pip")
    lock = os.path.join(SERVER_DIR, "requirements.lock")
    print("==> Installing the inference server (mlx-vlm and friends)")
    if subprocess.call([pip, "install", "--quiet", "--upgrade", "pip"]) != 0:
        return 1
    if subprocess.call([pip, "install", "-r", lock]) != 0:
        return 1
    return 0


def _setup_models():
    downloader = os.path.join(env_bin_dir(), "python3")
    snippet = ("import sys\n"
               "from huggingface_hub import snapshot_download\n"
               "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])\n")
    for model in MODELS:
        target = os.path.join(MODELS_DIR, model["dir"])
        if os.path.exists(os.path.join(target, "config.json")):
            print("==> Model {} already present, skipping".format(model["dir"]))
            continue
        print("==> Downloading {} ({}) from Hugging Face".format(model["repo"], model["size"]))
        print("    into {}".format(target))
        os.makedirs(target, exist_ok=True)
        if subprocess.call([downloader, "-c", snippet, model["repo"], target]) != 0:
            print("Download failed for {}. Re-run `{} setup` to resume."
                  .format(model["repo"], CLI_NAME), file=sys.stderr)
            return 1
    return 0


def _setup_ollama(cfg):
    return engines.get_engine(cfg).ensure_model()


def cmd_setup(args):
    skip_models = "--skip-models" in args
    os.makedirs(DATA_DIR, exist_ok=True)
    code = _setup_python()
    if code != 0:
        return code
    catalog.seed_defaults()
    cfg = load_config()  # writes the default config on first run
    try:
        if catalog.yaml_available():
            catalog.require_apple_silicon(catalog.system_profile(force=True))
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    if not skip_models:
        effective, trace = catalog.resolve(cfg)
        if trace:
            print(trace)
        if effective.get("catalogIdentity"):
            code = engines.get_engine(effective).ensure_model()
        elif engines.engine_name(cfg) == "ollama":
            code = _setup_ollama(cfg)
        else:
            code = _setup_models()
        if code != 0:
            return code
    print("\nSetup complete.")
    print("Config:   {}".format(CONFIG_FILE))
    print("Models:   {}".format(MODELS_DIR))
    print("Endpoint: {} (once started)".format(endpoint(cfg)))
    print("\nTurn Quickbot on from the menu bar app, or run `{} start`.".format(CLI_NAME))
    return 0


def cmd_health(cfg):
    try:
        cfg, _ = catalog.resolve(cfg)
    except catalog.CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    ok = health_ok(cfg)
    print("healthy" if ok else "unhealthy")
    return 0 if ok else 1


def cmd_log(cfg):
    os.execvp("tail", ["tail", "-f", log_file(cfg)])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        return cmd_setup(sys.argv[2:])
    if cmd == "profile":
        return catalog.cmd_profile(sys.argv[2:])
    if cmd == "catalog":
        return catalog.cmd_catalog(sys.argv[2:])
    cfg = load_config()
    if cmd in ("start", "on"):
        return cmd_start(cfg)
    if cmd in ("stop", "off"):
        return cmd_stop(cfg)
    if cmd == "toggle":
        return cmd_toggle(cfg)
    if cmd == "engine":
        return cmd_engine(cfg, sys.argv[2:])
    if cmd == "backend":
        return cmd_backend(cfg, sys.argv[2:])
    if cmd == "status":
        return cmd_status(cfg, as_json="--json" in sys.argv[2:])
    if cmd == "health":
        return cmd_health(cfg)
    if cmd == "log":
        return cmd_log(cfg)
    print("usage: {} {{setup|start|stop|toggle|engine [mlx_vlm|ollama]|backend [mlx_vlm|ollama]|catalog {{sync|validate|measure|list}}|profile [--json]|status [--json]|health|log}}"
          .format(CLI_NAME), file=sys.stderr)
    return 2


engines.bind(sys.modules[__name__])
catalog.bind(sys.modules[__name__])


if __name__ == "__main__":
    sys.exit(main())
