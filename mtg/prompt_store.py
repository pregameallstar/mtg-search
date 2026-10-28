"""Server-side store for custom LLM prompts.

Flask's signed session cookie is limited to roughly 4 KB, while the default
Commander prompt alone is ~10 KB. Prompts therefore live in a small JSON file
keyed by an opaque per-browser id stored in the cookie. The API key remains in
the cookie (it is tiny); only prompts are moved out.

The store deliberately treats the built-in prompts as defaults: saving a value
equal to the default removes the override, so resetting to default keeps the
store small and future prompt updates still apply.
"""

import os
import uuid

from flask import session

from mtg.prompts import (
    COMMANDER_SYSTEM_PROMPT,
    DEEPDIVE_SYSTEM_PROMPT,
    DECK_REVIEW_SYSTEM_PROMPT,
    DECK_REVIEW_VERIFY_SYSTEM_PROMPT,
    VERIFY_SYSTEM_PROMPT,
)
from mtg.shared import PROJECT_ROOT
from mtg.storage import read_json, write_json_atomic

PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompt_store")

DEFAULT_PROMPTS = {
    "eval_prompt": COMMANDER_SYSTEM_PROMPT,
    "deepdive_prompt": DEEPDIVE_SYSTEM_PROMPT,
    "verify_prompt": VERIFY_SYSTEM_PROMPT,
    "review_prompt": DECK_REVIEW_SYSTEM_PROMPT,
    "review_verify_prompt": DECK_REVIEW_VERIFY_SYSTEM_PROMPT,
}

PROMPT_KEYS = tuple(DEFAULT_PROMPTS)


def _store_id():
    """Return this browser's prompt-store id, creating one if needed."""
    store_id = session.get("prompt_store_id")
    if not store_id:
        store_id = uuid.uuid4().hex
        session["prompt_store_id"] = store_id
    return store_id


def _store_path():
    return os.path.join(PROMPTS_DIR, f"{_store_id()}.json")


def _read_store():
    data = read_json(_store_path(), default={})
    return data if isinstance(data, dict) else {}


def _write_store(prompts):
    path = _store_path()
    if prompts:
        write_json_atomic(path, prompts, mode=0o600)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def get_prompts():
    """Return the custom prompt mapping for this browser.

    Also migrates prompts left in the old (oversized) session cookie into the
    server-side store and removes them from the cookie.
    """
    prompts = _read_store()
    migrated = False
    for key, default in DEFAULT_PROMPTS.items():
        if key not in session:
            continue
        legacy = session.pop(key, "")
        if legacy and legacy != default:
            prompts[key] = legacy
            migrated = True
    if migrated:
        _write_store(prompts)
    return prompts


def get_prompt(key):
    """Return a custom prompt, or ``""`` when the built-in default applies."""
    if key not in DEFAULT_PROMPTS:
        raise KeyError(key)
    return get_prompts().get(key, "")


def save_prompt(key, value):
    """Persist a custom prompt, or remove the override when it is the default."""
    if key not in DEFAULT_PROMPTS:
        raise KeyError(key)
    value = (value or "").strip()
    prompts = _read_store()
    if not value or value == DEFAULT_PROMPTS[key]:
        prompts.pop(key, None)
    else:
        prompts[key] = value
    _write_store(prompts)
