import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from legal_privacy import (  # noqa: E402
    CURRENT_PRIVACY_VERSION,
    CURRENT_TERMS_VERSION,
    anonymize_account,
    cancel_then_anonymize,
    coarse_distance_text,
    consent_text,
    create_safety_report,
    export_chunks,
    has_current_acceptance,
    is_private_event,
    record_current_acceptance,
    set_profile_visibility,
    POLICIES,
)


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, value=None, values=None):
        self.value = value
        self.values = list(values) if values is not None else None
        self.calls = []

    def transaction(self):
        return Transaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if self.values is not None:
            return self.values.pop(0) if self.values else None
        return self.value


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return Acquire(self.connection)


class LegalTextTests(unittest.TestCase):
    def test_filter_copy_allows_normal_clothing_and_keeps_child_safety(self):
        terms = POLICIES["terms"]
        self.assertIn("עירום ותוכן המקדם הימורים", terms)
        self.assertIn("אינם סיבה לחסימה כשאין עירום", terms)
        self.assertIn("הגנת קטינים נשמרת", terms)
        self.assertNotIn("אסור להעלות תוכן מיני, חושפני", terms)

    def test_distance_is_always_coarse(self):
        self.assertEqual(coarse_distance_text(0.3), "עד 5 ק״מ")
        self.assertEqual(coarse_distance_text(4.99), "עד 5 ק״מ")
        self.assertEqual(coarse_distance_text(5), "5–10 ק״מ")
        self.assertEqual(coarse_distance_text(18.2), "15–20 ק״מ")

    def test_consent_is_versioned_and_explicit(self):
        text = consent_text()
        self.assertIn("18", text)
        self.assertIn("בישראל", text)
        self.assertIn("התנאים", text)
        self.assertIn("הפרטיות", text)
        self.assertLessEqual(len(text.splitlines()), 3)
        self.assertIn(CURRENT_TERMS_VERSION, POLICIES["terms"])
        self.assertIn(CURRENT_PRIVACY_VERSION, POLICIES["privacy"])
        self.assertIn("מיקום", POLICIES["privacy"])
        self.assertIn("מגדר", POLICIES["privacy"])
        self.assertIn("טיוטה חלקית", POLICIES["privacy"])

    def test_export_chunks_never_silently_truncates(self):
        lines = ["header", "x" * 25, "tail"]
        chunks = export_chunks(lines, limit=10)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), "".join(lines))

    def test_private_only_check(self):
        private = SimpleNamespace(chat=SimpleNamespace(type="private"))
        group = SimpleNamespace(chat=SimpleNamespace(type="group"))
        callback = SimpleNamespace(
            message=SimpleNamespace(chat=SimpleNamespace(type="private")))
        self.assertTrue(is_private_event(private))
        self.assertTrue(is_private_event(callback))
        self.assertFalse(is_private_event(group))


class PersistenceHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_acceptance_uses_exact_current_versions(self):
        connection = FakeConnection(value=1)
        pool = FakePool(connection)
        self.assertTrue(await has_current_acceptance(pool, 77))
        _, _, args = connection.calls[0]
        self.assertEqual(args, (77, CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION))

        connection.calls.clear()
        await record_current_acceptance(pool, 77, "ignored", "ignored")
        self.assertEqual(len(connection.calls), 1)
        self.assertEqual(
            connection.calls[0][2],
            (77, CURRENT_TERMS_VERSION, CURRENT_PRIVACY_VERSION),
        )
        self.assertNotIn("INSERT INTO users", connection.calls[0][1])

    async def test_anonymization_preserves_user_and_payment_rows(self):
        connection = FakeConnection(value=77)
        result = await anonymize_account(FakePool(connection), 77)
        self.assertEqual(result, 77)
        sql = "\n".join(call[1] for call in connection.calls)
        self.assertIn("DELETE FROM matches", sql)
        self.assertIn("DELETE FROM interactions", sql)
        self.assertIn("DELETE FROM blocked_users", sql)
        self.assertIn("DELETE FROM support_tickets", sql)
        self.assertIn("UPDATE users", sql)
        self.assertNotIn("DELETE FROM users", sql)
        self.assertNotIn("DELETE FROM payments", sql)
        self.assertIn("latitude=NULL", sql)
        self.assertIn("photos='{}'", sql)

    async def test_failed_subscription_cancellation_never_anonymizes(self):
        connection = FakeConnection(values=[1, 1])
        bot = SimpleNamespace()
        with (patch("premium_service.PremiumService.bootstrap", new_callable=AsyncMock),
              patch("premium_service.PremiumService.cancel_legacy", new_callable=AsyncMock)):
            result = await cancel_then_anonymize(bot, FakePool(connection), 77)
        self.assertEqual(result, "cancel_failed")
        sql = "\n".join(call[1] for call in connection.calls)
        self.assertNotIn("UPDATE users SET username=NULL", sql)
        self.assertNotIn("DELETE FROM matches", sql)

    async def test_resume_rejects_deleted_or_incomplete_profile(self):
        connection = FakeConnection(value=None)
        result = await set_profile_visibility(FakePool(connection), 77, True)
        self.assertEqual(result, "incomplete")
        query = connection.calls[0][1]
        self.assertIn("full_name<>'נמחק'", query)
        self.assertIn("cardinality(photos)>0", query)
        self.assertIn("latitude IS NOT NULL", query)

    async def test_report_blocks_pair_and_removes_old_disclosures(self):
        connection = FakeConnection(values=[42])
        ticket_id = await create_safety_report(
            FakePool(connection), 10, 20, 10, 900
        )
        self.assertEqual(ticket_id, 42)
        sql = "\n".join(call[1] for call in connection.calls)
        self.assertIn("INSERT INTO blocked_users", sql)
        self.assertIn("DELETE FROM matches", sql)
        self.assertIn("DELETE FROM interactions", sql)
        self.assertIn("INSERT INTO support_tickets", sql)


if __name__ == "__main__":
    unittest.main()