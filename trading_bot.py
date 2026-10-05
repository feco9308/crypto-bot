"""Compatibility entrypoint for the public market scanner."""

import sys

from crypto_bot.main import main

if __name__ == "__main__":
    sys.argv.insert(1, "scan")
    main()
