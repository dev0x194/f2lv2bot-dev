"""Compatibility facade for FileStream.bot.handlers.user.stats.

Canonical handler implementation resides in FileStream.bot.handlers.user.stats.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.user.stats')
from FileStream.bot.handlers.user.stats import *

sys.modules[__name__] = _canonical
