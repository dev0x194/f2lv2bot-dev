"""Bot clients management package."""
import sys
from . import manager

# Alias package to manager module so monkeypatches on FileStream.bot.clients
# (e.g. in tests) directly patch manager attributes seamlessly.
sys.modules[__name__] = manager

from .manager import *
from .manager import (
    initialize_clients,
    is_secondary_worker,
    setup_secondary_worker_handlers,
    clone_primary_handlers,
    log_client_diagnostics,
    Client,
    multi_clients,
    work_loads,
    FileStream,
)

__all__ = [
    "initialize_clients",
    "is_secondary_worker",
    "setup_secondary_worker_handlers",
    "clone_primary_handlers",
    "log_client_diagnostics",
    "Client",
    "multi_clients",
    "work_loads",
    "FileStream",
]
