"""Compatibility facade for FileStream.core.resilience.workload.

This module re-exports all symbols from FileStream.core.resilience.workload and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.workload')
from FileStream.core.resilience.workload import *

sys.modules[__name__] = _canonical
