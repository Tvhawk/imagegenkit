#!/usr/bin/env python3
"""imagegenkit - one command for AI image generation across providers (the CLI of the ImageGenKit Mac app).

  imagegenkit draft "PROMPT"         quick mockups and ideas   (provider from your settings)
  imagegenkit final "PROMPT"         final designs
  imagegenkit edit "PROMPT" -i x.png edit or refine an image
  imagegenkit transparent "PROMPT"   transparent PNG
  imagegenkit <provider> "PROMPT"    use one provider directly (openai, grok, codex, xai, gemini, ...)

Providers: Grok and Codex subscriptions (via the Grok Build and Codex CLIs), OpenAI, xAI, Google Gemini,
OpenRouter, Recraft, Together AI, fal.ai (API keys), and any OpenAI-compatible endpoint you add. API keys live in the macOS Keychain
("imagegenkit-<provider>"); env vars like OPENAI_API_KEY override them. Settings: ~/.config/imagegenkit/config.json,
best edited in the ImageGenKit app. Saved image paths go to stdout (one per line); everything else to stderr.
Stdlib only, Python 3.9+, macOS.
"""
import argparse
import base64
import datetime as dt
import getpass
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import http.client as http_client
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.1.0"
HOMEPAGE = "https://github.com/davidkrammer/imagegenkit"
GITHUB_REPO = "davidkrammer/imagegenkit"
# The env overrides are for testing against a local server.
RAW_BASE = os.environ.get("IMAGEGENKIT_RAW_BASE", f"https://raw.githubusercontent.com/{GITHUB_REPO}").rstrip("/")
RELEASES_API = os.environ.get("IMAGEGENKIT_RELEASES_API", f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
CATALOG_URL = f"{RAW_BASE}/main/catalog/providers.json"

CONFIG_DIR = Path.home() / ".config" / "imagegenkit"
LEGACY_DIR = Path.home() / ".config" / "imagegen"  # used by the old `imagegen` CLI
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "log.jsonl"
THUMB_DIR = CONFIG_DIR / "thumbs"  # 512 px previews, so the history keeps its pictures after files are deleted
THUMB_PX = 512
TIMEOUT = 600

TASKS = {
    "draft": "quick mockups, ideas and drafts",
    "final": "final designs and final-level mockups",
    "edit": "edit or refine existing images (needs -i)",
    "transparent": "transparent PNGs: logos, stickers, cut-outs",
}

# Built-in defaults. Override with `imagegenkit config set <key> <value>` or in the ImageGenKit app.
DEFAULTS = {
    "openai_model": "gpt-image-2.5-flare",        # fast everyday generation
    "openai_edit_model": "gpt-image-2.5-sunburst",  # used when reference images are given
    "xai_model": "grok-imagine-image",
    "gemini_model": "gemini-nano-banana-2.1",
    "resolution": "2K",
    "quality": "medium",
    "output_dir": "~/Pictures/ImageGenKit",
    "order": ["grok", "codex", "openai", "xai", "gemini", "local"],  # fallback order when the task's provider isn't set up
    "tasks": {
        "draft": {"provider": "grok", "quality": "low", "resolution": "1K"},
        "final": {"provider": "openai"},
        "edit": {"provider": "openai"},
        "transparent": {"provider": "openai"},
    },
    "providers": {},  # your own OpenAI-compatible endpoints: {"name": {"base_url": ..., "model": ...}}
    "provider_defaults": {},  # per provider: {"openai": {"quality": "medium", "resolution": "2K", "model": ...}}
    "limits": {},  # {"monthly_cost": 20, "monthly_images": 500, "stop_paid": true}
}

# Price estimates in USD (same defaults as the app; config "prices" and the cached catalog override them).
DEFAULT_PRICES = {
    "openai": {"text_in": 5, "image_in": 8, "image_out": 30},
    "xai": {"per_image": 0.02}, "gemini": {"per_image": 0.0336}, "openrouter": {"per_image": 0.009},
    "recraft": {"per_image": 0.035}, "together": {"per_image": 0.0154}, "fal": {"per_image": 0.003},
}

BUILTIN_PROVIDERS = {
    "openai": {"type": "openai", "label": "OpenAI", "base_url": "https://api.openai.com/v1",
               "env": "OPENAI_API_KEY", "key_url": "https://platform.openai.com/api-keys"},
    "grok": {"type": "grok-build", "label": "Grok subscription", "key_url": "https://grok.com"},
    "codex": {"type": "codex-cli", "label": "Codex subscription", "key_url": "https://chatgpt.com/codex"},
    "xai": {"type": "xai", "label": "xAI API", "base_url": "https://api.x.ai/v1",
            "env": "XAI_API_KEY", "key_url": "https://console.x.ai"},
    "gemini": {"type": "gemini", "label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey"},
    "local": {"type": "local", "label": "On this Mac"},
}
CAN_TRANSPARENT = {"openai"}  # provider types that return real alpha
CUSTOM_TYPES = ("openai-compatible", "openrouter", "together", "fal")  # adapters for providers in config/catalog
SUBSCRIPTION_TYPES = {"grok-build", "codex-cli"}  # log in instead of an API key
FREE_TYPES = SUBSCRIPTION_TYPES | {"local"}  # no per-image cost: never counted against limits
KEYLESS_TYPES = SUBSCRIPTION_TYPES | {"local"}
RESERVED = set(TASKS) | {"usage", "update", "check", "models", "set-key", "delete-key", "config", "providers", "provider", "version",
                         "help", "log", "local-models"}

MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".gif": "image/gif", ".heic": "image/heic", ".heif": "image/heif",
}
EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
GROK_HOME = Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))
GROK_WORKDIR = CONFIG_DIR / "grok-work"  # keeps these runs out of project session lists
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_WORKDIR = CONFIG_DIR / "codex-work"
GROK_ASPECTS = ["1:1", "16:9", "9:16", "3:2", "2:3"]
GROK_SYSTEM = ('You are an image generation runner. Do exactly what the user asks with the image tools, passing '
               'the prompt verbatim, then reply "done". Never call other tools. If a tool call is blocked or '
               'fails, reply with the error message instead.')
XAI_ASPECTS = ["1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "1:2", "2:1", "9:19.5", "19.5:9", "9:20",
               "20:9", "21:9", "5:2"]
GEMINI_API_REVISION = "2026-05-20"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


class Unavailable(Exception):
    """The provider isn't set up (no key, not logged in). Task commands fall back to the next one."""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    log(f"imagegenkit: {msg}")
    sys.exit(code)


# ---------- config ----------

def migrate_legacy():
    """Move ~/.config/imagegen (old CLI) to ~/.config/imagegenkit once, leaving a symlink so logged paths stay valid."""
    try:
        if LEGACY_DIR.is_dir() and not LEGACY_DIR.is_symlink() and not CONFIG_DIR.exists():
            LEGACY_DIR.rename(CONFIG_DIR)
            LEGACY_DIR.symlink_to(CONFIG_DIR)
    except OSError:
        pass


def read_user_config():
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except ValueError as e:
        die(f"bad JSON in {CONFIG_FILE}: {e}")
    return data if isinstance(data, dict) else {}


def write_user_config(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(CONFIG_FILE)


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    user = read_user_config()
    cfg["_user_keys"] = sorted(user)
    for k, v in user.items():
        if k == "tasks" and isinstance(v, dict):
            for task, preset in v.items():
                if isinstance(preset, dict):
                    cfg["tasks"].setdefault(task, {}).update(preset)
        else:
            cfg[k] = v
    return cfg


def enable_provider(name):
    """Add a provider to the fallback order (so task commands use it)."""
    data = read_user_config()
    order = data.get("order") or list(load_config().get("order") or [])
    if name not in order:
        order.append(name)
        data["order"] = order
        write_user_config(data)


def catalog_providers():
    """Entries of the cached catalog (~/.config/imagegenkit/catalog.json), refreshed from GitHub."""
    try:
        entries = json.loads((CONFIG_DIR / "catalog.json").read_text()).get("providers", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("id")] if isinstance(entries, list) else []


def catalog_entry(name):
    """The provider's entry in the cached catalog."""
    return next((e for e in catalog_providers() if e.get("id") == name), {})


def refresh_catalog():
    """Download the latest provider catalog (default models, prices, guides). Keeps the newer copy."""
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "imagegenkit"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = r.read()
    new = json.loads(data)
    if not new.get("providers"):
        return False
    path = CONFIG_DIR / "catalog.json"
    try:
        old = json.loads(path.read_text())
        if str(old.get("updated", "")) >= str(new.get("updated", "")):
            return False
    except (OSError, ValueError):
        pass
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return True


def pref(cfg, spec, key, legacy=None):
    """A provider setting: your choice (provider_defaults.<name>.<key>, or an old global key you set),
    then the catalog's default (so new default models arrive without an update), then the built-in default."""
    d = (cfg.get("provider_defaults") or {}).get(spec["name"]) or {}
    if d.get(key) not in (None, ""):
        return d[key]
    if legacy and legacy in (cfg.get("_user_keys") or []):
        return cfg.get(legacy)
    if key in ("model", "edit_model"):
        e = catalog_entry(spec.get("catalog") or spec["name"])
        if e.get(key):
            return e[key]
    return cfg.get(legacy) if legacy else None


def all_providers(cfg):
    out = {}
    for name, spec in BUILTIN_PROVIDERS.items():
        out[name] = dict(spec, name=name, builtin=True)
    for name, spec in (cfg.get("providers") or {}).items():
        if name in out or name in RESERVED or not isinstance(spec, dict):
            continue
        s = dict(spec)
        s.setdefault("type", "openai-compatible")
        if s["type"] not in CUSTOM_TYPES:
            s["type"] = "openai-compatible"
        s.setdefault("label", name)
        s.setdefault("env", re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY")
        s.update(name=name, builtin=False)
        out[name] = s
    # Catalog providers the app would add (OpenRouter, Recraft, ...), so they work without the app too.
    for e in catalog_providers():
        name = e["id"]
        if name in out and out[name].get("catalog") == name and e.get("edits") is False:
            out[name].setdefault("edits", False)  # added by the app: capability flags still come from the catalog
        if name in out or name in RESERVED or e.get("adapter") not in CUSTOM_TYPES or not e.get("base_url"):
            continue
        s = {"type": e["adapter"], "label": e.get("name") or name, "catalog": name, "base_url": e["base_url"],
             "model": e.get("model"), "env": re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_API_KEY",
             "key_url": e.get("key_url"), "name": name, "builtin": False}
        for k in ("edit_model", "size_style"):
            if e.get(k):
                s[k] = e[k]
        if e.get("edits") is False:
            s["edits"] = False
        out[name] = s
    return out


# ---------- keys & readiness ----------

def keychain_service(name):
    return f"imagegenkit-{name}"


def keychain_store(service, key, label):
    cmd = f'add-generic-password -U -a "{getpass.getuser()}" -s "{service}" -l "{label}" -w "{key}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)  # stdin keeps the key out of argv
    return r.returncode == 0


def keychain_read(service):
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def key_for(spec):
    env = spec.get("env")
    if env and os.environ.get(env):
        return os.environ[env], f"env {env}"
    k = keychain_read(keychain_service(spec["name"]))
    if not k:  # key saved by the old `imagegen` CLI: copy it to the new name
        k = keychain_read(f"imagegen-{spec['name']}")
        if k:
            keychain_store(keychain_service(spec["name"]), k, f"ImageGenKit {spec['label']} API key")
    return (k, "Keychain") if k else (None, None)


def require_key(spec):
    k, _ = key_for(spec)
    if not k:
        raise Unavailable(f"no API key. Add one in ImageGenKit or run: imagegenkit set-key {spec['name']}")
    return k


def codex_bin():
    g = shutil.which("codex")
    if g:
        return g
    candidates = [Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), Path.home() / ".local/bin/codex"]
    candidates += sorted(Path.home().glob(".nvm/versions/node/*/bin/codex"), reverse=True)
    return next((str(c) for c in candidates if c.exists()), None)


def grok_bin():
    g = shutil.which("grok") or str(GROK_HOME / "bin" / "grok")
    return g if Path(g).exists() else None


def readiness(spec):
    """Fast local check (no network): (ready, reason)."""
    if spec["type"] == "grok-build":
        if not grok_bin():
            return False, "Grok Build CLI not installed"
        if not (GROK_HOME / "auth.json").exists():
            return False, "not logged in (run: grok login)"
        return True, "logged in"
    if spec["type"] == "codex-cli":
        if not codex_bin():
            return False, "Codex CLI not installed"
        if not (CODEX_HOME / "auth.json").exists():
            return False, "not logged in (run: codex login)"
        return True, "logged in"
    if spec["type"] == "local":
        return local_readiness()
    k, src = key_for(spec)
    if not k:
        return False, "no API key"
    if spec["type"] in ("openai-compatible", "openrouter", "together") and not spec.get("base_url"):
        return False, "no base_url"
    return True, f"key from {src}"


# ---------- http ----------

def short_text(raw, limit=300):
    """A non-JSON body (often an HTML error page) as one short line."""
    text = " ".join(re.sub(r"<[^>]+>", " ", raw).split())
    return (text[:limit] + "...") if len(text) > limit else (text or "(empty)")


def http(method, url, headers=None, body=None, retries=2):
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise ApiError(0, f"the provider sent an unreadable answer: {short_text(raw.decode('utf-8', 'replace'))}")
            if not isinstance(data, dict):
                raise ApiError(0, "the provider sent an unexpected answer")
            return data
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(raw).get("error", raw)
                msg = err.get("message", raw) if isinstance(err, dict) else str(err)
            except (ValueError, AttributeError):
                msg = short_text(raw)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = 5 * (attempt + 1)
                log(f"  HTTP {e.code}, retrying in {wait}s: {msg[:200]}")
                time.sleep(wait)
                continue
            raise ApiError(e.code, msg)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise ApiError(0, f"no answer from the provider after {TIMEOUT:g}s (timed out)")
            if attempt < retries:
                time.sleep(3)
                continue
            raise ApiError(0, f"network error: {e.reason}")
        except (socket.timeout, TimeoutError):  # no retry: the provider may still bill a slow request
            raise ApiError(0, f"no answer from the provider after {TIMEOUT:g}s (timed out)")
        except (OSError, http_client.HTTPException) as e:
            raise ApiError(0, f"network error: {e}")


def multipart(fields, files):
    boundary = uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files:
        p = Path(path)
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{p.name}"\r\n'
            f'Content-Type: {mime_of(p)}\r\n\r\n'.encode() + p.read_bytes() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


# ---------- helpers ----------

def mime_of(path):
    return MIME_BY_EXT.get(Path(path).suffix.lower(), "application/octet-stream")


def sniff(data, fallback="image/png"):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return fallback


def data_uri(path):
    return f"data:{mime_of(path)};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def read_prompt(a):
    if a.prompt_file:
        text = Path(a.prompt_file).expanduser().read_text()
    elif a.prompt == "-":
        text = sys.stdin.read()
    else:
        text = a.prompt
    if not text or not text.strip():
        die("empty prompt (pass it as an argument, '-' for stdin, or --prompt-file)")
    return text.strip()


def check_refs(paths):
    out = []
    for p in paths or []:
        pp = Path(p).expanduser()
        if not pp.is_file():
            die(f"reference image not found: {p}")
        out.append(pp)
    return out


def parse_extra(pairs):
    extra = {}
    for kv in pairs or []:
        if "=" not in kv:
            die(f"--extra expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        try:
            extra[k] = json.loads(v)
        except ValueError:
            extra[k] = v
    return extra


def note_unsupported(a, provider, names):
    for n in names:
        if getattr(a, n, None) not in (None, False, []):
            log(f"  note: {provider} ignores --{n.replace('_', '-')}")


def slug(text, words=7):
    s = re.sub(r"[^a-z0-9]+", "-", " ".join(text.lower().split()[:words])).strip("-")
    return s[:48].strip("-") or "image"


def unique(path, overwrite):
    if overwrite or not path.exists():
        return path
    i = 2
    while True:
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
        i += 1


def save_images(images, out, prompt, overwrite, cfg):
    """images: list of (bytes, mime). Returns saved paths."""
    now = dt.datetime.now()
    if out:
        target = Path(out).expanduser()
        if out.endswith("/") or target.is_dir():
            stem, user_ext = target / f"{now:%H%M%S}-{slug(prompt)}", None
        else:
            stem, user_ext = target.with_suffix(""), target.suffix.lower() or None
    else:
        base = Path(str(cfg.get("output_dir") or DEFAULTS["output_dir"])).expanduser()
        stem, user_ext = base / f"{now:%Y-%m-%d}" / f"{now:%H%M%S}-{slug(prompt)}", None
    saved = []
    for i, (data, mime) in enumerate(images):
        mime = sniff(data, mime or "image/png")
        want = MIME_BY_EXT.get(user_ext or "")
        if want in ("image/png", "image/jpeg") and mime != want:  # -o x.png / x.jpg: save what was asked for
            data, mime = convert_image(data, mime, want)
        ext = EXT_BY_MIME.get(mime, ".png")
        if user_ext and MIME_BY_EXT.get(user_ext) == mime:
            ext = user_ext  # keep .jpeg vs .jpg as requested
        elif user_ext and user_ext != ext:
            log(f"  note: provider returned {mime}; saving with {ext} instead of {user_ext}")
        name = stem if len(images) == 1 else stem.with_name(f"{stem.name}-{i + 1}")
        path = unique(name.with_name(name.name + ext), overwrite)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        saved.append(path)
    return saved


def convert_image(data, mime, want):
    """Convert to PNG or JPEG via macOS sips when -o asks for that format. Keeps the original if it fails."""
    GROK_WORKDIR.mkdir(parents=True, exist_ok=True)
    src = GROK_WORKDIR / f"{uuid.uuid4().hex}{EXT_BY_MIME.get(mime, '.img')}"
    dst = src.with_suffix(".out" + EXT_BY_MIME[want])
    fmt = ["-s", "format", "png"] if want == "image/png" else ["-s", "format", "jpeg", "-s", "formatOptions", "90"]
    try:
        src.write_bytes(data)
        subprocess.run(["sips"] + fmt + [str(src), "--out", str(dst)], capture_output=True)
        if dst.exists() and dst.stat().st_size:
            return dst.read_bytes(), want
    finally:
        for f in (src, dst):
            if f.exists():
                f.unlink()
    return data, mime


def append_log(entry):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"  (could not write log: {e})")


def thumb_key(ts, path):
    """Shared with the ImageGenKit app: sha1('<ts>|<absolute output path>')[:16]."""
    return hashlib.sha1(f"{ts}|{path}".encode()).hexdigest()[:16]


def archive_thumbs(ts, paths):
    """512 px previews in ~/.config/imagegenkit/thumbs/<YYYY-MM>/. Never fails the run."""
    thumbs = []
    for p in paths:
        try:
            alpha = "hasAlpha: yes" in subprocess.run(["sips", "-g", "hasAlpha", str(p)], capture_output=True,
                                                      text=True, timeout=30).stdout
            out = THUMB_DIR / ts[:7] / (thumb_key(ts, p) + (".png" if alpha else ".jpg"))
            out.parent.mkdir(parents=True, exist_ok=True)
            fmt = ["-s", "format", "png"] if alpha else ["-s", "format", "jpeg", "-s", "formatOptions", "72"]
            subprocess.run(["sips", "-Z", str(THUMB_PX)] + fmt + [str(p), "--out", str(out)],
                           capture_output=True, timeout=60)
            thumbs.append(str(out) if out.exists() else None)
        except (OSError, subprocess.SubprocessError):
            thumbs.append(None)
    return thumbs


def caller_info():
    """Who ran imagegenkit (agent, Claude account, T3 thread), for the usage log."""
    env = os.environ
    info = {}
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT"):
        m = re.search(r"\.claude-account(\d+)", env.get("CLAUDE_CONFIG_DIR", ""))
        info.update(agent="claude", account=int(m.group(1)) if m else 1)
    elif any(k.startswith("CODEX_") for k in env):
        info["agent"] = "codex"
    else:
        info["agent"] = "terminal" if sys.stdin.isatty() else "other"
    for key, var in (("entrypoint", "CLAUDE_CODE_ENTRYPOINT"), ("session", "CLAUDE_CODE_SESSION_ID"),
                     ("t3_thread", "T3CODE_THREAD_ID")):
        if env.get(var):
            info[key] = env[var]
    return info


def finish(a, cfg, spec, task, model, prompt, params, refs, images, usage, t0, extra=None):
    """Save images, print their paths, append the log entry."""
    if not images:
        die("the provider returned no image")
    paths = save_images(images, a.out, prompt, a.overwrite, cfg)
    secs = round(time.time() - t0, 1)
    log(f"  done in {secs}s, {len(paths)} image(s)")
    for p in paths:
        print(p, flush=True)
    ts = dt.datetime.now().isoformat(timespec="seconds")
    outputs = [Path(p).absolute() for p in paths]
    entry = {"ts": ts, "provider": spec["name"], "type": spec["type"], "model": model, "task": task,
             "prompt": prompt, "params": params, "refs": [str(r.absolute()) for r in refs],
             "outputs": [str(p) for p in outputs], "usage": usage, "seconds": secs, "cwd": os.getcwd()}
    entry.update(extra or {})
    entry.update(thumbs=archive_thumbs(ts, outputs), caller=caller_info(), v=VERSION)
    append_log(entry)


def parse_aspect(aspect):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", aspect or "")
    if not m:
        die(f"bad --aspect {aspect!r}, expected like 16:9")
    return float(m.group(1)), float(m.group(2))


def nearest_aspect(aspect, allowed):
    rw, rh = parse_aspect(aspect)
    r = rw / rh
    return min(allowed, key=lambda s: abs(math.log(r / (lambda w, h: w / h)(*parse_aspect(s)))))


def image_aspect(path):
    """'W:H' of an image via macOS sips, or None."""
    try:
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                             capture_output=True, text=True).stdout
        w = int(re.search(r"pixelWidth: (\d+)", out).group(1))
        h = int(re.search(r"pixelHeight: (\d+)", out).group(1))
    except (OSError, AttributeError, ValueError):
        return None
    return f"{w}:{h}" if 1 / 3 <= w / h <= 3 else None


def openai_size(aspect, resolution):
    """Map aspect + 1K/2K/4K to a GPT Image custom size (edges multiple of 16, <=3840, ratio 1:3..3:1)."""
    rw, rh = parse_aspect(aspect or "1:1")
    r = rw / rh
    if not (1 / 3 <= r <= 3):
        die(f"OpenAI only supports aspect ratios between 1:3 and 3:1 (got {aspect})")
    budget = {"1K": 1024 * 1024, "2K": 2560 * 1440, "4K": 8_294_400}.get((resolution or "1K").upper())
    if not budget:
        die("--resolution must be 1K, 2K or 4K")
    w, h = math.sqrt(budget * r), math.sqrt(budget / r)
    scale = min(1.0, 3840 / max(w, h))
    w, h = int(w * scale) // 16 * 16, int(h * scale) // 16 * 16
    return f"{w}x{h}"


def b64_image(text):
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError):
        raise ApiError(0, "the provider sent an image that can't be decoded")


def download(url):
    """An image the provider returned as a URL."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "imagegenkit"}),
                                    timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, f"couldn't download the image from {url[:120]}")
    except (OSError, ValueError, http_client.HTTPException) as e:
        raise ApiError(0, f"couldn't download the image from {url[:120]}: {getattr(e, 'reason', e)}")


def collect_images(resp, skip=()):
    """Base64 or URL images from an OpenAI-style `data` list."""
    images = []
    for item in resp.get("data", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            data = b64_image(item["b64_json"])
        elif item.get("url"):
            data = download(item["url"])
        else:
            continue
        if hashlib.sha1(data).hexdigest() not in skip:
            images.append((data, item.get("mime_type") or sniff(data)))
        if item.get("revised_prompt"):
            log(f"  revised prompt: {item['revised_prompt']}")
    return images


# ---------- usage & limits ----------

def prices_for(cfg, provider, model=None):
    """USD prices: built-in, then the catalog (per provider, then per model), then your own in config."""
    p = dict(DEFAULT_PRICES.get(provider, {}))
    entry = catalog_entry(provider)
    p.update(entry.get("prices") or {})
    if model:
        p.update((entry.get("model_prices") or {}).get(model) or {})
    p.update((cfg.get("prices") or {}).get(provider) or {})
    return p


def estimate_cost(entry, cfg):
    """Estimated USD of one logged run (0 for subscriptions). Mirrors ConfigStore.cost in the app."""
    if entry.get("type") in FREE_TYPES or entry.get("provider") == "grok":
        return 0.0
    u = entry.get("usage") or {}
    if isinstance(u.get("cost_usd"), (int, float)):
        return float(u["cost_usd"])
    if isinstance(u.get("cost_in_usd_ticks"), (int, float)):
        return u["cost_in_usd_ticks"] / 1e10
    provider = entry.get("provider", "")
    p = prices_for(cfg, provider, entry.get("model"))
    if ("input_tokens" in u or "output_tokens" in u) and not p.get("image_out") and entry.get("type") == "openai":
        p = prices_for(cfg, "openai", entry.get("model"))
    if ("input_tokens" in u or "output_tokens" in u) and p.get("image_out"):
        d = u.get("input_tokens_details") or {}
        text = d.get("text_tokens") or 0
        image = d.get("image_tokens") if d.get("image_tokens") is not None else (u.get("input_tokens", 0) - text)
        return (text * p.get("text_in", 0) + image * p.get("image_in", 0) + u.get("output_tokens", 0) * p["image_out"]) / 1e6
    if p.get("per_image"):
        return p["per_image"] * len(entry.get("outputs") or [])
    return 0.0


def month_usage(cfg):
    """This calendar month: (estimated spend, images, runs)."""
    month = dt.datetime.now().strftime("%Y-%m")
    cost = images = runs = 0
    try:
        with LOG_FILE.open() as f:
            for line in f:
                if f'"ts": "{month}' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                cost += estimate_cost(e, cfg)
                images += len(e.get("outputs") or [])
                runs += 1
    except OSError:
        pass
    return cost, images, runs


class LimitReached(Unavailable):
    """A monthly limit with 'stop_paid' blocks this (paid) provider."""


def limit_values(cfg):
    """(spend limit, image limit, stop_paid) from config `limits`, ignoring values that aren't positive numbers."""
    lim = cfg.get("limits")
    lim = lim if isinstance(lim, dict) else {}

    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None
    return num(lim.get("monthly_cost")), num(lim.get("monthly_images")), lim.get("stop_paid") is True


def check_limits(cfg, spec):
    """With 'stop_paid', refuse paid providers once the spend or image limit is reached. Subscriptions keep working
    (same as the app: "Paid images are paused until next month. Subscriptions still work.")."""
    cost_limit, image_limit, stop = limit_values(cfg)
    if not stop or spec["type"] in FREE_TYPES or not (cost_limit or image_limit):
        return
    cost, images, _ = month_usage(cfg)
    if cost_limit and cost >= cost_limit:
        raise LimitReached(f"monthly spend limit reached (${cost:.2f} of ${cost_limit:.2f})")
    if image_limit and images >= image_limit:
        raise LimitReached(f"monthly image limit reached ({images} of {image_limit:g})")


# ---------- providers ----------

def run_openai(a, cfg, spec, task):
    """OpenAI Images API, also used for any OpenAI-compatible endpoint."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    if refs and spec.get("edits") is False:
        die(f"{spec['label']}: reference images aren't supported; use another provider for edits")
    mask = check_refs([a.mask])[0] if a.mask else None
    official = spec["type"] == "openai"
    if official:
        model = a.model or (pref(cfg, spec, "edit_model", "openai_edit_model") if refs else pref(cfg, spec, "model", "openai_model"))
    else:
        model = a.model or pref(cfg, spec, "model") or (
            spec.get("edit_model") if refs and spec.get("edit_model") else spec.get("model"))
        if not model:
            die(f"no model set for {spec['name']} (imagegenkit provider add {spec['name']} ... --model <id>)")
    fmt = a.format
    if not fmt and a.out and Path(a.out).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        fmt = {".jpg": "jpeg"}.get(Path(a.out).suffix.lower(), Path(a.out).suffix.lower()[1:])
    fmt = fmt or pref(cfg, spec, "format")
    resolution = a.resolution or pref(cfg, spec, "resolution", "resolution" if official else None)
    params = {}
    if a.n > 1:
        params["n"] = a.n
    if a.size:
        params["size"] = a.size
    elif official:
        aspect = a.aspect
        if not aspect and refs:  # keep the input image's shape instead of forcing square
            aspect = image_aspect(refs[0])
        params["size"] = openai_size(aspect, resolution) if aspect or not refs else "auto"
    elif a.aspect and spec.get("size_style") == "aspect":  # e.g. Recraft takes "16:9"
        w, h = parse_aspect(a.aspect)
        params["size"] = f"{w:g}:{h:g}"
    elif a.aspect:
        params["size"] = openai_size(a.aspect, resolution or "1K")
    quality = a.quality or pref(cfg, spec, "quality", "quality" if official else None)
    background = a.background or pref(cfg, spec, "background")
    for k, v in (("quality", quality), ("background", background), ("output_format", fmt if official else None),
                 ("output_compression", a.compression), ("moderation", a.moderation)):
        if v is not None:
            params[k] = v
    params.update(parse_extra(a.extra))

    log(f"{spec['label']} {model} | {'edit with ' + str(len(refs)) + ' ref(s)' if refs else 'generate'} | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({"url": spec["base_url"], "model": model, "prompt": prompt, **params}, indent=2))
    t0 = time.time()
    base = spec["base_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    if refs:
        fields = [("model", model), ("prompt", prompt)] + [
            (k, json.dumps(v) if isinstance(v, (dict, list, bool)) else v) for k, v in params.items()]
        files = [("image[]", p) for p in refs] + ([("mask", mask)] if mask else [])
        body, ctype = multipart(fields, files)
        resp = http("POST", f"{base}/images/edits", {**headers, "Content-Type": ctype}, body)
    else:
        body = json.dumps({"model": model, "prompt": prompt, **params}).encode()
        resp = http("POST", f"{base}/images/generations", {**headers, "Content-Type": "application/json"}, body)
    finish(a, cfg, spec, task, model, prompt, params, refs, collect_images(resp), resp.get("usage"), t0)


def run_xai(a, cfg, spec, task):
    """xAI API (Grok Imagine models), billed per image to your xAI account."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["quality", "background", "size", "mask", "compression", "moderation"])
    model = a.model or pref(cfg, spec, "model", "xai_model")
    params = {}
    if a.n > 1:
        params["n"] = a.n
    if a.aspect:
        params["aspect_ratio"] = nearest_aspect(a.aspect, XAI_ASPECTS)
    resolution = a.resolution or pref(cfg, spec, "resolution")
    if resolution:  # API default is 1k, the cheapest
        params["resolution"] = "1k" if resolution.upper() == "1K" else "2k"
    params.update(parse_extra(a.extra))
    body = {"model": model, "prompt": prompt, "response_format": "b64_json", **params}
    if refs:
        imgs = [{"url": data_uri(p), "type": "image_url"} for p in refs]
        body["image" if len(imgs) == 1 else "images"] = imgs[0] if len(imgs) == 1 else imgs
    log(f"{spec['label']} {model} | {'edit with ' + str(len(refs)) + ' ref(s)' if refs else 'generate'} | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({k: (v if k not in ("image", "images") else "<data uri>") for k, v in body.items()}, indent=2))
    t0 = time.time()
    path = "/images/edits" if refs else "/images/generations"
    resp = http("POST", spec["base_url"].rstrip("/") + path,
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json.dumps(body).encode())
    finish(a, cfg, spec, task, model, prompt, params, refs, collect_images(resp), resp.get("usage"), t0)


def run_gemini(a, cfg, spec, task):
    """Google Gemini image models (Nano Banana) through the Interactions API."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["quality", "background", "size", "mask", "compression", "moderation"])
    model = a.model or pref(cfg, spec, "model", "gemini_model")
    fmt = {"type": "image"}
    if a.aspect:
        w, h = parse_aspect(a.aspect)
        fmt["aspect_ratio"] = f"{w:g}:{h:g}"
    resolution = a.resolution or pref(cfg, spec, "resolution")
    if resolution:
        fmt["image_size"] = resolution.upper()
    inputs = [{"type": "text", "text": prompt}] + [
        {"type": "image", "mime_type": mime_of(p), "data": base64.b64encode(p.read_bytes()).decode()} for p in refs]
    body = {"model": model, "input": inputs, "response_format": fmt}
    body.update(parse_extra(a.extra))
    params = {k: v for k, v in fmt.items() if k != "type"}
    if a.n > 1:
        params["n"] = a.n
    log(f"{spec['label']} {model} | {'edit with ' + str(len(refs)) + ' ref(s)' if refs else 'generate'} | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({**body, "input": [i if i["type"] == "text" else "<image>" for i in inputs]}, indent=2))
    t0 = time.time()
    headers = {"x-goog-api-key": key, "Content-Type": "application/json", "Api-Revision": GEMINI_API_REVISION}
    skip = {hashlib.sha1(p.read_bytes()).hexdigest() for p in refs}
    images, usage = [], None
    for _ in range(a.n):  # one image per request
        resp = http("POST", spec["base_url"].rstrip("/") + "/interactions", headers, json.dumps(body).encode())
        images += find_inline_images(resp, skip)
        usage = resp.get("usage") or resp.get("usageMetadata") or resp.get("usage_metadata") or usage
        if not images:
            die(f"no image in the answer: {brief_response(resp)}")
    finish(a, cfg, spec, task, model, prompt, params, refs, images, usage, t0)


def brief_response(resp):
    """The provider's answer without base64 blobs, short enough to read (e.g. a text reply or a block reason)."""
    def strip(o):
        if isinstance(o, dict):
            return {k: ("<data>" if k == "data" and isinstance(v, str) and len(v) > 200 else strip(v)) for k, v in o.items()}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return json.dumps(strip(resp), ensure_ascii=False)[:600]


def find_inline_images(obj, skip=()):
    """Every base64 image block in a response, whatever the nesting (deduplicated)."""
    found, seen = [], set(skip)

    def walk(o):
        if isinstance(o, dict):
            data = mime = None
            if o.get("type") == "image" and isinstance(o.get("data"), str):
                data, mime = o["data"], o.get("mime_type") or o.get("mimeType")
            for k in ("inlineData", "inline_data"):
                if isinstance(o.get(k), dict) and isinstance(o[k].get("data"), str):
                    data, mime = o[k]["data"], o[k].get("mimeType") or o[k].get("mime_type")
            if data:
                raw = b64_image(data)
                digest = hashlib.sha1(raw).hexdigest()
                if digest not in seen:
                    seen.add(digest)
                    found.append((raw, mime or sniff(raw)))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return found


def run_grok(a, cfg, spec, task):
    """Grok Imagine through the logged-in Grok Build CLI (grok.com subscription, no API key)."""
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["quality", "background", "size", "mask", "compression", "moderation",
                                        "resolution", "format", "extra"])
    g = grok_bin()
    if not g:
        raise Unavailable("Grok Build CLI not installed (curl -fsSL https://x.ai/cli/install.sh | bash)")
    aspect = None
    if a.aspect:
        aspect = nearest_aspect(a.aspect, GROK_ASPECTS)
        if aspect != a.aspect.strip():
            log(f"  note: Grok only supports {', '.join(GROK_ASPECTS)}; using {aspect}")
    tool = "image_edit" if refs else "image_gen"
    call = f"Call the {tool} tool exactly once" if a.n == 1 else f"Call the {tool} tool exactly {a.n} times (separate calls, same arguments)"
    if refs:
        call += f" with image {json.dumps([str(r.resolve()) for r in refs])}"
    if aspect:
        call += f' with aspect_ratio "{aspect}"'
    instruction = (f"{call}, using exactly the text between the <prompt> tags as the prompt.\n\n"
                   f"<prompt>\n{prompt}\n</prompt>\n\nThen reply with just: done")
    sid = str(uuid.uuid4())
    cmd = [g, "-p", instruction, "--tools", "image_gen,image_edit", "--system-prompt-override", GROK_SYSTEM,
           "--disable-web-search", "--no-subagents", "--no-plan", "--max-turns", str(2 + a.n),
           "--always-approve", "--session-id", sid, "--cwd", str(GROK_WORKDIR)]
    if a.model:
        cmd += ["-m", a.model]
    log(f"Grok {tool} | {len(refs)} ref(s) | aspect {aspect or 'auto'} | n={a.n}")
    if a.dry_run:
        return log(" ".join(shlex.quote(c) for c in cmd))
    GROK_WORKDIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=GROK_WORKDIR)
    except subprocess.TimeoutExpired:
        die(f"Grok timed out after {TIMEOUT}s")
    files = sorted(GROK_HOME.glob(f"sessions/*/{sid}/images/*"), key=lambda f: f.stat().st_mtime)
    if not files:
        said = " ".join((r.stdout + " " + r.stderr).split())[-400:]
        if r.returncode and re.search(r"log ?in|auth|unauthori", said, re.I):
            raise Unavailable(f"not logged in (run: grok login). Grok said: {said}")
        die(f"Grok returned no image (exit {r.returncode}). Grok said: {said or '(nothing)'}")
    images = [(f.read_bytes(), MIME_BY_EXT.get(f.suffix.lower(), "image/jpeg")) for f in files]
    log(f"  grok session {sid}")
    usage = None
    try:
        u = subprocess.run([g, "usage", sid], capture_output=True, text=True, timeout=30)
        sess = json.loads(u.stdout).get("session", {})
        usage = {k: sess.get(k) for k in ("inputTokens", "outputTokens", "cachedReadTokens", "costUsdTicks",
                                         "primaryModelId")}
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    finish(a, cfg, spec, task, a.model or "default", prompt, {"tool": tool, "aspect": aspect, "n": a.n}, refs,
           images, usage, t0, {"grok_session": sid})


def run_openrouter(a, cfg, spec, task):
    """OpenRouter Image API: one key for many image models (POST /images, base64 out)."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["background", "size", "mask", "compression", "moderation"])
    model = a.model or pref(cfg, spec, "model") or spec.get("model")
    if not model:
        die(f"no model set for {spec['name']}")
    params = {}
    if a.n > 1:
        params["n"] = a.n
    if a.aspect:
        w, h = parse_aspect(a.aspect)
        params["aspect_ratio"] = f"{w:g}:{h:g}"
    for name, val in (("resolution", (a.resolution or pref(cfg, spec, "resolution") or "").upper() or None),
                      ("quality", a.quality or pref(cfg, spec, "quality")),
                      ("output_format", a.format or pref(cfg, spec, "format"))):
        if val:
            params[name] = val
    params.update(parse_extra(a.extra))
    body = {"model": model, "prompt": prompt, **params}
    if refs:
        body["input_references"] = [{"type": "image_url", "image_url": {"url": data_uri(p)}} for p in refs]
    log(f"{spec['label']} {model} | {'edit with ' + str(len(refs)) + ' ref(s)' if refs else 'generate'} | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({**body, **({"input_references": "<data uris>"} if refs else {})}, indent=2))
    t0 = time.time()
    resp = http("POST", spec["base_url"].rstrip("/") + "/images",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Title": "ImageGenKit", "HTTP-Referer": HOMEPAGE}, json.dumps(body).encode())
    usage = resp.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("cost"), (int, float)):
        usage = dict(usage, cost_usd=usage["cost"])
    finish(a, cfg, spec, task, model, prompt, params, refs, collect_images(resp), usage, t0)


def run_together(a, cfg, spec, task):
    """Together AI images API (FLUX models): width/height instead of size, base64 out."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["quality", "background", "mask", "compression", "moderation"])
    model = a.model or pref(cfg, spec, "model") or spec.get("model")
    resolution = a.resolution or pref(cfg, spec, "resolution")
    params = {}
    if a.n > 1:
        params["n"] = a.n
    size = a.size if a.size and "x" in a.size else (openai_size(a.aspect, resolution or "1K") if a.aspect or resolution else None)
    if size:
        w, h = size.split("x")
        params.update(width=int(w), height=int(h))
    if a.format in ("png", "jpeg"):
        params["output_format"] = a.format
    params.update(parse_extra(a.extra))
    body = {"model": model, "prompt": prompt, "response_format": "base64", **params}
    if refs:
        body["image_url"] = data_uri(refs[0])
    log(f"{spec['label']} {model} | {'edit' if refs else 'generate'} | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({k: (v if k != "image_url" else "<data uri>") for k, v in body.items()}, indent=2))
    t0 = time.time()
    resp = http("POST", spec["base_url"].rstrip("/") + "/images/generations",
                {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json.dumps(body).encode())
    finish(a, cfg, spec, task, model, prompt, params, refs, collect_images(resp), resp.get("usage"), t0)


def run_fal(a, cfg, spec, task):
    """fal.ai synchronous endpoint (fal.run/<model>). Generation only."""
    key = "<dry-run>" if a.dry_run else require_key(spec)
    prompt = read_prompt(a)
    if a.image:
        die(f"{spec['label']}: reference images aren't supported yet; use another provider for edits")
    note_unsupported(a, spec["label"], ["quality", "background", "mask", "compression", "moderation"])
    model = a.model or pref(cfg, spec, "model") or spec.get("model")
    resolution = a.resolution or pref(cfg, spec, "resolution")
    params = {"num_images": a.n} if a.n > 1 else {}
    size = a.size if a.size and "x" in a.size else (openai_size(a.aspect, resolution or "1K") if a.aspect or resolution else None)
    if size:
        w, h = size.split("x")
        params["image_size"] = {"width": int(w), "height": int(h)}
    if a.format in ("png", "jpeg"):
        params["output_format"] = a.format
    params.update(parse_extra(a.extra))
    body = {"prompt": prompt, "sync_mode": True, **params}
    base = (spec.get("base_url") or "https://fal.run").rstrip("/")
    log(f"{spec['label']} {model} | generate | {params or 'defaults'}")
    if a.dry_run:
        return log(json.dumps({"url": f"{base}/{model}", **body}, indent=2))
    t0 = time.time()
    resp = http("POST", f"{base}/{model}", {"Authorization": f"Key {key}", "Content-Type": "application/json"},
                json.dumps(body).encode())
    images = []
    for item in resp.get("images", []) or []:
        url = item.get("url", "") if isinstance(item, dict) else ""
        if url.startswith("data:"):
            data = b64_image(url.split(",", 1)[-1])
        elif url:
            data = download(url)
        else:
            continue
        images.append((data, item.get("content_type") or sniff(data)))
    finish(a, cfg, spec, task, model, prompt, params, [], images, None, t0)


def run_codex(a, cfg, spec, task):
    """Images through the logged-in Codex CLI (ChatGPT plan, no API key): its built-in image tool."""
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["size", "mask", "compression", "moderation", "format", "extra", "model"])
    c = codex_bin()
    if not c:
        raise Unavailable("Codex CLI not installed (npm install -g @openai/codex)")
    call = "Use your image generation tool exactly once" if a.n == 1 else \
        f"Use your image generation tool exactly {a.n} times (separate calls, same prompt)"
    if refs:
        call += ", editing or combining the attached image(s)"
    hints = []
    if a.aspect:
        w, h = parse_aspect(a.aspect)
        hints.append(f"aspect ratio {w:g}:{h:g}")
    quality = a.quality or pref(cfg, spec, "quality")
    if quality and quality != "auto":
        hints.append(f"{quality} quality")
    if (a.background or pref(cfg, spec, "background")) == "transparent":
        hints.append("transparent background")
    if hints:
        call += " with " + ", ".join(hints)
    instruction = (f"{call}, passing exactly the text between the <prompt> tags as the prompt. Do not run shell "
                   f"commands or edit files.\n\n<prompt>\n{prompt}\n</prompt>\n\nThen reply with just: done")
    cmd = [c, "exec", "--json", "--skip-git-repo-check", "-C", str(CODEX_WORKDIR), "-s", "read-only",
           "-c", "model_reasoning_effort=low"]
    for r in refs:
        cmd += ["-i", str(r.resolve())]
    cmd.append(instruction)
    log(f"Codex image_gen | {len(refs)} ref(s) | {', '.join(hints) or 'auto'} | n={a.n}")
    if a.dry_run:
        return log(" ".join(shlex.quote(x) for x in cmd))
    CODEX_WORKDIR.mkdir(parents=True, exist_ok=True)
    gen_dir = CODEX_HOME / "generated_images"
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=CODEX_WORKDIR,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        die(f"Codex timed out after {TIMEOUT}s")
    thread, failure, paths = None, None, []
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "thread.started":
            thread = ev.get("thread_id")
        elif ev.get("type") == "turn.failed":
            failure = (ev.get("error") or {}).get("message", "turn failed")
        paths += re.findall(r'[^"\s]*generated_images/[^"\s]+\.(?:png|jpe?g|webp)', line)
    files = [Path(p) for p in dict.fromkeys(paths) if Path(p).exists()]
    if not files and thread and not failure:
        files = sorted((f for f in (gen_dir / thread).glob("*") if f.stat().st_mtime >= t0 - 1),
                       key=lambda f: f.stat().st_mtime) if (gen_dir / thread).is_dir() else []
    if not files and not thread and not failure:  # last resort: anything Codex saved during this run
        files = sorted((f for f in gen_dir.glob("*/*") if f.stat().st_mtime >= t0 - 1),
                       key=lambda f: f.stat().st_mtime)
    files = [f for f in files if f.suffix.lower() in MIME_BY_EXT][: max(a.n, 1) * 2]
    if not files:
        said = failure or (r.stderr.strip()[-600:] or "(nothing)")
        if re.search(r"401|unauthori|log ?in|refresh token|sign in", said, re.I):
            raise Unavailable(f"not logged in (run: codex login). Codex said: {said}")
        die(f"Codex returned no image (exit {r.returncode}). Codex said: {said}")
    images = [(f.read_bytes(), MIME_BY_EXT.get(f.suffix.lower(), "image/png")) for f in files]
    finish(a, cfg, spec, task, "gpt-image (Codex)", prompt, {"tool": "image_gen", "aspect": a.aspect, "n": a.n},
           refs, images, None, t0, {"codex_thread": thread})


# ---------- local models (On this Mac) ----------
# Open image models run on the Mac by stable-diffusion.cpp (sd-cli). The engine and the model files are downloaded
# once (sha256-checked); catalog/local.json lists the engine build, curated models, the shared text encoders and
# VAEs, and the model families ("recipes") with their engine settings.

LOCAL_HOME = Path(os.environ.get("IMAGEGENKIT_LOCAL_HOME", Path.home() / ".local" / "share" / "imagegenkit"))
LOCAL_MODELS = LOCAL_HOME / "models"
LOCAL_ENGINES = LOCAL_HOME / "engine"
LOCAL_REGISTRY = LOCAL_HOME / "local-models.json"
LOCAL_CATALOG_URL = f"{RAW_BASE}/main/catalog/local.json"
LOCAL_CATALOG_CACHE = CONFIG_DIR / "local-catalog.json"
LOCAL_WORKDIR = CONFIG_DIR / "local-work"
HF_BASE = os.environ.get("IMAGEGENKIT_HF_BASE", "https://huggingface.co").rstrip("/")
LOCAL_TIMEOUT = 3600
# Preferred quantizations when a repo has several: small but good first, 8-bit last (twice the download).
QUANT_ORDER = ["q4_k_m", "q4_k", "q4_k_s", "q4_0", "q5_k_m", "q5_k", "q5_0", "q6_k", "q3_k_m", "q3_k", "q8_0"]


def gb(n):
    n = n or 0
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def apple_silicon():
    try:
        return subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True).stdout.strip() == "1"
    except OSError:
        return False


def mac_ram_gb():
    try:
        return round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2 ** 30)
    except (OSError, ValueError):
        return 0


def local_catalog(refresh=False):
    """The newest of the cached catalog, the copy next to this script (inside the app) and GitHub (once a day)."""
    found = []
    here = Path(__file__).resolve().parent
    for path in (LOCAL_CATALOG_CACHE, here / "local.json", here.parent / "catalog" / "local.json"):  # app bundle, checkout
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data.get("engine"):
                found.append(data)
        except (OSError, ValueError):
            pass
    best = max(found, key=lambda c: c.get("updated", ""), default=None)
    try:
        stale = time.time() - LOCAL_CATALOG_CACHE.stat().st_mtime > 86400
    except OSError:
        stale = True
    if refresh or stale or best is None:
        try:
            req = urllib.request.Request(LOCAL_CATALOG_URL, headers={"User-Agent": "imagegenkit"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            if isinstance(data, dict) and data.get("engine") and (not best or data.get("updated", "") >= best.get("updated", "")):
                best = data
        except (OSError, ValueError, http_client.HTTPException):
            pass
        if best:
            try:
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                LOCAL_CATALOG_CACHE.write_text(json.dumps(best))
            except OSError:
                pass
    if not best:
        raise Unavailable("can't load the local model list. Check your internet connection")
    return best


def local_registry():
    try:
        data = json.loads(LOCAL_REGISTRY.read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("models", [])
    return data


def save_local_registry(data):
    LOCAL_HOME.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(LOCAL_REGISTRY)


def local_path(rel):
    p = Path(rel).expanduser()
    return p if p.is_absolute() else LOCAL_MODELS / p


def local_model_ready(m):
    return all(local_path(f).is_file() for f in (m.get("files") or {}).values())


def local_engine():
    """The installed engine binary, or None. IMAGEGENKIT_SD_CLI points at another build (testing)."""
    if os.environ.get("IMAGEGENKIT_SD_CLI"):
        return os.environ["IMAGEGENKIT_SD_CLI"]
    e = local_registry().get("engine") or {}
    if not e.get("version"):
        return None
    exe = LOCAL_ENGINES / e["version"] / e.get("binary", "sd-cli")
    return str(exe) if os.access(exe, os.X_OK) else None


def local_models():
    return [m for m in local_registry()["models"] if local_model_ready(m)]


def local_readiness():
    if not apple_silicon():
        return False, "needs a Mac with Apple silicon"
    if not local_engine():
        return False, "no local model downloaded"
    n = len(local_models())
    return (True, f"{n} model{'s' if n != 1 else ''}") if n else (False, "no local model downloaded")


def local_check():
    exe = local_engine()
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return "error", f"engine doesn't start: {e}"
    if r.returncode != 0:
        return "error", f"engine doesn't start: {(r.stderr or r.stdout).strip()[-200:]}"
    names = ", ".join(m["name"] for m in local_models())
    return "ok", f"{names} ({local_registry().get('engine', {}).get('version', '?')})"


def hf_token():
    return os.environ.get("HF_TOKEN") or keychain_read(keychain_service("huggingface"))


def hf_get(path):
    headers = {"User-Agent": "imagegenkit", "Accept": "application/json"}
    token = hf_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(HF_BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ApiError(404, "not found on Hugging Face")
        if e.code in (401, 403):
            raise ApiError(e.code, "this Hugging Face model needs a login (gated or private)")
        raise ApiError(e.code, f"Hugging Face: {short_text(e.read().decode('utf-8', 'replace'))}")
    except (OSError, ValueError, http_client.HTTPException) as e:
        raise ApiError(0, f"can't reach Hugging Face: {getattr(e, 'reason', e)}")


def hf_file_url(repo, path):
    return f"{HF_BASE}/{repo}/resolve/main/{urllib.parse.quote(path)}"


class Progress:
    """Download progress: a line on stderr, or JSON lines on stdout for the app (--json)."""

    def __init__(self, total, as_json):
        self.total, self.done, self.json, self.last, self.started = total, 0, as_json, 0.0, time.time()

    def event(self, **kw):
        if self.json:
            print(json.dumps(kw), flush=True)

    def add(self, n, label, force=False):
        self.done += n
        now = time.time()
        if not force and now - self.last < 0.25:
            return
        self.last = now
        if self.json:
            self.event(event="progress", done=self.done, total=self.total, file=label)
        else:
            pct = int(self.done * 100 / self.total) if self.total else 0
            sys.stderr.write(f"\r  downloading {label}: {gb(self.done)} of {gb(self.total)} ({pct}%)   ")
            sys.stderr.flush()

    def end(self):
        if not self.json and self.total:
            sys.stderr.write("\n")


def fetch_file(url, dest, size, sha256, progress, label, auth=False):
    """Download to dest (resumes a .part file), verifying size and sha256."""
    dest = Path(dest)
    if dest.is_file() and (not size or dest.stat().st_size == size):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink()
        have = 0
    digest = hashlib.sha256()
    if have and sha256:
        with part.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                digest.update(chunk)
    req = urllib.request.Request(url, headers={"User-Agent": "imagegenkit"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    token = hf_token() if auth else None
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")  # never sent on to the CDN
    progress.add(have, label, force=True)
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and size and have == size:
            r = None
        elif e.code in (401, 403):
            raise ApiError(e.code, f"{label} needs a Hugging Face login (accept the model's terms on huggingface.co "
                                   "and add a token: imagegenkit set-key huggingface)")
        else:
            raise ApiError(e.code, f"couldn't download {label} (HTTP {e.code})")
    except (OSError, http_client.HTTPException) as e:
        raise ApiError(0, f"couldn't download {label}: {getattr(e, 'reason', e)}")
    if r is not None:
        with r:
            mode = "ab"
            if have and r.status != 206:  # the server ignored the range: start over
                progress.add(-have, label)
                have, digest, mode = 0, hashlib.sha256(), "wb"
            try:
                with part.open(mode) as out:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        digest.update(chunk)
                        have += len(chunk)
                        progress.add(len(chunk), label)
            except (OSError, http_client.HTTPException) as e:
                raise ApiError(0, f"download of {label} interrupted ({e}). Run it again to continue")
    if size and have != size:
        raise ApiError(0, f"download of {label} incomplete ({gb(have)} of {gb(size)}). Run it again to continue")
    if sha256 and digest.hexdigest() != sha256:
        part.unlink()
        raise ApiError(0, f"{label} is damaged (checksum mismatch). Run it again to download it fresh")
    part.replace(dest)
    progress.add(0, label, force=True)  # the last chunk's update may have been skipped


def install_engine(cat, progress):
    eng = cat["engine"]
    dest = LOCAL_ENGINES / eng["version"]
    exe = dest / eng.get("binary", "sd-cli")
    if not os.access(exe, os.X_OK):
        archive = LOCAL_ENGINES / f"{eng['version']}.zip"
        fetch_file(eng["url"], archive, eng.get("size"), eng.get("sha256"), progress, "engine")
        tmp = LOCAL_ENGINES / f".{eng['version']}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        if subprocess.run(["ditto", "-x", "-k", str(archive), str(tmp)], capture_output=True).returncode != 0:
            raise ApiError(0, "couldn't unpack the engine")
        found = next((p for p in tmp.rglob(eng.get("binary", "sd-cli")) if p.is_file()), None)
        if not found:
            raise ApiError(0, "the engine download has no sd-cli")
        shutil.rmtree(dest, ignore_errors=True)
        found.parent.rename(dest)
        shutil.rmtree(tmp, ignore_errors=True)
        archive.unlink()
    try:
        ok = subprocess.run([str(exe), "--help"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        raise ApiError(0, "the engine doesn't start on this Mac")
    for old in LOCAL_ENGINES.iterdir():
        if old.is_dir() and old != dest:
            shutil.rmtree(old, ignore_errors=True)
    reg = local_registry()
    reg["engine"] = {"version": eng["version"], "binary": eng.get("binary", "sd-cli")}
    save_local_registry(reg)
    return str(exe)


def base_models(info):
    """The base_model repos a Hugging Face model names (card data and tags), and whether it is a LoRA adapter."""
    card = info.get("cardData") or {}
    bases = card.get("base_model") or []
    bases = [bases] if isinstance(bases, str) else list(bases)
    adapter = False
    for t in info.get("tags") or []:
        if t.startswith("base_model:"):
            parts = t.split(":")
            if len(parts) == 3 and parts[1] == "adapter":
                adapter = True
            bases.append(parts[-1])
    return list(dict.fromkeys(b for b in bases if isinstance(b, str))), adapter


def match_recipe(info, cat, lookups=None):
    """(recipe id, reason): which model family this repo belongs to, following base_model up to two levels."""
    recipes = cat["recipes"]
    by_base = {b.lower(): rid for rid, r in recipes.items() for b in r.get("base", [])}
    lookups = {} if lookups is None else lookups
    repo = info.get("id") or info.get("modelId") or ""
    if repo.lower() in by_base:
        return by_base[repo.lower()], None
    bases, adapter = base_models(info)
    if adapter:
        return None, "a LoRA add-on, not a full model"
    for depth in range(2):
        for b in bases:
            if b.lower() in by_base:
                return by_base[b.lower()], None
        if depth == 1:
            break
        nxt = []
        for b in bases[:3]:
            if b not in lookups:
                try:
                    lookups[b] = base_models(hf_get(f"/api/models/{urllib.parse.quote(b, safe='/')}?expand[]=cardData&expand[]=tags"))[0]
                except ApiError:
                    lookups[b] = []
            nxt += lookups[b]
        bases = nxt
    return None, "a model family ImageGenKit can't run yet"


# Adult and "uncensored" models are never downloaded: the EU AI Act bans AI systems that can foreseeably make intimate
# images of real people without consent (Article 5, from 2 December 2026). catalog/local.json can extend the list.
BLOCK_TAGS = {"not-for-all-audiences", "nsfw"}
BLOCK_WORDS = ["uncensor", "nsfw", "nude", "nudity", "naked", "undress", "porn", "hentai", "lewd", "erotic", "explicit", "xxx",
               "abliterat", "18+", "biglove", "lustify"]
BLOCKED_MESSAGE = "ImageGenKit doesn't download uncensored or adult models"


def local_blocked(cat, names, tags=()):
    """Whether a model is adult or uncensored, by its repo or file name, its base models or its Hugging Face tags."""
    rules = cat.get("block") or {}
    words = set(BLOCK_WORDS) | {w.lower() for w in rules.get("words", [])}
    block_tags = BLOCK_TAGS | {t.lower() for t in rules.get("tags", [])}
    repos = {r.lower() for r in rules.get("repos", [])}
    text = " ".join(n for n in names if n).lower()
    return (any(w in text for w in words) or any(t.lower() in block_tags for t in tags or [])
            or any((n or "").lower() in repos for n in names))


def license_of(card):
    """A model's license, with the name of a custom one ("other"), and whether it forbids commercial use."""
    lic = card.get("license")
    lic = lic if isinstance(lic, str) else None
    if lic == "other" and isinstance(card.get("license_name"), str):
        lic = card["license_name"]
    nc = bool(lic and re.search(r"non-?commercial|research|-nc(-|$)|noncommercial", lic, re.I))
    return (None if lic == "other" else lic), nc


def quant_rank(name):
    n = name.lower()
    for i, q in enumerate(QUANT_ORDER):
        if re.search(rf"(?<![a-z0-9]){q}(?![a-z0-9])", n.replace("-", "_").replace(".", "_")):
            return i
    return len(QUANT_ORDER)


def weights_candidates(files, recipe):
    skip = re.compile(r"mmproj|vae|text_encoder|clip|t5|lora|controlnet|upscal|tae", re.I)
    cands = [x for x in files if x["path"].endswith(".gguf") and not skip.search(Path(x["path"]).name)]
    if recipe.get("main") == "model":
        cands += [x for x in files if "/" not in x["path"] and x["path"].endswith(".safetensors")
                  and (x.get("size") or 0) > 1e9 and not skip.search(x["path"])]
    return sorted(cands, key=lambda x: (quant_rank(x["path"]), x.get("size") or 0))


def pick_weights(files, recipe, wanted=None):
    """The weights file to download from a repo's file list."""
    if wanted:
        f = next((x for x in files if x["path"] == wanted), None)
        if not f:
            die(f"no file {wanted} in that repo")
        return f
    cands = weights_candidates(files, recipe)
    if not cands:
        die("that repo has no single weights file ImageGenKit can use (it needs a .gguf, or a single .safetensors "
            "for Stable Diffusion models)")
    return cands[0]


def quant_label(name):
    m = re.search(r"(?i)(?<![a-z0-9])(i?q\d_k_[msl]|i?q\d_k|q\d_\d|bf16|f16|fp16|fp8)(?![a-z0-9])", name.replace("-", "_").replace(".", "_"))
    return m.group(1).upper() if m else None


def local_id(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:64]


def resolve_local(spec, cat, recipe=None, wanted=None):
    """What `local-models add` installs: a curated id, a Hugging Face repo or link, a download link or a local file."""
    for m in cat["models"]:
        if spec.lower() in (m["id"].lower(), m["name"].lower()):
            return dict(m, source=f"{HF_BASE}/{m['file']['repo']}")
    if recipe and recipe not in cat["recipes"]:
        die(f"unknown model family {recipe} (one of: {', '.join(cat['recipes'])})")
    hf = re.match(r"^(?:https?://(?:www\.)?huggingface\.co/)?([\w.-]+/[\w.-]+)(?:/(?:blob|resolve|tree)/[^/]+/(.+?))?/?(?:[?#].*)?$", spec)
    if hf and not Path(spec).expanduser().exists() and (spec.startswith("http") or spec.count("/") == 1):
        repo, path = hf.group(1), urllib.parse.unquote(hf.group(2)) if hf.group(2) else wanted
        info = hf_get(f"/api/models/{repo}?expand[]=cardData&expand[]=tags&expand[]=gated")
        if local_blocked(cat, [repo, path] + base_models(info)[0], info.get("tags")):
            die(BLOCKED_MESSAGE)
        rid, why = (recipe, None) if recipe else match_recipe(info, cat)
        if not rid:
            die(f"{repo} is {why}")
        files = [x for x in hf_get(f"/api/models/{repo}/tree/main?recursive=true") if x.get("type") == "file"]
        f = pick_weights(files, cat["recipes"][rid], path)
        q = quant_label(Path(f["path"]).name)
        author, base = repo.split("/")[0], re.sub(r"(?i)[-_ ]?gguf$", "", repo.split("/")[-1])
        name = f"{base} ({author}{' ' + q if q else ''})"
        license_, _ = license_of(info.get("cardData") or {})
        return {"id": local_id(f"{author}-{base}-{q or Path(f['path']).stem}"), "name": name, "recipe": rid,
                "file": {"repo": repo, "path": f["path"], "size": f.get("size"), "sha256": (f.get("lfs") or {}).get("oid")},
                "license": license_, "source": f"{HF_BASE}/{repo}",
                "gated": bool(info.get("gated"))}
    if not recipe:
        die("for a download link or a file, say which model family it is: --recipe "
            + " | ".join(r for r in cat["recipes"]))
    if local_blocked(cat, [spec]):
        die(BLOCKED_MESSAGE)
    p = Path(spec).expanduser()
    if p.is_file():
        return {"id": local_id(p.stem), "name": p.stem, "recipe": recipe, "file": {"local": str(p.resolve()), "size": p.stat().st_size},
                "source": str(p.resolve())}
    if not re.match(r"^https?://", spec):
        die(f"{spec!r} isn't a model in the list, a Hugging Face repo, a link or a file (see: imagegenkit local-models list)")
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(spec).path).name) or "model"
    size = None
    try:
        with urllib.request.urlopen(urllib.request.Request(spec, method="HEAD", headers={"User-Agent": "imagegenkit"}), timeout=30) as r:
            size = int(r.headers.get("Content-Length") or 0) or None
    except (OSError, ValueError, http_client.HTTPException):
        pass
    return {"id": local_id(Path(name).stem), "name": Path(name).stem, "recipe": recipe,
            "file": {"url": spec, "path": name, "size": size}, "source": spec}


def cmd_local_list(a, cfg):
    cat = local_catalog(refresh=getattr(a, "refresh", False))
    reg = local_registry()
    installed = [dict(m, ready=local_model_ready(m), family=cat["recipes"].get(m.get("recipe"), {}).get("name"))
                 for m in reg["models"]]
    ids = {m["id"] for m in installed if m["ready"]}
    used = sum(f.stat().st_size for f in LOCAL_MODELS.rglob("*") if f.is_file()) if LOCAL_MODELS.exists() else 0
    free = shutil.disk_usage(LOCAL_HOME if LOCAL_HOME.exists() else Path.home()).free
    out = {"supported": apple_silicon(), "ram_gb": mac_ram_gb(),
           "engine": {"installed": bool(local_engine()), "version": (reg.get("engine") or {}).get("version"),
                      "latest": cat["engine"]["version"], "size": cat["engine"].get("size")},
           "models": installed, "catalog": [dict(m, installed=m["id"] in ids) for m in cat["models"]],
           "recipes": {k: {"name": r["name"], "edit": r.get("edit", False)} for k, r in cat["recipes"].items()},
           "dir": str(LOCAL_MODELS), "bytes": used, "free": free}
    if a.json:
        print(json.dumps(out, indent=1))
        return
    print(f"Installed ({gb(used)} in {LOCAL_MODELS}):")
    for m in installed:
        print(f"  {m['id']:<28} {m['name']}" + ("" if m["ready"] else "  (files missing)"))
    if not installed:
        print("  none")
    print("\nAvailable (imagegenkit local-models add <id>):")
    for m in cat["models"]:
        mark = "installed" if m["id"] in ids else gb(m.get("download"))
        print(f"  {m['id']:<28} {m['name']:<18} {mark:<10} {m.get('note', '')}")
    print("\nMore: imagegenkit local-models search <words>, or add any Hugging Face GGUF repo or link.")


def cmd_local_search(a, cfg):
    cat = local_catalog()
    expand = ["downloads", "likes", "tags", "gated", "cardData", "gguf"]
    found = {}
    for tag in ("text-to-image", "image-to-image"):
        q = [("search", a.query or ""), ("filter", "gguf"), ("pipeline_tag", tag), ("sort", "downloads"),
             ("direction", "-1"), ("limit", str(a.limit))] + [("expand[]", e) for e in expand]
        for m in hf_get("/api/models?" + urllib.parse.urlencode(q)):
            found.setdefault(m["id"], m)
    installed = {(m.get("file") or {}).get("repo") for m in local_registry()["models"]}
    have = {Path(f).as_posix() for m in local_registry()["models"] for f in (m.get("files") or {}).values()}
    lookups, rows = {}, []
    allowed = [m for m in found.values() if not local_blocked(cat, [m["id"]] + base_models(m)[0], m.get("tags"))]
    for m in sorted(allowed, key=lambda m: -(m.get("downloads") or 0))[: a.limit]:
        rid, why = match_recipe(m, cat, lookups)
        rec = cat["recipes"].get(rid) or {}
        extra = sum(c["size"] for c in (cat["components"][cid] for cid in rec.get("slots", {}).values())
                    if f"{c['repo']}/{c['path']}" not in have)
        license_, noncommercial = license_of(m.get("cardData") or {})
        base_license, base_nc = license_of({"license": rec.get("license")})
        if base_nc or not license_:  # a copy or fine-tune can't loosen the original model's license
            license_, noncommercial = base_license or license_, base_nc or noncommercial
        rows.append({"repo": m["id"], "author": m["id"].split("/")[0], "name": m["id"].split("/")[-1],
                     "downloads": m.get("downloads") or 0, "likes": m.get("likes") or 0,
                     "license": license_, "noncommercial": noncommercial,
                     "gated": bool(m.get("gated")), "recipe": rid, "family": rec.get("name"), "edit": bool(rec.get("edit")),
                     "file": None, "quant": None, "size": None, "extra": extra if rid else None,
                     "supported": bool(rid), "reason": why, "installed": m["id"] in installed})

    def weights(row):  # the file `add` would download, so the size shown is the real one
        try:
            files = [x for x in hf_get(f"/api/models/{row['repo']}/tree/main?recursive=true") if x.get("type") == "file"]
        except ApiError:
            return
        cands = weights_candidates(files, cat["recipes"][row["recipe"]])
        if not cands:
            row.update(supported=False, reason="no single weights file (it needs a .gguf)")
            return
        row.update(file=cands[0]["path"], quant=quant_label(Path(cands[0]["path"]).name), size=cands[0].get("size"))

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(weights, [r for r in rows if r["supported"]]))
    if a.json:
        print(json.dumps(rows, indent=1))
        return
    for r in rows:
        status = f"{r['family']}, {gb((r['size'] or 0) + (r['extra'] or 0))} ({r['quant'] or 'file'})" if r["supported"] \
            else f"not supported ({r['reason']})"
        print(f"{r['repo']:<55} {r['downloads']:>9} downloads  {status}{'  [gated]' if r['gated'] else ''}")
    if not rows:
        print("nothing found")


def cmd_local_add(a, cfg):
    if not apple_silicon():
        die("local models need a Mac with Apple silicon (M1 or newer)")
    cat = local_catalog()
    m = resolve_local(a.model, cat, a.recipe, a.file)
    rec = cat["recipes"][m["recipe"]]
    main_slot = rec.get("main", "diffusion_model")
    f = m["file"]
    if f.get("local"):
        main_dest = Path(f["local"])
        jobs = []
    else:
        main_dest = LOCAL_MODELS / (f["repo"] if f.get("repo") else "links/" + urllib.parse.urlparse(f["url"]).netloc) / f["path"]
        url = hf_file_url(f["repo"], f["path"]) if f.get("repo") else f["url"]
        jobs = [(m["name"], url, main_dest, f.get("size"), f.get("sha256"), bool(f.get("repo")))]
    files = {main_slot: str(main_dest.relative_to(LOCAL_MODELS)) if not f.get("local") else str(main_dest)}
    for slot, cid in rec.get("slots", {}).items():
        c = cat["components"][cid]
        dest = LOCAL_MODELS / c["repo"] / c["path"]
        files[slot] = str(dest.relative_to(LOCAL_MODELS))
        if not any(j[2] == dest for j in jobs):
            jobs.append((Path(c["path"]).name, hf_file_url(c["repo"], c["path"]), dest, c["size"], c.get("sha256"), True))
    need_engine = not local_engine()

    def remaining(size, dest):
        if dest.is_file():
            return 0
        part = dest.with_name(dest.name + ".part")
        return max(0, (size or 0) - (part.stat().st_size if part.exists() else 0))

    todo = [j for j in jobs if not (j[2].is_file() and (not j[3] or j[2].stat().st_size == j[3]))]
    need = sum(remaining(j[3], j[2]) for j in todo) + (cat["engine"].get("size", 0) * 3 if need_engine else 0)
    LOCAL_MODELS.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(LOCAL_MODELS).free
    if need and free - need < 5e9:  # keep 5 GB free: macOS starts clearing caches when the disk is nearly full
        die(f"not enough disk space: {m['name']} needs {gb(need)}, {gb(free)} free (keep at least 5 GB free)")
    if not (LOCAL_HOME / ".tm-excluded").exists():  # re-downloadable, keep them out of Time Machine
        if subprocess.run(["tmutil", "addexclusion", str(LOCAL_MODELS)], capture_output=True).returncode == 0:
            (LOCAL_HOME / ".tm-excluded").touch()
    total = sum(j[3] or 0 for j in todo) + (cat["engine"].get("size", 0) if need_engine else 0)
    progress = Progress(total, a.json)
    progress.event(event="start", id=m["id"], name=m["name"], total=total)
    if not a.json:
        log(f"{m['name']}: {gb(total)} to download" if total else f"{m['name']}: files already here")
    if need_engine:
        install_engine(cat, progress)
    for label, url, dest, size, sha, auth in todo:
        fetch_file(url, dest, size, sha, progress, label, auth=auth)
    progress.end()
    entry = {"id": m["id"], "name": m["name"], "recipe": m["recipe"], "files": files, "source": m.get("source"),
             "license": m.get("license"), "edit": bool(rec.get("edit")), "file": {k: v for k, v in f.items() if k != "local"},
             "bytes": sum((local_path(p).stat().st_size if local_path(p).is_file() else 0) for p in files.values()),
             "added": dt.datetime.now().isoformat(timespec="seconds")}
    if f.get("local"):
        entry["external"] = True
    reg = local_registry()
    reg["models"] = [x for x in reg["models"] if x["id"] != m["id"]] + [entry]
    save_local_registry(reg)
    enable_provider("local")
    progress.event(event="done", model=entry)
    if not a.json:
        log(f"Ready: {m['name']}. Try: imagegenkit local \"a red mug on a wooden table\" -m {m['id']}")


def cmd_local_remove(a, cfg):
    reg = local_registry()
    m = next((x for x in reg["models"] if a.model.lower() in (x["id"].lower(), x["name"].lower())), None)
    if not m:
        die(f"no local model {a.model} (see: imagegenkit local-models list)")
    others = [x for x in reg["models"] if x is not m]
    in_use = {str(local_path(p)) for x in others for p in (x.get("files") or {}).values()}
    freed = 0
    for rel in (m.get("files") or {}).values():
        p = local_path(rel)
        if str(p) in in_use or (m.get("external") and not str(p).startswith(str(LOCAL_MODELS))):
            continue
        for q in (p, p.with_name(p.name + ".part")):
            if q.is_file():
                freed += q.stat().st_size
                q.unlink()
        d = p.parent
        while d != LOCAL_MODELS and str(d).startswith(str(LOCAL_MODELS)) and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
    reg["models"] = others
    save_local_registry(reg)
    data = read_user_config()
    if ((data.get("provider_defaults") or {}).get("local") or {}).get("model") == m["id"]:
        data["provider_defaults"]["local"].pop("model", None)
        write_user_config(data)
    if a.json:
        print(json.dumps({"removed": m["id"], "freed": freed}))
    else:
        log(f"Removed {m['name']} ({gb(freed)} freed)")


def local_size(a, recipe, refs):
    """Width and height: --size, else --aspect (or the first reference's shape) at --resolution, in the family's steps."""
    d = recipe.get("defaults") or {}
    mult = recipe.get("multiple", 16)
    if a.size and a.size != "auto":
        m = re.fullmatch(r"(\d+)\s*x\s*(\d+)", a.size)
        if not m:
            die(f"bad --size {a.size!r}, expected WIDTHxHEIGHT")
        w, h = int(m.group(1)), int(m.group(2))
    else:
        area = {"0.5K": 512 * 512, "1K": 1024 * 1024, "2K": 1536 * 1536}.get((a.resolution or "").upper(),
                                                                          d.get("width", 1024) * d.get("height", 1024))
        aspect = a.aspect or (image_aspect(refs[0]) if refs else None)
        rw, rh = parse_aspect(aspect) if aspect else (d.get("width", 1024), d.get("height", 1024))
        w, h = math.sqrt(area * rw / rh), math.sqrt(area * rh / rw)
    w, h = max(mult, round(w / mult) * mult), max(mult, round(h / mult) * mult)
    return w, h


def run_local(a, cfg, spec, task):
    """Generate on this Mac with stable-diffusion.cpp."""
    prompt = read_prompt(a)
    refs = check_refs(a.image)
    note_unsupported(a, spec["label"], ["mask", "compression", "moderation", "quality"])
    if a.background == "transparent":
        raise Unavailable("local models can't make transparent images")
    ready, why = local_readiness()
    if not ready:
        raise Unavailable(f"{why}. Download one in ImageGenKit › Settings › Providers › On this Mac, "
                          "or: imagegenkit local-models add flux2-klein-4b")
    models = local_models()
    want = a.model or pref(cfg, spec, "model")
    m = next((x for x in models if want and want.lower() in (x["id"].lower(), x["name"].lower())), None)
    if want and not m and a.model:
        die(f"local model {want} isn't downloaded (have: {', '.join(x['id'] for x in models)})")
    if refs and not (m or {}).get("edit"):
        m = next((x for x in models if x.get("edit")), None) if not a.model else m
        if not m or not m.get("edit"):
            raise Unavailable("no downloaded local model can edit images (FLUX.2 klein can: "
                              "imagegenkit local-models add flux2-klein-4b)")
    m = m or models[0]
    cat = local_catalog()
    rec = cat["recipes"].get(m["recipe"])
    if not rec:
        die(f"{m['name']}: unknown model family {m['recipe']} (try: imagegenkit update)")
    d = rec.get("defaults") or {}
    x = parse_extra(a.extra)
    steps = int(x.get("steps") or d.get("steps") or 20)
    cfg_scale = float(x.get("cfg") or d.get("cfg") or 1)
    sampler = str(x.get("sampler") or d.get("sampler") or "euler")
    seed = int(x["seed"]) if "seed" in x else random.randint(0, 2 ** 31 - 1)
    w, h = local_size(a, rec, refs)
    fmt = (a.format or "png").lower().replace("jpg", "jpeg")
    ext = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}.get(fmt, ".png")
    work = LOCAL_WORKDIR / uuid.uuid4().hex[:12]
    work.mkdir(parents=True, exist_ok=True)
    flags = {"model": "-m", "diffusion_model": "--diffusion-model", "llm": "--llm", "llm_vision": "--llm_vision",
             "vae": "--vae", "clip_l": "--clip_l", "t5xxl": "--t5xxl"}
    cmd = [local_engine()]
    for slot, rel in m["files"].items():
        cmd += [flags.get(slot, "--" + slot), str(local_path(rel))]
    cmd += ["-p", prompt, "-W", str(w), "-H", str(h), "--steps", str(steps), "--cfg-scale", f"{cfg_scale:g}",
            "--sampling-method", sampler, "-s", str(seed), "-b", str(a.n), "-o", str(work / f"image-%02d{ext}")]
    if x.get("negative"):
        cmd += ["-n", str(x["negative"])]
    if x.get("flow_shift") or d.get("flow_shift"):
        cmd += ["--flow-shift", f"{float(x.get('flow_shift') or d['flow_shift']):g}"]
    if d.get("guidance") or x.get("guidance"):
        cmd += ["--guidance", f"{float(x.get('guidance') or d['guidance']):g}"]
    if rec.get("model_args"):
        cmd += ["--model-args", rec["model_args"]]
    for r in refs:
        cmd += ["-r", str(r.resolve())]
    log(f"{m['name']} on this Mac | {w}x{h} | {steps} steps | seed {seed}" + (f" | {len(refs)} ref(s)" if refs else ""))
    if a.dry_run:
        return log(" ".join(shlex.quote(c) for c in cmd))
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=work)

    def stop(signum, frame):  # the app quit or was cancelled: don't leave the engine running
        proc.terminate()
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    errors = []

    def read_errors():
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if text.startswith("[E]") or "error" in text.lower():
                errors.append(re.sub(r"\s+---\s+\S+$", "", text[3:].strip() if text.startswith("[E]") else text))

    reader = threading.Thread(target=read_errors, daemon=True)
    reader.start()
    buf, image, last_step, loaded = b"", 0, 0, False
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "replace")
            s = re.search(r"\|\s*(\d+)/(\d+)\s*-\s*([\d.]+)\s*(s/it|it/s)", line)
            if s:
                step, total = int(s.group(1)), int(s.group(2))
                if step < last_step:
                    image += 1
                last_step = step
                log(f"  step {step}/{total}" + (f" (image {image + 1} of {a.n})" if a.n > 1 else ""))
            elif "MB/s" in line and not loaded:
                loaded = True
                log("  loading the model")
            elif "decoding" in line and "latent" in line:
                log("  decoding")
            elif line.startswith("[E]"):
                errors.append(re.sub(r"\s+---\s+\S+$", "", line[3:].strip()))
    try:
        code = proc.wait(timeout=LOCAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        die(f"the local model took longer than {LOCAL_TIMEOUT // 60} minutes")
    reader.join(timeout=2)
    files = sorted(p for p in work.iterdir() if p.suffix.lower() in MIME_BY_EXT)
    if code != 0 or not files:
        shutil.rmtree(work, ignore_errors=True)
        detail = "; ".join(dict.fromkeys(errors))[-500:] or f"engine exit code {code}"
        if code in (-9, 137) or re.search(r"out of memory|failed to allocate|insufficient memory", detail, re.I):
            die(f"{m['name']} ran out of memory. Close other apps, try a smaller size (--resolution 0.5K), or use a smaller model")
        die(f"{m['name']} couldn't make the image: {detail}")
    images = [(p.read_bytes(), MIME_BY_EXT[p.suffix.lower()]) for p in files]
    shutil.rmtree(work, ignore_errors=True)
    finish(a, cfg, spec, task, m["name"], prompt,
           {"model_id": m["id"], "size": f"{w}x{h}", "steps": steps, "cfg": cfg_scale, "sampler": sampler,
            "seed": seed, "n": a.n}, refs, images, None, t0, {"engine": local_registry().get("engine", {}).get("version")})


RUNNERS = {"openai": run_openai, "openai-compatible": run_openai, "xai": run_xai, "gemini": run_gemini,
           "grok-build": run_grok, "codex-cli": run_codex, "openrouter": run_openrouter, "together": run_together,
           "fal": run_fal, "local": run_local}


def api_message(spec, e):
    """A provider error in plain words, with what to do for the common cases."""
    hint = {401: "the key was rejected. Check it in ImageGenKit › Settings › Providers",
            403: "access denied. Check the key and the account's permissions",
            429: "too many requests or no credits left. Wait a bit or check the account's billing"}.get(e.status)
    if e.status and e.status >= 500:
        hint = "the provider has a problem right now. Try again later"
    return f"{spec['label']}: " + (f"{hint} (HTTP {e.status}: {e.message[:300]})" if hint else str(e)[:400])


LIMIT_HINT = "Paid images are paused until next month; subscriptions still work. Change it in ImageGenKit › Settings › Limits"


def run_provider(a, cfg):
    spec = all_providers(cfg)[a.cmd]
    try:
        if not a.dry_run:
            check_limits(cfg, spec)
        RUNNERS[spec["type"]](a, cfg, spec, None)
    except LimitReached as e:
        die(f"{spec['name']}: {e}. {LIMIT_HINT}")
    except Unavailable as e:
        die(f"{spec['name']}: {e}")
    except ApiError as e:
        die(api_message(spec, e))


def run_task(a, cfg):
    """draft / final / edit / transparent: the provider from your settings, then the fallback order."""
    task = a.cmd
    preset = cfg["tasks"].get(task) or {}
    provs = all_providers(cfg)
    if task == "edit" and not a.image:
        die("edit needs at least one reference image: -i image.png")
    first = a.provider or preset.get("provider")
    candidates = []
    for name in [first] + list(cfg.get("order") or []):
        if name and name in provs and name not in candidates:
            candidates.append(name)
    if a.provider:
        candidates = [a.provider] if a.provider in provs else die(f"unknown provider {a.provider}")
    skipped, limit = [], None
    for name in candidates:
        spec = provs[name]
        if task == "transparent" and spec["type"] not in CAN_TRANSPARENT:
            skipped.append(f"{name}: no transparency")
            continue
        ok, why = readiness(spec)
        if not ok:
            skipped.append(f"{name}: {why}")
            continue
        if spec["type"] in ("openai", "openai-compatible", "xai", "gemini"):
            if not a.quality and preset.get("quality") and spec["type"] == "openai":
                a.quality = preset["quality"]
            if not a.resolution and preset.get("resolution"):
                a.resolution = preset["resolution"]
        if task == "transparent":
            a.background = "transparent"
            if not a.format and not (a.out and Path(a.out).suffix):
                a.format = "png"
        try:
            if not a.dry_run:
                check_limits(cfg, spec)
        except LimitReached as e:
            limit = str(e)
            skipped.append(f"{name}: monthly limit")
            continue
        log(f"{task} -> {name}" + (f" (skipped {'; '.join(skipped)})" if skipped else ""))
        try:
            return RUNNERS[spec["type"]](a, cfg, spec, task)
        except Unavailable as e:
            skipped.append(f"{name}: {e}")
        except ApiError as e:
            if e.status not in (401, 403):
                die(api_message(spec, e))
            skipped.append(f"{name}: {e}")
    if limit:
        die(f"{limit}. {LIMIT_HINT}")
    die(f"no provider ready for '{task}' ({'; '.join(skipped) or 'none configured'}). "
        "Set one up in the ImageGenKit app, or: imagegenkit set-key openai")


# ---------- utility commands ----------

def check_provider(spec, cfg):
    """(status, message) with a live call where possible. status: ok | missing | error."""
    ok, why = readiness(spec)
    if not ok:
        return "missing", why
    if spec["type"] == "local":
        return local_check()
    if spec["type"] == "codex-cli":
        try:
            out = subprocess.run([codex_bin(), "login", "status"], capture_output=True, text=True, timeout=60,
                                 stdin=subprocess.DEVNULL)
            text = (out.stdout + out.stderr).strip()
        except subprocess.SubprocessError:
            text = ""
        return ("ok", text.splitlines()[0]) if "logged in" in text.lower() else ("error", "not logged in (run: codex login)")
    if spec["type"] == "grok-build":
        try:
            out = subprocess.run([grok_bin(), "models"], capture_output=True, text=True, timeout=60).stdout
        except subprocess.SubprocessError:
            out = ""
        m = re.search(r"logged in with ([^\s.]+(?:\.[a-z]+)?)", out)
        return ("ok", f"logged in with {m.group(1)}") if m else ("error", "not logged in (run: grok login)")
    key, src = key_for(spec)
    base = (spec.get("base_url") or "").rstrip("/")
    try:
        if spec["type"] == "fal":
            return "ok", f"key from {src} (checked on first run)"
        if spec["type"] == "openrouter":
            resp = http("GET", f"{base}/key", {"Authorization": f"Bearer {key}"}, retries=0)
            d = resp.get("data", {})
            left = d.get("limit_remaining")
            return "ok", f"key from {src}" + (f"; ${left:.2f} left" if isinstance(left, (int, float)) else "")
        if spec["type"] == "gemini":
            resp = http("GET", f"{base}/models?pageSize=200", {"x-goog-api-key": key}, retries=0)
            names = [m.get("name", "") for m in resp.get("models", [])]
            return "ok", f"key from {src}; {sum('image' in n or 'banana' in n for n in names)} image models"
        if spec["type"] == "xai":
            resp = http("GET", f"{base}/image-generation-models", {"Authorization": f"Bearer {key}"}, retries=0)
            ids = [m.get("id") for m in resp.get("models", resp.get("data", []))]
            return "ok", f"key from {src}; models: {', '.join(i for i in ids if i) or '?'}"
        resp = http("GET", f"{base}/models", {"Authorization": f"Bearer {key}"}, retries=0)
        ids = [m.get("id", "") for m in resp.get("data", [])]
        if spec["type"] == "openai":
            image_ids = [i for i in ids if re.search(r"image|dall-e", i)]
            missing = [m for m in (cfg["openai_model"], cfg["openai_edit_model"]) if m not in image_ids]
            return "ok", f"key from {src}; {len(image_ids)} image models" + (
                f"; not available: {', '.join(missing)}" if missing else "")
        return "ok", f"key from {src}; {len(ids)} models"
    except ApiError as e:
        if e.status in (401, 403):
            return "error", f"key rejected ({e.message[:120]})"
        if e.status in (404, 405) and spec["type"] not in ("openai", "xai", "gemini"):
            return "ok", f"key from {src} (endpoint has no /models list)"
        return "error", str(e)[:200]


def cmd_check(a, cfg):
    provs = all_providers(cfg)
    names = [a.provider] if a.provider else list(provs)
    results = []
    for n in names:
        if n not in provs:
            die(f"unknown provider {n}")
        status, msg = check_provider(provs[n], cfg)
        results.append({"provider": n, "label": provs[n]["label"], "status": status, "message": msg})
    if a.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            print(f"{r['label']:<18} {r['status'].upper():<8} {r['message']}")
    sys.exit(0 if any(r["status"] == "ok" for r in results) else 1)


def cmd_providers(a, cfg):
    provs = all_providers(cfg)
    rows = []
    for name, spec in provs.items():
        ready, why = readiness(spec)
        rows.append({"name": name, "label": spec["label"], "type": spec["type"], "builtin": spec["builtin"],
                     "needs_key": spec["type"] not in KEYLESS_TYPES, "keychain": keychain_service(name)
                     if spec["type"] not in KEYLESS_TYPES else None, "env": spec.get("env"), "ready": ready,
                     "status": why, "base_url": spec.get("base_url"), "model": spec.get("model"),
                     "key_url": spec.get("key_url")})
    if a.json:
        print(json.dumps({"providers": rows, "order": cfg.get("order"), "tasks": cfg.get("tasks")}, indent=2))
        return
    for r in rows:
        print(f"{r['name']:<12} {r['label']:<18} {'ready' if r['ready'] else '-':<6} {r['status']}")
    print("\norder: " + " > ".join(cfg.get("order") or []))
    for t, p in (cfg.get("tasks") or {}).items():
        print(f"{t:<12} -> {p.get('provider')}")


def cmd_provider(a, cfg):
    data = read_user_config()
    custom = data.setdefault("providers", {})
    if a.action == "add":
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", a.name or ""):
            die("provider name: lowercase letters, digits and dashes, 2-31 chars")
        if a.name in BUILTIN_PROVIDERS or a.name in RESERVED:
            die(f"'{a.name}' is reserved")
        if not a.base_url or not a.model:
            die("usage: imagegenkit provider add <name> --base-url https://.../v1 --model <id> [--edit-model <id>] [--label X]")
        spec = {"type": "openai-compatible", "label": a.label or a.name, "base_url": a.base_url.rstrip("/"),
                "model": a.model}
        if a.edit_model:
            spec["edit_model"] = a.edit_model
        custom[a.name] = spec
        order = data.setdefault("order", list(DEFAULTS["order"]))
        if a.name not in order:
            order.append(a.name)
        write_user_config(data)
        print(f"Added {a.name}. Store its key with: imagegenkit set-key {a.name}")
    elif a.action == "remove":
        if custom.pop(a.name, None) is None:
            die(f"no custom provider named {a.name}")
        if a.name in data.get("order", []):
            data["order"].remove(a.name)
        write_user_config(data)
        print(f"Removed {a.name} (its Keychain key stays until: imagegenkit delete-key {a.name})")


def cmd_set_key(a, cfg):
    provs = all_providers(cfg)
    spec = provs.get(a.provider)
    if a.provider == "huggingface":  # a token for gated or private local models
        spec = {"type": "token", "label": "Hugging Face", "name": "huggingface"}
    if not spec:
        die(f"unknown provider {a.provider} (see: imagegenkit providers)")
    if spec["type"] == "local":
        die(f"{spec['label']} needs no key. Download a model with: imagegenkit local-models add flux2-klein-4b")
    if spec["type"] in SUBSCRIPTION_TYPES:
        die(f"{spec['label']} has no key. Log in with: {'grok login' if spec['type'] == 'grok-build' else 'codex login'}")
    if a.clipboard:
        key = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout.strip()
        if not key:
            die("clipboard is empty")
    elif a.stdin or not sys.stdin.isatty():
        key = sys.stdin.read().strip()
    else:
        key = getpass.getpass(f"Paste {spec['label']} {'token' if spec['type'] == 'token' else 'API key'} (input hidden): ").strip()
    if not key or re.search(r'[\s"\\]', key):
        die("that does not look like an API key")
    service = keychain_service(a.provider)
    if not keychain_store(service, key, f"ImageGenKit {spec['label']} API key") or keychain_read(service) != key:
        die("could not store the key in the Keychain")
    if spec["type"] != "token":
        enable_provider(a.provider)
    if a.clipboard:
        subprocess.run(["pbcopy"], input="", text=True)
    print(f"Saved in Keychain as '{service}'.")
    if spec["type"] == "token":
        return
    if spec.get("env") and os.environ.get(spec["env"]):
        print(f"note: env {spec['env']} is set and takes precedence.")
    status, msg = check_provider(spec, load_config())
    print(f"Check: {status.upper()} {msg}")


def cmd_delete_key(a, cfg):
    r = subprocess.run(["security", "delete-generic-password", "-s", keychain_service(a.provider)],
                       capture_output=True, text=True)
    print("Deleted." if r.returncode == 0 else f"No key stored for {a.provider}.")


def cmd_models(a, cfg):
    spec = all_providers(cfg).get(a.provider or "openai")
    if not spec or spec["type"] in ("grok-build", "codex-cli", "gemini"):
        die("models works for openai, xai and OpenAI-compatible providers")
    key = require_key(spec)
    if spec["type"] == "xai":
        resp = http("GET", spec["base_url"] + "/image-generation-models", {"Authorization": f"Bearer {key}"}, retries=0)
        ids = [m.get("id") for m in resp.get("models", resp.get("data", []))]
    else:
        resp = http("GET", spec["base_url"] + "/models", {"Authorization": f"Bearer {key}"}, retries=0)
        ids = [m["id"] for m in resp.get("data", []) if spec["type"] != "openai" or re.search(r"image|dall-e", m["id"])]
    for i in sorted(i for i in ids if i):
        print(i)


def cmd_usage(a, cfg):
    cost, images, runs = month_usage(cfg)
    cost_limit, image_limit, stop = limit_values(cfg)
    lim = {k: v for k, v in (("monthly_cost", cost_limit), ("monthly_images", image_limit and int(image_limit)),
                             ("stop_paid", stop)) if v}
    out = {"month": dt.datetime.now().strftime("%Y-%m"), "cost": round(cost, 4), "images": images, "runs": runs,
           "limits": lim}
    if a.json:
        print(json.dumps(out, indent=2))
        return
    print(f"{out['month']}: ${cost:.2f} estimated · {images} images · {runs} runs")
    if lim.get("monthly_cost"):
        print(f"spend limit  ${lim['monthly_cost']:.2f}  ({cost / lim['monthly_cost']:.0%} used)")
    if lim.get("monthly_images"):
        print(f"image limit  {lim['monthly_images']}  ({images / lim['monthly_images']:.0%} used)")
    if lim.get("stop_paid"):
        print("when reached: paid images pause, subscriptions keep working")
    elif lim:
        print("limits warn only (stop_paid is off)")


def version_tuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", str(v).lstrip("vV")))


def self_update(force_check=True):
    """Install the CLI (and ImageGenKit-managed skills) from the latest GitHub release. Returns a short message."""
    me = Path(os.path.realpath(__file__))
    if (me.parent.parent / ".git").exists():
        return f"not updating a source checkout ({me.parent.parent}); use git pull"
    ua = {"User-Agent": "imagegenkit", "Accept": "application/vnd.github+json"}
    req = urllib.request.Request(RELEASES_API, headers=ua)
    with urllib.request.urlopen(req, timeout=8) as r:
        tag = json.loads(r.read())["tag_name"]

    def fetch(path):
        url = f"{RAW_BASE}/{tag}/{path}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "imagegenkit"}), timeout=20) as r:
            return r.read()

    code = fetch("cli/imagegenkit.py")
    m = re.search(rb'^VERSION = "([^"]+)"', code, re.M)
    if not m:
        return f"latest release {tag} has no CLI version"
    remote = m.group(1).decode()
    if version_tuple(remote) <= version_tuple(VERSION):
        return f"up to date ({VERSION})"
    compile(code, "imagegenkit.py", "exec")  # never install something that doesn't parse
    tmp = me.with_name(me.name + ".new")
    tmp.write_bytes(code)
    tmp.chmod(0o755)
    tmp.replace(me)
    try:
        skill = fetch("skill/SKILL.md")
        for home in (Path.home() / ".claude", Path.home() / ".codex"):
            target = home / "skills" / "imagegenkit" / "SKILL.md"
            if target.exists() and "managed-by: ImageGenKit" in target.read_text(errors="ignore"):
                target.write_bytes(skill)
    except (OSError, urllib.error.URLError):
        pass
    return f"updated {VERSION} -> {remote}"


def auto_update(cfg):
    """Once a day, after a normal run: update from the latest release in a detached background process, so the
    run never waits for GitHub (config auto_update: false turns it off)."""
    if cfg.get("auto_update") is False or os.environ.get("IMAGEGENKIT_NO_UPDATE"):
        return
    stamp = CONFIG_DIR / ".update-check"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 86400:
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        subprocess.Popen([sys.executable, os.path.realpath(__file__), "_auto-update"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:  # read-only config, no python: try again tomorrow
        pass


def background_update():
    """The detached part of auto_update: offline, rate limited, private repo, read-only install: try again tomorrow."""
    try:
        refresh_catalog()
    except Exception:
        pass
    try:
        self_update()
    except Exception:
        pass


def cmd_update(a, cfg):
    try:
        if refresh_catalog():
            print("Provider list updated.", flush=True)
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        print(self_update())
    except urllib.error.HTTPError as e:
        why = {404: "no release found", 403: "GitHub limits requests, try again in an hour"}.get(e.code, f"HTTP {e.code}")
        die(f"couldn't check for updates ({why}). {HOMEPAGE}/releases")
    except (urllib.error.URLError, OSError) as e:
        die(f"couldn't reach GitHub ({getattr(e, 'reason', e)}). Check your internet connection")
    except (ValueError, KeyError, SyntaxError):
        die(f"couldn't update: unexpected answer from GitHub. {HOMEPAGE}/releases")


def set_dotted(data, dotted, value):
    keys = dotted.split(".")
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            die(f"{dotted}: {k} is not an object")
    if value is None:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value


def cmd_config(a, cfg):
    if a.action == "get":
        if not a.key:
            die("usage: imagegenkit config get <key>   (dotted keys work: tasks.draft.provider)")
        node = {k: v for k, v in cfg.items() if not k.startswith("_")}
        for k in a.key.split("."):
            if not isinstance(node, dict) or k not in node:
                die(f"{a.key} is not set")
            node = node[k]
        print(node if isinstance(node, str) else json.dumps(node, indent=2, ensure_ascii=False))
        return
    if a.action in ("set", "unset"):
        if not a.key or (a.action == "set" and a.value is None):
            die("usage: imagegenkit config set <key> <value>   (dotted keys work: tasks.draft.provider openai)")
        data = read_user_config()
        value = None
        if a.action == "set":
            try:
                value = json.loads(a.value)
            except ValueError:
                value = a.value
        set_dotted(data, a.key, value)
        write_user_config(data)
    elif a.action == "reset":
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
    print(json.dumps({k: v for k, v in load_config().items() if not k.startswith("_")}, indent=2, ensure_ascii=False))


# ---------- CLI ----------

def add_gen_args(p, task=False):
    p.add_argument("prompt", nargs="?", help="prompt text, or '-' to read stdin")
    p.add_argument("-f", "--prompt-file", help="read the prompt from a file")
    p.add_argument("-o", "--out", help="output file, or a directory ending in / (default ~/Pictures/ImageGenKit/<date>/)")
    p.add_argument("-i", "--image", action="append", help="reference image to edit or combine (repeatable)")
    p.add_argument("-n", type=int, default=1, help="number of images (default 1)")
    p.add_argument("-m", "--model", help="model id")
    p.add_argument("--aspect", help="aspect ratio, e.g. 1:1 3:2 16:9 9:16")
    p.add_argument("--resolution", help="1K | 2K | 4K")
    p.add_argument("--size", help="exact WIDTHxHEIGHT or auto (OpenAI-style providers)")
    p.add_argument("--quality", help="low | medium | high | auto (OpenAI)")
    p.add_argument("--background", help="transparent | opaque | auto (OpenAI)")
    p.add_argument("--format", help="png | jpeg | webp")
    p.add_argument("--compression", type=int, help="0-100 for jpeg/webp (OpenAI)")
    p.add_argument("--moderation", help="auto | low (OpenAI)")
    p.add_argument("--mask", help="PNG mask with alpha, transparent = area to edit (OpenAI)")
    p.add_argument("--extra", action="append", help="pass-through API param key=value (JSON values ok)")
    p.add_argument("--overwrite", action="store_true", help="overwrite existing output instead of adding -2, -3")
    p.add_argument("--dry-run", action="store_true", help="show the request without calling the provider")
    if task:
        p.add_argument("-p", "--provider", help="use this provider instead of the one in your settings")


def main():
    migrate_legacy()
    if sys.argv[1:] == ["_auto-update"]:
        return background_update()
    cfg = load_config()
    provs = all_providers(cfg)
    ap = argparse.ArgumentParser(
        prog="imagegenkit", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"One command for AI image generation across providers (ImageGenKit {VERSION}).",
        epilog="examples:\n"
               "  imagegenkit draft \"creator filming a laser engraver, top-down\" --aspect 9:16 -o idea.jpg\n"
               "  imagegenkit final \"product photo of a red mug on marble\" -o mug.png --aspect 4:5\n"
               "  imagegenkit edit \"same mug, on a wooden table\" -i mug.png -o mug-wood.png\n"
               "  imagegenkit transparent \"flat sticker of a smiling sun\" -o sun.png\n"
               "  imagegenkit openai \"...\" --quality high     imagegenkit grok \"...\"\n"
               "  imagegenkit providers | check | set-key <provider> | config set tasks.draft.provider openai\n"
               f"\n{HOMEPAGE}")
    ap.add_argument("--version", action="version", version=f"imagegenkit {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")

    for task, desc in TASKS.items():
        preset = cfg["tasks"].get(task) or {}
        p = sub.add_parser(task, help=f"{desc} (now: {preset.get('provider', '?')})", description=desc)
        add_gen_args(p, task=True)
        p.set_defaults(fn=run_task)
    for name, spec in provs.items():
        p = sub.add_parser(name, help=f"{spec['label']} directly", description=f"{spec['label']} ({spec['type']})")
        add_gen_args(p)
        p.set_defaults(fn=run_provider)

    p = sub.add_parser("providers", help="list providers, readiness, fallback order and task routing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_providers)
    p = sub.add_parser("check", help="verify keys and logins with a live call (never prints keys)")
    p.add_argument("provider", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("set-key", help="store a provider's API key in the macOS Keychain")
    p.add_argument("provider")
    p.add_argument("--clipboard", action="store_true", help="read the key from the clipboard, then clear it")
    p.add_argument("--stdin", action="store_true", help="read the key from stdin")
    p.set_defaults(fn=cmd_set_key)
    p = sub.add_parser("delete-key", help="remove a provider's API key from the Keychain")
    p.add_argument("provider")
    p.set_defaults(fn=cmd_delete_key)
    p = sub.add_parser("provider", help="add or remove your own OpenAI-compatible provider")
    p.add_argument("action", choices=["add", "remove"])
    p.add_argument("name")
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--edit-model")
    p.add_argument("--label")
    p.set_defaults(fn=cmd_provider)
    p = sub.add_parser("local-models", help="models that run on this Mac: list, search, add, remove")
    lsub = p.add_subparsers(dest="action", required=True, metavar="action")
    q = lsub.add_parser("list", help="downloaded models and the ones you can add")
    q.add_argument("--json", action="store_true")
    q.add_argument("--refresh", action="store_true", help="reload the model list from GitHub")
    q.set_defaults(fn=cmd_local_list)
    q = lsub.add_parser("search", help="search Hugging Face for models this Mac can run")
    q.add_argument("query", nargs="?", default="")
    q.add_argument("--limit", type=int, default=30)
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_local_search)
    q = lsub.add_parser("add", help="download a model: an id from the list, a Hugging Face repo or link, a link or a file")
    q.add_argument("model")
    q.add_argument("--recipe", help="model family, for links and files (see: local-models list --json)")
    q.add_argument("--file", help="which file of a Hugging Face repo (default: a 4-bit .gguf)")
    q.add_argument("--json", action="store_true", help="progress as JSON lines")
    q.set_defaults(fn=cmd_local_add)
    q = lsub.add_parser("remove", help="delete a downloaded model (shared files stay while another model uses them)")
    q.add_argument("model")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_local_remove)
    p = sub.add_parser("models", help="image models a provider's key can use (openai, xai, custom)")
    p.add_argument("provider", nargs="?")
    p.set_defaults(fn=cmd_models)
    p = sub.add_parser("config", help="show or change settings (~/.config/imagegenkit/config.json)")
    p.add_argument("action", nargs="?", choices=["show", "get", "set", "unset", "reset"], default="show")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser("usage", help="this month's estimated spend, images and limits")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("update", help="update imagegenkit (and its skills) from the latest GitHub release")
    p.set_defaults(fn=cmd_update)
    p = sub.add_parser("version", help="print the version")
    p.set_defaults(fn=lambda a, c: print(VERSION))

    a = ap.parse_args()
    if getattr(a, "n", 1) < 1:
        die("-n must be >= 1")
    try:
        a.fn(a, cfg)
        if a.fn in (run_task, run_provider) and not getattr(a, "dry_run", False):
            auto_update(cfg)  # after the images are saved and printed
    except ApiError as e:
        die(str(e))
    except Unavailable as e:
        die(str(e))
    except KeyboardInterrupt:
        die("interrupted", 130)
    except Exception as e:  # never show a traceback to non-technical users
        if os.environ.get("IMAGEGENKIT_DEBUG"):
            raise
        die(f"unexpected error: {type(e).__name__}: {e} (set IMAGEGENKIT_DEBUG=1 for details)")


if __name__ == "__main__":
    main()
