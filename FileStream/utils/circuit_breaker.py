"""Compatibility facade for FileStream.core.resilience.circuit_breaker.

This module re-exports all symbols from FileStream.core.resilience.circuit_breaker and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.resilience.circuit_breaker')
from FileStream.core.resilience.circuit_breaker import *

sys.modules[__name__] = _canonical
