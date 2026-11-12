"""Legacy entrypoint compatibility.

Some deployments still run: python FileStream/main.py
This module proxies execution to FileStream.__main__.
"""

import os
import sys
import logging
import traceback
from dotenv import load_dotenv

# Ensure stdout/stderr flush immediately in Docker and non-interactive environments
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_file = os.path.join(_root_dir, ".env")
if os.path.isfile(_env_file):
    load_dotenv(dotenv_path=_env_file)
elif os.path.isfile(".env"):
    load_dotenv(dotenv_path=".env")

from FileStream.server import web_server
app = web_server()

import asyncio
from FileStream.__main__ import cleanup, start_services, main


try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        logging.critical("Fatal error running FileStream: %s", traceback.format_exc())

