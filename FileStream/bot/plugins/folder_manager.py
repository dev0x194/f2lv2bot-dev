"""Compatibility facade for FileStream.bot.handlers.folder.folder_manager.

Canonical handler implementation resides in FileStream.bot.handlers.folder.folder_manager.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.folder.folder_manager')
from FileStream.bot.handlers.folder.folder_manager import *

sys.modules[__name__] = _canonical
