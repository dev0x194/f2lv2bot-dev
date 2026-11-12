"""Compatibility facade for FileStream.core.security.nsfw.

This module re-exports all symbols from FileStream.core.security.nsfw and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.security.nsfw')
from FileStream.core.security.nsfw import *

sys.modules[__name__] = _canonical
