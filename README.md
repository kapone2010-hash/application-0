# Application 0: GovDash SDR Prospecting

A Streamlit prototype for finding government contractors that recently won public contract awards, then turning each award into an SDR-ready GovDash demo angle.

## What It Does

- Pulls recent federal contract awards from the USAspending public API.
- Checks USAspending source freshness before the full lead pull using the newest `Last Modified Date` for the current filters.
- Groups award winners into account-level views.
- Flags duplicate or parent/subsidiary account risk using UEI, company domain, normalized-name similarity, address, and state overlap before HubSpot sync.
- Ranks accounts by GovDash fit, recent award value, number of awards, agencies, NAICS/PSC, and follow-on signals.
- Adds a dynamic SDR action queue that blends award fit, verified contacts, public call-intel signals, pain evidence, open tasks, and contact readiness.
- Lists award details, UEI, public address, NAICS/PSC, agency, amount, and dates.
- Scans public web sources for company descriptions, likely website, source pages, public business emails/phones, named contacts, call-intel signals, evidence-backed pain points, and public LinkedIn result signals when available.
- Enriches active accounts with SAM.gov award-notice context when `SAM_API_KEY` is configured, including notice type, solicitation number, set-aside, NAICS/PSC, contracting organization, place of performance, government POC, and source links when available.
- Explains what the company won and gives a reasoned hypothesis for why it may have won, without pretending USAspending exposes evaluation rationale.
- Generates public research links for company site, leadership, LinkedIn, news, USAspending, and SAM.gov.
- Ranks the best contact targets for each account, explains why each role matters, and gives source-backed public contacts, LinkedIn profile-result signals, and search links to verify named people.
- Uses Hunter.io contact enrichment when `HUNTER_API_KEY` is configured, ranking professional emails by role relevance, confidence, verification status, phone availability, and source evidence.
- Scores contact-list freshness and relevance before SDR use, including source freshness, role match, named-person status, business email/phone availability, and next verification step.
- Adds a verified-contact Sequence Gate with verified age, evidence grade, and SDR action so reps know whether a person is ready to sequence, needs recheck, or should be blocked.
- Adds confidence labels across major account, contact, pain-point, call-intel, source-audit, Hunter, HubSpot, SAM.gov, and pursuit-package tables so SDRs can distinguish official sources, verified sources, vendor enrichment, public sources, hypotheses, stale data, and items needing review.
- Adds a selected-account sales cockpit with readiness state, action score, contact readiness, domain status, HubSpot link status, award value, call signals, pain signals, and next best action.
- Builds a source audit trail for contacts, pain points, call-intel signals, and scanned pages with source URL, capture/verification timestamp, evidence snippet, audit status, and SDR action.
- Persists source-audit snapshots with reviewer/owner and review-note fields so evidence can be defended after outreach.
- Saves verified contacts from manual research or enrichment CSV exports so verified people outrank public web guesses.
- Syncs active companies, verified contacts, individual CRM activities, and one-click 14-day cadence task launches into HubSpot when `HUBSPOT_ACCESS_TOKEN` is configured and the private app has the needed activity scopes.
- Pulls call-relevance signals beyond the award, including public LinkedIn updates/search signals, announcements, past press releases, podcasts/interviews, hiring/growth, partnerships, webinars, and leadership changes when public sources expose them.
- Categorizes each account by industry and separates company-specific pain evidence from industry benchmark pain points that SDRs should verify on the call.
- Shows "why now" triggers and recommended next best actions.
- Generates downloadable call-prep and account research briefs with account context, what they won, why they may have won, best contact path, pain points, call intel, trust gaps, objections, discovery questions, CRM state, and GovDash demo angle.
- Adds CRM-style fields for status, owner, cadence stage, email/call tracking, outcomes, next step date, persona, notes, and activity/task logging.
- Persists CRM fields, verified contacts, and activity history in Supabase when configured, with a local SQLite fallback (`application0_crm.sqlite3`).
- Creates a GovDash demo asset pack, award-specific demo flow, discovery questions, email copy, call opener, and a 14-day sequence.
- Creates a one-click full pursuit package that runs public intel, SAM.gov enrichment, Hunter enrichment, HubSpot duplicate/sync, account brief generation, and cadence prep for the active company.
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

When those two secrets are present and the schema exists, Application 0 uses Supabase for CRM accounts, verified contacts, activity history, and saved source-audit snapshots. If Supabase is not configured, the app continues using local SQLite.

## SAM.gov Enrichment

Add this secret locally and in Streamlit Community Cloud to enable official SAM.gov award-notice context:

```toml
SAM_API_KEY = "your_sam_gov_api_key"
```

SAM.gov enrichment is run on demand from the Public Intel tab for the active account. Government points of contact returned by SAM.gov are shown as procurement context, not as SDR targets at the contractor company.

## Hunter Contact Enrichment

Add this secret locally and in Streamlit Community Cloud to enable Hunter.io Domain Search in Contact Finder:

```toml
HUNTER_API_KEY = "your_hunter_api_key"
```

Hunter results are review-first. The app shows Hunter confidence, verification status, role signals, source URLs, and lets the SDR save selected people into verified contacts.

## HubSpot Sync

Add this secret locally and in Streamlit Community Cloud to sync companies, verified contacts, and CRM cadence activities:

```toml
HUBSPOT_ACCESS_TOKEN = "your_hubspot_private_app_token"
```

The current HubSpot integration syncs companies and verified contacts in one click. SDRs do not need to type a domain or run a separate check: the app auto-detects the company domain from public company intel or verified-contact email domains, falls back to a quick public website search when needed, and runs the HubSpot duplicate check automatically during sync. If no saved contact has an email and Hunter is configured, the same click attempts Hunter enrichment, imports up to five email-ready contacts as review-needed records, and syncs them to HubSpot. After each run, the Contact Finder tab shows a Last HubSpot Sync Results panel with the domain decision, duplicate result, company ID, contact counts, skipped contacts, errors, and next action.

The CRM Cadence tab can also create HubSpot timeline activities. Planned follow-ups become HubSpot tasks, notes become HubSpot notes, and completed or outcome-based calls become HubSpot calls. The 14-day cadence launcher creates six dated follow-up activities in Application 0 and can create the matching HubSpot tasks in one click. If HubSpot denies a specific activity object because the private app is missing that scope, Application 0 still saves the activity locally/Supabase and shows a warning.

Before creating a HubSpot company, Application 0 searches HubSpot by domain, exact name, and fuzzy name token match. Exact matches update the existing company. Likely fuzzy duplicates are blocked for manual review instead of creating another company record.

## Deploy

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Streamlit Community Cloud deployment checklist.

## Data Notes

The current prototype uses USAspending because the public API does not require authorization and includes federal award recipient data. Before pulling the full lead list, the app runs a one-record freshness check sorted by USAspending `Last Modified Date` with the same filters. Responses are cached for 30 minutes, and the app retries transient API failures before surfacing an error.

Freshness labels are based on the newest matching USAspending modification: `Current` is within 7 days, `Aging` is 8 to 14 days, `Stale` is more than 14 days, and `No matching data` means the API responded but the filters found no records.

Public award data usually does not include verified direct emails or phone numbers. The app now performs a best-effort public web scan and records source URLs for any names, emails, phone numbers, LinkedIn result signals, announcements, interviews, podcasts, pain evidence, and other call-intel triggers it finds. It does not bypass LinkedIn login, other logins, or paywalls, and SDRs should verify each contact and pain point before outreach.

The contact list uses a readiness gate. `Ready to verify` means there is a named, relevant public contact with enough source evidence for an SDR to manually confirm. `Verify first` means it is a research lead. `Not ready` means the app did not find enough public evidence and the SDR should use manual LinkedIn research or a verified enrichment provider before sequencing.

Verified contacts use a stricter Sequence Gate. `Ready to sequence` requires a verified current role, source evidence, and a usable business email or phone. `Verify before sequence`, `Verify missing fields`, and `Recheck before sequence` tell the SDR exactly what needs to be confirmed before outreach. `Do not sequence` keeps blocked contacts out of cadence. The 14-day cadence launcher is blocked unless the selected verified contact passes this gate.

The Source Audit Trail is available after public scans and in Contact Finder. It gives SDRs a downloadable evidence table for contacts, pain points, call-intel signals, and scanned pages so they can see where each recommendation came from before calling or emailing. SDRs can also save an audit snapshot with reviewer/owner and review notes.

The Account Radar tab includes an Account Dedupe & Parent/Subsidiary Risk table. It is a review queue, not an automatic merge tool: use it before syncing to HubSpot so activity, contacts, and cadence tasks do not get split across duplicate company records.

Verified contacts can be added manually or imported from CSV enrichment exports. Accepted CSV columns include `company`, `full_name` or `name`, `title`, `email`, `phone`, `linkedin_url` or `linkedin`, `source_url` or `source`, `source_type`, `verification_status` or `status`, `verified_by` or `reviewer`, `verification_method` or `method`, and `notes`.

Verified enrichment vendors such as CRM/contact-data providers can also be connected later if you want higher-confidence direct dials and emails.
