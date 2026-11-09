# Purdue Macro Finder

A web app that builds a Purdue dining court plate matching your protein, carb, and fat
targets, from what is actually on the menu, and tells you where and when to get it.

## What it does

- **Right now or plan ahead.** Search the meals being served at this moment (using each
  dining court's posted hours), or pick a date and meal periods. When nothing is open,
  Right now plans the next meal instead (breakfast tomorrow, after dinner closes).
- **Swap anything.** "Not this" on any item leaves that food off and rebuilds the plate at
  the same dining court; the foods left off show as chips you can tap to put back.
- **Every station, not just entrées.** Salad bars, deli bars, pasta and pizza bars, stir-fry
  and taco stations list their foods as components; those are all searchable, labelled with
  the station they're served at.
- **Where and when.** Each result names the court, the meal, and that meal's hours, and
  shows the best plate at every other dining court too, so you can pick the closest one.
- **Realistic plates.** One meal period at one court per plate, up to five different foods,
  each in about a normal helping: two pork tenderloins, or a 16-slice stack of deli turkey
  (Purdue lists turkey one 3.7 g-protein slice at a time). Sauces, dressings, toppings and
  spreads come with something from their station to go on, and buns with a filling.
- **Diet and allergen filters.** Vegetarian and vegan, and avoid gluten/wheat, milk, eggs,
  soy, peanuts, tree nuts, fish, shellfish, sesame, or coconut, using Purdue's allergen
  tags. Each item lists what it contains. Plus a free-text list of foods to skip.
- **More than macros.** Live calorie estimate for your targets; each plate also reports
  fiber, sugar, saturated fat, and sodium.
- **Consistent.** The same search always returns the same plate, so a shared link shows
  exactly what the sender saw.
- **Remembers you, privately.** Settings are kept in your browser's local storage, and
  Copy link makes a shareable URL of the current search. Nothing is stored server-side.
- Installable to a phone's home screen, works on phones, follows the system light/dark
  setting, and keyboard-accessible.

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
  plates of up to 5 different foods, each capped at about a normal helping (350 kcal, 2
  cups or 8 oz; at least 2 servings, at most 20 for protein-dense foods and 6 otherwise).
  Simulated annealing runs from one-serving-at-a-time and portion-at-a-time greedy seeds,
  then a local search over one- and two-serving changes, paired amount changes, and
  restacking, scoring weighted, asymmetric misses (under-protein, over-carb, and over-fat
  cost more). Moves that would strand an add-on (a sauce with nothing to go on, a bun with
  no filling) are skipped. On random entree-style menus it finds the brute-force optimum
  58 times in 60, at about 6 ms per court and meal. Searches are deterministic, so recent
  results are cached in memory and repeats are instant. Tunables live in `config.py`.

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
static/                app.js, app.css, legal.css, chart.umd.min.js, manifest and icons
terms.html, privacy.html
test_file.py           Unit and HTTP tests
render.yaml, Procfile  Deployment
```

Not affiliated with Purdue University or Purdue University Dining.
