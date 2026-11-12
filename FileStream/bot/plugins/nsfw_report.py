"""Compatibility facade for FileStream.bot.handlers.system.nsfw_report.

Canonical handler implementation resides in FileStream.bot.handlers.system.nsfw_report.
This facade ensures full backward compatibility for existing tests and callers.
"""
import sys
import importlib

_canonical = importlib.import_module('FileStream.bot.handlers.system.nsfw_report')
from FileStream.bot.handlers.system.nsfw_report import *

sys.modules[__name__] = _canonical
