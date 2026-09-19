"""Owner moderation tests: real disposable PostgreSQL, mocked Telegram/provider."""
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from aiogram import Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import admin_content as admin
import moderation
import test_moderation as review_tests
import test_support as support_tests
from admin_stats import STATS_SQL
from legal_privacy import anonymize_account, record_current_acceptance, set_profile_visibility


def message(uid=99, chat="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(id=uid, type=chat),
        answer=AsyncMock(), answer_photo=AsyncMock(), text="Explicit reason",
    )


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_handler_denies_before_accessing_state_or_database(self):
        for uid, chat, owner in [(3, "private", 99), (99, "group", 99), (99, "private", None)]:
            dp, get_pool = Dispatcher(), AsyncMock()
            router = admin.register_admin_content(dp, SimpleNamespace(), get_pool, owner)
            state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(), get_state=AsyncMock())
            for handler in router.message.handlers:
                if handler.callback.__name__ == "inbox":
                    continue
                await handler.callback(message(uid, chat), state)
            callback = SimpleNamespace(
                from_user=SimpleNamespace(id=uid), message=message(uid, chat),
                answer=AsyncMock(), data="ac:confirm:forged",
            )
            await router.callback_query.handlers[0].callback(callback, state)
            get_pool.assert_not_awaited()
            state.clear.assert_not_awaited()
            state.get_data.assert_not_awaited()
            state.get_state.assert_not_awaited()

    def test_reason_enum_never_displays_model_prose(self):
        for code in moderation.REASONS:
            verdict = moderation.parse_verdict(
                review_tests.response('{"allowed":false,"reason_code":"' + code + '"}'))
            self.assertEqual(verdict, "rejected")
            self.assertEqual(verdict.reason, code)
            self.assertIn("/editprofile", moderation.reason_message(code))
        self.assertEqual(moderation.parse_verdict(review_tests.response(
            '{"allowed":false,"reason_code":"secret arbitrary output"}')), "unavailable")
        self.assertEqual(moderation.parse_verdict(review_tests.response(
            '{"allowed":true,"reason_code":"sexual"}')), "unavailable")


class PostgreSQLAdminTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(support_tests.PostgreSQLSupportTests.setUpClass.__func__)
    command = classmethod(support_tests.PostgreSQLSupportTests.command.__func__)
    stop_cluster = classmethod(support_tests.PostgreSQLSupportTests.stop_cluster.__func__)
    connect = support_tests.PostgreSQLSupportTests.connect
    close_pool = support_tests.PostgreSQLSupportTests.close_pool

    async def asyncSetUp(self):
        await support_tests.PostgreSQLSupportTests.asyncSetUp(self)
        await self.pool.execute(self.schema_sql)
        for uid, gender, target in [(1, "male", "female"), (2, "female", "male")]:
            await self.pool.execute(
                """INSERT INTO users(telegram_id,full_name,age,gender,target_gender,bio,
                   photos,latitude,longitude,is_premium,premium_until,moderation_status)
                   VALUES($1,'Name',25,$2,$3,'Bio',ARRAY['one','two'],32,34,TRUE,
                   NOW()+INTERVAL '10 days','approved')""", uid, gender, target)
            await record_current_acceptance(self.pool, uid, None, "Name")
        self.bot = SimpleNamespace(send_message=AsyncMock(), get_chat=AsyncMock(), send_photo=AsyncMock())

    async def user(self, uid=1):
        return await self.pool.fetchrow("SELECT * FROM users WHERE telegram_id=$1", uid)

    async def action(self, action, revision=None, reason="Policy violation"):
        if action == "restore":
            hidden_id = await self.pool.fetchval(
                "SELECT id FROM hidden_content WHERE telegram_id=1 ORDER BY id LIMIT 1")
            action = f"restore_{hidden_id or 1}"
        revision = (await self.user())["moderation_revision"] if revision is None else revision
        return await admin.apply_action(self.pool, 1, revision, action, reason, 99)

    async def test_hold_survives_auto_approval_registration_and_resume(self):
        await self.action("hold")
        self.assertEqual(await set_profile_visibility(self.pool, 1, True), "incomplete")
        await self.pool.execute("UPDATE users SET moderation_status='unreviewed' WHERE telegram_id=1")
        with patch.object(moderation, "moderate", AsyncMock(return_value="approved")):
            self.assertEqual(await moderation.review_existing(self.pool, self.bot, 1), "approved")
        self.assertTrue((await self.user())["admin_hold"])
        self.assertEqual(await set_profile_visibility(self.pool, 1, True), "incomplete")
        data = dict(name="Edited", age=25, gender="male", target_gender="female",
                    bio="Corrected", photos=["new"], latitude=32., longitude=34.,
                    profile_revision=(await self.user())["moderation_revision"])
        state = SimpleNamespace(get_data=AsyncMock(return_value=data), clear=AsyncMock())
        event = message(1)
        event.text = "done"
        event.from_user.username = None
        funcs = review_tests.load_functions(
            "finish_photos_or_explain", Message=object, FSMContext=object,
            get_pool=AsyncMock(return_value=self.pool), check_submission=AsyncMock(return_value=True),
            send_db_error=AsyncMock(), ReplyKeyboardRemove=lambda: None, show_next_profile=AsyncMock())
        await funcs.finish_photos_or_explain(event, state)
        self.assertEqual((await self.user())["full_name"], "Edited")
        self.assertTrue((await self.user())["admin_hold"])
        self.assertTrue((await self.user())["is_premium"])
        await set_profile_visibility(self.pool, 1, False)
        await self.action("release")
        self.assertFalse((await self.user())["is_active"])
        self.assertFalse((await self.user())["admin_hold"])

    async def test_removals_completeness_and_full_takedown(self):
        for action, field, expected in [("photo_0", "photos", ["two"]),
                                        ("name", "full_name", ""), ("bio", "bio", "")]:
            await self.action(action)
            self.assertEqual((await self.user())[field], expected)
            self.assertEqual((await self.user())["moderation_status"], "unreviewed")
        self.assertEqual(await set_profile_visibility(self.pool, 1, True), "incomplete")
        await self.action("takedown")
        self.assertTrue((await self.user())["admin_hold"])
        self.assertTrue((await self.user())["is_premium"])
        self.assertIsNotNone((await self.user())["premium_until"])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM content_events"), 4)

    async def test_hidden_restore_requires_fresh_review_and_revision(self):
        await self.action("hide_photo_0")
        self.assertEqual((await self.user())["photos"], ["two"])
        await self.action("restore")
        self.assertEqual((await self.user())["photos"], ["one", "two"])
        self.assertEqual((await self.user())["moderation_status"], "unreviewed")
        self.assertEqual(await set_profile_visibility(self.pool, 1, True), "incomplete")
        with patch.object(moderation, "moderate", AsyncMock(
                return_value=moderation.Verdict("rejected", "revealing"))):
            await moderation.review_existing(self.pool, self.bot, 1)
        self.assertEqual((await self.user())["moderation_reason"], "revealing")
        for action, field in [("hide_name", "full_name"), ("hide_bio", "bio")]:
            await self.action(action)
            self.assertEqual((await self.user())[field], "")
            await self.action("restore")
            self.assertTrue((await self.user())[field])
        await self.action("hide_name")
        revision = (await self.user())["moderation_revision"]
        await self.pool.execute(
            "UPDATE users SET moderation_revision=moderation_revision+1,full_name='New' WHERE telegram_id=1")
        with self.assertRaises(ValueError):
            await self.action("restore", revision)
        with self.assertRaises(ValueError):
            await self.action("restore")
        self.assertEqual((await self.user())["full_name"], "New")

    async def test_independent_hidden_items_survive_admin_actions(self):
        await self.action("hide_name")
        await self.action("hide_bio")
        await self.action("hide_photo_0")
        await self.action("hold")
        await self.action("release")
        items = await self.pool.fetch("SELECT * FROM hidden_content ORDER BY id")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[2]["value"], "one")
        await self.action("photo_0")  # permanently remove the OTHER photo
        for item in items:
            await self.action(f"restore_{item['id']}")
        row = await self.user()
        self.assertEqual(row["full_name"], "Name")
        self.assertEqual(row["bio"], "Bio")
        self.assertEqual(row["photos"], ["one"])  # never resurrect removed 'two'
        self.assertEqual(row["moderation_status"], "unreviewed")

    async def test_reason_delivery_failure_durable_and_not_retried(self):
        with self.assertRaises(ValueError):
            await self.action("hold", reason=" ")
        self.assertFalse((await self.user())["admin_hold"])
        event_id = await self.action("hold", reason="Specific explicit reason")
        self.bot.send_message.side_effect = TimeoutError()
        self.assertEqual(await admin.deliver(self.pool, self.bot, event_id), "uncertain")
        self.assertEqual(await admin.deliver(self.pool, self.bot, event_id), "uncertain")
        self.bot.send_message.assert_awaited_once()
        event = message(1)
        await admin.show_notices(event, self.pool, 1)
        text = "\n".join(call.args[0] for call in event.answer.call_args_list)
        for item in ("Specific explicit reason", "/support", "/editprofile"):
            self.assertIn(item, text)
        self.assertEqual(await self.pool.fetchval(
            "SELECT delivery FROM content_events WHERE id=$1", event_id), "uncertain")

    async def test_delete_cleans_private_audit_and_cannot_restore(self):
        await self.action("hide_bio")
        rev = (await self.user())["moderation_revision"]
        await anonymize_account(self.pool, 1)
        for table in ("content_events", "hidden_content", "submission_rejections"):
            self.assertEqual(await self.pool.fetchval(f"SELECT count(*) FROM {table}"), 0)
        with self.assertRaises(ValueError):
            await self.action("restore", rev)
        self.assertTrue((await self.user())["is_premium"])
        self.assertEqual((await self.user())["full_name"], "נמחק")

    async def test_discovery_actions_notifications_and_stats_exclude_hold(self):
        await self.action("hold")
        f = review_tests.load_functions(
            "next_candidate", "register_action", "visible_profile", "send_match_contact",
            get_pool=AsyncMock(return_value=self.pool), bot=self.bot,
            premium_is_active=lambda _: True, datetime=datetime, timezone=timezone,
            Optional=object, user_link=lambda *_: "", fetch_user=self.user,
            logger=SimpleNamespace(warning=lambda *_: None))
        for viewer, target in ((1, 2), (2, 1)):
            self.assertIsNone(await f.next_candidate(viewer))
            self.assertEqual(await f.register_action(viewer, target, "yes"), "missing_user")
            self.assertEqual(await f.register_action(viewer, target, "no"), "missing_user")
            await f.send_match_contact(viewer, {"telegram_id": target})
        self.bot.send_message.assert_not_awaited()
        stats = await self.pool.fetchrow(STATS_SQL)
        self.assertEqual(stats["active"], 1)
        self.assertEqual(stats["total"], 2)

    async def test_confirmation_and_reason_reject_revised_profile(self):
        dp = Dispatcher()
        router = admin.register_admin_content(dp, self.bot, AsyncMock(return_value=self.pool), 99)
        state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=99, user_id=99))
        callback = router.callback_query.handlers[0].callback
        reason = next(h.callback for h in router.message.handlers if h.callback.__name__ == "reason")
        event = SimpleNamespace(from_user=SimpleNamespace(id=99), message=message(),
                                answer=AsyncMock(), data="ac:act:1:0:hold")
        await callback(event, state)
        await self.pool.execute("UPDATE users SET moderation_revision=1 WHERE telegram_id=1")
        await reason(message(), state)
        self.assertIsNone(await state.get_state())
        event.data = "ac:act:1:1:hold"
        await callback(event, state)
        await reason(message(), state)
        self.assertFalse((await self.user())["admin_hold"])
        event.data = (await state.get_data())["admin_confirmation"]
        await self.pool.execute("UPDATE users SET moderation_revision=2 WHERE telegram_id=1")
        await callback(event, state)
        self.assertFalse((await self.user())["admin_hold"])
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM content_events"), 0)

    async def test_pending_registration_rejection_is_reason_only_and_durable(self):
        event = message(44)
        f = review_tests.load_functions(
            "check_submission", get_pool=AsyncMock(return_value=self.pool),
            moderate=AsyncMock(return_value=moderation.Verdict("rejected", "offensive")),
            report_failure=AsyncMock(), bot=self.bot)
        await record_current_acceptance(self.pool, 44, None, "ignored")
        self.assertFalse(await f.check_submission(event, name="Unstored rejected input"))
        self.assertEqual(await self.pool.fetchval(
            "SELECT reason FROM submission_rejections WHERE telegram_id=44"), "offensive")
        await admin.show_notices(event, self.pool, 44)
        self.assertIn("/editprofile", "\n".join(c.args[0] for c in event.answer.call_args_list))