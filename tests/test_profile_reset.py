import ast
import pathlib
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from profile_reset import reset_profile
from legal_privacy import CURRENT_PRIVACY_VERSION, CURRENT_TERMS_VERSION
import test_support as support_tests


ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_function(name, **namespace):
    tree = ast.parse((ROOT / "main.py").read_text())
    node = next(
        item for item in tree.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == name
    )
    node.decorator_list = []
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"),
        namespace,
    )
    return namespace[name]


class PostgreSQLProfileResetTests(unittest.IsolatedAsyncioTestCase):
    """Reset behavior against a disposable local PostgreSQL cluster."""

    setUpClass = classmethod(
        support_tests.PostgreSQLSupportTests.setUpClass.__func__
    )
    command = classmethod(
        support_tests.PostgreSQLSupportTests.command.__func__
    )
    stop_cluster = classmethod(
        support_tests.PostgreSQLSupportTests.stop_cluster.__func__
    )
    connect = support_tests.PostgreSQLSupportTests.connect
    close_pool = support_tests.PostgreSQLSupportTests.close_pool

    async def asyncSetUp(self):
        await support_tests.PostgreSQLSupportTests.asyncSetUp(self)
        await self.pool.execute(self.schema_sql)
        for uid in (1, 2):
            await self.pool.execute(
                """INSERT INTO users
                   (telegram_id,username,full_name,age,gender,target_gender,bio,
                    photos,latitude,longitude,is_premium,premium_until,is_active,
                    moderation_status,moderation_revision)
                   VALUES ($1,$2,'Old name',30,'male','female','Old bio',
                    ARRAY['old-photo'],32,34,TRUE,NOW()+INTERVAL '7 days',
                    FALSE,'rejected',4)""",
                uid,
                f"user{uid}",
            )

    async def test_atomic_reset_preserves_entitlements_safety_and_hold(self):
        await self.pool.execute(
            """UPDATE users SET daily_likes_count=7,
               last_like_reset=NOW()-INTERVAL '3 hours' WHERE telegram_id=1"""
        )
        limit_before = await self.pool.fetchrow(
            """SELECT daily_likes_count,last_like_reset
               FROM users WHERE telegram_id=1"""
        )
        await self.pool.execute(
            """UPDATE users SET admin_hold=TRUE,moderation_reason='sanction'
               WHERE telegram_id=1"""
        )
        await self.pool.execute(
            """INSERT INTO policy_acceptances
               VALUES (1,'terms','privacy',NOW())"""
        )
        await self.pool.execute(
            """INSERT INTO payments
               (telegram_id,payload,currency,amount,telegram_payment_charge_id,
                premium_until)
               VALUES (1,'paid','XTR',250,'charge-1',NOW()+INTERVAL '7 days')"""
        )
        await self.pool.execute(
            """INSERT INTO premium_cancellations
               (charge_id,telegram_id,status) VALUES ('legacy',1,'canceled')"""
        )
        await self.pool.execute(
            "INSERT INTO blocked_users(blocker_id,blocked_id) VALUES (1,2)"
        )
        await self.pool.execute(
            """INSERT INTO support_tickets
               (telegram_id,chat_id,message_id,category,details)
               VALUES (1,1,10,'general','safety report')"""
        )
        await self.pool.execute(
            "INSERT INTO interactions(from_user,to_user,action) VALUES (1,2,'yes')"
        )
        await self.pool.execute(
            "INSERT INTO matches(user_a,user_b) VALUES (1,2)"
        )
        await self.pool.execute(
            "INSERT INTO hidden_content(telegram_id,revision,field,value) "
            "VALUES (1,4,'bio','hidden')"
        )
        await self.pool.execute(
            "INSERT INTO submission_rejections VALUES (1,'offensive')"
        )
        await self.pool.execute(
            """INSERT INTO content_events
               (telegram_id,actor_id,action,reason,notice,revision)
               VALUES (1,9,'hide','old','old notice',4)"""
        )

        self.assertEqual(await reset_profile(self.pool, 1, 4), "reset")
        row = await self.pool.fetchrow(
            "SELECT * FROM users WHERE telegram_id=1"
        )
        self.assertEqual(row["username"], "user1")
        self.assertEqual(row["full_name"], "")
        self.assertEqual(row["bio"], "")
        self.assertEqual(list(row["photos"]), [])
        self.assertIsNone(row["latitude"])
        self.assertFalse(row["is_active"])
        self.assertEqual(row["moderation_status"], "unreviewed")
        self.assertEqual(row["moderation_revision"], 5)
        self.assertTrue(row["admin_hold"])
        self.assertEqual(row["moderation_reason"], "sanction")
        self.assertTrue(row["is_premium"])
        self.assertGreater(row["premium_until"], datetime.now(timezone.utc))
        self.assertEqual(row["daily_likes_count"], limit_before["daily_likes_count"])
        self.assertEqual(row["last_like_reset"], limit_before["last_like_reset"])

        for table in (
            "interactions", "matches", "hidden_content",
            "submission_rejections", "content_events",
        ):
            self.assertEqual(
                await self.pool.fetchval(f"SELECT count(*) FROM {table}"), 0
            )
        for table in (
            "payments", "premium_cancellations", "policy_acceptances",
            "blocked_users", "support_tickets",
        ):
            self.assertEqual(
                await self.pool.fetchval(f"SELECT count(*) FROM {table}"), 1
            )

    async def test_stale_double_missing_and_deleted_rows(self):
        self.assertEqual(await reset_profile(self.pool, 1, 3), "stale")
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT full_name FROM users WHERE telegram_id=1"
            ),
            "Old name",
        )
        self.assertEqual(await reset_profile(self.pool, 1, 4), "reset")
        self.assertEqual(await reset_profile(self.pool, 1, 4), "stale")
        self.assertEqual(await reset_profile(self.pool, 99, -1), "missing")
        self.assertEqual(await reset_profile(self.pool, 99, 0), "stale")

        await self.pool.execute(
            """UPDATE users SET full_name='נמחק',bio='',photos='{}',
               latitude=NULL,longitude=NULL,is_active=FALSE
               WHERE telegram_id=2"""
        )
        self.assertEqual(await reset_profile(self.pool, 2, 4), "reset")

    async def test_completed_fresh_registration_reactivates_unless_held(self):
        finish = load_function(
            "finish_photos_or_explain",
            Message=object,
            FSMContext=object,
            get_pool=AsyncMock(return_value=self.pool),
            check_submission=AsyncMock(return_value=True),
            send_db_error=AsyncMock(),
            ReplyKeyboardRemove=lambda: None,
            show_next_profile=AsyncMock(),
        )
        for uid, held in ((1, False), (2, True)):
            if held:
                await self.pool.execute(
                    """UPDATE users SET admin_hold=TRUE,
                       moderation_reason='keep sanction' WHERE telegram_id=$1""",
                    uid,
                )
            self.assertEqual(await reset_profile(self.pool, uid, 4), "reset")
            revision = await self.pool.fetchval(
                """UPDATE users SET moderation_revision=moderation_revision+1
                   WHERE telegram_id=$1 RETURNING moderation_revision""",
                uid,
            )
            await self.pool.execute(
                """INSERT INTO policy_acceptances
                   (telegram_id,terms_version,privacy_version)
                   VALUES ($1,$2,$3)""",
                uid,
                CURRENT_TERMS_VERSION,
                CURRENT_PRIVACY_VERSION,
            )
            data = {
                "name": "New name",
                "age": 29,
                "gender": "male",
                "target_gender": "female",
                "bio": "New bio",
                "photos": ["new-photo"],
                "latitude": 32.1,
                "longitude": 34.8,
                "profile_revision": revision,
                "fresh_after_reset": True,
            }
            state = SimpleNamespace(
                get_data=AsyncMock(return_value=data),
                clear=AsyncMock(),
            )
            message = SimpleNamespace(
                text="סיימתי",
                from_user=SimpleNamespace(id=uid, username=f"new{uid}"),
                chat=SimpleNamespace(id=uid),
                answer=AsyncMock(),
            )
            await finish(message, state)
            row = await self.pool.fetchrow(
                """SELECT full_name,is_active,admin_hold,moderation_status,
                          moderation_reason
                   FROM users WHERE telegram_id=$1""",
                uid,
            )
            self.assertEqual(row["full_name"], "New name")
            self.assertEqual(row["moderation_status"], "approved")
            self.assertEqual(row["is_active"], not held)
            if held:
                self.assertEqual(row["moderation_reason"], "keep sanction")


class HandlerContractTests(unittest.TestCase):
    def test_confirmation_and_registration_routing_are_guarded(self):
        source = (ROOT / "main.py").read_text()
        self.assertIn('Command("resetprofile", "editprofile")', source)
        self.assertIn("ProfileReset.confirming", source)
        self.assertIn("reset_revision", source)
        self.assertIn("reset_token", source)
        self.assertIn('result == "stale"', source)
        self.assertIn("fresh_after_reset=True", source)
        self.assertIn("WHEN $12 THEN NOT users.admin_hold", source)
        self.assertIn("CommandStart(), StateFilter(ProfileReset)", source)
        self.assertNotIn("/editprofile —", source)
        self.assertLess(
            source.index('@router.message(Command("cancel"))'),
            source.index("@router.message(Registration.consent)\n"),
        )


class ResetCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_command_clears_consent_state(self):
        cancel = load_function(
            "cancel",
            Message=object,
            FSMContext=object,
            ReplyKeyboardRemove=lambda: None,
        )
        state = SimpleNamespace(clear=AsyncMock())
        message = SimpleNamespace(answer=AsyncMock())
        await cancel(message, state)
        state.clear.assert_awaited_once()
        self.assertIn("בוטל", message.answer.call_args.args[0])

    async def test_cancel_and_stale_callback_never_touch_database(self):
        get_pool = AsyncMock()
        profile_reset_state = SimpleNamespace(
            confirming=SimpleNamespace(state="ProfileReset:confirming")
        )
        handler = load_function(
            "resetprofile_callback",
            CallbackQuery=object,
            FSMContext=object,
            ProfileReset=profile_reset_state,
            get_pool=get_pool,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="reset:cancel:4:token",
            message=message,
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(
            get_data=AsyncMock(
                return_value={"reset_revision": 4, "reset_token": "token"}
            ),
            get_state=AsyncMock(return_value="ProfileReset:confirming"),
            clear=AsyncMock(),
        )
        await handler(callback, state)
        state.clear.assert_awaited_once()
        self.assertIn("ללא שינוי", message.answer.call_args.args[0])
        get_pool.assert_not_awaited()

        callback.data = "reset:confirm:4:old-token"
        state.clear.reset_mock()
        message.answer.reset_mock()
        await handler(callback, state)
        get_pool.assert_not_awaited()
        state.clear.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])


class TrackingState:
    def __init__(self, revision, token):
        self.data = {"reset_revision": revision, "reset_token": token}
        self.current = "ProfileReset:confirming"
        self.operations = []

    async def get_data(self):
        return dict(self.data)

    async def get_state(self):
        return self.current

    async def clear(self):
        self.operations.append("clear")
        self.data = {}
        self.current = None

    async def update_data(self, **values):
        self.operations.append(("update", values))
        self.data.update(values)


class PostgreSQLHandlerTests(unittest.IsolatedAsyncioTestCase):
    setUpClass = classmethod(
        support_tests.PostgreSQLSupportTests.setUpClass.__func__
    )
    command = classmethod(
        support_tests.PostgreSQLSupportTests.command.__func__
    )
    stop_cluster = classmethod(
        support_tests.PostgreSQLSupportTests.stop_cluster.__func__
    )
    connect = support_tests.PostgreSQLSupportTests.connect
    close_pool = support_tests.PostgreSQLSupportTests.close_pool
    asyncSetUp = PostgreSQLProfileResetTests.asyncSetUp

    async def test_successful_confirmation_clears_fsm_and_cannot_replay(self):
        profile_reset_state = SimpleNamespace(
            confirming=SimpleNamespace(state="ProfileReset:confirming")
        )
        consent = AsyncMock(return_value=True)

        async def begin(message, state, user_id):
            state.operations.append("begin")
            state.current = "Registration:name"

        handler = load_function(
            "resetprofile_callback",
            CallbackQuery=object,
            FSMContext=object,
            ProfileReset=profile_reset_state,
            get_pool=AsyncMock(return_value=self.pool),
            require_current_consent=consent,
            begin_registration=begin,
            send_db_error=AsyncMock(),
            logger=SimpleNamespace(exception=lambda *_: None),
        )
        await self.pool.execute(
            """UPDATE users SET admin_hold=TRUE,moderation_reason='hold',
               daily_likes_count=6 WHERE telegram_id=1"""
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            answer=AsyncMock(),
        )
        callback = SimpleNamespace(
            data="reset:confirm:4:good",
            message=message,
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        state = TrackingState(4, "good")
        await handler(callback, state)

        self.assertEqual(
            state.operations,
            ["clear", ("update", {"fresh_after_reset": True}), "begin"],
        )
        consent.assert_awaited_once()
        row = await self.pool.fetchrow(
            """SELECT full_name,is_premium,admin_hold,moderation_reason,
                      daily_likes_count,moderation_revision
               FROM users WHERE telegram_id=1"""
        )
        self.assertEqual(row["full_name"], "")
        self.assertTrue(row["is_premium"])
        self.assertTrue(row["admin_hold"])
        self.assertEqual(row["moderation_reason"], "hold")
        self.assertEqual(row["daily_likes_count"], 6)
        self.assertEqual(row["moderation_revision"], 5)

        await handler(callback, state)
        self.assertEqual(callback.answer.await_count, 2)
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])
        self.assertEqual(
            await self.pool.fetchval(
                "SELECT moderation_revision FROM users WHERE telegram_id=1"
            ),
            5,
        )

    async def test_confirm_missing_account_starts_registration(self):
        profile_reset_state = SimpleNamespace(
            confirming=SimpleNamespace(state="ProfileReset:confirming")
        )
        began = AsyncMock()
        handler = load_function(
            "resetprofile_callback",
            CallbackQuery=object,
            FSMContext=object,
            ProfileReset=profile_reset_state,
            get_pool=AsyncMock(return_value=self.pool),
            require_current_consent=AsyncMock(return_value=True),
            begin_registration=began,
            send_db_error=AsyncMock(),
            logger=SimpleNamespace(exception=lambda *_: None),
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"), answer=AsyncMock()
        )
        callback = SimpleNamespace(
            data="reset:confirm:-1:new",
            message=message,
            from_user=SimpleNamespace(id=99),
            answer=AsyncMock(),
        )
        state = TrackingState(-1, "new")
        await handler(callback, state)
        began.assert_awaited_once_with(message, state, 99)
        self.assertIsNone(
            await self.pool.fetchval(
                "SELECT telegram_id FROM users WHERE telegram_id=99"
            )
        )

    async def test_group_confirmation_and_existing_start_are_non_destructive(self):
        profile_reset_state = SimpleNamespace(
            confirming=SimpleNamespace(state="ProfileReset:confirming")
        )
        get_pool = AsyncMock(return_value=self.pool)
        handler = load_function(
            "resetprofile_callback",
            CallbackQuery=object,
            FSMContext=object,
            ProfileReset=profile_reset_state,
            get_pool=get_pool,
        )
        callback = SimpleNamespace(
            data="reset:confirm:4:good",
            message=SimpleNamespace(
                chat=SimpleNamespace(type="group"), answer=AsyncMock()
            ),
            from_user=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        await handler(callback, TrackingState(4, "good"))
        get_pool.assert_not_awaited()
        self.assertTrue(callback.answer.call_args.kwargs["show_alert"])

        user = dict(await self.pool.fetchrow(
            "SELECT * FROM users WHERE telegram_id=1"
        ))
        user["bio"] = ""
        start = load_function(
            "start",
            Message=object,
            FSMContext=object,
            fetch_user=AsyncMock(return_value=user),
            get_pool=AsyncMock(return_value=self.pool),
            send_db_error=AsyncMock(),
            require_current_consent=AsyncMock(),
            begin_registration=AsyncMock(),
            ensure_review=AsyncMock(),
            ReplyKeyboardRemove=lambda: None,
            show_next_profile=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=1),
            chat=SimpleNamespace(id=1),
            answer=AsyncMock(),
        )
        before = await self.pool.fetchrow(
            """SELECT full_name,bio,moderation_revision
               FROM users WHERE telegram_id=1"""
        )
        await start(message, state)
        after = await self.pool.fetchrow(
            """SELECT full_name,bio,moderation_revision
               FROM users WHERE telegram_id=1"""
        )
        self.assertEqual(before, after)
        start.__globals__["require_current_consent"].assert_not_awaited()
        start.__globals__["begin_registration"].assert_not_awaited()
        self.assertIn("/resetprofile", message.answer.call_args.args[0])
