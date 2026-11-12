"""Compatibility facade for FileStream.core.monitoring.metrics.

This module re-exports all symbols from FileStream.core.monitoring.metrics and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.metrics')
from FileStream.core.monitoring.metrics import *

sys.modules[__name__] = _canonical
