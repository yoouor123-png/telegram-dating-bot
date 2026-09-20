"""Stateless, requester-scoped profile detail callbacks."""
import hashlib
import hmac


def _base36(value):
    value = int(value)
    if value < 0:
        raise ValueError("negative callback value")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, digit = divmod(value, 36)
        result = alphabet[digit] + result
    return result or "0"


def _from_base36(value):
    return int(value, 36)


def signed_bio_callback(secret, viewer_id, target_id, revision, page=0):
    payload = ":".join(map(_base36, (viewer_id, target_id, revision, page)))
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
    ).hexdigest()[:10]
    return f"pb:{payload}:{signature}"


def parse_bio_callback(secret, data):
    prefix, viewer, target, revision, page, signature = data.split(":")
    if prefix != "pb":
        raise ValueError("wrong callback kind")
    payload = ":".join((viewer, target, revision, page))
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
    ).hexdigest()[:10]
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid callback signature")
    return tuple(map(_from_base36, (viewer, target, revision, page)))


async def fetch_profile_detail(pool, viewer_id, target_id, revision):
    """Return live public detail only while both users remain eligible."""
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            """SELECT t.full_name,t.bio,t.moderation_revision
               FROM users v JOIN users t ON t.telegram_id=$2
               WHERE v.telegram_id=$1 AND v.telegram_id<>t.telegram_id
                 AND v.is_active AND t.is_active
                 AND v.moderation_status='approved'
                 AND t.moderation_status='approved'
                 AND NOT v.admin_hold AND NOT t.admin_hold
                 AND content_complete(v) AND content_complete(t)
                 AND t.moderation_revision=$3
                 AND NOT EXISTS (
                   SELECT 1 FROM blocked_users b
                   WHERE (b.blocker_id=$1 AND b.blocked_id=$2)
                      OR (b.blocker_id=$2 AND b.blocked_id=$1)
                 )""",
            viewer_id, target_id, revision,
        )