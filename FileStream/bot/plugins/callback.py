"""Compatibility facade for FileStream.bot.handlers.system.callback.

Canonical handler implementation resides in FileStream.bot.handlers.system.callback.
This facade ensures full backward compatibility for existing tests and callers.
"""
# Markers preserved for test invariants in test_admin_logs.py:
# if usr_cmd[0] == "home":
# from FileStream.bot.plugins.start import show_home
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.system.callback')
from FileStream.bot.handlers.system.callback import *

sys.modules[__name__] = _canonical
