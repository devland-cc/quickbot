"""Quickbot model catalog.

Top-level imports are stdlib only: this module is imported by serverctl.py
before setup, on a bare runtime with no pip packages. `import yaml` happens
inside functions; without PyYAML or without the catalog files, resolve()
returns the legacy config.json untouched.
"""

from __future__ import print_function

import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

INDEX_BEGIN = "# >>> QUICKBOT-INDEX BEGIN (managed by `serverctl catalog sync`) <<<"
INDEX_END = "# >>> QUICKBOT-INDEX END <<<"
BLOCK_MARKER_FMT = "# --- quickbot-model: {identity} ---"
BLOCK_MARKER_RE = re.compile(r"^# --- quickbot-model: (.+) ---\s*$", re.M)
TIER_ORDER = (32, 16, 8)
KNOWN_ENGINES = ("mlx_vlm", "ollama")
DECODER_KINDS = ("mtp", "draft", "none")
CAPABILITY_KEYS = ("vision", "tools", "thinking")
CATALOG_FILENAMES = (
    "catalog_seed.list",
    "catalog_tiers.yml",
    "catalog.json",
    "catalog_settings.yml",
)
HF_USER_AGENT = "Quickbot/catalog"

_ctl = None


class CatalogError(Exception):
    """User-facing catalog failure; commands print `str(e)` and return 1."""


def bind(ctl):
    global _ctl
    _ctl = ctl


def _sc():
    if _ctl is None:
        raise RuntimeError("catalog.bind() was not called")
    return _ctl


# --- Paths ----------------------------------------------------------------


def _data_dir():
    return _sc().DATA_DIR


def _server_dir():
    return _sc().SERVER_DIR


def seed_path():
    return os.path.join(_data_dir(), "catalog_seed.list")


def tiers_path():
    return os.path.join(_data_dir(), "catalog_tiers.yml")


def catalog_path():
    return os.path.join(_data_dir(), "catalog.json")


def settings_path():
    return os.path.join(_data_dir(), "catalog_settings.yml")


def profile_path():
    return os.path.join(_data_dir(), "profile.json")


def operation_path():
    return os.path.join(_data_dir(), ".catalog-operation.json")


# --- YAML / activation ----------------------------------------------------


def yaml_available():
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


def _need_yaml():
    try:
        import yaml
        return yaml
    except ImportError:
        raise CatalogError(
            "catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME))


def catalog_files_present():
    return all(os.path.exists(p) for p in (catalog_path(), tiers_path(), settings_path()))


def catalog_active():
    """True when PyYAML is importable and the three resolve files exist."""
    return yaml_available() and catalog_files_present()


def seed_defaults():
    """Copy the four shipped defaults SERVER_DIR → DATA_DIR when relocated
    and absent. No-op in a repo checkout (the two dirs are the same)."""
    sc = _sc()
    if not sc.RELOCATED:
        return
    os.makedirs(sc.DATA_DIR, exist_ok=True)
    for name in CATALOG_FILENAMES:
        src = os.path.join(sc.SERVER_DIR, name)
        dst = os.path.join(sc.DATA_DIR, name)
        if os.path.exists(dst) or not os.path.exists(src):
            continue
        with open(src, "rb") as inf, open(dst, "wb") as outf:
            outf.write(inf.read())


def _ensure_catalog_files():
    """Create missing catalog files from the shipped defaults (sync / setup)."""
    sc = _sc()
    os.makedirs(sc.DATA_DIR, exist_ok=True)
    for name in CATALOG_FILENAMES:
        dst = os.path.join(sc.DATA_DIR, name)
        if os.path.exists(dst):
            continue
        src = os.path.join(sc.SERVER_DIR, name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            with open(src, "rb") as inf, open(dst, "wb") as outf:
                outf.write(inf.read())
        elif name == "catalog.json":
            save_catalog({"version": 1, "models": {}})
        elif name == "catalog_seed.list":
            with open(dst, "w") as f:
                f.write("# Quickbot model manifest. One address per line.\n"
                        "# Schemes: hf: (default when omitted), ollama:\n")
        elif name == "catalog_tiers.yml":
            with open(dst, "w") as f:
                f.write("tiers:\n"
                        "  8:  { headroomGB: 3, overrides: { contextLength: 8192, promptCache: false } }\n"
                        "  16: { headroomGB: 3, overrides: { contextLength: 16384 } }\n"
                        "  32: { headroomGB: 4, overrides: {} }\n")
        elif name == "catalog_settings.yml":
            with open(dst, "w") as f:
                f.write("# Quickbot model settings.\n\n")
                f.write(INDEX_BEGIN + "\n")
                f.write("index:\n  8: []\n  16: []\n  32: []\n")
                f.write(INDEX_END + "\n\nmodels:\n")


def catalog_need_setup():
    """True when catalog CLI should refuse (pre-setup, no PyYAML)."""
    return not yaml_available()


# --- Identity / seed ------------------------------------------------------


def normalize_address(raw):
    s = (raw or "").strip()
    if not s or s.startswith("#"):
        return None
    # A trailing inline comment is not part of the address.
    if " #" in s:
        s = s[:s.index(" #")].rstrip()
    if s.startswith("hf:") or s.startswith("ollama:"):
        return s
    return "hf:" + s


def split_address(identity):
    identity = normalize_address(identity) or identity
    if identity.startswith("ollama:"):
        return "ollama", identity[7:]
    if identity.startswith("hf:"):
        return "hf", identity[3:]
    return "hf", identity


def identity_name(identity):
    scheme, rest = split_address(identity)
    if scheme == "hf":
        return rest.rsplit("/", 1)[-1]
    return rest


def parse_seed(text=None):
    """Return a list of normalized identities, preserving order, unique."""
    if text is None:
        path = seed_path()
        if not os.path.exists(path):
            return []
        with open(path) as f:
            text = f.read()
    seen = set()
    out = []
    for line in text.splitlines():
        ident = normalize_address(line)
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
    return out


# --- catalog.json ---------------------------------------------------------


def load_catalog():
    path = catalog_path()
    if not os.path.exists(path):
        return {"version": 1, "models": {}}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {"version": 1, "models": {}}
    data.setdefault("version", 1)
    data.setdefault("models", {})
    return data


def save_catalog(data):
    os.makedirs(_data_dir(), exist_ok=True)
    path = catalog_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# --- YAML loaders ---------------------------------------------------------


def load_tiers():
    yaml = _need_yaml()
    path = tiers_path()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("tiers") or {}
    out = {}
    for key, val in raw.items():
        try:
            tier = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(val, dict):
            val = {}
        out[tier] = {
            "headroomGB": float(val.get("headroomGB") or 0),
            "overrides": dict(val.get("overrides") or {}),
        }
    return out


def load_settings():
    """Parse catalog_settings.yml. Returns (index, models, raw_text)."""
    yaml = _need_yaml()
    path = settings_path()
    if not os.path.exists(path):
        return {8: [], 16: [], 32: []}, {}, ""
    with open(path) as f:
        text = f.read()
    data = yaml.safe_load(text) or {}
    models = data.get("models") or {}
    if not isinstance(models, dict):
        models = {}
    index = parse_index(text)
    return index, models, text


def save_settings_text(text):
    os.makedirs(_data_dir(), exist_ok=True)
    path = settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        if text and not text.endswith("\n"):
            f.write("\n")
    os.replace(tmp, path)


# --- Text surgery (pure) --------------------------------------------------


def _marker_span(text, begin, end):
    """Return (start, end) of the exclusive inner region, or raise."""
    b = text.find(begin)
    e = text.find(end)
    if b < 0 or e < 0 or e < b:
        raise CatalogError(
            "catalog_settings.yml is missing the QUICKBOT-INDEX markers. "
            "Restore them (see the shipped default) and re-run.")
    inner_start = b + len(begin)
    if text[inner_start:inner_start + 1] == "\n":
        inner_start += 1
    return inner_start, e


def parse_index(text):
    """Parse the managed index region. Values are lists of
    {identity, pinned, comment} dicts, keyed by int tier."""
    yaml = _need_yaml()
    start, end = _marker_span(text, INDEX_BEGIN, INDEX_END)
    region = text[start:end]
    comments = {}
    for line in region.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        rest = stripped[1:].strip()
        comment = ""
        if " #" in rest:
            rest, comment = rest.split(" #", 1)
            comment = comment.strip()
            rest = rest.strip()
        rest = rest.strip().strip("'").strip('"')
        pinned = rest.startswith("*")
        ident = rest[1:] if pinned else rest
        ident = normalize_address(ident) or ident
        if ident:
            comments[ident] = comment
    try:
        loaded = yaml.safe_load(region) or {}
    except Exception as e:
        raise CatalogError("catalog_settings.yml index is not valid YAML: {}".format(e))
    raw = loaded.get("index") if isinstance(loaded, dict) else None
    if raw is None and isinstance(loaded, dict):
        raw = loaded
    if not isinstance(raw, dict):
        raw = {}
    out = {8: [], 16: [], 32: []}
    for key, items in raw.items():
        try:
            tier = int(key)
        except (TypeError, ValueError):
            continue
        if tier not in out:
            out[tier] = []
        if items is None:
            items = []
        if not isinstance(items, list):
            continue
        seen = set()
        for item in items:
            ident, pinned = _index_item(item)
            if not ident or ident in seen:
                continue
            seen.add(ident)
            out[tier].append({
                "identity": ident,
                "pinned": pinned,
                "comment": comments.get(ident, ""),
            })
    return out


def _index_item(item):
    pinned = False
    if isinstance(item, dict):
        # Unquoted `hf: repo` parsed as a one-key mapping.
        if len(item) == 1:
            k, v = next(iter(item.items()))
            ident = "{}:{}".format(k, v)
        else:
            return None, False
    else:
        ident = str(item).strip()
    ident = ident.strip().strip("'").strip('"')
    if ident.startswith("*"):
        pinned = True
        ident = ident[1:]
    ident = normalize_address(ident) or ident
    return ident, pinned


def format_index(index):
    """Serialize an index dict to the YAML that sits between the markers."""
    lines = ["index:"]
    for tier in (8, 16, 32):
        entries = index.get(tier) or []
        if not entries:
            lines.append("  {}: []".format(tier))
            continue
        lines.append("  {}:".format(tier))
        for entry in entries:
            ident = entry["identity"]
            token = "*" + ident if entry.get("pinned") else ident
            line = '    - "{}"'.format(token)
            comment = (entry.get("comment") or "").strip()
            if comment:
                line += "   # {}".format(comment)
            elif entry.get("pinned") and ident.startswith("hf:"):
                line += "   # * = pinned (auto-selected)"
            lines.append(line)
    lines.append("")
    return "\n".join(lines)


def rewrite_index(text, index):
    """Replace only the bytes between the BEGIN/END marker lines."""
    start, end = _marker_span(text, INDEX_BEGIN, INDEX_END)
    inner = format_index(index)
    # Keep a newline before END.
    if not inner.endswith("\n"):
        inner += "\n"
    return text[:start] + inner + text[end:]


def append_model_block(text, identity, block_yaml):
    """Append a new model block at EOF. Does not rewrite existing blocks."""
    marker = BLOCK_MARKER_FMT.format(identity=identity)
    if marker in text:
        return text
    chunk = block_yaml if block_yaml.startswith("\n") else "\n" + block_yaml
    if not text.endswith("\n"):
        text += "\n"
    return text + chunk


def remove_model_block(text, identity):
    """Remove from the block marker to the next marker or EOF."""
    marker = BLOCK_MARKER_FMT.format(identity=identity)
    lines = text.splitlines(True)
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\n") == marker:
            start = i
            break
    if start is None:
        return text
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if BLOCK_MARKER_RE.match(lines[j].rstrip("\n")):
            end = j
            break
    # Trailing user comments after the last block are not part of it.
    if end == len(lines):
        k = end - 1
        while k > start:
            stripped = lines[k].strip()
            if stripped == "" or (stripped.startswith("#") and not BLOCK_MARKER_RE.match(stripped)):
                k -= 1
                continue
            break
        end = k + 1
    # Drop a leading blank line sitting just above the marker.
    drop_from = start
    if start > 0 and lines[start - 1].strip() == "":
        drop_from = start - 1
    return "".join(lines[:drop_from] + lines[end:])


def scaffold_block(identity, meta, settings=None):
    """Render a new settings block for `identity`. `meta` is an hf_metadata dict
    (or catalog.json entry). `settings` optional overrides."""
    settings = settings or {}
    name = identity_name(identity)
    display = settings.get("displayName") or _humanize_name(name)
    scheme, _ = split_address(identity)
    engine = settings.get("engine") or ("ollama" if scheme == "ollama" else "mlx_vlm")
    max_ctx = meta.get("maxModelContext") if meta else None
    ctx = settings.get("contextLength")
    if ctx is None:
        ctx = min(int(max_ctx), 32768) if max_ctx else 8192
    decoder = settings.get("decoder") or {"kind": "none"}
    kind = decoder.get("kind") or "none"
    draft = decoder.get("draft")
    if kind in ("mtp", "draft") and draft:
        decoder_yaml = '{{ kind: {}, draft: "{}" }}'.format(kind, draft)
    else:
        decoder_yaml = "{{ kind: {} }}".format(kind)
    caps = (meta or {}).get("capabilities") or {}
    user_caps = settings.get("capabilities") or {}
    vision = bool(user_caps.get("vision") if "vision" in user_caps else caps.get("vision"))
    tools = bool(user_caps.get("tools") if "tools" in user_caps else caps.get("tools", True))
    thinking = bool(user_caps.get("thinking") if "thinking" in user_caps else caps.get("thinking"))
    prompt_cache = settings.get("promptCache")
    if prompt_cache is None:
        prompt_cache = True
    kv = settings.get("kvCacheQuant")
    kv_yaml = "null" if kv is None else str(kv)
    return (
        BLOCK_MARKER_FMT.format(identity=identity) + "\n"
        '  "{}":\n'.format(identity)
        + '    displayName: "{}"\n'.format(_yaml_escape(display))
        + "    engine: {}\n".format(engine)
        + "    contextLength: {}\n".format(int(ctx))
        + "    decoder: {}\n".format(decoder_yaml)
        + "    promptCache: {}\n".format("true" if prompt_cache else "false")
        + "    kvCacheQuant: {}\n".format(kv_yaml)
        + "    capabilities: {{ vision: {}, tools: {}, thinking: {} }}\n".format(
            _bool(vision), _bool(tools), _bool(thinking))
        + "    envVars: {}\n"
        + "    extraArgs: []\n"
    )


def _bool(v):
    return "true" if v else "false"


def _yaml_escape(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _humanize_name(name):
    s = name
    for suffix in ("-4bit", "-8bit", "-fp16", "-Instruct", "-instruct"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace("-", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or name


def pick_pin(entries, warnings=None):
    """First `*` in `entries` wins. Extra pins are appended to `warnings`."""
    warnings = warnings if warnings is not None else []
    winner = None
    for entry in entries or []:
        if not entry.get("pinned"):
            continue
        if winner is None:
            winner = entry["identity"]
        else:
            warnings.append(
                "duplicate pin in tier: {} ignored (first pin {} wins)".format(
                    entry["identity"], winner))
    return winner


def pick_pin_walk(index, start_tier, warnings=None):
    """Walk start_tier → smaller until a pin is found. Returns (identity, tier) or (None, None)."""
    warnings = warnings if warnings is not None else []
    try:
        start = int(start_tier)
    except (TypeError, ValueError):
        start = 0
    for tier in TIER_ORDER:
        if tier > start:
            continue
        ident = pick_pin(index.get(tier) or [], warnings)
        if ident:
            return ident, tier
    return None, None


# --- Profiling ------------------------------------------------------------


def _sha256_profile(chip, ram_gb, macos_version):
    payload = "{}|{}|{}".format(chip, ram_gb, macos_version)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _detect_profile():
    sc = _sc()

    def sysctl(key):
        try:
            out = __import__("subprocess").run(
                ["/usr/sbin/sysctl", "-n", key],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return out
        except Exception:
            return ""

    mem = sysctl("hw.memsize")
    try:
        ram_gb = int(round(int(mem) / (1024.0 ** 3)))
    except (TypeError, ValueError):
        ram_gb = 0
    apple = sysctl("hw.optional.arm64") == "1"
    chip = sysctl("machdep.cpu.brand_string") or "unknown"
    try:
        macos = __import__("subprocess").run(
            ["/usr/bin/sw_vers", "-productVersion"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        macos = ""
    try:
        st = os.statvfs(sc.DATA_DIR if os.path.isdir(sc.DATA_DIR) else "/")
        free_disk = (st.f_bavail * st.f_frsize) / 1e9
    except OSError:
        free_disk = 0.0
    return {
        "appleSilicon": apple,
        "chip": chip,
        "macosVersion": macos,
        "ramGB": ram_gb,
        "freeDiskGB": round(free_disk, 1),
        "profiledAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash": _sha256_profile(chip, ram_gb, macos),
    }


def _finalize_profile(prof):
    if not prof.get("hash"):
        prof["hash"] = _sha256_profile(
            prof.get("chip") or "",
            prof.get("ramGB") or 0,
            prof.get("macosVersion") or "",
        )
    return prof


def system_profile(force=False):
    """Load or (re)detect the machine profile. Honours QUICKBOT_FAKE_PROFILE."""
    fake = os.environ.get("QUICKBOT_FAKE_PROFILE")
    if fake:
        with open(fake) as f:
            return _finalize_profile(json.load(f))
    path = profile_path()
    if not force and os.path.exists(path):
        try:
            with open(path) as f:
                return _finalize_profile(json.load(f))
        except (OSError, ValueError):
            pass
    prof = _detect_profile()
    os.makedirs(_data_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(prof, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return prof


def require_apple_silicon(profile=None):
    profile = profile or system_profile()
    if not profile.get("appleSilicon"):
        raise CatalogError(
            "Quickbot requires Apple silicon. Intel Macs are not supported.")
    return profile


def select_tier(ram_gb, tiers=None):
    """Largest of {8, 16, 32} that is ≤ RAM. None if RAM is below 8 GB."""
    if tiers is None:
        try:
            tiers = load_tiers()
        except CatalogError:
            tiers = {8: {}, 16: {}, 32: {}}
    available = sorted((t for t in tiers if t <= int(ram_gb or 0)), reverse=True)
    return available[0] if available else None


# --- HF metadata ----------------------------------------------------------


def _http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": HF_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def hf_metadata(repo):
    """weightsGB (safetensors blobs), maxModelContext, capabilities.

    Network errors raise CatalogError. Partial results are still returned
    when config.json is missing.
    """
    api = "https://huggingface.co/api/models/{}?blobs=true".format(
        urllib.parse.quote(repo, safe="/"))
    try:
        info = _http_json(api)
    except urllib.error.HTTPError as e:
        raise CatalogError("Hugging Face API {} for {}: {}".format(e.code, repo, e.reason))
    except Exception as e:
        raise CatalogError("Hugging Face API failed for {}: {}".format(repo, e))

    weights = 0
    for sib in info.get("siblings") or []:
        name = (sib.get("rfilename") or "").lower()
        if name.endswith(".safetensors"):
            weights += int(sib.get("size") or 0)
    if not weights:
        for sib in info.get("siblings") or []:
            weights += int(sib.get("size") or 0)
    weights_gb = round(weights / 1e9, 1) if weights else None

    cfg = {}
    for path in (
        "https://huggingface.co/{}/raw/main/config.json".format(repo),
        "https://huggingface.co/{}/resolve/main/config.json".format(repo),
    ):
        try:
            cfg = _http_json(path)
            break
        except Exception:
            continue

    max_ctx = (
        cfg.get("max_position_embeddings")
        or (cfg.get("text_config") or {}).get("max_position_embeddings")
        or cfg.get("max_sequence_length")
        or (cfg.get("text_config") or {}).get("max_sequence_length")
    )
    if max_ctx is not None:
        try:
            max_ctx = int(max_ctx)
        except (TypeError, ValueError):
            max_ctx = None

    arches = " ".join(cfg.get("architectures") or []).lower()
    name = (repo.rsplit("/", 1)[-1] + " " + (info.get("modelId") or "")).lower()
    vision = bool(cfg.get("vision_config")) or "vl" in arches or "vision" in arches
    thinking = "qwen3" in name or "qwen2.5" in name
    tools = True
    return {
        "weightsGB": weights_gb,
        "maxModelContext": max_ctx,
        "capabilities": {"vision": vision, "tools": tools, "thinking": thinking},
    }


def estimated_peak_gb(weights_gb):
    if weights_gb is None:
        return None
    return round(float(weights_gb) * 1.2 + 2.0, 1)


def fitting_tiers(peak_gb, tiers):
    """Tiers where peak + headroom ≤ tier GB."""
    if peak_gb is None:
        return []
    out = []
    for tier in (8, 16, 32):
        spec = tiers.get(tier) or {}
        headroom = float(spec.get("headroomGB") or 0)
        if peak_gb + headroom <= tier:
            out.append(tier)
    return out


# --- resolve --------------------------------------------------------------


def _apply_prompt_cache(cfg, enabled):
    env = dict(cfg.get("envVars") or {})
    if enabled:
        env["APC_ENABLED"] = "1"
    else:
        env.pop("APC_ENABLED", None)
    cfg["envVars"] = env
    cfg["promptCache"] = bool(enabled)


def _model_dir(identity):
    scheme, rest = split_address(identity)
    if scheme == "hf":
        return os.path.join(_sc().MODELS_DIR, rest.rsplit("/", 1)[-1])
    return None


def resolve(cfg):
    """Return (effective_cfg, trace_or_none). Never raises.

    When the catalog is inactive or no pin is found, `cfg` is returned
    unchanged (legacy config.json path) with a notice in the trace.
    """
    if not catalog_active():
        return cfg, None
    try:
        return _resolve(cfg)
    except CatalogError as e:
        if "Apple silicon" in str(e):
            raise
        print("catalog: {} — using config.json".format(e), file=sys.stderr)
        return cfg, "catalog inactive: {}".format(e)
    except Exception as e:
        print("catalog resolve failed, using config.json: {}".format(e), file=sys.stderr)
        return cfg, "catalog inactive: {}".format(e)


def _resolve(cfg, identity=None):
    import engines
    profile = system_profile(force=False)
    require_apple_silicon(profile)
    tiers = load_tiers()
    index, models, _ = load_settings()
    catalog = load_catalog()
    catalog_models = catalog.get("models") or {}
    warnings = []

    selected_tier = select_tier(profile.get("ramGB"), tiers)
    if identity is None:
        pin, pin_tier = pick_pin_walk(index, selected_tier, warnings)
    else:
        pin = normalize_address(identity) or identity
        pin_tier = selected_tier
        # Prefer the smallest tier that actually lists this identity so
        # overrides from that tier apply; fall back to the machine tier.
        for t in (8, 16, 32):
            if any(e["identity"] == pin for e in (index.get(t) or [])):
                if t <= (selected_tier or t):
                    pin_tier = t
        if identity is not None and pin_tier is None:
            pin_tier = selected_tier

    if not pin:
        notice = "resolved: no pin in tier {} (or smaller) — using config.json".format(
            selected_tier)
        for w in warnings:
            print("catalog: {}".format(w), file=sys.stderr)
        return cfg, notice

    facts = catalog_models.get(pin) or {}
    block = models.get(pin) or {}
    if not block:
        notice = "resolved: pin {} has no settings block — using config.json".format(pin)
        return cfg, notice

    tier_spec = tiers.get(pin_tier) or {}
    overrides = dict(tier_spec.get("overrides") or {})

    effective = dict(cfg)
    scheme, rest = split_address(pin)
    engine = block.get("engine") or ("ollama" if scheme == "ollama" else "mlx_vlm")
    engine = engines.engine_key(engine)
    if engine not in KNOWN_ENGINES:
        engine = "mlx_vlm"

    context = block.get("contextLength")
    prompt_cache = block.get("promptCache")
    if prompt_cache is None:
        prompt_cache = True
    kv = block.get("kvCacheQuant")
    decoder = dict(block.get("decoder") or {"kind": "none"})
    extra_args = list(block.get("extraArgs") or [])
    env_vars = dict(block.get("envVars") or {})
    caps_user = dict(block.get("capabilities") or {})
    caps_canon = dict(facts.get("capabilities") or {})

    clamps = []
    if "contextLength" in overrides and context is not None:
        ov = overrides["contextLength"]
        if ov != context:
            clamps.append("ctx")
            context = ov
    elif "contextLength" in overrides:
        context = overrides["contextLength"]
        clamps.append("ctx")
    if "promptCache" in overrides and overrides["promptCache"] != prompt_cache:
        prompt_cache = overrides["promptCache"]
        clamps.append("promptCache")
    if "kvCacheQuant" in overrides:
        kv = overrides["kvCacheQuant"]
        clamps.append("kvCacheQuant")
    if "engine" in overrides:
        engine = engines.engine_key(overrides["engine"])
        clamps.append("engine")

    # Capabilities: settings can only DISABLE what catalog.json grants.
    caps = {}
    for key in CAPABILITY_KEYS:
        granted = bool(caps_canon.get(key)) if caps_canon else bool(caps_user.get(key))
        caps[key] = bool(caps_user.get(key)) if key in caps_user else granted
        if caps[key] and caps_canon and not caps_canon.get(key):
            caps[key] = False

    effective["engine"] = engine
    effective["backend"] = engine  # leftover key; same value as engine
    if context is not None:
        effective["contextLength"] = int(context)
    effective["decoder"] = decoder
    effective["kvCacheQuant"] = kv
    effective["extraArgs"] = extra_args
    effective["envVars"] = env_vars
    effective["capabilities"] = caps
    effective["displayName"] = block.get("displayName") or facts.get("name") or identity_name(pin)
    effective["tier"] = pin_tier
    effective["catalogIdentity"] = pin
    effective["catalogActive"] = True
    _apply_prompt_cache(effective, prompt_cache)

    if engine == "ollama":
        effective["ollamaModel"] = rest
        effective["model"] = rest
        effective["draftModel"] = None
    else:
        effective["executable"] = os.path.join(_sc().env_bin_dir(), "mlx_vlm.server")
        effective["model"] = _model_dir(pin)
        draft_ident = decoder.get("draft") if decoder.get("kind") in ("mtp", "draft") else None
        if draft_ident:
            d_scheme, d_rest = split_address(draft_ident)
            if d_scheme == "hf":
                effective["draftModel"] = os.path.join(_sc().MODELS_DIR, d_rest.rsplit("/", 1)[-1])
            else:
                effective["draftModel"] = d_rest
        else:
            effective["draftModel"] = None

    cap_note = " (tier-capped)" if "ctx" in clamps else ""
    trace = "resolved: tier={} pin={} engine={} ctx={}{}".format(
        pin_tier, pin, engine, effective.get("contextLength"), cap_note)
    if clamps and "ctx" not in clamps:
        trace += " (tier-capped: {})".format(",".join(clamps))
    for w in warnings:
        print("catalog: {}".format(w), file=sys.stderr)
    return effective, trace


def resolve_identity(cfg, identity):
    """Resolve as if `identity` were the pin (used by measure/validate --live)."""
    if not catalog_active():
        raise CatalogError(
            "catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME))
    return _resolve(cfg, identity=identity)


# --- Operation marker -----------------------------------------------------


def current_operation():
    """Return the live operation dict, or None. Deletes a stale (dead-pid) marker."""
    path = operation_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        pid = int(data.get("pid") or 0)
    except (OSError, ValueError, TypeError):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    if pid and not _sc().is_alive(pid):
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    return data


def _write_operation(label):
    os.makedirs(_data_dir(), exist_ok=True)
    payload = {
        "label": label,
        "pid": os.getpid(),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    path = operation_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.write("\n")
    os.replace(tmp, path)


def _clear_operation():
    try:
        os.remove(operation_path())
    except OSError:
        pass


@contextlib.contextmanager
def catalog_operation(cfg, label):
    """Stop the stack, publish a marker, restore afterwards (incl. ctrl-C)."""
    sc = _sc()
    state, _, _ = sc.current_state(cfg)
    was_up = state != "stopped"
    if was_up:
        sc.cmd_stop(cfg)
    _write_operation(label)
    try:
        yield
    finally:
        _clear_operation()
        if was_up:
            sc.cmd_start(cfg)


# --- Benchmark ------------------------------------------------------------


def _prefill_prompt(n_tokens=3000):
    # ~1 token per word for this filler; a little extra to land near n_tokens.
    return ("alpha " * n_tokens).strip()


def _decode_prompt():
    return "Write a numbered list of twenty common household objects, one per line."


def run_benchmark(cfg, on_progress=None):
    """Start the engine (no proxy), run prefill + decode probes, stop it.

    Returns dict with prefillTokPerSec, decodeTokPerSec, expectedPeakGB.
    """
    import engines
    eng = engines.get_engine(cfg)
    if on_progress:
        on_progress("starting engine")
    _proc, pid = engines.spawn(cfg)
    peak_rss = [0.0]
    stop_sample = threading.Event()

    def sampler():
        while not stop_sample.wait(0.2):
            try:
                peak_rss[0] = max(peak_rss[0], eng.peak_memory_gb(pid))
            except Exception:
                pass

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    try:
        if not engines.wait_healthy(cfg, timeout=900):
            raise CatalogError("engine did not become healthy in time (see the log)")
        eng.post_start()
        # Ollama answers / instantly; wait until the runner has the model.
        if eng.name == "ollama":
            deadline = time.time() + 300
            while time.time() < deadline:
                loaded = False
                try:
                    loaded = eng.model_loaded()
                except Exception:
                    loaded = False
                if loaded or peak_rss[0] > 8.0:
                    break
                time.sleep(1)
        if on_progress:
            on_progress("prefill probe")
        t0 = time.perf_counter()
        prefill_stats, _ = engines.chat_completions(
            cfg, [{"role": "user", "content": _prefill_prompt()}], max_tokens=16)
        prefill_dt = time.perf_counter() - t0
        if on_progress:
            on_progress("decode probe")
        t1 = time.perf_counter()
        decode_stats, _ = engines.chat_completions(
            cfg, [{"role": "user", "content": _decode_prompt()}], max_tokens=200)
        decode_dt = time.perf_counter() - t1
    finally:
        stop_sample.set()
        engines.terminate(cfg, pid)

    peaks = [v for v in (
        prefill_stats.get("peak_memory"),
        decode_stats.get("peak_memory"),
        peak_rss[0],
    ) if v]
    peak = max(peaks) if peaks else None
    prefill_tps = prefill_stats.get("prompt_per_second")
    if not prefill_tps and prefill_stats.get("prompt_tokens") and prefill_dt > 0:
        prefill_tps = float(prefill_stats["prompt_tokens"]) / prefill_dt
    decode_tps = decode_stats.get("predicted_per_second")
    if not decode_tps and decode_stats.get("completion_tokens") and decode_dt > 0:
        decode_tps = float(decode_stats["completion_tokens"]) / decode_dt
    return {
        "prefillTokPerSec": round(float(prefill_tps), 1) if prefill_tps else None,
        "decodeTokPerSec": round(float(decode_tps), 1) if decode_tps else None,
        "expectedPeakGB": round(float(peak), 1) if peak else None,
        "prefill": prefill_stats,
        "decode": decode_stats,
        "rssPeakGB": round(float(peak_rss[0]), 1) if peak_rss[0] else None,
    }


def _engine_version(engine):
    pkgs = {"mlx_vlm": "mlx-vlm", "ollama": None}
    pkg = pkgs.get(engine)
    if not pkg:
        if engine == "ollama":
            import engines
            # Best-effort `ollama --version`.
            try:
                proc = __import__("subprocess").run(
                    ["ollama", "--version"], capture_output=True, text=True, timeout=5,
                )
                lines = ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines()
                for line in reversed(lines):
                    text = line.strip()
                    if not text:
                        continue
                    lower = text.lower()
                    if lower.startswith("warning") or "could not connect" in lower:
                        continue
                    return text
                return None
            except Exception:
                return None
        return None
    try:
        import importlib.metadata
        return importlib.metadata.version(pkg)
    except Exception:
        return None


# --- CLI: profile / list / sync / validate / measure ----------------------


def _confirm(prompt, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        reply = input(prompt + " ").strip().lower()
    except EOFError:
        return False
    return reply in ("y", "yes")


def cmd_profile(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    as_json = "--json" in args
    try:
        prof = system_profile(force=True)
        require_apple_silicon(prof)
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    tiers = load_tiers() if catalog_files_present() else {8: {}, 16: {}, 32: {}}
    tier = select_tier(prof.get("ramGB"), tiers)
    pin = None
    display = None
    if catalog_active():
        index, models, _ = load_settings()
        pin, _ = pick_pin_walk(index, tier, [])
        if pin:
            display = (models.get(pin) or {}).get("displayName")
    info = dict(prof)
    info["tier"] = tier
    info["pinned"] = pin
    info["displayName"] = display
    if as_json:
        print(json.dumps(info))
        return 0
    print("Chip:     {}".format(prof.get("chip")))
    print("macOS:    {}".format(prof.get("macosVersion")))
    print("RAM:      {} GB → tier {}".format(prof.get("ramGB"), tier if tier else "—"))
    print("Disk:     {} GB free".format(prof.get("freeDiskGB")))
    if pin:
        extra = " ({})".format(display) if display else ""
        print("Pinned:   {}{}".format(pin, extra))
    else:
        print("Pinned:   (none — config.json)")
    return 0


def cmd_list(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    if not catalog_files_present():
        print("No catalog files yet. Run `{} catalog sync`.".format(_sc().CLI_NAME))
        return 0
    catalog = load_catalog()
    index, models, _ = load_settings()
    # identity → {tiers, pinned_tiers}
    placement = {}
    for tier in (8, 16, 32):
        for entry in index.get(tier) or []:
            ident = entry["identity"]
            rec = placement.setdefault(ident, {"tiers": [], "pins": []})
            rec["tiers"].append(str(tier) if not entry.get("pinned") else "*" + str(tier))
            if entry.get("pinned"):
                rec["pins"].append(tier)
    identities = list(dict.fromkeys(
        list((catalog.get("models") or {}).keys()) + list(models.keys()) + list(placement.keys())
    ))
    if not identities:
        print("(empty catalog)")
        return 0
    rows = []
    for ident in identities:
        facts = (catalog.get("models") or {}).get(ident) or {}
        block = models.get(ident) or {}
        status = facts.get("status") or "—"
        place = " ".join(placement.get(ident, {}).get("tiers") or ["—"])
        peak = facts.get("expectedPeakGB")
        if peak is None and facts.get("estimated"):
            peak = estimated_peak_gb(facts.get("weightsGB"))
        if peak is None:
            peak_s = "—"
        elif facts.get("estimated"):
            peak_s = "~{:.1f}".format(peak)
        else:
            peak_s = "{:.1f}".format(peak)
        tps = (facts.get("performance") or {}).get("decodeTokPerSec")
        tps_s = "{:.0f}".format(tps) if tps is not None else "—"
        engine = block.get("engine") or facts.get("scheme") or "—"
        if engine == "hf":
            engine = "mlx_vlm"
        rows.append((ident, status, place, peak_s, tps_s, engine))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    widths[0] = max(widths[0], 10)
    for ident, status, place, peak_s, tps_s, engine in rows:
        print("{:{w0}}  {:{w1}}  {:{w2}}  peak {:>{w3}} GB  {:>{w4}} tok/s  {}".format(
            ident, status, place, peak_s, tps_s, engine,
            w0=widths[0], w1=widths[1], w2=widths[2], w3=widths[3], w4=widths[4]))
    return 0


def cmd_sync(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    assume_yes = "--yes" in args
    no_measure = "--no-measure" in args or not sys.stdin.isatty()
    _ensure_catalog_files()
    if not yaml_available():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1

    seed = parse_seed()
    catalog = load_catalog()
    catalog_models = catalog.setdefault("models", {})
    tiers = load_tiers()
    index, models, text = load_settings()
    seed_set = set(seed)
    existing = set(catalog_models) | set(models)
    added = [i for i in seed if i not in existing]
    # Also treat "in seed but missing catalog.json entry" as add.
    added = [i for i in seed if i not in catalog_models]
    removed = [i for i in list(catalog_models) if i not in seed_set]

    # Removal: prune catalog.json, index, and the settings block.
    for ident in removed:
        catalog_models.pop(ident, None)
        text = remove_model_block(text, ident)
        for tier in list(index):
            index[tier] = [e for e in index[tier] if e["identity"] != ident]
        print("removed {}".format(ident))

    new_for_measure = []
    for ident in seed:
        if ident in catalog_models and ident in (models or {}) and any(
                e["identity"] == ident for t in index.values() for e in t):
            continue
        scheme, rest = split_address(ident)
        if scheme == "ollama":
            import engines
            if not engines.ollama_model_is_mlx(rest):
                print("err: {} is not an MLX Ollama model (tag must contain "
                      "'mlx', e.g. qwen3.8:27b-mlx) — skipped".format(ident),
                      file=sys.stderr)
                continue
        meta = {"weightsGB": None, "maxModelContext": None,
                "capabilities": {"vision": False, "tools": True, "thinking": False}}
        if scheme == "hf":
            try:
                meta = hf_metadata(rest)
            except CatalogError as e:
                print("warning: {} — scaffolding with estimates skipped".format(e),
                      file=sys.stderr)
        weights = meta.get("weightsGB")
        peak = estimated_peak_gb(weights)
        if ident not in catalog_models:
            catalog_models[ident] = {
                "address": ident,
                "scheme": scheme,
                "name": identity_name(ident),
                "status": "unmeasured",
                "weightsGB": weights,
                "expectedPeakGB": peak,
                "estimated": True,
                "performance": None,
                "capabilities": meta.get("capabilities") or {
                    "vision": False, "tools": True, "thinking": False},
                "maxModelContext": meta.get("maxModelContext"),
                "measurement": None,
            }
            print("added {} (unmeasured, est. peak {} GB)".format(
                ident, peak if peak is not None else "?"))
            new_for_measure.append(ident)
        # Scaffold a settings block only if one is not already there.
        if BLOCK_MARKER_FMT.format(identity=ident) not in text:
            text = append_model_block(text, ident, scaffold_block(ident, meta))
        # Index: add to fitting tiers if the identity is in none of them.
        in_any = any(e["identity"] == ident for t in index.values() for e in t)
        if not in_any:
            fits = fitting_tiers(peak, tiers)
            if not fits:
                print("warning: {} estimated peak {} GB does not fit any tier; "
                      "pin it manually if you want it auto-selected".format(
                          ident, peak), file=sys.stderr)
            for tier in fits:
                index.setdefault(tier, []).append({
                    "identity": ident, "pinned": False, "comment": "",
                })

    # Duplicate-pin warnings always printed.
    warnings = []
    for tier in (8, 16, 32):
        pick_pin(index.get(tier) or [], warnings)
    for w in warnings:
        print("warning: {}".format(w), file=sys.stderr)

    text = rewrite_index(text, index)
    save_settings_text(text)
    save_catalog(catalog)
    print("sync complete ({} models).".format(len(catalog_models)))

    if no_measure or not new_for_measure:
        return 0
    cfg = _sc().load_config()
    for ident in new_for_measure:
        facts = catalog_models.get(ident) or {}
        size = facts.get("weightsGB")
        size_s = "~{} GB".format(size) if size is not None else "an unknown size"
        prompt = ("Measure now? Downloads {} and pauses Quickbot for ~2 min. [y/N]"
                  .format(size_s))
        if not _confirm(prompt, assume_yes):
            continue
        code = cmd_measure([ident, "--yes"])
        if code != 0:
            return code
    return 0


def _lookup_identity(raw):
    ident = normalize_address(raw) or raw
    catalog = load_catalog()
    models = (catalog.get("models") or {})
    if ident in models:
        return ident
    # Unique suffix / name match.
    matches = [k for k in models if k.endswith(raw) or identity_name(k) == raw]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        # Settings-only identity (not yet in catalog.json).
        if catalog_files_present():
            _, blocks, _ = load_settings()
            if ident in blocks:
                return ident
        raise CatalogError("unknown model: {}".format(raw))
    raise CatalogError("ambiguous model {!r}: {}".format(raw, ", ".join(matches)))


def cmd_measure(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    assume_yes = "--yes" in args
    rest = [a for a in args if a != "--yes"]
    if not rest:
        print("usage: {} catalog measure <identity> [--yes]".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 2
    try:
        ident = _lookup_identity(rest[0])
        require_apple_silicon()
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1

    sc = _sc()
    cfg = sc.load_config()
    try:
        effective, trace = resolve_identity(cfg, ident)
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    if trace:
        print(trace)

    import engines
    eng = engines.get_engine(effective)
    need_download = not eng.model_present()
    catalog = load_catalog()
    facts = (catalog.get("models") or {}).get(ident) or {}
    size = facts.get("weightsGB")
    size_s = "~{} GB".format(size) if size is not None else "an unknown size"
    if need_download:
        prompt = ("Measure {}? Downloads {} and pauses Quickbot for a few minutes. [y/N]"
                  .format(ident, size_s))
    else:
        prompt = ("Measure {}? This pauses Quickbot for a few minutes. [y/N]"
                  .format(ident))
    if not _confirm(prompt, assume_yes):
        print("cancelled.")
        return 0

    label = "Benchmarking {}".format(ident)
    # Resolve the *currently serving* cfg so stop/start restore the right stack.
    serving, _ = resolve(cfg)
    try:
        with catalog_operation(serving, label):
            if need_download:
                code = eng.ensure_model()
                if code != 0:
                    return code
            print("==> Benchmarking {}".format(ident))
            result = run_benchmark(effective, on_progress=lambda s: print("    {}…".format(s)))
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — restoring the previous stack.", file=sys.stderr)
        return 130

    catalog = load_catalog()
    models = catalog.setdefault("models", {})
    entry = models.get(ident) or {
        "address": ident,
        "scheme": split_address(ident)[0],
        "name": identity_name(ident),
        "capabilities": (facts.get("capabilities") or
                         {"vision": False, "tools": True, "thinking": False}),
        "maxModelContext": facts.get("maxModelContext"),
        "weightsGB": facts.get("weightsGB"),
    }
    try:
        _, blocks, _ = load_settings()
        block_caps = (blocks.get(ident) or {}).get("capabilities")
        if block_caps:
            entry["capabilities"] = dict(block_caps)
    except CatalogError:
        pass
    peak = result.get("expectedPeakGB")
    entry.update({
        "status": "measured",
        "estimated": False,
        "expectedPeakGB": peak,
        "performance": {
            "prefillTokPerSec": result.get("prefillTokPerSec"),
            "decodeTokPerSec": result.get("decodeTokPerSec"),
        },
        "measurement": {
            "date": time.strftime("%Y-%m-%d"),
            "engine": engines.engine_name(effective),
            "engineVersion": _engine_version(engines.engine_name(effective)),
            "contextLength": effective.get("contextLength"),
            "machineProfileHash": system_profile().get("hash"),
        },
    })
    models[ident] = entry
    save_catalog(catalog)
    print("measured {}: peak {} GB, decode {} tok/s, prefill {} tok/s".format(
        ident, peak, result.get("decodeTokPerSec"), result.get("prefillTokPerSec")))
    return 0


def cmd_validate(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    live = "--live" in args
    rest = [a for a in args if a != "--live"]
    identity = rest[0] if rest else None
    if not catalog_files_present():
        print("No catalog files yet. Run `{} catalog sync`.".format(_sc().CLI_NAME))
        return 0
    try:
        errors, warnings = validate(identity)
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    for w in warnings:
        print("warning: {}".format(w), file=sys.stderr)
    for e in errors:
        print("err: {}".format(e), file=sys.stderr)
    if errors:
        return 1
    if not live:
        print("validate: ok" + (" ({} warning{})".format(
            len(warnings), "" if len(warnings) == 1 else "s") if warnings else ""))
        return 0
    if not identity:
        print("validate --live requires an identity", file=sys.stderr)
        return 2
    return _validate_live(identity)


def validate(identity=None):
    """Deterministic checks. Returns (errors, warnings) lists of strings."""
    catalog = load_catalog()
    catalog_models = catalog.get("models") or {}
    tiers = load_tiers()
    index, models, text = load_settings()
    errors = []
    warnings = []
    identities = [identity] if identity else None
    if identities:
        identities = [_lookup_identity(identity)]

    def consider(ident):
        return identities is None or ident in identities

    # Duplicate pins.
    for tier in (8, 16, 32):
        pick_pin(index.get(tier) or [], warnings)

    # Index ↔ blocks ↔ catalog.json
    indexed = []
    for tier in (8, 16, 32):
        for entry in index.get(tier) or []:
            indexed.append(entry["identity"])
            ident = entry["identity"]
            if not consider(ident):
                continue
            if ident not in models:
                msg = "dangling index entry {} (no settings block)".format(ident)
                if msg not in errors:
                    errors.append(msg)
            if ident not in catalog_models:
                msg = "dangling index entry {} (not in catalog.json)".format(ident)
                if msg not in errors:
                    errors.append(msg)

    for ident, block in models.items():
        if not consider(ident):
            continue
        if ident not in catalog_models:
            errors.append("settings block {} has no catalog.json entry".format(ident))
        errs, warns = _validate_block(ident, block, catalog_models.get(ident), tiers, index)
        errors.extend(errs)
        warnings.extend(warns)

    for ident in catalog_models:
        if not consider(ident):
            continue
        if ident not in models:
            warnings.append("catalog.json entry {} has no settings block".format(ident))

    return errors, warnings


def _validate_block(ident, block, facts, tiers, index):
    errors = []
    warnings = []
    if not isinstance(block, dict):
        return ["settings block {} is not a mapping".format(ident)], []
    engine = block.get("engine")
    if engine not in KNOWN_ENGINES:
        errors.append("unknown engine {} for {}".format(engine, ident))
    scheme, rest = split_address(ident)
    if engine == "ollama" or scheme == "ollama":
        import engines
        tag = rest if scheme == "ollama" else ""
        if tag and not engines.ollama_model_is_mlx(tag):
            errors.append(
                "{} is not an MLX Ollama model (tag must contain 'mlx', "
                "e.g. qwen3.8:27b-mlx)".format(ident))
    ctx = block.get("contextLength")
    if ctx is None:
        errors.append("{} missing contextLength".format(ident))
    else:
        try:
            ctx = int(ctx)
        except (TypeError, ValueError):
            errors.append("{} contextLength is not an integer".format(ident))
            ctx = None
    decoder = block.get("decoder") or {}
    if not isinstance(decoder, dict) or decoder.get("kind") not in DECODER_KINDS:
        errors.append("{} decoder.kind must be one of {}".format(ident, ", ".join(DECODER_KINDS)))
    elif decoder.get("kind") in ("mtp", "draft") and not decoder.get("draft"):
        errors.append("{} decoder.kind={} requires decoder.draft".format(
            ident, decoder.get("kind")))
    elif decoder.get("kind") in ("mtp", "draft"):
        draft = decoder.get("draft")
        d_scheme, d_rest = split_address(draft)
        if d_scheme == "hf":
            path = os.path.join(_sc().MODELS_DIR, d_rest.rsplit("/", 1)[-1])
            if not os.path.exists(os.path.join(path, "config.json")):
                # Not fatal before setup/measure — warn.
                warnings.append("{} draft {} is not downloaded yet".format(ident, draft))
    if "displayName" not in block:
        errors.append("{} missing displayName".format(ident))

    facts = facts or {}
    max_ctx = facts.get("maxModelContext")
    if ctx is not None and max_ctx is not None and ctx > int(max_ctx):
        errors.append("{} contextLength {} exceeds maxModelContext {}".format(
            ident, ctx, max_ctx))

    canon_caps = facts.get("capabilities") or {}
    user_caps = block.get("capabilities") or {}
    for key in CAPABILITY_KEYS:
        if user_caps.get(key) and canon_caps and not canon_caps.get(key):
            errors.append('capability "{}" enabled but the model does not possess {}'.format(
                key, key))

    peak = facts.get("expectedPeakGB")
    estimated = bool(facts.get("estimated"))
    for tier in (8, 16, 32):
        if not any(e["identity"] == ident for e in (index.get(tier) or [])):
            continue
        headroom = float((tiers.get(tier) or {}).get("headroomGB") or 0)
        if peak is None:
            continue
        if peak + headroom > tier:
            msg = "{} expectedPeakGB {} + headroom {} exceeds tier {} GB".format(
                ident, peak, headroom, tier)
            if estimated:
                warnings.append(msg + " (estimated)")
            else:
                errors.append(msg)
        overrides = (tiers.get(tier) or {}).get("overrides") or {}
        if ctx is not None and "contextLength" in overrides and overrides["contextLength"] != ctx:
            warnings.append(
                "{} contextLength {} clamped to {} in tier {}".format(
                    ident, ctx, overrides["contextLength"], tier))
        if "promptCache" in overrides and overrides["promptCache"] != block.get("promptCache"):
            warnings.append(
                "{} promptCache {} clamped to {} in tier {}".format(
                    ident, block.get("promptCache"), overrides["promptCache"], tier))
    return errors, warnings


def _validate_live(identity):
    ident = _lookup_identity(identity)
    sc = _sc()
    cfg = sc.load_config()
    try:
        effective, trace = resolve_identity(cfg, ident)
        require_apple_silicon()
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    if trace:
        print(trace)
    serving, _ = resolve(cfg)
    catalog = load_catalog()
    facts = (catalog.get("models") or {}).get(ident) or {}
    label = "Validating {}".format(ident)
    try:
        with catalog_operation(serving, label):
            print("==> Live-validating {}".format(ident))
            result = run_benchmark(effective, on_progress=lambda s: print("    {}…".format(s)))
    except CatalogError as e:
        print(e, file=sys.stderr)
        return 1
    peak = result.get("expectedPeakGB")
    decode = result.get("decodeTokPerSec")
    expected_peak = facts.get("expectedPeakGB")
    expected_decode = (facts.get("performance") or {}).get("decodeTokPerSec")
    print("live: peak {} GB, decode {} tok/s".format(peak, decode))
    if expected_decode and decode and decode < 0.85 * float(expected_decode):
        print("warning: decode {} tok/s is >15% worse than expected {}".format(
            decode, expected_decode), file=sys.stderr)
    if expected_peak and peak and peak > 1.15 * float(expected_peak):
        print("warning: peak {} GB is >15% worse than expected {}".format(
            peak, expected_peak), file=sys.stderr)
    return 0


def cmd_catalog(args):
    if catalog_need_setup():
        print("catalog features need setup first (run: {} setup)".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 1
    if not args:
        print("usage: {} catalog {{sync|validate|measure|list}}".format(_sc().CLI_NAME),
              file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    if sub == "sync":
        return cmd_sync(rest)
    if sub == "validate":
        return cmd_validate(rest)
    if sub == "measure":
        return cmd_measure(rest)
    if sub == "list":
        return cmd_list(rest)
    print("usage: {} catalog {{sync|validate|measure|list}}".format(_sc().CLI_NAME),
          file=sys.stderr)
    return 2
