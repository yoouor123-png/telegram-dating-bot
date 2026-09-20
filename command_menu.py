"""Public commands plus a private, owner-scoped administration menu."""
from aiogram.types import BotCommand, BotCommandScopeChat

PUBLIC_COMMANDS = [
    ("start", "התחלה"),
    ("menu", "פתיחת התפריט"),
]
ADMIN_COMMANDS = []


async def configure_command_menu(bot, owner_id):
    public = [BotCommand(command=name, description=text) for name, text in PUBLIC_COMMANDS]
    await bot.set_my_commands(public)
    if owner_id is not None and ADMIN_COMMANDS:
        commands = [
            BotCommand(command=name, description=text)
            for name, text in ADMIN_COMMANDS
        ] + public
        # Menu visibility is not authorization; every handler still checks sender.
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=owner_id))