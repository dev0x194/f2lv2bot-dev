"""Compatibility facade for FileStream.core.resilience.floodwait.

This module re-exports all symbols from FileStream.core.resilience.floodwait and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.floodwait')
from FileStream.core.resilience.floodwait import *

sys.modules[__name__] = _canonical
