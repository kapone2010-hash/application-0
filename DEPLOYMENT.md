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
