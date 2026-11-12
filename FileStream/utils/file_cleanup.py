"""Compatibility facade for FileStream.core.runtime.file_cleanup.

This module re-exports all symbols from FileStream.core.runtime.file_cleanup and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.file_cleanup')
from FileStream.core.runtime.file_cleanup import *

sys.modules[__name__] = _canonical
