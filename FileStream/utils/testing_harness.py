"""Compatibility facade for FileStream.core.resilience.testing_harness.

This module re-exports all symbols from FileStream.core.resilience.testing_harness and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.testing_harness')
from FileStream.core.resilience.testing_harness import *

sys.modules[__name__] = _canonical
