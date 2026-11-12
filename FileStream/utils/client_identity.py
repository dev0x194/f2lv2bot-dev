"""Compatibility facade for FileStream.bot.helpers.client_identity.

This module re-exports all symbols from FileStream.bot.helpers.client_identity and synchronizes
with sys.modules to preserve 100% backward compatibility for existing callers and tests.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.helpers.client_identity')
from FileStream.bot.helpers.client_identity import *

sys.modules[__name__] = _canonical
