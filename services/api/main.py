"""Compatibility entrypoint: uvicorn services.api.main:app."""

from services.backend.api.app import app

__all__ = ["app"]
