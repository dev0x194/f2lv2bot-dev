"""Compatibility facade for FileStream.core.media.file_properties.

This module re-exports all symbols from FileStream.core.media.file_properties and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.core.media.file_properties')
from FileStream.core.media.file_properties import *

sys.modules[__name__] = _canonical
