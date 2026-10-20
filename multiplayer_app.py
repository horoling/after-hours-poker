"""Standalone private-room server; excludes the single-player debug APIs."""
import os
from pathlib import Path

from flask import Flask, jsonify, redirect
from private_rooms import rooms


def create_app(config=None):
    app = Flask(__name__, static_folder=str(Path(__file__).parent / "web"), static_url_path="/assets")
    app.config.update(MAX_CONTENT_LENGTH=8192, SEND_FILE_MAX_AGE_DEFAULT=0)
    if config:
        app.config.update(config)
    app.register_blueprint(rooms)

    @app.get("/")
    def index():
        return redirect("/friends")

    @app.get("/healthz")
    def health():
        return jsonify(status="ok")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8780)), threaded=True)
