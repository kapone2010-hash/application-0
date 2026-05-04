# Deploy Application 0

The fastest deployment path is Streamlit Community Cloud because this app is already a Streamlit app and has a root-level `app.py` plus `requirements.txt`.

## 1. Put the App on GitHub

The app is already on GitHub:

- Repository: `https://github.com/kapone2010-hash/application-0`
- Branch: `main`
- Main file: `app.py`

To push future changes from this computer:

```powershell
cd "C:\Users\Aniya\OneDrive\Documents\New project 3"
git status
git add app.py README.md requirements.txt DEPLOYMENT.md .github/workflows/ci.yml
git commit -m "Update Application 0"
git push origin main
```

## 2. Deploy on Streamlit Community Cloud

Direct deploy URL:

```text
https://share.streamlit.io/deploy?repository=kapone2010-hash/application-0&branch=main&mainModule=app.py
```

1. Go to Streamlit Community Cloud.
2. Sign in with GitHub.
3. Create a new app.
4. Select `kapone2010-hash/application-0`.
5. Use branch `main`.
6. Set the main file path to:

```text
app.py
```

7. Deploy.

## 3. Add Supabase Secrets

Before sharing the app, open Supabase and run `supabase_schema.sql` in the SQL Editor. Then add these secrets in Streamlit Community Cloud app settings:

```toml
SUPABASE_URL = "https://your-project-ref.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "your-service-role-key"
SAM_API_KEY = "your-sam-gov-api-key"
HUNTER_API_KEY = "your-hunter-api-key"
```

The deployed app will use Supabase for CRM accounts, verified contacts, and activity history when those secrets are configured. Without them, it falls back to local SQLite, which is not durable across cloud redeploys. `SAM_API_KEY` enables the optional SAM.gov enrichment button in Public Intel. `HUNTER_API_KEY` enables optional Hunter.io contact enrichment in Contact Finder.

## 4. After Deploy

- Open the deployed Streamlit URL.
- Test the default filters.
- Confirm recent awards load.
- Confirm the sidebar says `Using Supabase`.
- Share the URL with SDR users.

## Troubleshooting

- If dependencies fail, confirm `requirements.txt` is in the repository root.
- If the app cannot find `app.py`, confirm the main file path is exactly `app.py`.
- If no leads appear, widen the date range or lower the minimum award amount.
- If USAspending is temporarily unavailable, retry after a few minutes.
- If the deployed app redirects to Streamlit sign-in instead of opening for a prospect, open the app in Streamlit Community Cloud, go to app settings/share settings, and make sure the app is public or shared with the viewer.
- If public/LinkedIn scanning feels slow, the app now stops the scan after a short time budget and keeps whatever public evidence it found. Use the role-specific LinkedIn buttons when search engines return no public result data.
