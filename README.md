# Existing Telegram dating bot

Production remains on Render with the user's Neon PostgreSQL database.
Do not start another polling worker against the production bot token.

`premium_handlers.py`, in-bot support, and `legal_privacy.py` are registered
before onboarding handlers so payment, cancellation, support, and legal/account
commands cannot be swallowed by registration state filters.
Premium uses one-time Telegram Stars invoice links (30 days), with no automatic
renewal. The existing
`PREMIUM_PRICE_STARS` setting is preserved (default 250); no ILS conversion is
promised. `/paysupport` opens the in-bot support intake. The existing bot process
runs a durable expiry-notice worker: each expired period receives a system
message offering optional manual purchase, not an automatic charge.

The private bot UI is emoji-first. `/start` and `/menu` expose a two-by-two
main keyboard for profiles, account, Premium, and other tools; only the
configured owner receives the extra administration row. The visible Telegram
command list is intentionally limited to `/start` and `/menu`. Legacy slash
commands, including owner-only `/admin` and the `/editprofile` alias,
remain supported. During registration, support, or an administrator draft,
menu emoji offer an explicit continue/cancel choice without consuming or
discarding the draft or replacing its contextual keyboard.
Legacy recurring subscriptions are queued for renewal cancellation, preserving
already-paid time. Failed cancellations remain pending for retry and are not
reported as successful. `/cancelpremium` remains for these legacy subscriptions.

Routine bot copy is kept to at most three explicit short lines. Telegram may
still wrap a line differently by client, font, or accessibility settings.
Profiles use a compact preview with authenticated detail pages; long private
exports, support replies, and moderation notices use UTF-8 text attachments so
their full content remains available without flooding the chat.

Support links outside the bot are intentionally disabled. `SUPPORT_USERNAME` is
retained in legacy settings but is not passed to the active support router.

## Legal/privacy safety controls

The in-bot `/legal` center links Hebrew `/privacy`, `/terms`, `/refunds`, and
`/safety` text. These texts explicitly remain incomplete drafts: operator
identity/address details and legal review are still outstanding, and the bot
does not claim legal compliance.

Current policy versions require explicit 18+, Israel, profile visibility,
location, and sensitive matching-data acceptance before a new profile is
registered. Existing profiles are prompted on `/start`; paid entitlement,
`/cancelpremium`, and support remain available. Exact coordinates remain stored
for matching, while candidate cards show only five-kilometre ranges.

`/pause` and `/resume` control profile visibility. `/mydata` exports only the
requester's stored profile, acceptance, and payment fields as a private UTF-8
text attachment instead of sending a sequence of long chat messages.
`/deleteaccount` requires confirmation and attempts legacy recurring
subscription cancellation before anonymizing the account; a new one-time
payment does not need renewal cancellation. Failed legacy
cancellation leaves that account intact and routes the user to in-bot payment
support. Deletion clears and deactivates the profile and removes
relationship/support data, but it is not full anonymization: the identifying
Telegram user key and minimal payment records remain for reconciliation. A
final payment-retention period still requires legal review.

`/resetprofile` replaces profile editing. It requires confirmation before clearing
the profile and its matches/interactions, then starts fresh registration. Paid
Premium, payment records, safety blocks/reports, support history and administrator
holds remain. The new profile must pass the usual consent and content checks
before publication. `/editprofile` is a compatibility alias for the confirmation
prompt, not an editing shortcut. `/deleteaccount` remains a separate account
deletion flow.

## Support operator setup on Render

The configured operator can send `/stats` in a private chat for live aggregate
counts: completed registered profiles (excluding deleted profiles), profiles
active for discovery, profiles awaiting automatic review, and unexpired Premium
entitlements (including paused profiles). Active means moderation-approved AND
`is_active`, not recently online. The command uses the same
`SUPPORT_OWNER_TELEGRAM_ID` authorization and refuses access if it is unset.
It is registered before conversational states so it works during support intake.

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
If a successful payment cannot be persisted, the user is directed to payment
support and logs record the error type without payment identifiers;
automatic entitlement reconciliation/refund is not implemented.

No live charge, cancellation, or production DB write was performed during build.
After deployment, the maintenance worker attempts legacy renewal cancellation
and processes due expiry notices. New successful payments and expiry notices are
tested with a disposable local database and mocked Telegram calls, not live charges.

## Automatic pre-publication content checks

Set `OPENAI_API_KEY` as a secret on the **existing Render worker**. This is a
direct OpenAI API integration, not a Replit integration. Do not commit the key,
create another polling worker, or assume that a configured key proves model
access. Missing credentials, network/provider errors, refusal, incomplete or
malformed responses all fail closed: new/unreviewed content is not published.
The optional operator-only private `/moderation` command reports configuration,
not live readiness. There is no manual approval bypass. The private owner dashboard
can inspect saved profiles, including unreviewed/rejected profiles; rejected
registration submissions are not stored as profile content.

### Owner content moderation

Set `SUPPORT_OWNER_TELEGRAM_ID` to the operator's Telegram user ID. Use `/admin`
in a private chat. Pages include pending, rejected, paused and held profiles.
Open a profile to see every saved photo, name/bio and status. Every mutation
requires a typed reason and an explicit confirmation; revised/deleted profiles
invalidate old controls. Owner media uses Telegram content protection (not a
guarantee against screenshots).

Actions: suspend/release publication, take down the entire profile (hide through
an independent administrator hold, not account deletion), remove a photo/name/bio,
or hide individual items. Hidden items have separate restore buttons and retain
only their necessary value (photo file ID and position, or text). Other owner
actions preserve hidden items; confirming profile reset or deleting the account
invalidates them. Restoring content sets `unreviewed`; `/start` runs automatic
review before any publication. Releasing a hold neither approves content nor
reactivates a user-paused account. `/resume`, `/start`, profile resets and automatic approval
cannot clear an owner hold. Incomplete content cannot publish.

The mutation, minimal actor/reason/time audit and notification are committed
together. Telegram notification is best effort, claimed once, and never blindly
retried: `sending` after interruption and `uncertain` mean delivery is unknown.
The owner sees delivery status in the profile's recent audit. Users see durable
reasons/remediation on `/start` and `/profile`; `/contentnotices [page]` provides
older history (five entries per page). `/resetprofile` starts a new profile after confirmation; `/support`
appeals owner holds. Paid entitlement and payment history are not reset.
Account deletion clears private action content and hidden items, retaining paid
accounting data as described in the policy.

Automatic denials use fixed safe reason codes: `sexual`, `revealing`, `offensive`,
`violence`, `other`. Provider outages remain unavailable, not accusations.
Rejected registration inputs persist only their reason code, not the input.
Policy version `2025-02-draft-3-owner-moderation` discloses owner inspection.

Offline validation (no Telegram polling or real provider calls):

```sh
python -m unittest discover -s telegram-bot/tests -v
python -m py_compile telegram-bot/*.py telegram-bot/tests/*.py
git diff --check
```

Integration tests start disposable Unix-socket PostgreSQL clusters using `initdb`
and `pg_ctl`; they never use application database credentials. Schema is applied
idempotently on the bot's first database access; the main operator controls deployment.

Checks use `omni-moderation-latest` for the independent child-sexual-safety gate,
followed by `gpt-5.4-mini` via Responses with a strict JSON schema. Automatic
profile filtering is limited to actual nudity and gambling promotion/facilitation,
plus the mandatory child-sexual-safety protection. Ordinary portraits, swimwear,
revealing clothing, underwear and shirtless male torsos are not disallowed just
because of clothing or exposed skin. Generic provider sexual, offensive or violence
flags are not evidence of nudity and do not automatically reject the profile.
User content is explicitly untrusted input. General terms, age eligibility,
reports, blocks and administrator sanctions remain separate safety controls.
The narrower rules do not expand data processing or invalidate existing consent;
they do not reset approval or sanctions or automatically republish old rejections.
The request format follows OpenAI's official Structured Outputs guide:
https://developers.openai.com/api/docs/guides/structured-outputs
(`text.format`, `type=json_schema`, `strict=true` for Responses).
This reduces risk but does not guarantee detection, establish age or identity,
or establish legality. Never knowingly submit or forward known/suspected CSAM;
content flagged for sexual material involving minors is not sent to the second
model. No such imagery is used in tests.

Name, bio and each photo are checked before entering registration state. The
complete profile is checked again before saving. Rejected incoming messages are
deleted best-effort; deletion can fail and Telegram/user copies remain outside
our control. Documents, videos and animations are not accepted as profile media.
Images are downloaded through the bot into memory and sent as base64, never
as Telegram file URLs containing the bot token. Content/provider bodies and
exceptions are not logged. Responses uses `store=false`; this is not a promise
of zero provider retention. See versioned `/privacy`, `/terms` and consent copy.

The additive migration deliberately marks **all existing profiles unreviewed**
and hidden. There is no background bulk scan or automatic transmission of old
profiles. Existing users must accept the new policy and use `/start` or `/browse`
to check and publish their profile. Consent is checked before transmission.
Denied saved profiles can be deleted and rebuilt with `/resetprofile`; Premium
entitlements remain intact. Approval does not reactivate a paused profile.
`/resume`, old action buttons, discovery, matches and match notifications require
approval. Billing, support, export and deletion remain available.

`moderation_revision` invalidates in-flight checks when editing or deleting.
External calls run without holding a DB connection/transaction. Final writes
compare the reviewed revision. Aiogram `SimpleEventIsolation` serializes private
updates (including cancel/delete) while checks run: those commands may wait for
the active check, then clear state/delete normally. This is an in-memory lock
for the existing **single worker**, not distributed locking for multiple workers.

Readiness: implementation is covered with network-mocked tests and disposable
local PostgreSQL tests. Provider availability, model access, Hebrew accuracy and
false-positive/negative rates require authorized live verification before rollout.
No provider call, production database access or deployment is part of these tests.

## Test commands

Run from the repository root:

```sh
python -m unittest discover -s telegram-bot/tests -v
```

Requires the Python dependencies in `pyproject.toml` and PostgreSQL's `initdb`
and `pg_ctl` on PATH, running as a non-root user. Missing PostgreSQL tools fail
the suite explicitly rather than silently skipping integration coverage.

The integration tests create and clean up their own temporary PostgreSQL
cluster, accepting connections only through a private Unix socket (no TCP).
They never use `DATABASE_URL`, inherited PostgreSQL connection settings, or
Telegram credentials. They extract the actual `SCHEMA_SQL` literal without
importing `main.py`, mock only Telegram delivery, and restart the temporary
database with new connection pools to check persistence. No polling worker or
background notification loop is started. Retry deadlines are advanced directly
in the isolated database so tests do not sleep through backoff intervals.