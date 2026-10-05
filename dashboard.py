"""Compatibility entrypoint; production uses crypto_bot.web.app:create_app."""

from crypto_bot.web.app import create_app
from crypto_bot.web.server import web_bind

if __name__ == "__main__":
    host, port = web_bind()
    create_app().run(host=host, port=port)
