"""Compatibility facade for FileStream.bot.handlers.user.settings.

Canonical handler implementation resides in FileStream.bot.handlers.user.settings.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.user.settings')
from FileStream.bot.handlers.user.settings import *

sys.modules[__name__] = _canonical
