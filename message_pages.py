"""Small, lossless pages for non-policy Telegram messages."""
import textwrap

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MOBILE_LINE_WIDTH = 28
MAX_MESSAGE_LINES = 3


def short_pages(text, *, heading=None, width=MOBILE_LINE_WIDTH):
    """Return pages of at most three short lines without dropping user text."""
    body = " ".join(str(text or "").split())
    combined = f"{heading} {body}" if heading else body
    lines = textwrap.wrap(
        combined, width=width, break_long_words=True, break_on_hyphens=False,
    ) or [""]
    return [
        "\n".join(lines[index:index + MAX_MESSAGE_LINES])
        for index in range(0, len(lines), MAX_MESSAGE_LINES)
    ]


def page_keyboard(callback_prefix, page, total, extra_rows=()):
    rows = []
    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(
            text="הקודם", callback_data=f"{callback_prefix}:{page - 1}",
        ))
    if page + 1 < total:
        navigation.append(InlineKeyboardButton(
            text="הבא", callback_data=f"{callback_prefix}:{page + 1}",
        ))
    if navigation:
        rows.append(navigation)
    rows.extend(extra_rows)
    return InlineKeyboardMarkup(inline_keyboard=rows)