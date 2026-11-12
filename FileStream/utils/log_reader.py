"""Compatibility facade for FileStream.core.monitoring.log_reader.

This module re-exports all symbols from FileStream.core.monitoring.log_reader and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.monitoring.log_reader')
from FileStream.core.monitoring.log_reader import *

sys.modules[__name__] = _canonical
