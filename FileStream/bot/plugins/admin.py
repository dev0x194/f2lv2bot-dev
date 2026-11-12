"""Compatibility facade for FileStream.bot.handlers.admin.admin.

Canonical handler implementation resides in FileStream.bot.handlers.admin.admin.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.admin.admin')
from FileStream.bot.handlers.admin.admin import *

sys.modules[__name__] = _canonical
