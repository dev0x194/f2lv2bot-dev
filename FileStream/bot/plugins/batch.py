"""Compatibility facade for FileStream.bot.handlers.media.batch.

Canonical handler implementation resides in FileStream.bot.handlers.media.batch.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.media.batch')
from FileStream.bot.handlers.media.batch import *

sys.modules[__name__] = _canonical
