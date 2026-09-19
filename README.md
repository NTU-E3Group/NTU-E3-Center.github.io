# NTU E3 Center Official Website (legacy)

> **The site has moved to [e3center.net](https://e3center.net).**
>
> Since September 2026 every page served at [e3center.caece.net](https://e3center.caece.net)
> is a client-side redirect to the same path on the new site. This repository exists to keep that
> redirect alive: **do not delete or archive it**, and leave `static/CNAME` and the GitHub Pages
> settings as they are. The new site's source code lives in its own repository, not here.
>
> - **Old site preserved:** the last version without any redirect code is on the
>   [`legacy-site`](https://github.com/NTU-E3Group/NTU-E3-Center.github.io/tree/legacy-site) branch.
>   It is a frozen snapshot; do not commit to it.
> - **How the redirect works:** GitHub Pages cannot send HTTP 301s, so each page carries an inline
>   script (preserves path, query and hash) plus a 0-second `meta refresh` fallback. The destination
>   is `REDIRECT_HOST` in `config.py`; the snippet is `templates/partials/redirect.html`, included by
>   `base.html` and the four standalone item templates. `static/404.html` catches unknown paths.
> - **Why the full generator is still here:** every old URL keeps returning HTTP 200 with an instant
>   redirect, and `sitemap.xml` still lists the old URLs, so search engines recrawl them and transfer
>   them to the new site. Once search results have moved over, `source` can be slimmed down to a
>   static redirect (`index.html`, `404.html`, `CNAME`, `robots.txt`); the generator stays on `legacy-site`.

Live site: [e3center.net](https://e3center.net) — this repository serves the redirect at
[e3center.caece.net](https://e3center.caece.net).

Project started: Fall 2023

**March 2026 – Present:**
Front-End Development and Maintenance: Jian Hern Yeoh

**March 2025 – Feb 2026:**
Maintenance: Jun-Wei Ding (Gary)

**Fall 2023 – Fall 2025:**
Design / Development / Maintenance: [Yuan-Hsi Chien (Thomas)](https://github.com/dobahsi)

For bug reports and suggestions, contact Jian Hern or Gary.

---

## How It Works

This site uses a custom Python static site generator. Source files live in `contents/` and `templates/`; `build.py` compiles them into `docs/` (gitignored — the build output never lives on the `source` branch).

Pushing to the `source` branch triggers a GitHub Actions workflow (`.github/workflows/deploy.yml`) that runs `build.py` and pushes the generated `docs/` to the `gh-pages` branch via `peaceiris/actions-gh-pages`. GitHub Pages serves the site from `gh-pages`.

---

## Local Development

### Prerequisites

- Conda (Miniconda or Anaconda) with Python 3.12

### Setup (first time)

```bash
conda create -n E3website python=3.12
conda activate E3website
pip install -r requirements.txt
```

### Build and preview

```bash
conda activate E3website

# Build the site
python build.py

# Serve locally (run from the repo root)
python -m http.server 8000 --directory docs
# Open http://localhost:8000
```

> `docs/` is overwritten on every build. Never edit files there directly.

---

## Editing Content

All content is data-driven — no Python or HTML edits required. After making changes, build and preview locally before pushing.

### Adding or updating a member

Member data has **two halves**:

- **Admin fields** — `contents/members/member-info.xlsx` (11 columns: WebID, Full Name, Nickname, Chinese Name, Website Section, Admission Year, Graduated, Also in Alumni, Alumni Admission Year, Current Position, Batch). You control these.
- **Content fields** — `contents/members/{webId}/`: `member.json` (position, emails, interests, profile links), `about.md` (bio prose), `photo.{jpg,png}`. Members supply these.

**Update an existing member:**

```bash
# Send their folder + the instructions
cp -r contents/members/{webId} /tmp/pkg/
cp contents/members/MEMBER_TEMPLATE/README.md /tmp/pkg/
# zip /tmp/pkg/ and email it
```

They edit `member.json` + `about.md` and send the folder back. Drop the returned files into `contents/members/{webId}/` (overwriting), run `python build.py`, review `git diff`, commit.

**Add a new member:**

1. Add a row to `contents/members/member-info.xlsx` with the 11 admin columns.
2. Seed their folder: `cp -r contents/members/MEMBER_TEMPLATE contents/members/{webId}`
3. Send the new folder + `README.md` to the member.
4. When returned, overwrite `contents/members/{webId}/` with their files and drop their `photo.{jpg,png}` into the same folder.
5. `python build.py` — the build **fails** if `member.json` has invalid JSON or a schema mismatch, **warns** about a missing `about.md` or photo.

`pubName` (used to auto-populate a member's publications) is derived from the `Full Name` admin column — no separate field to maintain. `contents/members/member-info.legacy.xlsx` is a read-only archive of the original spreadsheet — never edit it.

### Adding a publication

Open `contents/publications/publications.json`. Find the right section (e.g., `"Journal Articles"`, `"Conference Papers"`) and add an entry:

```json
{
  "citationId": "lastname2025title",
  "authors": "F. Lastname, A. Coauthor",
  "title": "Publication Title",
  "journal": "Journal Name",
  "year": "'25",
  "month": "Jan.",
  "E3": true,
  "status": "published"
}
```

- `citationId` — unique identifier
- `year` / `month` — the journal **issue** date, as a `"'YY"` string and a
  three-letter `"Mon."` abbreviation (not the "available online" date — see
  [CLAUDE.md](CLAUDE.md)). Sorting relies on this format.
- `E3: true` + `status: "published"` — needed to show on the homepage
- `authors` must include the member's `pubName` exactly for it to appear on their profile page automatically

### Adding a news item

1. Add an entry to `contents/news/news.json` with a `pageLink` like `/news/2026-foo/`.
2. Create the article body at `contents/news/articles/2026-foo.md`.
3. (Optional) Drop images into `contents/news/images/2026-foo/`. `0.{jpg,png,…}` becomes the hero; the rest appear in the gallery sorted numerically.

### Adding a project

Open `contents/projects/projects.json` and add an entry under the appropriate funding-source section.

### Adding group-life photos

1. Place the photo (JPG or PNG) in `contents/group-life/images/`. The build generates WebP variants at 200w through 2000w.
2. Register it in `contents/group-life/group-life.json`.

### Editing About / Contact / page text

- `contents/about/about.md`
- `contents/contact/contact.md`

Standard Markdown + `md_in_html` for inline HTML.

---

## Design & SEO

- **[DESIGN_RULES/](DESIGN_RULES/README.md)** — canonical reference for typography, color, spacing, and responsive scaling. Consult before changing CSS or adding UI.
- **[SEO/RUNBOOK.md](SEO/RUNBOOK.md)** — operator runbook for indexing new pages, Google Search Console workflows, and SEO warnings emitted by the build.
- **[SEO/](SEO/)** — full SEO research folder: analysis, strategy, recommendations.

## Project Structure

For a full directory map and the data-flow diagram, see **[STRUCTURE.md](STRUCTURE.md)**.

---

## Workflow Summary

```
Edit JSON / Markdown / images
        ↓
python build.py
        ↓
Preview at http://localhost:8000
        ↓
git add + commit + push to source branch
        ↓
GitHub Actions builds and deploys automatically
```
