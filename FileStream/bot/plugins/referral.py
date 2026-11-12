"""Compatibility facade for FileStream.bot.handlers.user.referral.

Canonical handler implementation resides in FileStream.bot.handlers.user.referral.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.user.referral')
from FileStream.bot.handlers.user.referral import *

sys.modules[__name__] = _canonical
