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
        self.assertEqual(bot.set_my_commands.await_count, 2)
        public, owner = bot.set_my_commands.await_args_list
        self.assertEqual(
            {c.command for c in public.args[0]},
            {"start", "menu"},
        )
        self.assertNotIn("scope", public.kwargs)
        self.assertEqual(owner.kwargs["scope"].chat_id, 123)
        self.assertEqual(
            {c.command for c in owner.args[0]},
            {"start", "menu"},
        )

    async def test_existing_owner_menu_is_replaced_on_every_startup(self):
        menus = {123: ["admin", "stats", "browse", "profile", "support_inbox"]}

        async def set_commands(commands, scope=None):
            menus[scope.chat_id if scope else "default"] = [
                command.command for command in commands
            ]

        bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=set_commands))
        await configure_command_menu(bot, 123)
        self.assertEqual(menus[123], ["start", "menu"])
        self.assertEqual(menus[123], menus["default"])
        await configure_command_menu(bot, 123)
        self.assertEqual(menus[123], ["start", "menu"])

    async def test_owner_menu_update_failure_is_not_silently_ignored(self):
        bot = SimpleNamespace(
            set_my_commands=AsyncMock(side_effect=[True, RuntimeError("failed")])
        )
        with self.assertRaises(RuntimeError):
            await configure_command_menu(bot, 123)

    async def test_unconfigured_owner_only_gets_public_menu(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await configure_command_menu(bot, None)
        bot.set_my_commands.assert_awaited_once()
        self.assertNotIn("scope", bot.set_my_commands.await_args.kwargs)

    async def test_telegram_failure_is_not_silently_ignored(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError))
        with self.assertRaises(RuntimeError):
            await configure_command_menu(bot, 123)