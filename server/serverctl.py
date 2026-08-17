#!/usr/bin/env python3
"""Quickbot server controller.

Owns the whole mlx_vlm.server lifecycle: start, stop, adoption of externally
started servers, health checks and status reporting. Every other component
(menu bar app, CLI) drives the server exclusively through this script.

Usage: serverctl {setup|start|stop|toggle|status [--json]|health|log}
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
MODELS_DIR = (os.path.join(DATA_DIR, "models") if RELOCATED
              else os.path.join(os.path.dirname(SERVER_DIR), "models"))

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
    "executable": os.path.join(VENV_DIR, "bin/mlx_vlm.server"),
    "model": os.path.join(MODELS_DIR, MODELS[0]["dir"]),
    "draftModel": os.path.join(MODELS_DIR, MODELS[1]["dir"]),
    "port": 8080,
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
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, sort_keys=True)
            f.write("\n")
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


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
    return "http://{}:{}/v1".format(cfg["host"], cfg["port"])


def proxy_endpoint(cfg):
    return "http://{}:{}/v1".format(cfg["host"], cfg.get("proxyPort", 8081))


def server_command(cfg):
    cmd = [expand(cfg["executable"]), "--model", expand(cfg["model"])]
    draft = cfg.get("draftModel")
    if draft:
        cmd += ["--draft-model", expand(draft)]
    cmd += ["--port", str(cfg["port"])]
    cmd += cfg.get("extraArgs", [])
    return cmd


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
    url = "http://{}:{}{}".format(cfg["host"], cfg["port"], cfg["healthPath"])
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
    lpid = listening_pid(cfg["port"])
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
    python = os.path.join(os.path.dirname(expand(cfg["executable"])), "python")
    script = os.path.join(SERVER_DIR, "toolproxy.py")
    if not (os.access(python, os.X_OK) and os.path.exists(script)):
        return
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["QUICKBOT_UPSTREAM"] = "http://{}:{}".format(cfg["host"], cfg["port"])
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
    state, pid, _ = current_state(cfg)
    if state != "stopped":
        start_proxy(cfg)  # heal a missing proxy next to a live server
        print("Server already {} (PID {}).".format(state, pid))
        return 0

    setup_hint = "Run `{} setup` to install it.".format(CLI_NAME)
    exe = expand(cfg["executable"])
    if not os.access(exe, os.X_OK):
        print("Executable not found:\n{}\n{}".format(exe, setup_hint), file=sys.stderr)
        return 1
    model = expand(cfg["model"])
    if not os.path.exists(model):
        print("Model not found:\n{}\n{}".format(model, setup_hint), file=sys.stderr)
        return 1
    draft = cfg.get("draftModel")
    if draft and not os.path.exists(expand(draft)):
        print("Draft model not found:\n{}\n{}".format(expand(draft), setup_hint),
              file=sys.stderr)
        return 1

    cmd = server_command(cfg)
    log = log_file(cfg)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(log, "a") as lf:
        lf.write("\n===== Quickbot start {} =====\n{}\n".format(stamp, " ".join(cmd)))

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(cfg.get("envVars") or {})

    with open(log, "ab") as lf:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    with open(PID_FILE, "w") as f:
        f.write(str(proc.pid))
    start_proxy(cfg)
    print("Starting server (PID {}). Loading the model takes ~40s.".format(proc.pid))
    return 0


def cmd_stop(cfg):
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
    leftover = listening_pid(cfg["port"])
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
    state, _, _ = current_state(cfg)
    return cmd_stop(cfg) if state in ("running", "starting") else cmd_start(cfg)


def cmd_status(cfg, as_json):
    state, pid, adopted = current_state(cfg)
    started = process_start_epoch(pid) if pid is not None else None
    model_path = expand(cfg["model"])
    draft = cfg.get("draftModel")
    info = {
        "state": state,
        "pid": pid,
        "adopted": adopted,
        "startedAtEpoch": started,
        "modelName": os.path.basename(model_path.rstrip("/")),
        "modelPath": model_path,
        "draftModelPath": expand(draft) if draft else None,
        "endpoint": endpoint(cfg),
        "webSearch": bool(cfg.get("webSearch", True)) and proxy_alive(cfg),
        "configFile": CONFIG_FILE,
        "logFile": log_file(cfg),
        "stopServerOnQuit": bool(cfg.get("stopServerOnQuit", False)),
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


def _setup_python():
    mlx_server = os.path.join(VENV_DIR, "bin/mlx_vlm.server")
    if os.access(mlx_server, os.X_OK):
        print("==> Python environment already present ({})".format(VENV_DIR))
        return 0
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
    downloader = os.path.join(VENV_DIR, "bin/python")
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


def cmd_setup(args):
    skip_models = "--skip-models" in args
    os.makedirs(DATA_DIR, exist_ok=True)
    code = _setup_python()
    if code != 0:
        return code
    if not skip_models:
        code = _setup_models()
        if code != 0:
            return code
    cfg = load_config()  # writes the default config on first run
    print("\nSetup complete.")
    print("Config:   {}".format(CONFIG_FILE))
    print("Models:   {}".format(MODELS_DIR))
    print("Endpoint: {} (once started)".format(endpoint(cfg)))
    print("\nTurn Quickbot on from the menu bar app, or run `{} start`.".format(CLI_NAME))
    return 0


def cmd_health(cfg):
    ok = health_ok(cfg)
    print("healthy" if ok else "unhealthy")
    return 0 if ok else 1


def cmd_log(cfg):
    os.execvp("tail", ["tail", "-f", log_file(cfg)])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        return cmd_setup(sys.argv[2:])
    cfg = load_config()
    if cmd in ("start", "on"):
        return cmd_start(cfg)
    if cmd in ("stop", "off"):
        return cmd_stop(cfg)
    if cmd == "toggle":
        return cmd_toggle(cfg)
    if cmd == "status":
        return cmd_status(cfg, as_json="--json" in sys.argv[2:])
    if cmd == "health":
        return cmd_health(cfg)
    if cmd == "log":
        return cmd_log(cfg)
    print("usage: {} {{setup|start|stop|toggle|status [--json]|health|log}}".format(CLI_NAME),
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
