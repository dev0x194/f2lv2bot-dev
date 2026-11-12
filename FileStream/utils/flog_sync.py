"""Compatibility facade for FileStream.core.runtime.flog_sync.

This module re-exports all symbols from FileStream.core.runtime.flog_sync and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.flog_sync')
from FileStream.core.runtime.flog_sync import *

sys.modules[__name__] = _canonical
