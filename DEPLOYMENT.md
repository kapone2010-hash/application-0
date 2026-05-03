# Deploy Application 0

The fastest deployment path is Streamlit Community Cloud because this app is already a Streamlit app and has a root-level `app.py` plus `requirements.txt`.

## 1. Put the App on GitHub

This computer does not currently have `git` installed, so use one of these paths:

### Option A: Install Git and Push

1. Install Git for Windows.
2. Open PowerShell in this folder:

```powershell
cd "C:\Users\Aniya\OneDrive\Documents\New project 3"
```

3. Run:

```powershell
git init
git add .env.example .gitignore app.py README.md requirements.txt DEPLOYMENT.md .github/workflows/ci.yml
git commit -m "Initial Application 0 prototype"
git branch -M main
git remote add origin YOUR_GITHUB_REPO_URL
git push -u origin main
```

Replace `YOUR_GITHUB_REPO_URL` with the HTTPS URL for your empty GitHub repository.

### Option B: Upload Files in the GitHub Website

Create a new GitHub repository, then upload these files:

- `.env.example`
- `.gitignore`
- `app.py`
- `README.md`
- `requirements.txt`
- `DEPLOYMENT.md`
- `.github/workflows/ci.yml`

## 2. Deploy on Streamlit Community Cloud

1. Go to Streamlit Community Cloud.
2. Sign in with GitHub.
3. Create a new app.
4. Select your GitHub repository.
5. Use branch `main`.
6. Set the main file path to:

```text
app.py
```

7. Deploy.

## 3. After Deploy

- Open the deployed Streamlit URL.
- Test the default filters.
- Confirm recent awards load.
- Share the URL with SDR users.

## Troubleshooting

- If dependencies fail, confirm `requirements.txt` is in the repository root.
- If the app cannot find `app.py`, confirm the main file path is exactly `app.py`.
- If no leads appear, widen the date range or lower the minimum award amount.
- If USAspending is temporarily unavailable, retry after a few minutes.
