"""Inference-engine façade for Quickbot.

Quickbot always runs on MLX. The two engines are serving processes, not
competing runtimes: `mlx_vlm` is `mlx_vlm.server`, `ollama` is `ollama serve`
with Ollama's MLX runner (MLX-format tags, never GGUF/llama.cpp). Each engine
is a stateless class that reads an effective config dict (legacy config.json
or the catalog resolver's output) and knows how to start, health-check,
download, and benchmark its server.

`mlx_vlm.server` exposes `--max-kv-size` (KV cache size in tokens). When the
catalog is active, `contextLength` is passed through that flag. On the legacy
config.json path the flag is omitted, so start/stop behaviour is unchanged.
On ollama, `contextLength` maps to `OLLAMA_CONTEXT_LENGTH`.
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

ENGINES = {}
# Old config.json used "backend": "mlx" for mlx_vlm.server. That key is not a
# compute choice — both engines are MLX — and is only read as an engine alias.
LEGACY = {"mlx": "mlx_vlm"}

_ctl = None


def bind(ctl):
    """Point at the serverctl module (avoids a circular import / double-load)."""
    global _ctl
    _ctl = ctl


def _sc():
    if _ctl is None:
        raise RuntimeError("engines.bind() was not called")
    return _ctl


def expand(path):
    return os.path.expanduser(path)


def engine_key(name):
    """Canonical engine id (`mlx_vlm` | `ollama`) from a user-facing name."""
    if not name:
        return "mlx_vlm"
    return LEGACY.get(name, name)


def engine_name(cfg):
    """Canonical engine id from cfg, honouring `engine` then the legacy `backend` key."""
    return engine_key(cfg.get("engine") or cfg.get("backend") or "mlx_vlm")


def ollama_model_is_mlx(name):
    """Ollama tags Quickbot will serve: the registry name must contain 'mlx'.

    Ollama can also run GGUF via llama.cpp/Metal; Quickbot does not. The
    `-mlx` suffix (e.g. qwen3.8:27b-mlx) is how the registry selects the
    MLX runner.
    """
    return bool(name) and "mlx" in str(name).lower()


def get_engine(name_or_cfg, cfg=None):
    if cfg is None:
        if isinstance(name_or_cfg, dict):
            cfg = name_or_cfg
            name = engine_name(cfg)
        else:
            raise TypeError("get_engine() expected a config dict")
    else:
        name = engine_key(name_or_cfg)
    cls = ENGINES.get(name) or ENGINES["mlx_vlm"]
    return cls(cfg)


class Engine:
    """Base engine. Subclasses fill in the serving-software specifics."""

    name = ""

    def __init__(self, cfg):
        self.cfg = cfg

    def upstream_port(self):
        return self.cfg["port"]

    def health_path(self):
        return self.cfg.get("healthPath", "/health")

    def server_command(self):
        raise NotImplementedError

    def env(self):
        env = dict(os.environ)
        env.update(self.cfg.get("envVars") or {})
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def model_present(self):
        raise NotImplementedError

    def ensure_model(self):
        """Download weights if missing. Return a process-style exit code."""
        raise NotImplementedError

    def post_start(self):
        """Warm-up / follow-up after the process is spawned. Default: no-op."""

    def api_model_id(self):
        """Value for the OpenAI `model` field."""
        return expand(self.cfg.get("model") or "")

    def read_stream_stats(self, chunk):
        """Pull peak_memory (GB) and tok/s out of one parsed SSE JSON object."""
        timings = chunk.get("timings") or {}
        usage = chunk.get("usage") or {}
        peak = timings.get("peak_memory")
        if peak is None:
            peak = timings.get("peak_memory_gb")
        return {
            "peak_memory": peak,
            "predicted_per_second": timings.get("predicted_per_second"),
            "prompt_per_second": timings.get("prompt_per_second"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }

    def peak_memory_gb(self, pid):
        """RSS of pid + children, in GB. Used when the stream has no timings."""
        return _rss_gb(pid)


class MlxVlmEngine(Engine):
    name = "mlx_vlm"

    def server_command(self):
        cfg = self.cfg
        cmd = [expand(cfg["executable"]), "--model", expand(cfg["model"])]
        draft = cfg.get("draftModel")
        if draft:
            cmd += ["--draft-model", expand(draft)]
        decoder = cfg.get("decoder") if isinstance(cfg.get("decoder"), dict) else {}
        kind = decoder.get("kind")
        if kind in ("mtp", "dflash", "eagle3"):
            cmd += ["--draft-kind", kind]
        ctx = cfg.get("contextLength")
        if ctx:
            cmd += ["--max-kv-size", str(int(ctx))]
        kv = cfg.get("kvCacheQuant")
        if kv is not None:
            cmd += ["--kv-bits", str(kv)]
        cmd += ["--port", str(cfg["port"])]
        cmd += list(cfg.get("extraArgs") or [])
        return cmd

    def model_present(self):
        cfg = self.cfg
        exe = expand(cfg.get("executable") or "")
        if not os.access(exe, os.X_OK):
            return False
        model = expand(cfg.get("model") or "")
        if not os.path.exists(model):
            return False
        draft = cfg.get("draftModel")
        if draft and not os.path.exists(expand(draft)):
            return False
        return True

    def missing_reason(self):
        """Human-readable reason `model_present` is false, or None."""
        sc = _sc()
        hint = "Run `{} setup` to install it.".format(sc.CLI_NAME)
        exe = expand(self.cfg.get("executable") or "")
        if not os.access(exe, os.X_OK):
            return "Executable not found:\n{}\n{}".format(exe, hint)
        model = expand(self.cfg.get("model") or "")
        if not os.path.exists(model):
            return "Model not found:\n{}\n{}".format(model, hint)
        draft = self.cfg.get("draftModel")
        if draft and not os.path.exists(expand(draft)):
            return "Draft model not found:\n{}\n{}".format(expand(draft), hint)
        return None

    def ensure_model(self):
        sc = _sc()
        targets = [_hf_target(self.cfg.get("model"), self.cfg.get("catalogIdentity"))]
        decoder = self.cfg.get("decoder") if isinstance(self.cfg.get("decoder"), dict) else {}
        draft_addr = decoder.get("draft") or self.cfg.get("draftModel")
        if draft_addr:
            targets.append(_hf_target(self.cfg.get("draftModel"), draft_addr))
        downloader = os.path.join(sc.env_bin_dir(), "python3")
        snippet = ("import sys\n"
                   "from huggingface_hub import snapshot_download\n"
                   "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])\n")
        for repo, target in targets:
            if not repo or not target:
                continue
            if os.path.exists(os.path.join(target, "config.json")):
                print("==> Model {} already present, skipping".format(os.path.basename(target)))
                continue
            print("==> Downloading {} from Hugging Face".format(repo))
            print("    into {}".format(target))
            os.makedirs(target, exist_ok=True)
            if subprocess.call([downloader, "-c", snippet, repo, target]) != 0:
                print("Download failed for {}. Re-run `{} setup` to resume."
                      .format(repo, sc.CLI_NAME), file=sys.stderr)
                return 1
        return 0


class OllamaEngine(Engine):
    name = "ollama"

    def upstream_port(self):
        return self.cfg.get("ollamaPort", 11434)

    def health_path(self):
        # Ollama has no /health; its root answers "Ollama is running".
        return "/"

    def server_command(self):
        return [self.find_ollama() or expand(self.cfg.get("ollamaExecutable") or "ollama"),
                "serve"]

    def env(self):
        cfg = self.cfg
        env = dict(os.environ)
        env["OLLAMA_HOST"] = "{}:{}".format(cfg["host"], cfg.get("ollamaPort", 11434))
        env.update(cfg.get("ollamaEnvVars") or {})
        # Always the MLX runner — GGUF/llama.cpp/Metal is not a Quickbot path.
        env["OLLAMA_LLM_LIBRARY"] = "mlx"
        ctx = cfg.get("contextLength")
        if ctx and "OLLAMA_CONTEXT_LENGTH" not in env:
            env["OLLAMA_CONTEXT_LENGTH"] = str(int(ctx))
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def find_ollama(self):
        exe = expand(self.cfg.get("ollamaExecutable") or "")
        if os.access(exe, os.X_OK):
            return exe
        return shutil.which("ollama")

    def api_model_id(self):
        return self.cfg.get("ollamaModel") or ""

    def model_present(self):
        """Whether ollamaModel has been pulled, checked via its manifest file
        (`ollama list` needs a running server, which we may not have yet)."""
        if self.find_ollama() is None:
            return False
        name = self.cfg.get("ollamaModel") or ""
        model, _, tag = name.partition(":")
        models_dir = ((self.cfg.get("ollamaEnvVars") or {}).get("OLLAMA_MODELS")
                      or os.path.expanduser("~/.ollama/models"))
        manifest = os.path.join(models_dir, "manifests/registry.ollama.ai/library",
                                model, tag or "latest")
        return os.path.exists(manifest)

    def missing_reason(self):
        sc = _sc()
        hint = "Run `{} setup` to install it.".format(sc.CLI_NAME)
        name = self.cfg.get("ollamaModel") or ""
        if not ollama_model_is_mlx(name):
            return ("Ollama in Quickbot only serves MLX models (the tag must "
                    "contain 'mlx', e.g. qwen3.8:27b-mlx). Got: {}".format(
                        name or "(none)"))
        if self.find_ollama() is None:
            return "ollama not found. Install it (`brew install ollama`)."
        if not self.model_present():
            return "Ollama model not pulled: {}\n{}".format(name, hint)
        return None

    def ensure_model(self):
        sc = _sc()
        name = self.cfg.get("ollamaModel") or ""
        if not ollama_model_is_mlx(name):
            print("Ollama in Quickbot only serves MLX models (the tag must "
                  "contain 'mlx', e.g. qwen3.8:27b-mlx). Got: {}".format(
                      name or "(none)"), file=sys.stderr)
            return 1
        exe = self.find_ollama()
        if exe is None:
            print("ollama not found. Install it (`brew install ollama`), then "
                  "re-run `{} setup`.".format(sc.CLI_NAME), file=sys.stderr)
            return 1
        if self.model_present():
            print("==> Ollama model {} already present, skipping".format(name))
            return 0
        started = None
        env = self.env()
        if sc.listening_pid(self.cfg.get("ollamaPort", 11434)) is None:
            started = subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=env,
                                       start_new_session=True)
            deadline = time.time() + 30
            while time.time() < deadline and not sc.health_ok(self.cfg):
                time.sleep(0.5)
        print("==> Pulling {} from the Ollama registry".format(name))
        code = subprocess.call([exe, "pull", name], env=env)
        if started is not None:
            started.terminate()
        if code != 0:
            print("Pull failed for {}. Re-run `{} setup` to resume."
                  .format(name, sc.CLI_NAME), file=sys.stderr)
        return code

    def model_loaded(self):
        """True once `GET /api/ps` lists our model (the MLX runner is up)."""
        url = "http://{}:{}/api/ps".format(self.cfg["host"], self.upstream_port())
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Quickbot/catalog"})
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.load(resp)
        except Exception:
            return False
        want = self.api_model_id()
        for item in data.get("models") or []:
            listed = item.get("name") or item.get("model") or ""
            if want and want in listed:
                return True
        return False

    def peak_memory_gb(self, pid):
        # The MLX runner is often a grandchild (or a separate ollama process).
        return max(_rss_gb(pid), _ollama_rss_gb())

    def post_start(self):
        """Ollama loads the model on the first request; fire a detached load
        request so the first chat prompt does not pay the ~30s load."""
        cfg = self.cfg
        url = "http://{}:{}".format(cfg["host"], self.upstream_port())
        body = json.dumps({"model": cfg.get("ollamaModel"),
                           "keep_alive": (cfg.get("ollamaEnvVars") or {})
                           .get("OLLAMA_KEEP_ALIVE", "1h")})
        script = ("until curl -s -m 2 {url}/ >/dev/null 2>&1; do sleep 1; done; "
                  "curl -s -m 600 -X POST -d '{body}' {url}/api/generate "
                  ">/dev/null 2>&1").format(url=url, body=body)
        subprocess.Popen(["/bin/sh", "-c", script], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


ENGINES["mlx_vlm"] = MlxVlmEngine
ENGINES["ollama"] = OllamaEngine


def _hf_target(path_or_none, identity_or_path):
    """(repo_id, local_dir) for an hf model or draft."""
    sc = _sc()
    identity = identity_or_path or ""
    repo = identity
    if identity.startswith("hf:"):
        repo = identity[3:]
    elif identity.startswith("ollama:"):
        return None, None
    if "/" not in repo and path_or_none:
        # Already a filesystem path; derive repo from the catalog identity if
        # we have one, otherwise skip (nothing to download).
        local = expand(path_or_none)
        name = os.path.basename(local.rstrip("/"))
        return None, local if os.path.isdir(os.path.dirname(local)) else (
            None, os.path.join(sc.MODELS_DIR, name))
    name = repo.rsplit("/", 1)[-1] if repo else ""
    local = expand(path_or_none) if path_or_none else os.path.join(sc.MODELS_DIR, name)
    return repo, local


def _rss_gb(pid):
    """Sum `ps` RSS (kB) for pid and all descendants, in GB."""
    if not pid:
        return 0.0
    return _rss_gb_for_pids(_descendant_pids(int(pid)))


def _descendant_pids(root):
    by_parent = {}
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                child, parent = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            by_parent.setdefault(parent, []).append(child)
    except Exception:
        return [int(root)]
    found, stack, seen = [], [int(root)], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        found.append(pid)
        stack.extend(by_parent.get(pid, []))
    return found


def _ollama_rss_gb():
    """RSS of every process whose args mention ollama (serve + MLX runner)."""
    pids = []
    try:
        out = subprocess.run(
            ["/bin/ps", "-axo", "pid=,rss=,args="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "ollama" not in line.lower():
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pids.append(int(parts[0]))
            except ValueError:
                continue
    except Exception:
        return 0.0
    return _rss_gb_for_pids(pids)


def _rss_gb_for_pids(pids):
    total_kb = 0
    seen = set()
    for p in pids:
        if p in seen:
            continue
        seen.add(p)
        try:
            raw = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(p)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if raw:
                total_kb += int(raw.split()[0])
        except Exception:
            continue
    return total_kb / (1024.0 * 1024.0)


def spawn(cfg):
    """Start the engine process only (no proxy). Returns (Popen, pid)."""
    sc = _sc()
    eng = get_engine(cfg)
    cmd = eng.server_command()
    env = eng.env()
    log = sc.log_file(cfg)
    with open(log, "ab") as lf:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT, env=env,
            start_new_session=True,
        )
    with open(sc.PID_FILE, "w") as f:
        f.write(str(proc.pid))
    return proc, proc.pid


def wait_healthy(cfg, timeout=900):
    sc = _sc()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sc.health_ok(cfg):
            return True
        time.sleep(0.5)
    return False


def terminate(cfg, pid):
    """SIGTERM then SIGKILL a spawned engine; free its port."""
    sc = _sc()
    tree = _descendant_pids(pid) if pid else []
    for p in tree:
        _kill(p, signal.SIGTERM)
    deadline = time.time() + 20
    while time.time() < deadline and any(sc.is_alive(p) for p in tree):
        time.sleep(0.2)
    for p in tree:
        if sc.is_alive(p):
            _kill(p, signal.SIGKILL)
    deadline = time.time() + 5
    while time.time() < deadline and any(sc.is_alive(p) for p in tree):
        time.sleep(0.1)
    leftover = sc.listening_pid(get_engine(cfg).upstream_port())
    if leftover is not None and leftover != pid:
        _kill(leftover, signal.SIGTERM)
        time.sleep(1.0)
        if sc.is_alive(leftover):
            _kill(leftover, signal.SIGKILL)
    try:
        os.remove(sc.PID_FILE)
    except OSError:
        pass


def _kill(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def chat_completions(cfg, messages, max_tokens, timeout=600):
    """Stream a generation. Returns (stats, text).

    stats keys: peak_memory, predicted_per_second, prompt_per_second,
    prompt_tokens, completion_tokens, elapsed. Missing values are None.
    mlx_vlm uses OpenAI `/v1/chat/completions`; ollama uses native `/api/chat`
    so we get prompt_eval/eval timings (the OpenAI stream has no tok/s).
    """
    eng = get_engine(cfg)
    if eng.name == "ollama":
        return _ollama_chat(cfg, eng, messages, max_tokens, timeout)
    url = "http://{}:{}/v1/chat/completions".format(cfg["host"], eng.upstream_port())
    body = json.dumps({
        "model": eng.api_model_id(),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0,
        "enable_thinking": False,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Quickbot/catalog"},
        method="POST",
    )
    stats = {
        "peak_memory": None,
        "predicted_per_second": None,
        "prompt_per_second": None,
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    text_parts = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                got = eng.read_stream_stats(chunk)
                for key, value in got.items():
                    if value is not None:
                        stats[key] = value
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        text_parts.append(piece)
    except urllib.error.HTTPError as e:
        raise RuntimeError("chat completions HTTP {}: {}".format(
            e.code, e.read()[:300].decode("utf-8", "replace"))) from e
    return stats, "".join(text_parts)


def _ollama_chat(cfg, eng, messages, max_tokens, timeout=600):
    """Native `/api/chat` stream — last object carries eval timings in ns."""
    url = "http://{}:{}/api/chat".format(cfg["host"], eng.upstream_port())
    options = {"num_predict": int(max_tokens), "temperature": 0}
    ctx = cfg.get("contextLength")
    if ctx:
        options["num_ctx"] = int(ctx)
    body = json.dumps({
        "model": eng.api_model_id(),
        "messages": messages,
        "stream": True,
        "keep_alive": (cfg.get("ollamaEnvVars") or {}).get("OLLAMA_KEEP_ALIVE", "1h"),
        "options": options,
    }).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Quickbot/catalog"},
        method="POST",
    )
    stats = {
        "peak_memory": None,
        "predicted_per_second": None,
        "prompt_per_second": None,
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    text_parts = []
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                msg = chunk.get("message") or {}
                piece = msg.get("content")
                if piece:
                    text_parts.append(piece)
                if not chunk.get("done"):
                    continue
                prompt_n = chunk.get("prompt_eval_count")
                prompt_ns = chunk.get("prompt_eval_duration")
                eval_n = chunk.get("eval_count")
                eval_ns = chunk.get("eval_duration")
                if prompt_n is not None:
                    stats["prompt_tokens"] = int(prompt_n)
                if eval_n is not None:
                    stats["completion_tokens"] = int(eval_n)
                if prompt_n and prompt_ns:
                    stats["prompt_per_second"] = float(prompt_n) / (float(prompt_ns) / 1e9)
                if eval_n and eval_ns:
                    stats["predicted_per_second"] = float(eval_n) / (float(eval_ns) / 1e9)
    except urllib.error.HTTPError as e:
        raise RuntimeError("ollama /api/chat HTTP {}: {}".format(
            e.code, e.read()[:300].decode("utf-8", "replace"))) from e
    stats["elapsed"] = time.perf_counter() - t0
    return stats, "".join(text_parts)
