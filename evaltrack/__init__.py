import os

from flask import Flask, current_app, g

from .db import connect, init_db


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get("EVALTRACK_DB", "evaltrack.db"),
        SECRET_KEY=os.environ.get("EVALTRACK_SECRET", "dev-only-change-me"),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if config:
        app.config.update(config)

    with app.app_context():
        init_db(get_db())
    app.teardown_appcontext(close_db)

    from . import filters, routes
    filters.register(app)
    app.register_blueprint(routes.bp)
    return app
