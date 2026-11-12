"""Compatibility facade for FileStream.core.resilience.load_shedding.

This module re-exports all symbols from FileStream.core.resilience.load_shedding and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.load_shedding')
from FileStream.core.resilience.load_shedding import *

sys.modules[__name__] = _canonical
