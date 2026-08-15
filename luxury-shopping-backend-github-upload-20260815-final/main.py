"""Compatibility entrypoint for existing uvicorn and hosting commands."""

try:
    from backend.app.main import app
except ModuleNotFoundError:
    from app.main import app


__all__ = ["app"]
