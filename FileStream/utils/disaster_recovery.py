"""Compatibility facade for FileStream.core.resilience.disaster_recovery.

This module re-exports all symbols from FileStream.core.resilience.disaster_recovery and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.disaster_recovery')
from FileStream.core.resilience.disaster_recovery import *

sys.modules[__name__] = _canonical
