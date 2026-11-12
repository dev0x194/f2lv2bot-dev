"""Compatibility facade for FileStream.bot.helpers.broadcast_helper.

This module re-exports all symbols from FileStream.bot.helpers.broadcast_helper and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.helpers.broadcast_helper')
from FileStream.bot.helpers.broadcast_helper import *

sys.modules[__name__] = _canonical
