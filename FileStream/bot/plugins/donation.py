"""Compatibility facade for FileStream.bot.handlers.user.donation.

Canonical handler implementation resides in FileStream.bot.handlers.user.donation.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.user.donation')
from FileStream.bot.handlers.user.donation import *

sys.modules[__name__] = _canonical
