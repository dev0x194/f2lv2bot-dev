"""Compatibility facade for FileStream.bot.helpers.bot_utils.

This module re-exports all symbols from FileStream.bot.helpers.bot_utils and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.helpers.bot_utils')
from FileStream.bot.helpers.bot_utils import *

sys.modules[__name__] = _canonical
