"""Compatibility facade for FileStream.bot.handlers.user.start.

Canonical handler implementation resides in FileStream.bot.handlers.user.start.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.user.start')
from FileStream.bot.handlers.user.start import *

sys.modules[__name__] = _canonical
