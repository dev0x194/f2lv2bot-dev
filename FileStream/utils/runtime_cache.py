"""Compatibility facade for FileStream.core.runtime.runtime_cache.

This module re-exports all symbols from FileStream.core.runtime.runtime_cache and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.runtime_cache')
from FileStream.core.runtime.runtime_cache import *

sys.modules[__name__] = _canonical
