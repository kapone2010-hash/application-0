# Salon Missed-Call Assistant

A Streamlit prototype for hair salons that turns a missed call into an automatic text flow, checks the salon service database for prices, books an appointment on a simple calendar, and prepares a stylist notification.

## What It Does

- Captures a missed call and creates the first automatic SMS-style reply.
- Processes a client reply for booking, price checks, service questions, and reschedule/cancel requests.
- Matches the client's text against a service and price database.
- Shows open appointment slots by stylist, date, service duration, and existing bookings.
- Books an appointment and creates a message for the stylist with what the client asked and what was booked.
- Opens with a front-desk overview showing open conversations, same-day bookings, staff updates, setup readiness, and missing launch items.
- Tracks client consent status, STOP/HELP replies, opt-outs, and consent history.
- Adds staff access mode through `SALON_STAFF_PASSCODE` for real client-data demos.
- Adds deposit rules, cancellation windows, consultation flags, and prep notes to service pricing.
- Queues appointment reminders and payment requests when a deposit is required.
- Exports `.ics` calendar events and tracks calendar-sync attempts.
- Includes webhook processing for missed calls and inbound SMS, plus an optional FastAPI receiver.
- Shows owner analytics for missed-call recovery, replies, bookings, and estimated recovered revenue.
- Includes admin screens to edit services, prices, stylist specialties, phone numbers, and active status.
- Includes a launch plan tab that separates app work from outside setup such as phone-provider registration and consent policy.
- Uses local SQLite storage by default so the demo runs without cloud setup.
- Is browser-based and responsive, so it can be used on phones, tablets, and desktop screens.

## Run Locally

From `C:\Users\Aniya\OneDrive\Documents\New project 3`:

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install -r salon_missed_call_assistant\requirements.txt
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m streamlit run salon_missed_call_assistant\app.py
```

The demo creates `salon_assistant.sqlite3` inside this folder on first run.

## Optional SMS Settings

The current app simulates SMS unless these environment variables are configured:

```powershell
$env:TWILIO_ACCOUNT_SID='your_account_sid'
$env:TWILIO_AUTH_TOKEN='your_auth_token'
$env:TWILIO_FROM_NUMBER='+15550142233'
$env:SALON_NAME='Your Salon Name'
$env:SALON_PHONE='Your Salon Phone'
$env:SALON_STAFF_PASSCODE='choose-a-staff-passcode'
$env:SALON_WEBHOOK_SECRET='shared-webhook-signing-secret'
$env:SALON_CONSENT_POLICY_APPROVED='true'
$env:BOOKING_PROVIDER='Google Calendar'
$env:PAYMENT_PROVIDER='Square'
$env:PAYMENT_CHECKOUT_BASE_URL='https://payments.example.com'
```

In production, the missed-call trigger should come from a phone provider webhook. Twilio, RingCentral, OpenPhone, GoHighLevel, Square Appointments, Fresha, or another salon phone/booking system can be connected depending on what the salon already uses.

## Optional Webhook Receiver

The Streamlit app includes webhook-processing functions. For a real provider callback, run the optional FastAPI receiver beside the app:

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m uvicorn webhook_receiver:api --app-dir salon_missed_call_assistant --host 0.0.0.0 --port 8510
```

Endpoints:

- `POST /webhooks/missed-call`
- `POST /webhooks/inbound-sms`
- `GET /health`

Use `X-Salon-Signature` with an HMAC SHA-256 signature of the JSON payload when `SALON_WEBHOOK_SECRET` is configured.

## Production Storage

`production_schema.sql` is a hosted Postgres/Supabase starting point for the production tables. The Streamlit demo still uses SQLite locally, but the schema now defines the production shape for salons, staff, clients, consent events, messages, appointments, webhooks, payments, reminders, calendar sync, and audit events.

## Parts Codex Can Write

- The mobile/desktop web app.
- The local or hosted database schema.
- Service and price lookup logic.
- Booking calendar logic.
- Client conversation flow.
- Stylist notification flow.
- Consent ledger and opt-out handling.
- Deposit/payment-link scaffolding.
- Calendar export/sync scaffolding.
- Staff access screens.
- Owner analytics.
- Provider adapters for SMS, calendar, email, or CRM tools.
- Deployment instructions.

## Parts That Need Outside Setup

- Purchasing or verifying a real salon phone number.
- Creating provider accounts and entering payment details.
- Completing carrier registration or texting compliance steps.
- Confirming the salon has permission to text missed callers.
- Supplying the salon's real service menu, pricing rules, stylists, availability, cancellation policy, and booking data.
- Choosing the final production host and domain.
- Final legal/compliance approval.
- Creating live payment accounts and approving deposit/refund policy.

## Next Production Upgrades

- Replace simulated missed calls with a real phone webhook.
- Add staff login and role-based permissions.
- Connect to the salon's real booking system or calendar.
- Add text consent and opt-out handling.
- Send stylist notifications by SMS, email, or staff dashboard.
- Add client confirmations and reminder texts.
- Add deposits, no-show rules, cancellation windows, and service add-on pricing.
- Add owner analytics for missed-call recovery, booking conversion, response time, and recovered revenue.
- Move storage to Supabase, Postgres, or another managed database.
