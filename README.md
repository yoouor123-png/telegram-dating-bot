# Existing Telegram dating bot

Production remains on Render with the user's Neon PostgreSQL database.
Do not start another polling worker against the production bot token.

`premium_handlers.py` is registered before onboarding handlers so commands and
successful payments cannot be swallowed by registration state filters.
The previous unregistered payment functions in main.py are retained temporarily
for reference, not executed.

Premium uses Telegram Stars recurring invoice links (30 days). The existing
`PREMIUM_PRICE_STARS` setting is preserved (default 250); no ILS conversion is
promised. Configure `SUPPORT_USERNAME` on Render for `/paysupport`.
Cancellation stops renewal, not the already-paid entitlement.

## Support operator setup on Render

Set `SUPPORT_OWNER_TELEGRAM_ID` on the **existing Render background worker** to
the positive numeric Telegram ID of the one support operator. The value is used
only when explicitly configured; it is never inferred from usernames, database
users, chats, or other environment variables. A user can send
`/support_identity` to the bot in a private chat to learn their own ID. This
command does not enroll them or grant any permission.

After saving the variable, deploy/restart only the existing worker. Do not add a
second worker using the same bot token. The configured owner can use
`/support_inbox` in their private chat with the bot to page through tickets,
view details and the audit trail, reply, and open or close tickets. Every
operator command, callback, and reply state verifies both the private chat and
the sender ID. Invalid or non-positive configuration prevents startup. Never
put the owner ID or bot token in source control, and do not log secrets.

Ticket notifications (including tickets stored before operator setup) use the
existing Neon pool and a single persistent retry queue with exponential
backoff. The delivery task runs inside the existing polling worker and stops
gracefully with it. Operator replies make one Telegram send attempt: an
ambiguous failure is recorded honestly and is not retried automatically, which
avoids duplicate replies.

If `SUPPORT_OWNER_TELEGRAM_ID` is not configured, the bot explicitly tells
users that existing and new inbox tickets are stored only and cannot be
answered from the bot. `SUPPORT_USERNAME` remains an optional, separate public
contact link and never grants operator access.

Payment records and entitlement updates commit in one transaction. Duplicate
charge notifications do not grant extra time; delayed notifications cannot
shorten the entitlement. Pending Telegram updates are preserved across restarts.
If a successful payment cannot be persisted, logs identify the affected user for
manual reconciliation; automatic reconciliation/refund is not implemented.

No live charge, cancellation, or production DB write was performed during build.