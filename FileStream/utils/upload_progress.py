"""Compatibility facade for FileStream.core.runtime.upload_progress.

This module re-exports all symbols from FileStream.core.runtime.upload_progress and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.upload_progress')
from FileStream.core.runtime.upload_progress import *

sys.modules[__name__] = _canonical
