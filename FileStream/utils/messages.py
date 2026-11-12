"""Compatibility shim for FileStream.utils.messages.

Canonical Telegram bot messages now live in FileStream.bot.messages.
"""

from FileStream.bot.messages import LANG

__all__ = ["LANG"]
