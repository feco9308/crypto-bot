"""Shared bind settings for Flask and Gunicorn; independent of scanner settings."""

import os

from dotenv import load_dotenv


def web_bind(host=None, port=None):
    load_dotenv()
    host = host if host is not None else os.getenv("WEB_HOST", "0.0.0.0")
    port = int(port if port is not None else os.getenv("WEB_PORT", "8000"))
    if not host.strip():
        raise ValueError("WEB_HOST must not be empty")
    if not 1 <= port <= 65535:
        raise ValueError("WEB_PORT must be between 1 and 65535")
    return host, port
