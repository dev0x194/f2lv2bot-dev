"""Compatibility facade for FileStream.core.runtime.public_links.

This module re-exports all symbols from FileStream.core.runtime.public_links and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.runtime.public_links')
from FileStream.core.runtime.public_links import *

sys.modules[__name__] = _canonical
