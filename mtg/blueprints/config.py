"""Config page — LLM settings, embedding, MCP, database ingest."""

import os
import json
import threading
import signal
import subprocess
import time
import traceback

from urllib.parse import urlparse

from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify, abort

from mtg.shared import get_db, resolve_bind_path, DATABASE, db_path
from mtg.csrf import csrf_ok
from mtg.prompts import (
    COMMANDER_SYSTEM_PROMPT,
    DEEPDIVE_SYSTEM_PROMPT,
    DECK_REVIEW_SYSTEM_PROMPT,
    VERIFY_SYSTEM_PROMPT,
)
from mtg.prompt_store import get_prompts, save_prompt
from mtg.mcp_control import (
    MCP_SSE_PORT, MCPO_PORT, MCP_HOST, MCP_DISPLAY_HOST,
    port_alive, restart_mcp,
)

config_bp = Blueprint("config", __name__, url_prefix="/config")


def _same_origin_request():
    """Reject cross-site browser form/multipart requests.

    Curl and other non-browser clients send neither Origin nor Referer and are
    allowed. Modern browsers attach Origin to cross-site POSTs, which this
    check uses in addition to the session CSRF token.
    """
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in ("http", "https") and parsed.netloc == request.host


def _csrf_ok():
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return csrf_ok(token)


@config_bp.route("/", methods=["GET", "POST"])
def config_page():
    """LLM configuration — shared across tools that use the LLM."""
    import mtg.embed as embed

    if request.method == "POST":
        if not _same_origin_request() or not _csrf_ok():
            abort(403)
        # Migrate any prompts left in the old oversized session cookie before
        # writing new values; otherwise the legacy value would win on next read.
        get_prompts()

        # Only the main LLM form is allowed to change/clear the API key. The
        # prompt/history/pricing/search forms intentionally omit the key field,
        # and must not wipe it as a side effect.
        if request.form.get("save_llm") == "1":
            api_key = request.form.get("llm_api_key", "").strip()
            if api_key:
                session["llm_api_key"] = api_key
            elif "llm_api_key" in session:
                session.pop("llm_api_key", None)
        if "llm_backend" in request.form:
            session["llm_backend"] = request.form.get("llm_backend", "").strip()
        if "llm_base_url" in request.form:
            session["llm_base_url"] = request.form.get("llm_base_url", "").strip()
        if "llm_model" in request.form:
            session["llm_model"] = request.form.get("llm_model", "").strip()
        if request.form.get("save_prompt") == "1":
            save_prompt("eval_prompt", request.form.get("eval_prompt", ""))
        if request.form.get("save_deepdive_prompt") == "1":
            save_prompt("deepdive_prompt", request.form.get("deepdive_prompt", ""))
        if request.form.get("save_verify_prompt") == "1":
            save_prompt("verify_prompt", request.form.get("verify_prompt", ""))
        if request.form.get("save_review_prompt") == "1":
            save_prompt("review_prompt", request.form.get("review_prompt", ""))
        # Search Logic config
        if request.form.get("save_search_logic") == "1":
            similar_method = request.form.get("similar_method", "").strip()
            if similar_method in ("embed", "legacy"):
                session["similar_method"] = similar_method
            elif "similar_method" in session:
                session.pop("similar_method", None)
            eval_similar_method = request.form.get("eval_similar_method", "").strip()
            if eval_similar_method in ("embed", "legacy"):
                session["eval_similar_method"] = eval_similar_method
            elif "eval_similar_method" in session:
                session.pop("eval_similar_method", None)
        # Pricing config
        if request.form.get("save_pricing") == "1":
            pricing_store = request.form.get("pricing_store", "").strip()
            if pricing_store in ("usd", "usd_foil", "usd_etched", "eur"):
                session["pricing_store"] = pricing_store
            elif "pricing_store" in session:
                session.pop("pricing_store", None)
        # History config
        if request.form.get("save_history") == "1":
            try:
                threshold = int(request.form.get("history_warning_days", "30"))
                if 7 <= threshold <= 365:
                    session["history_warning_days"] = str(threshold)
            except ValueError:
                session.pop("history_warning_days", None)
        return redirect(url_for("config.config_page"))

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))
    llm_has_key = bool(session.get("llm_api_key") or os.environ.get("LLM_API_KEY"))
    prompts = get_prompts()
    eval_prompt = prompts.get("eval_prompt", "")
    deepdive_prompt = prompts.get("deepdive_prompt", "")
    verify_prompt = prompts.get("verify_prompt", "")
    review_prompt = prompts.get("review_prompt", "")
    similar_method = session.get("similar_method", "embed")
    eval_similar_method = session.get("eval_similar_method", "embed")
    pricing_store = session.get("pricing_store") or os.environ.get("PRICING_STORE", "usd")
    history_warning_days = session.get("history_warning_days", "30")

    last_ingest = None
    _ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".last_ingest.json")
    _actual = resolve_bind_path(_ingest_path)
    if not os.path.isfile(_actual):
        _actual = None
    if _actual:
        try:
            with open(_actual) as f:
                last_ingest = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    db_card_count = None
    try:
        db = get_db()
        row = db.execute("SELECT COUNT(*) AS cnt FROM (SELECT DISTINCT name FROM cards WHERE language = 'English' AND (side IS NULL OR side = 'a'))").fetchone()
        db_card_count = row["cnt"]
    except Exception:
        pass

    embed_status = embed.status()

    return render_template("config.html",
                           llm_backend=llm_backend,
                           llm_base_url=llm_base_url,
                           llm_model=llm_model,
                           llm_has_key=llm_has_key,
                           eval_prompt=eval_prompt,
                           deepdive_prompt=deepdive_prompt,
                           verify_prompt=verify_prompt,
                           review_prompt=review_prompt,
                           similar_method=similar_method,
                           eval_similar_method=eval_similar_method,
                           pricing_store=pricing_store,
                           history_warning_days=history_warning_days,
                           default_prompt=COMMANDER_SYSTEM_PROMPT,
                           default_deepdive_prompt=DEEPDIVE_SYSTEM_PROMPT,
                           default_verify_prompt=VERIFY_SYSTEM_PROMPT,
                           default_review_prompt=DECK_REVIEW_SYSTEM_PROMPT,
                           last_ingest=last_ingest,
                           db_card_count=db_card_count,
                           embed_status=embed_status,
                           mcp_sse_port=MCP_SSE_PORT,
                           mcpo_port=MCPO_PORT,
                           mcp_host=MCP_HOST,
                           mcp_display_host=MCP_DISPLAY_HOST)


@config_bp.route("/embed-status")
def embed_status():
    """Return current embedding index build status as JSON."""
    import mtg.embed as embed
    return jsonify(embed.status())


@config_bp.route("/embed-build", methods=["POST"])
def embed_build():
    """Trigger an embedding index rebuild. Returns immediately; build runs async."""
    import mtg.embed as embed

    if not _same_origin_request() or not _csrf_ok():
        abort(403)
    status_info = embed.status()
    if status_info["building"]:
        return jsonify({"success": False, "error": "Build already in progress."}), 409

    def _build():
        try:
            embed.build(db_path(DATABASE))
        except Exception:
            pass
    threading.Thread(target=_build, daemon=True).start()
    return jsonify({"success": True})


@config_bp.route("/mcp-status")
def mcp_status():
    """Return MCP server status — SSE backend + MCPO proxy."""
    sse_alive = port_alive(MCP_SSE_PORT)
    mcpo_alive = port_alive(MCPO_PORT)
    return jsonify({
        "running": sse_alive and mcpo_alive,
        "host": MCP_DISPLAY_HOST,
        "sse": {
            "alive": sse_alive,
            "port": MCP_SSE_PORT,
        },
        "mcpo": {
            "alive": mcpo_alive,
            "port": MCPO_PORT,
            "url": f"http://{MCP_DISPLAY_HOST}:{MCPO_PORT}/openapi.json",
        },
    })


@config_bp.route("/mcp-restart", methods=["POST"])
def mcp_restart():
    """Restart MCP SSE server + MCPO proxy."""
    import shutil as _shutil

    if not _same_origin_request() or not _csrf_ok():
        abort(403)
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))  # mtg/blueprints/
        script_dir = os.path.join(script_dir, "..", "..")        # back to project root
        sse_pid = restart_mcp(
            "sse", MCP_SSE_PORT,
            os.path.join(script_dir, ".mcp_server.pid"),
            os.path.join(script_dir, ".mcp_server.log"),
        )
        # Restart MCPO by killing it and letting docker-entrypoint.sh re-spawn it
        mcpo_pid_file = os.path.join(script_dir, ".mcpo.pid")
        try:
            with open(mcpo_pid_file) as f:
                old = int(f.read().strip())
            os.kill(old, signal.SIGTERM)
            time.sleep(0.5)
        except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
            pass
        mcpo_bin = os.path.join(script_dir, "venv", "bin", "mcpo")
        if not os.path.isfile(mcpo_bin):
            found = _shutil.which("mcpo")
            if not found:
                return jsonify({"success": False, "error": "mcpo not installed. Run: pip install mcpo"}), 500
            mcpo_bin = found
        mcpo_log = os.path.join(script_dir, ".mcpo.log")
        proc = subprocess.Popen(
            [mcpo_bin, "--type", "sse", "--port", str(MCPO_PORT),
             "--name", "mtg-search", "--",
             f"http://127.0.0.1:{MCP_SSE_PORT}/sse"],
            cwd=script_dir,
            stdout=open(mcpo_log, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with open(mcpo_pid_file, "w") as f:
            f.write(str(proc.pid))
        return jsonify({
            "success": True,
            "sse": {"pid": sse_pid, "port": MCP_SSE_PORT},
            "mcpo": {"pid": proc.pid, "port": MCPO_PORT},
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@config_bp.route("/ingest", methods=["POST"])
def ingest_database():
    """Accept and ingest a new SQLite database file.

    Supports .sqlite, .gz, .bz2, .xz, and .zip. Replaces the active
    DATABASE atomically.
    """
    from mtg.ingest import IngestError, process_upload

    if not _same_origin_request() or not _csrf_ok():
        abort(403)
    if "database" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["database"]
    if not f.filename:
        return jsonify({"error": "No file selected."}), 400

    ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".last_ingest.json")
    ingest_path = resolve_bind_path(ingest_path)

    try:
        result = process_upload(f, f.filename, DATABASE, ingest_path)
        return jsonify(result)

    except IngestError as e:
        return jsonify({"error": str(e)}), e.status_code

    except Exception as e:
        tb = traceback.format_exc()
        traceback.print_exc()
        return jsonify({"error": str(e), "traceback": tb}), 500
