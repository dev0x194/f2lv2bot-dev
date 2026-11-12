"""Compatibility facade for FileStream.bot.helpers.bot_commands.

This module re-exports all symbols from FileStream.bot.helpers.bot_commands and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.helpers.bot_commands')
from FileStream.bot.helpers.bot_commands import *

sys.modules[__name__] = _canonical
