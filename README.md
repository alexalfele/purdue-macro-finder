# Purdue Macro Finder

A web app that builds a Purdue dining court plate matching your protein, carb, and fat
targets, from what is actually on the menu, and tells you where and when to get it.

## What it does

- **Right now or plan ahead.** Search the meals being served at this moment (using each
  dining court's posted hours), or pick a date and meal periods.
- **Every station, not just entrées.** Salad bars, deli bars, pasta and pizza bars, stir-fry
  and taco stations list their foods as components; those are all searchable, labelled with
  the station they're served at.
- **Where and when.** Each result names the court, the meal, and that meal's hours, and
  shows the best plate at every other dining court too, so you can pick the closest one.
- **Realistic plates.** One meal period at one court per plate, up to five servings, at most
  two of any one food (how 60 g+ protein targets become reachable).
- **Dietary filters and exclusions.** Vegetarian, vegan, and gluten-free (excludes anything
  Purdue tags with gluten or wheat), plus a free-text list of foods to skip.
- **Remembers you, privately.** Settings are kept in your browser's local storage, and
  Copy link makes a shareable URL of the current search. Nothing is stored server-side.
- Works on phones, follows the system light/dark setting, and keyboard-accessible.

## How it works

- **Data.** Menus come from Purdue Dining's public GraphQL API
  (`https://api.hfs.purdue.edu/menus/v3/GraphQL`). Each day's response is cached on disk
  (`menu_cache_<date>.json`, versioned) and in memory. Today's and future menus refresh
  after 12 hours; partial or unpublished days are retried after 10 minutes, and a stale
  menu keeps being served while a fresh one loads in the background. Today's and
  tomorrow's menus are preloaded at startup.
- **Time.** "Today" and "right now" are computed in Purdue's time zone
  (America/Indiana/Indianapolis), not the server clock, which is UTC on Render.
- **Optimizer** (`meal_finder_engine.py`). For each (dining court, meal period) it searches
  plates of 2 to 5 servings: simulated annealing from several greedy seeds, then a local
  search over every one- and two-serving change, scoring weighted, asymmetric misses
  (under-protein, over-carb, and over-fat cost more). On random menus it finds the
  brute-force optimum 92% of the time and otherwise lands within a few grams, at about
  10 ms per court and meal. Tunables live in `config.py`.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

Or double-click `start_app.command` from the parent folder on macOS.

Run the tests (no network access needed; Purdue's API is stubbed):

```bash
python -m unittest test_file -v
```

## Deploying

Hosted on [Render](https://render.com). Pushing to `main` on GitHub auto-deploys.

1. Fork this repo on GitHub.
2. Sign up at [render.com](https://render.com) with your GitHub account.
3. New → Blueprint → select your fork. Render reads `render.yaml` automatically.
4. Deploy. The first build takes about 3 minutes.

GitHub Actions runs the test suite on every push and pull request
(`.github/workflows/tests.yml`).

## Security and privacy

- Only explicit routes are served: the three pages and an allow-list of files in
  `static/`. Source files, `.env`, and caches are not reachable over HTTP.
- Strict Content Security Policy with no `'unsafe-inline'` and no third-party origins;
  Chart.js 4.6.1 is self-hosted (`static/chart.umd.min.js`, MIT licensed), so visitors'
  browsers contact no other site.
- HSTS, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, COOP, and a restrictive
  Permissions-Policy on every response; API responses are `no-store`.
- Per-IP rate limits on the API, and validation of every request field.
- Static assets are content-hashed, gzip-compressed, and cached for a year.

## Project layout

```
app.py                 Flask app: routes, validation, headers, static serving
meal_finder_engine.py  Menu fetching/caching, optimizer, "right now" logic
config.py              Tunables: weights, limits, cache TTLs, time zone, courts
index.html             Main page (markup only)
static/                app.js, app.css, legal.css, chart.umd.min.js
terms.html, privacy.html
test_file.py           Unit and HTTP tests
render.yaml, Procfile  Deployment
```

Not affiliated with Purdue University or Purdue University Dining.
