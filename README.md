# Purdue Macro Finder

A web app that helps Purdue students build dining-court meals that hit specific macro targets (protein, carbs, fat). Pulls live menu data from Purdue's dining API and uses simulated annealing to optimize meal combinations.

## Live deployment

Hosted on [Render](https://render.com). Pushing to `main` on GitHub auto-deploys.

## Local development

```bash
# 1. Set up Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run
python app.py
# Open http://localhost:5000
```

Or just double-click `start_app.command` from the parent folder if you're on macOS.

## Deploying your own copy

1. Fork this repo on GitHub.
2. Sign up at [render.com](https://render.com) with your GitHub account.
3. New → Blueprint → select your fork. Render reads `render.yaml` automatically.
4. Deploy. The first build takes ~3 minutes.

## Stack

- **Backend**: Flask + gunicorn, Flask-Limiter for rate limiting
- **Frontend**: Single-file vanilla JS in `index.html`
- **Optimization**: Simulated annealing with configurable weights/penalties (`config.py`)
