# Application 0: GovDash SDR Prospecting

A Streamlit prototype for finding government contractors that recently won public contract awards, then turning each award into an SDR-ready GovDash demo angle.

## What It Does

- Pulls recent federal contract awards from the USAspending public API.
- Groups award winners into account-level views.
- Ranks accounts by GovDash fit, recent award value, number of awards, agencies, NAICS/PSC, and follow-on signals.
- Lists award details, UEI, public address, NAICS/PSC, agency, amount, and dates.
- Generates public research links for company site, leadership, LinkedIn, news, USAspending, and SAM.gov.
- Suggests likely SDR personas such as capture, proposal, BD, contracts, and technical leaders.
- Shows "why now" triggers and recommended next best actions.
- Adds CRM-style fields for status, owner, next step date, persona, and notes.
- Creates GovDash demo flow, discovery questions, email copy, call opener, and a 14-day sequence.
- Exports account radar, award-level records, and CRM-ready CSVs.

## Setup

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pip install -r requirements.txt
```

## Run

```powershell
& 'C:\Users\Aniya\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m streamlit run app.py
```

## Deploy

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the GitHub and Streamlit Community Cloud deployment checklist.

## Data Notes

The current prototype uses USAspending because the public API does not require authorization and includes federal award recipient data. Responses are cached for 30 minutes, and the app retries transient API failures before surfacing an error.

Public award data usually does not include verified direct emails or phone numbers, so the app provides public address data, suggested personas, and contact-discovery links rather than inventing contact details.

SAM.gov Contract Awards can be added later with a SAM.gov public API key for deeper award records.
