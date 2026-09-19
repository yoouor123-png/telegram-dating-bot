import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from command_menu import ADMIN_COMMANDS, PUBLIC_COMMANDS, configure_command_menu


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_reset_replaces_edit_and_account_deletion_stays_distinct(self):
        commands = dict(PUBLIC_COMMANDS)
        self.assertIn("resetprofile", commands)
        self.assertNotIn("editprofile", commands)
        self.assertEqual(commands["deleteaccount"], "מחיקת חשבון")
        self.assertIn("מחדש", commands["resetprofile"])

    async def test_owner_gets_private_menu_without_exposing_public_admin_commands(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await configure_command_menu(bot, 123)
        public, private = bot.set_my_commands.await_args_list
        admin_names = {name for name, _ in ADMIN_COMMANDS}
        self.assertTrue(admin_names.isdisjoint({c.command for c in public.args[0]}))
        self.assertNotIn("scope", public.kwargs)
        self.assertTrue(admin_names.issubset({c.command for c in private.args[0]}))
        self.assertEqual(private.kwargs["scope"].type, "chat")
        self.assertEqual(private.kwargs["scope"].chat_id, 123)
        self.assertIn("browse", {c.command for c in private.args[0]})

    async def test_unconfigured_owner_only_gets_public_menu(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock())
        await configure_command_menu(bot, None)
        bot.set_my_commands.assert_awaited_once()
        self.assertNotIn("scope", bot.set_my_commands.await_args.kwargs)

    async def test_telegram_failure_is_not_silently_ignored(self):
        bot = SimpleNamespace(set_my_commands=AsyncMock(side_effect=RuntimeError))
        with self.assertRaises(RuntimeError):
            await configure_command_menu(bot, 123)