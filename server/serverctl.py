#!/usr/bin/env python3
"""Quickbot server controller.

Owns the whole mlx_vlm.server lifecycle: start, stop, adoption of externally
started servers, health checks and status reporting. Every other component
(menu bar app, CLI) drives the server exclusively through this script.

Usage: serverctl {start|stop|toggle|status [--json]|health|log}
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SERVER_DIR, "config.json")
PID_FILE = os.path.join(SERVER_DIR, ".server.pid")

DEFAULT_CONFIG = {
    "executable": os.path.join(SERVER_DIR, ".venv/bin/mlx_vlm.server"),
    "model": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-4bit",
    "draftModel": "~/Devland/_experimental/quickbot/models/Qwen3.8-27B-MTP-4bit",
    "port": 8080,
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
    return "http://{}:{}/v1".format(cfg["host"], cfg["port"])


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


def listening_pid(cfg):
    """PID of the process listening on the configured port (via lsof)."""
    try:
        out = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-iTCP:{}".format(cfg["port"]), "-sTCP:LISTEN", "-t"],
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
    lpid = listening_pid(cfg)
    own = pidfile_pid()
    if lpid is not None:
        adopted = own is None or own != lpid
        return ("running" if health_ok(cfg) else "starting", lpid, adopted)
    if own is not None:
        return ("starting", own, False)
    return ("stopped", None, False)


# --- Commands -------------------------------------------------------------


def cmd_start(cfg):
    state, pid, _ = current_state(cfg)
    if state != "stopped":
        print("Server already {} (PID {}).".format(state, pid))
        return 0

    exe = expand(cfg["executable"])
    if not os.access(exe, os.X_OK):
        print("Executable not found:\n{}".format(exe), file=sys.stderr)
        return 1
    model = expand(cfg["model"])
    if not os.path.exists(model):
        print("Model not found:\n{}".format(model), file=sys.stderr)
        return 1
    draft = cfg.get("draftModel")
    if draft and not os.path.exists(expand(draft)):
        print("Draft model not found:\n{}".format(expand(draft)), file=sys.stderr)
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
    print("Starting server (PID {}). Loading the model takes ~40s.".format(proc.pid))
    return 0


def cmd_stop(cfg):
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
    leftover = listening_pid(cfg)
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


def cmd_health(cfg):
    ok = health_ok(cfg)
    print("healthy" if ok else "unhealthy")
    return 0 if ok else 1


def cmd_log(cfg):
    os.execvp("tail", ["tail", "-f", log_file(cfg)])


def main():
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
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
    print("usage: serverctl {start|stop|toggle|status [--json]|health|log}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
