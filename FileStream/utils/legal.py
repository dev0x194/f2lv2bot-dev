"""Compatibility facade for FileStream.bot.helpers.legal.

This module re-exports all symbols from FileStream.bot.helpers.legal and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.helpers.legal')
from FileStream.bot.helpers.legal import *

sys.modules[__name__] = _canonical
