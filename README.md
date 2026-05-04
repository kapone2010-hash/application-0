# Application 0: GovDash SDR Prospecting

A Streamlit prototype for finding government contractors that recently won public contract awards, then turning each award into an SDR-ready GovDash demo angle.

## What It Does

- Pulls recent federal contract awards from the USAspending public API.
- Checks USAspending source freshness before the full lead pull using the newest `Last Modified Date` for the current filters.
- Groups award winners into account-level views.
- Ranks accounts by GovDash fit, recent award value, number of awards, agencies, NAICS/PSC, and follow-on signals.
- Adds a dynamic SDR action queue that blends award fit, verified contacts, public call-intel signals, pain evidence, open tasks, and contact readiness.
- Lists award details, UEI, public address, NAICS/PSC, agency, amount, and dates.
- Scans public web sources for company descriptions, likely website, source pages, public business emails/phones, named contacts, call-intel signals, evidence-backed pain points, and public LinkedIn result signals when available.
- Explains what the company won and gives a reasoned hypothesis for why it may have won, without pretending USAspending exposes evaluation rationale.
- Generates public research links for company site, leadership, LinkedIn, news, USAspending, and SAM.gov.
- Ranks the best contact targets for each account, explains why each role matters, and gives source-backed public contacts, LinkedIn profile-result signals, and search links to verify named people.
- Scores contact-list freshness and relevance before SDR use, including source freshness, role match, named-person status, business email/phone availability, and next verification step.
- Saves verified contacts from manual research or enrichment CSV exports so verified people outrank public web guesses.
- Pulls call-relevance signals beyond the award, including public LinkedIn updates/search signals, announcements, past press releases, podcasts/interviews, hiring/growth, partnerships, webinars, and leadership changes when public sources expose them.
- Categorizes each account by industry and separates company-specific pain evidence from industry benchmark pain points that SDRs should verify on the call.
- Shows "why now" triggers and recommended next best actions.
- Generates a downloadable call-prep brief with account context, what they won, why they may have won, best contact path, pain points, objections, discovery questions, and GovDash demo angle.
- Adds CRM-style fields for status, owner, cadence stage, email/call tracking, outcomes, next step date, persona, notes, and activity/task logging.
- Persists CRM fields, verified contacts, and activity history in Supabase when configured, with a local SQLite fallback (`application0_crm.sqlite3`).
- Creates a GovDash demo asset pack, award-specific demo flow, discovery questions, email copy, call opener, and a 14-day sequence.
- Exports account radar, award-level records, public intel, and CRM-ready cadence CSVs.
- Shows product gaps and recommended next upgrades such as verified enrichment, SAM.gov detail, durable CRM storage, and activity sync.

## Setup

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install -r requirements.txt
```

## Run

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m streamlit run app.py
```

## Supabase Storage

1. Create a Supabase project.
2. Open the Supabase SQL Editor and run `supabase_schema.sql`.
3. Copy `streamlit-secrets.example.toml` into local `.streamlit/secrets.toml` or Streamlit Community Cloud secrets.
4. Fill in `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.

When those two secrets are present and the schema exists, Application 0 uses Supabase for CRM accounts, verified contacts, and activity history. If Supabase is not configured, the app continues using local SQLite.

## Deploy

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Streamlit Community Cloud deployment checklist.

## Data Notes

The current prototype uses USAspending because the public API does not require authorization and includes federal award recipient data. Before pulling the full lead list, the app runs a one-record freshness check sorted by USAspending `Last Modified Date` with the same filters. Responses are cached for 30 minutes, and the app retries transient API failures before surfacing an error.

Freshness labels are based on the newest matching USAspending modification: `Current` is within 7 days, `Aging` is 8 to 14 days, `Stale` is more than 14 days, and `No matching data` means the API responded but the filters found no records.

Public award data usually does not include verified direct emails or phone numbers. The app now performs a best-effort public web scan and records source URLs for any names, emails, phone numbers, LinkedIn result signals, announcements, interviews, podcasts, pain evidence, and other call-intel triggers it finds. It does not bypass LinkedIn login, other logins, or paywalls, and SDRs should verify each contact and pain point before outreach.

The contact list uses a readiness gate. `Ready to verify` means there is a named, relevant contact with enough source evidence for an SDR to manually confirm. `Verify first` means it is a research lead. `Not ready` means the app did not find enough public evidence and the SDR should use manual LinkedIn research or a verified enrichment provider before sequencing.

Verified contacts can be added manually or imported from CSV enrichment exports. Accepted CSV columns include `company`, `full_name` or `name`, `title`, `email`, `phone`, `linkedin_url` or `linkedin`, `source_url` or `source`, `source_type`, `verification_status` or `status`, and `notes`.

SAM.gov Contract Awards can be added later with a SAM.gov public API key for deeper award records. Verified enrichment vendors such as CRM/contact-data providers can also be connected later if you want higher-confidence direct dials and emails.
