import inspect
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import navigation


class Connection:
    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        if "target_active" in query:
            return None
        if "SELECT is_active FROM users" in query:
            return True
        return None

    async def fetchrow(self, query, *args):
        return {
            "is_premium": False,
            "premium_until": None,
            "daily_likes_count": 0,
            "last_like_reset": None,
        }

    async def execute(self, query, *args):
        return "OK"


class Pool:
    def __init__(self):
        self.connection = Connection()

    def acquire(self):
        return self.connection


class MainBootstrapRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_does_not_shadow_profile_business_action(self):
        business_action = main.register_action
        main.register_handlers()

        self.assertIs(main.register_action, business_action)
        self.assertTrue(inspect.iscoroutinefunction(main.register_action))
        self.assertEqual(
            list(inspect.signature(main.register_action).parameters),
            ["from_user", "to_user", "action"],
        )
        self.assertTrue({
            "browse", "start", "profile", "matches", "resetprofile", "help",
        }.issubset(navigation._actions))

        with patch.object(main, "get_pool", return_value=Pool()):
            self.assertEqual(
                await main.register_action(42, 84, "no"),
                "saved",
            )
            self.assertEqual(
                await main.register_action(43, 85, "yes"),
                "saved",
            )

        # The real profile callback resolves the unchanged three-argument
        # business function from its module globals after bootstrap.
        self.assertIs(main.profile_action.__globals__["register_action"],
                      business_action)
