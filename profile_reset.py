"""Atomic profile reset primitives, independent from Telegram handlers."""

import secrets


def new_reset_token() -> str:
    return secrets.token_urlsafe(8)


async def reset_profile(pool, telegram_id: int, expected_revision: int) -> str:
    """Clear dating data while preserving identity, billing, and safety records."""
    async with pool.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """SELECT moderation_revision FROM users
                   WHERE telegram_id=$1 FOR UPDATE""",
                telegram_id,
            )
            if row is None:
                return "missing" if expected_revision == -1 else "stale"
            if row["moderation_revision"] != expected_revision:
                return "stale"

            await connection.execute(
                "DELETE FROM matches WHERE user_a=$1 OR user_b=$1", telegram_id
            )
            await connection.execute(
                "DELETE FROM interactions WHERE from_user=$1 OR to_user=$1",
                telegram_id,
            )
            await connection.execute(
                "DELETE FROM hidden_content WHERE telegram_id=$1", telegram_id
            )
            await connection.execute(
                "DELETE FROM submission_rejections WHERE telegram_id=$1", telegram_id
            )
            await connection.execute(
                "DELETE FROM content_events WHERE telegram_id=$1", telegram_id
            )
            await connection.execute(
                """UPDATE users SET full_name='', age=18, gender='male',
                   target_gender='female', bio='', photos='{}',
                   latitude=NULL, longitude=NULL, is_active=FALSE,
                   moderation_status='unreviewed',
                   moderation_reason=CASE WHEN admin_hold THEN moderation_reason ELSE NULL END,
                   moderation_revision=moderation_revision+1,
                   updated_at=NOW()
                   WHERE telegram_id=$1""",
                telegram_id,
            )
            return "reset"