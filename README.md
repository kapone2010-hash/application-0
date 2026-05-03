# Application 0: GovDash SDR Prospecting

A Streamlit prototype for finding government contractors that recently won public contract awards, then turning each award into an SDR-ready GovDash demo angle.

## What It Does

- Pulls recent federal contract awards from the USAspending public API.
- Ranks winners by a simple GovDash fit score.
- Lists award details, UEI, public address, NAICS/PSC, agency, amount, and dates.
- Generates public research links for company site, leadership, LinkedIn, news, USAspending, and SAM.gov.
- Suggests likely SDR personas such as capture, proposal, BD, contracts, and technical leaders.
- Creates a GovDash demo talk track based on the specific contract the company won.
- Exports the lead board to CSV.

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

The current prototype uses USAspending because the public API does not require authorization and includes federal award recipient data. Public award data usually does not include verified direct emails or phone numbers, so the app provides public address data, suggested personas, and contact-discovery links rather than inventing contact details.

SAM.gov Contract Awards can be added later with a SAM.gov public API key for deeper award records.
