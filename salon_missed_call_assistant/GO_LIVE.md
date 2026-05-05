# Go-Live Guide

This app has two deployable pieces:

- Staff dashboard: `salon_missed_call_assistant/app.py`
- Public webhook receiver: `salon_missed_call_assistant/webhook_receiver.py`

## Recommended Live Path

Use Render for the first live version because it can run both the Streamlit dashboard and the FastAPI webhook receiver from the same repository. Streamlit Community Cloud is fine for a public dashboard demo, but it does not solve the separate public webhook service by itself.

## 1. Push the Project to GitHub

Render and Streamlit Community Cloud deploy from GitHub. Commit these files before creating the service:

- `salon_missed_call_assistant/app.py`
- `salon_missed_call_assistant/webhook_receiver.py`
- `salon_missed_call_assistant/requirements.txt`
- `salon_missed_call_assistant/production_schema.sql`
- `salon_missed_call_assistant/.env.example`
- `render.yaml`

Do not commit real `.env` files, API keys, Twilio tokens, payment keys, or database passwords.

## 2. Deploy on Render

Use the Blueprint flow with `render.yaml`.

1. Create a Render account.
2. Connect the GitHub repository.
3. Choose Blueprint deployment.
4. Review the two services:
   - `salon-assistant-ui`
   - `salon-assistant-webhooks`
5. Fill every `sync: false` environment variable in Render.
6. Deploy.

Render will provide URLs like:

- `https://salon-assistant-ui.onrender.com`
- `https://salon-assistant-webhooks.onrender.com`

## 3. Required Environment Variables

Set these on both services when relevant:

```text
SALON_NAME=
SALON_SLUG=
SALON_PHONE=
SALON_TIMEZONE=America/New_York
SALON_STAFF_PASSCODE=
SALON_WEBHOOK_SECRET=
SALON_REQUIRE_WEBHOOK_SECRET=true
SALON_CONSENT_POLICY_APPROVED=true
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
BOOKING_PROVIDER=
PAYMENT_PROVIDER=
PAYMENT_CHECKOUT_BASE_URL=
SALON_DATABASE_URL=
```

Use a long random value for `SALON_WEBHOOK_SECRET`. Use a staff-only passcode for `SALON_STAFF_PASSCODE`.
Keep `SALON_REQUIRE_WEBHOOK_SECRET=true` in production so unsigned or incorrectly signed webhook requests are rejected.

For more than one salon, use these values as the first/default salon. After the dashboard is live, add the other salons from `Admin Database` -> `Salon workspaces` and give each salon its own phone/from-number values.

## 4. Configure the Phone Provider

Point the phone/SMS provider to the webhook service:

- Missed calls: `https://YOUR-WEBHOOK-SERVICE.onrender.com/webhooks/missed-call`
- Inbound SMS: `https://YOUR-WEBHOOK-SERVICE.onrender.com/webhooks/inbound-sms`
- Health check: `https://YOUR-WEBHOOK-SERVICE.onrender.com/health`

The app expects JSON payloads. If your provider sends form data instead, add a provider-specific adapter in `webhook_receiver.py`.
Twilio inbound SMS webhooks send `application/x-www-form-urlencoded` data, which `webhook_receiver.py` accepts.

For multi-salon routing, include `salon_id` in custom webhooks when possible. If the provider cannot include that, make sure its `To`, `Called`, or `salon_phone` field matches the salon's saved phone, `sms_from_number`, or `twilio_from_number`.
If neither `salon_id` nor a configured destination phone matches, the app rejects the webhook instead of placing the client into the wrong salon workspace.

## 5. Add Additional Salons

In the dashboard:

1. Open `Admin Database`.
2. Add or edit rows under `Salon workspaces`.
3. Save each salon's name, slug, public phone, timezone, SMS/from number, booking provider, payment provider, checkout URL, and database URL.
4. Switch the sidebar to that salon.
5. Enter that salon's services, stylists, staff users, deposits, cancellation windows, and prep notes.
6. Test one missed-call webhook and one inbound SMS for that salon before turning on live traffic.

## 6. Move Storage Off Local SQLite

The live demo can run with local SQLite, but production client records should move to hosted Postgres/Supabase. Start from:

```text
salon_missed_call_assistant/production_schema.sql
```

After creating the hosted database, put the connection string in `SALON_DATABASE_URL`. The current app still uses local SQLite for the demo runtime, so the next production engineering step is wiring the app's persistence layer to Postgres.

## 7. Domain and Access

For real salon use:

- Add a custom domain to the staff dashboard.
- Keep `SALON_STAFF_PASSCODE` enabled.
- Do not share the dashboard link publicly.
- Use the webhook URL only inside the phone provider settings.

## 8. Final Launch Checklist

- Phone number purchased or connected.
- SMS carrier registration completed.
- Texting consent and opt-out language approved.
- Twilio or phone-provider credentials entered.
- Real salon services, deposits, durations, and cancellation windows entered.
- Real stylists and staff users entered.
- Each additional salon created with its own phone/from-number and service menu.
- Hosted database selected and connected.
- Payment provider selected and deposit policy approved.
- Booking/calendar provider selected.
- Test missed call creates a conversation.
- Test inbound `HELP` returns help text.
- Test inbound `STOP` opts the client out.
- Test a long service blocks overlapping slots.
- Test appointment creates stylist notification, reminder, deposit request, and calendar export.

## Fastest Demo-Only Alternative

For a demo that does not need webhooks, use Streamlit Community Cloud:

1. Push the repo to GitHub.
2. Create an app from the repo.
3. Set the entrypoint to `salon_missed_call_assistant/app.py`.
4. Add secrets in the app settings.
5. Deploy.

This gives you a shareable dashboard URL, but it is not enough for real phone/SMS automation.
