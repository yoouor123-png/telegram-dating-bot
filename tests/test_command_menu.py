import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from command_menu import ADMIN_COMMANDS, PUBLIC_COMMANDS, configure_command_menu


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_command_list_is_intentionally_minimal(self):
        commands = dict(PUBLIC_COMMANDS)
        self.assertEqual(set(commands), {"start", "menu"})

    async def test_owner_has_no_extra_visible_slash_commands(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await configure_command_menu(bot, 123)
        bot.set_my_commands.assert_awaited_once()
        public = bot.set_my_commands.await_args
        self.assertEqual(
            {c.command for c in public.args[0]},
            {"start", "menu"},
        )
        self.assertNotIn("scope", public.kwargs)

    async def test_unconfigured_owner_only_gets_public_menu(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await configure_command_menu(bot, None)
        bot.set_my_commands.assert_awaited_once()
        self.assertNotIn("scope", bot.set_my_commands.await_args.kwargs)

    async def test_telegram_failure_is_not_silently_ignored(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError))
        with self.assertRaises(RuntimeError):
            await configure_command_menu(bot, 123)