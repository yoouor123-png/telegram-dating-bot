import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aiogram import Dispatcher
from navigation import (
    KNOWN_TEXT, OWNER_ACTIONS, main_keyboard, register_action,
    register_navigation,
)


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def test_main_keyboard_is_two_by_two_and_owner_only_row(self):
        public = main_keyboard(False).keyboard
        owner = main_keyboard(True).keyboard
        self.assertEqual([len(row) for row in public], [2, 2])
        self.assertEqual([len(row) for row in owner], [2, 2, 1])
        self.assertNotIn("🛠 הנהלה", {b.text for row in public for b in row})
        self.assertEqual(owner[-1][0].text, "🛠 הנהלה")

    def test_bare_and_labelled_main_inputs_are_known(self):
        for text in ("🔍", "🔍 פרופילים", "👤", "👤 החשבון שלי",
                     "💎", "💎 פרימיום", "📋", "📋 אחר", "🛠", "🛠 הנהלה"):
            self.assertIn(text, KNOWN_TEXT)

    async def test_callback_uses_callback_actor_not_bot_message_actor(self):
        seen = []

        async def action(message, state):
            seen.append(message.from_user.id)

        register_action("profile", action)
        dp = Dispatcher()
        router = register_navigation(dp, 99)
        handler = next(
            h.callback for h in router.callback_query.handlers
            if h.callback.__name__ == "action_callback"
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=777),
            model_copy=lambda update: SimpleNamespace(
                from_user=update["from_user"], answer=AsyncMock(),
                chat=SimpleNamespace(type="private"),
            ),
            answer=AsyncMock(),
            chat=SimpleNamespace(type="private"),
        )
        callback = SimpleNamespace(
            data="nav:action:profile", from_user=SimpleNamespace(id=42),
            message=message, answer=AsyncMock(),
        )
        state = SimpleNamespace(get_state=AsyncMock(return_value=None))
        await handler(callback, state)
        self.assertEqual(seen, [42])

    async def test_forged_owner_callback_denied_before_action(self):
        for command in OWNER_ACTIONS:
            called = AsyncMock()
            register_action(command, called)
            dp = Dispatcher()
            router = register_navigation(dp, 99)
            handler = next(
                h.callback for h in router.callback_query.handlers
                if h.callback.__name__ == "action_callback"
            )
            message = SimpleNamespace(
                answer=AsyncMock(), chat=SimpleNamespace(type="private"),
                model_copy=lambda update: None,
            )
            callback = SimpleNamespace(
                data=f"nav:action:{command}",
                from_user=SimpleNamespace(id=42), message=message,
                answer=AsyncMock(),
            )
            await handler(
                callback, SimpleNamespace(get_state=AsyncMock(return_value=None))
            )
            called.assert_not_awaited()

    async def test_active_state_is_preserved_and_context_keyboard_not_replaced(self):
        called = AsyncMock()
        register_action("browse", called)
        dp = Dispatcher()
        router = register_navigation(dp, 99)
        handler = next(
            h.callback for h in router.message.handlers
            if h.callback.__name__ == "navigation_text"
        )
        message = SimpleNamespace(
            text="🔍", from_user=SimpleNamespace(id=42),
            answer=AsyncMock(), chat=SimpleNamespace(type="private"),
        )
        state = SimpleNamespace(
            get_state=AsyncMock(return_value="Registration:name"),
            update_data=AsyncMock(),
        )
        await handler(message, state)
        called.assert_not_awaited()
        markup = message.answer.await_args.kwargs["reply_markup"]
        self.assertTrue(hasattr(markup, "inline_keyboard"))
