"""Self-hosted MTG card search — Scryfall replica.

ponytail: single-file Flask app, SQLite, no ORM, no JS, no build step.
ponytail: global lock via SQLite single-writer (we're read-only, irrelevant).
ponytail: routes split into blueprint modules under mtg/blueprints/.
"""

import os

from flask import Flask, g, session, request

from mtg.shared import (
    resolve_bind_path,
    card_image, render_mana, pagination_url,
)
from mtg.similarity import similarity_label

app = Flask(__name__)

# ponytail: persist secret key so sessions survive restarts
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    _key_path = resolve_bind_path(_key_path)
    try:
        with open(_key_path, "rb") as f:
            _secret_key = f.read()
    except (FileNotFoundError, IsADirectoryError):
        _secret_key = os.urandom(24)
        _wrote = False
        try:
            with open(_key_path, "wb") as f:
                f.write(_secret_key)
            _wrote = True
        except (OSError, IsADirectoryError):
            pass
        if not _wrote:
            import warnings
            warnings.warn("Could not persist secret key — sessions will not survive restarts.")
app.secret_key = _secret_key

app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600  # 30 days


@app.before_request
def _mark_session_permanent():
    session.permanent = True


@app.after_request
def _add_security_headers(response):
    if request.args.get("embed") == "1":
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline'; font-src 'self' https://cdn.jsdelivr.net; "
        "frame-ancestors 'self'"
    )
    return response


# --- Jinja globals ---

app.jinja_env.globals.update(
    card_image=card_image,
    render_mana=render_mana,
    pagination_url=pagination_url,
    similarity_label=similarity_label,
)


# --- DB teardown ---

@app.teardown_appcontext
def _close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def _inject_db_state():
    """ponytail: db_unseeded=False by default. Routes that detect a missing DB set it True."""
    return {"db_unseeded": False}


# --- Blueprints ---

from mtg.blueprints.search import search_bp
from mtg.blueprints.similar import similar_bp
from mtg.blueprints.config import config_bp
from mtg.blueprints.eval import eval_bp
from mtg.blueprints.eval_landing import eval_landing_bp
from mtg.blueprints.decks import decks_bp
from mtg.blueprints.templates_bp import templates_bp
from mtg.blueprints.tags import tags_bp

app.register_blueprint(search_bp)
app.register_blueprint(similar_bp)
app.register_blueprint(config_bp)
app.register_blueprint(eval_bp)
app.register_blueprint(eval_landing_bp)
app.register_blueprint(decks_bp)
app.register_blueprint(templates_bp)
app.register_blueprint(tags_bp)


# --- Run ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
