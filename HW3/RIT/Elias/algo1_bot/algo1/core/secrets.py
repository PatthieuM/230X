"""Secrets: the RIT API key and connection details, loaded from `secrets.env` into the process.

`secrets.env` (gitignored, next to CLAUDE.md) holds `KEY=VALUE` lines:
    RIT_API_KEY=...        the key shown behind the API icon in the RIT client status bar
    RIT_HOST=localhost     optional
    RIT_PORT=9999          optional

Rules (also in CLAUDE.md): the file is read only by `load_secrets()`, values are never printed,
logged, written to the rings, the run summary or a report, and Claude Code never opens it.
Environment variables already set win over the file, so `RIT_API_KEY=… python -m algo1 run`
also works. `secrets.env.example` documents the format with placeholder values.
"""
from __future__ import annotations

import os

DEFAULT_PATH = "secrets.env"
KEYS = ("RIT_API_KEY", "RIT_HOST", "RIT_PORT")


def load_secrets(path=DEFAULT_PATH):
    """Populate os.environ from `path` (existing env wins). Returns the names found, not values."""
    found = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
                    _FILE_KEYS.add(k)
                found.append(k)
    except FileNotFoundError:
        pass
    return found


def apply_to_config(cfg, path=DEFAULT_PATH):
    """Fill cfg.api_key / host / port from the environment (after loading `path`)."""
    load_secrets(path)
    key = os.environ.get("RIT_API_KEY")
    if key and not cfg.api_key:
        cfg.api_key = key
    host = os.environ.get("RIT_HOST")
    if host:
        cfg.host = host
    port = os.environ.get("RIT_PORT")
    if port:
        try:
            cfg.port = int(port)
        except ValueError:
            pass
    return cfg


def redacted(cfg):
    """Config view safe to print: the key is shown as its length only."""
    d = dict(cfg.__dict__)
    d["api_key"] = f"<{len(cfg.api_key)} chars>" if cfg.api_key else "<empty>"
    return d


def key_status(cfg, path=DEFAULT_PATH):
    """Human-readable, secret-free description of where the key came from (or why not)."""
    import os as _os
    if not cfg.api_key:
        exists = _os.path.exists(path)
        twin = _os.path.exists(path + ".txt")
        if twin:
            return f"NO KEY: found '{path}.txt' — Notepad added an extension; rename it to '{path}'"
        if not exists:
            return f"NO KEY: '{path}' not found in {_os.getcwd()} (create it from secrets.env.example)"
        return f"NO KEY: '{path}' exists but has no RIT_API_KEY=... line"
    src = "environment variable" if _os.environ.get("RIT_API_KEY") == cfg.api_key and not _loaded_from_file(path) else path
    return f"key loaded from {src} ({len(cfg.api_key)} chars) for {cfg.host}:{cfg.port}"


_FILE_KEYS = set()


def _loaded_from_file(path):
    return "RIT_API_KEY" in _FILE_KEYS
