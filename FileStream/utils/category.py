"""Compatibility facade for FileStream.core.media.category.

This module re-exports all symbols from FileStream.core.media.category and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.media.category')
from FileStream.core.media.category import *

sys.modules[__name__] = _canonical
