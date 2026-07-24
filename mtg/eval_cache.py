"""Server-side analysis cache for the Commander Eval feature.

Flask cookie sessions cap at ~4KB — expanded bracket + kill-on-sight
output exceeds that, so we keep large analysis payloads in memory.

FIFO-capped at 100 entries (eval) and 50 (progress). Thread-safe via
_cache_lock — reads are atomic dict lookups and OK unlocked.
"""

import threading
from collections import OrderedDict

_eval_cache = OrderedDict()  # {id: {"analysis": {...}, "error": "..."}}
_cache_lock = threading.Lock()

# Progress entries are tiny (~3 fields) and transient — capped at 50 to
# prevent unbounded growth from abandoned/refreshed eval pages.
_progress_cache = OrderedDict()  # {id: {"step": N, "total": 6, "label": "..."}}


def cache_put(key, entry):
    """Store in eval cache, evicting oldest if over cap."""
    with _cache_lock:
        _eval_cache[key] = entry
        while len(_eval_cache) > 100:
            _eval_cache.popitem(last=False)


def cache_update(key, **kwargs):
    """Update sub-keys of an existing cache entry, lock-guarded.

    Call cache_update_locked(key, ...) when you already hold _cache_lock."""
    with _cache_lock:
        cache_update_locked(key, **kwargs)


def cache_update_locked(key, **kwargs):
    """Update sub-keys of an existing cache entry. Caller must hold _cache_lock."""
    entry = _eval_cache.get(key)
    if entry is not None:
        entry.update(kwargs)
