"""Config page — LLM settings, embedding, MCP, database ingest."""

import os
import json
import threading
import signal
import subprocess
import time
import traceback

from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify

from mtg.shared import get_db, resolve_bind_path, DATABASE, db_path
from mtg.prompts import COMMANDER_SYSTEM_PROMPT, DEEPDIVE_SYSTEM_PROMPT, VERIFY_SYSTEM_PROMPT
from mtg.mcp_control import (
    MCP_SSE_PORT, MCPO_PORT, MCP_HOST, MCP_DISPLAY_HOST,
    port_alive, restart_mcp,
)

config_bp = Blueprint("config", __name__, url_prefix="/config")


@config_bp.route("/", methods=["GET", "POST"])
def config_page():
    """LLM configuration — shared across tools that use the LLM."""
    import mtg.embed as embed

    if request.method == "POST":
        api_key = request.form.get("llm_api_key", "").strip()
        if api_key:
            session["llm_api_key"] = api_key
        elif api_key == "" and "llm_api_key" in session:
            session.pop("llm_api_key", None)
        session["llm_backend"] = request.form.get("llm_backend", "").strip()
        session["llm_base_url"] = request.form.get("llm_base_url", "").strip()
        session["llm_model"] = request.form.get("llm_model", "").strip()
        if request.form.get("save_prompt") == "1":
            eval_prompt = request.form.get("eval_prompt", "").strip()
            if eval_prompt:
                session["eval_prompt"] = eval_prompt
            elif "eval_prompt" in session:
                session.pop("eval_prompt", None)
        if request.form.get("save_deepdive_prompt") == "1":
            deepdive_prompt = request.form.get("deepdive_prompt", "").strip()
            if deepdive_prompt:
                session["deepdive_prompt"] = deepdive_prompt
            elif "deepdive_prompt" in session:
                session.pop("deepdive_prompt", None)
        if request.form.get("save_verify_prompt") == "1":
            verify_prompt = request.form.get("verify_prompt", "").strip()
            if verify_prompt:
                session["verify_prompt"] = verify_prompt
            elif "verify_prompt" in session:
                session.pop("verify_prompt", None)
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
        return redirect(url_for("config.config_page"))

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))
    llm_has_key = bool(session.get("llm_api_key") or os.environ.get("LLM_API_KEY"))
    eval_prompt = session.get("eval_prompt", "")
    deepdive_prompt = session.get("deepdive_prompt", "")
    verify_prompt = session.get("verify_prompt", "")
    similar_method = session.get("similar_method", "embed")
    eval_similar_method = session.get("eval_similar_method", "embed")

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
                           similar_method=similar_method,
                           eval_similar_method=eval_similar_method,
                           default_prompt=COMMANDER_SYSTEM_PROMPT,
                           default_deepdive_prompt=DEEPDIVE_SYSTEM_PROMPT,
                           default_verify_prompt=VERIFY_SYSTEM_PROMPT,
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
