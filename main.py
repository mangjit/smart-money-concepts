"""Railway-compatible ASGI entrypoint.

Railpack detects root-level ``main.py`` files. The application implementation remains
in webui.main so local, Render, and Railway commands share one FastAPI app.
"""

from webui.main import app

__all__ = ["app"]
