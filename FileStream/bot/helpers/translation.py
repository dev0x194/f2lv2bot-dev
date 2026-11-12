"""Bot UI inline keyboard button helpers.

Canonical home for bot menu and modal buttons.
"""

from FileStream.bot.messages import LANG
from FileStream.bot.helpers.legal import build_policy_url
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton


from FileStream.bot.helpers.ui import (
    make_home_keyboard,
    make_help_keyboard,
    make_about_keyboard,
    make_legal_keyboard,
    make_ping_keyboard,
)


class BUTTON(object):
    @staticmethod
    def start_buttons(bot=None):
        return make_home_keyboard(bot)

    @staticmethod
    def help_buttons(bot=None):
        return make_help_keyboard(bot)

    @staticmethod
    def about_buttons(bot=None):
        return make_about_keyboard(bot)

    @staticmethod
    def legal_buttons(bot=None, command="legal"):
        return make_legal_keyboard(bot, command=command)

    @staticmethod
    def ping_buttons(bot=None):
        return make_ping_keyboard(bot)

    START_BUTTONS = start_buttons()
    HELP_BUTTONS = help_buttons()
    ABOUT_BUTTONS = about_buttons()
    LEGAL_BUTTONS = legal_buttons()
    PING_BUTTONS = ping_buttons()

