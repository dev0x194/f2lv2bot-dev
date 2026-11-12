"""Compatibility facade for FileStream.bot.handlers.media.stream.

Canonical handler implementation resides in FileStream.bot.handlers.media.stream.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.media.stream')
from FileStream.bot.handlers.media.stream import *

sys.modules[__name__] = _canonical
