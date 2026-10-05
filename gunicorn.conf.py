"""Production web defaults; WEB_HOST / WEB_PORT may be set in .env or env."""

from crypto_bot.web.server import web_bind

host, port = web_bind()
bind = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
workers = 2
