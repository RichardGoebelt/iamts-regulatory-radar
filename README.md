# IAMTS Regulatory & Policy Radar – GitHub Pages Edition

This package publishes the **IAMTS Regulatory & Policy Radar – Connected & Automated Driving** as a static GitHub Pages website and updates the regulatory data automatically once per week.

There is **no browser-to-authority fetch, no CORS proxy, no backend server, no database, no login and no API key**. GitHub Actions performs the source retrieval on the server side and creates `public/radar.json`. The browser only reads that static JSON file.

## What is included

- `public/index.html` – the complete English browser application (HTML/CSS/JavaScript, no external libraries)
- `public/radar.json` – current published monitoring snapshot
- `scripts/update_radar.py` – source retrieval, classification, priority scoring and weekly comparison
- `data/state.json` – previous successful source state used for `New / Updated / No change`
- `.github/workflows/radar.yml` – automatic update and GitHub Pages deployment
- `tests/` – small offline tests for the monitoring logic

## Fixed official source scope

The source configuration is clearly separated near the top of `scripts/update_radar.py`.

Configured source groups:

- UNECE GRVA
- UNECE WP.29
- European Commission DG GROW publications / harmonised standards
- European Commission CCAM / automotive legislation
- U.S. Federal Register – NHTSA
- NHTSA
- U.S. DOT
- China MIIT
- China SAMR / SAC OpenSTD

An unavailable source is recorded as **Currently unavailable**. Other sources continue. Missing information is never replaced with sample regulatory developments.

## Installation – simplest route

### 1. Create a GitHub repository

Create a new repository, for example:

`iamts-regulatory-radar`

For GitHub Free, make it **Public** if you want to use GitHub Pages without a paid GitHub plan.

### 2. Upload this package

Extract the ZIP and upload the complete contents to the repository. Keep the folders exactly as supplied, especially:

`.github/workflows/radar.yml`

Commit to the `main` branch.

### 3. Enable GitHub Pages

Open the repository and go to:

**Settings → Pages → Build and deployment → Source → GitHub Actions**

### 4. Run the radar once

Open:

**Actions → Update and deploy IAMTS Radar → Run workflow**

The workflow will:

1. test the monitoring logic,
2. retrieve the official sources,
3. generate `public/radar.json`,
4. compare it with the previous successful source state,
5. commit the new monitoring state,
6. publish the website to GitHub Pages.

The resulting address will normally look like:

`https://YOUR-USERNAME.github.io/iamts-regulatory-radar/`

### 5. Automatic weekly update

The supplied workflow runs every **Monday at 07:17 Europe/Berlin time**. The non-round minute is intentional because GitHub notes that scheduled Actions can be delayed during high load, especially at the start of an hour.

You can also trigger an immediate source check at any time via **Actions → Run workflow**.

## If the workflow cannot save `radar.json`

The workflow requests `contents: write`. Some organisation policies may still restrict the `GITHUB_TOKEN`.

If the step **Save monitoring state for next weekly comparison** fails with a permission error, check:

**Settings → Actions → General → Workflow permissions**

and permit read/write workflow access, if your organisation policy allows it.

## How weekly comparison works

`data/state.json` retains the last **successful** check for each source separately.

- New ID → `New`
- Same ID, changed content hash → `Updated`
- Same ID, same content hash → `No change`

If one source is temporarily unavailable, its previous monitoring state is retained internally but its old entries are **not shown as current live results**. When that source becomes available again, comparison resumes against its last successful state rather than falsely marking everything as new.

## Refresh Radar button

The browser's **Refresh Radar** button reloads the newest published `radar.json`. It does not scrape official websites from the browser. The official source retrieval occurs only in the GitHub Action.

## Important Version 1 limitation

This is a deliberately simple rule-based regulatory radar. Official websites can change their markup. A source can therefore become unavailable until its adapter is adjusted. This condition is visible in **Source Status** and does not break the remaining sources.
