"""Compatibility entrypoint; production uses crypto_bot.web.app:create_app."""
from crypto_bot.web.app import create_app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=6000)
