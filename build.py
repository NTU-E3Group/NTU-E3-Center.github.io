import os
import re
import json
import shutil
import markdown
from PIL import Image, ImageOps
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

# ── Sync content from Excel before building ───────────────────────────────────
from lib.excel_to_content import build_member_data
from lib import seo_helpers
from config import (SITE_URL, OUTPUT_DIR, SUBPAGE_IMG_WIDTHS,
                    MEMBER_IMG_WIDTHS, LAZY_IMG_WIDTHS,
                    WEBP_QUALITY, WEBP_LAZY_QUALITY, REDIRECT_HOST)
_member_data = build_member_data()
# ─────────────────────────────────────────────────────────────────────────────

# Set up Jinja2 environment
env = Environment(loader=FileSystemLoader(['templates']),
                  trim_blocks=True,
                  lstrip_blocks=True)

# Register SEO helpers as Jinja globals so all templates can call them
env.globals['seo_meta_description'] = seo_helpers.generate_meta_description
env.globals['seo_strip_markdown'] = seo_helpers.strip_markdown
env.globals['seo_detect_language'] = seo_helpers.detect_language

# Site origin and redirect destination, so the head partial
# (templates/partials/redirect.html) and canonical links never hardcode a domain.
env.globals['SITE_URL'] = SITE_URL
env.globals['REDIRECT_HOST'] = REDIRECT_HOST


def bold_author(authors_str, name):
    """Wrap occurrences of `name` in an authors string with <strong>.
    Both inputs are HTML-escaped first; the search uses non-word/non-hyphen
    boundaries so "I-Yun Hsieh" does not match "I-Yun Hsieh-Chen". Also
    matches the collapsed-hyphen romanization variant ("Wan-Ting Hsu" ↔
    "Wanting Hsu") — papers print whichever form they like, and the bolded
    text keeps the paper's spelling. Returns Markup so the result is
    rendered as HTML, not as literal tags."""
    if not authors_str:
        return Markup('')
    escaped = str(escape(authors_str))
    if not name:
        return Markup(escaped)
    variants = {str(escape(name))}
    collapsed = re.sub(r'-(\w)', lambda m: m.group(1).lower(), name)
    variants.add(str(escape(collapsed)))
    alternation = '|'.join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    pattern = re.compile(r'(?<![\w\-])(?:' + alternation + r')(?![\w\-])')
    return Markup(pattern.sub(lambda m: f'<strong>{m.group(0)}</strong>', escaped))


env.filters['bold_author'] = bold_author

# Helper function to get sortable date from publication item
def get_pub_sort_key(item):
    # Extract year and handle 'YY format
    year_str = item.get('year', '0')
    if isinstance(year_str, str) and year_str.startswith("'"):
        year = int("20" + year_str[1:])
    else:
        try:
            year = int(year_str)
        except (ValueError, TypeError):
            year = 2000 # Fallback
            
    # Heuristic for month
    month_str = item.get('month', '')
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    month_num = 0
    for i, m in enumerate(months):
        if m in month_str.lower():
            month_num = i + 1
            break
            
    return (year, month_num)


# ── Publication detail-page helpers: slug + derived BibTeX ────────────────────
# A publication earns a detail page once it has an `abstract`. Its URL slug and
# its BibTeX are derived from the structured fields so there is a single source
# of truth (no duplicated blob to drift). An explicit `slug` or `bibtex` field,
# if present, overrides the derived value.

_BIBTEX_STOPWORDS = {"a", "an", "the", "on", "of", "in", "for", "and", "to", "with"}
_MONTHS3 = {"jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec"}


def pub_year4(item):
    """4-digit year string from a publication's `year` (handles the "'YY" form)."""
    y = str(item.get('year', '') or '')
    return ('20' + y[1:]) if y.startswith("'") else y


def slugify_title(text, max_words=7):
    """Lowercase, hyphenated slug keeping the first `max_words` significant words."""
    words = re.sub(r'[^a-z0-9\s-]', '', text.lower()).split()
    return '-'.join(words[:max_words]).strip('-')


def pub_slug(item):
    """Explicit `slug` wins; otherwise derive `<year>-<title-slug>`."""
    if item.get('slug'):
        return item['slug']
    return f"{pub_year4(item) or 'na'}-{slugify_title(item.get('title', ''))}".strip('-')


def proj_slug(item):
    """Suggest a project slug `<startYear>-<title-slug>` (parallel to pub_slug).

    The opt-in trigger is the explicit `slug` field in projects.json; this helper
    only generates a candidate value to paste in. An explicit `slug` always wins."""
    if item.get('slug'):
        return item['slug']
    year = (item.get('startDate') or '')[:4] or 'na'
    return f"{year}-{slugify_title(item.get('titleEn', ''))}".strip('-')


def _bibtex_authors(item):
    """'Last, First and Last, First'. Prefers structured authorList; falls back
    to the comma-separated `authors` string."""
    if item.get('authorList'):
        names = [a.get('name', '') for a in item['authorList'] if a.get('name')]
    else:
        names = [n.strip() for n in (item.get('authors') or '').split(',') if n.strip()]
    out = []
    for n in names:
        parts = n.split()
        out.append(f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) >= 2 else n)
    return ' and '.join(out)


def _bibtex_key(item):
    """Scholar-style key: <firstAuthorLastname><year><firstSignificantTitleWord>."""
    if item.get('authorList') and item['authorList']:
        first = item['authorList'][0].get('name', '')
    else:
        first = (item.get('authors') or '').split(',')[0]
    parts = first.split()
    last = re.sub(r'[^a-z0-9]', '', parts[-1].lower()) if parts else 'anon'
    word = ''
    for w in re.sub(r'[^a-z0-9\s-]', '', item.get('title', '').lower()).split():
        if w not in _BIBTEX_STOPWORDS:
            word = w.split('-')[0]
            break
    return f"{last}{pub_year4(item)}{word}"


def generate_bibtex(item):
    """Assemble a BibTeX entry from structured fields. An explicit `bibtex` field
    overrides the derived output verbatim."""
    if item.get('bibtex'):
        return item['bibtex']
    fields = [
        ('title', item.get('title')),
        ('author', _bibtex_authors(item)),
        ('journal', (item.get('journal') or '').replace('&', r'\&')),
        ('volume', item.get('volume')),
        ('number', item.get('issue')),
        ('pages', item.get('articleNo') or item.get('pages')),
        ('year', pub_year4(item)),
        ('month', (item.get('month') or '').strip('.').lower()[:3]),
        ('doi', item.get('doi')),
    ]
    rendered = []
    for k, v in fields:
        if not v:
            continue
        if k == 'month':
            if v in _MONTHS3:           # bare macro, no braces (per house style)
                rendered.append(f"  month={v}")
        else:
            rendered.append(f"  {k}={{{v}}}")
    if not rendered:
        return ''
    return "@article{" + _bibtex_key(item) + ",\n" + ',\n'.join(rendered) + "\n}"


def news_slug_from_pagelink(page_link):
    """Extract the news slug from a pageLink like '/news/2026-foo/' -> '2026-foo'."""
    return page_link.rstrip('/').rsplit('/', 1)[-1] if page_link else ''

# Load page structure from an external JSON file
with open("contents/pages.json", "r") as f:
    pages = json.load(f)

# Output directory
output_dir = OUTPUT_DIR

# Load JSON for each subpage from its dedicated folder. Each entry is
# (key_in_structures, path).
structures = {}
_SUBPAGE_JSON_SOURCES = [
    ('publications', 'contents/publications/publications.json'),
    ('news',         'contents/news/news.json'),
    ('research',     'contents/research/research.json'),
    ('group-life',   'contents/group-life/group-life.json'),
    ('about',        'contents/about/about.json'),
    ('contact',      'contents/contact/contact.json'),
    ('videos',       'contents/videos/videos.json'),
    ('projects',     'contents/projects/projects.json'),
]
for _key, _path in _SUBPAGE_JSON_SOURCES:
    with open(_path, 'r', encoding='utf-8') as _f:
        structures[_key] = json.load(_f)

# Use the in-memory members data returned by build_member_data() instead of
# re-reading the gitignored on-disk artifacts. The listing replaces the
# members.json the glob just loaded; members_by_id replaces the
# contents/structures/members/{webId}.json reads.
# Center members (PI + staff) vs. Prof. Hsieh's research-group students
# (Ph.D., Master, Alumni) — split once here so every template and JSON-LD
# block consumes the right roster without per-template filtering.
# Spec: specs/2026-08-05-students-split-design.md
_CENTER_SECTIONS = ('Principal Investigator', 'Staff')
structures['members'] = [
    g for g in _member_data['members_listing']
    if g['sectionTitle'] in _CENTER_SECTIONS]
structures['students'] = [
    g for g in _member_data['members_listing']
    if g['sectionTitle'] not in _CENTER_SECTIONS]
members_by_id = _member_data['members_by_id']
structures['members_by_id'] = members_by_id

# Annotate news listing items with a small thumbnail when their detail page
# has a hero image (0.* in contents/news/images/{slug}/). The listing shows
# the 200w WebP variant emitted by compress_and_convert_images().
_NEWS_IMG_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp')
for _section in structures.get('news', []):
    for _item in _section.get('items', []):
        _plink = _item.get('pageLink', '')
        if not _plink.startswith('/news/'):
            continue
        _slug = news_slug_from_pagelink(_plink)
        _folder = os.path.join('contents', 'news', 'images', _slug)
        if os.path.isdir(_folder) and any(
                os.path.exists(os.path.join(_folder, f'0{_e}')) for _e in _NEWS_IMG_EXTS):
            _item['thumb'] = f'/assets/news/{_slug}/0-200w.webp'

# Year-grouped view for the news listing page: newest year first, items
# newest-first within each year (source items are oldest-first).
_news_years = []
for _section in structures.get('news', []):
    for _item in _section.get('items', [])[::-1]:
        _yr = '20' + _item.get('year', "'00")[1:]
        if not _news_years or _news_years[-1]['year'] != _yr:
            _news_years.append({'year': _yr, 'items': []})
        _news_years[-1]['items'].append(_item)
structures['news_years'] = _news_years

# Filter and sort publications for home page
if 'publications' in structures:
    home_publications = []
    for section in structures['publications']:
        new_section = section.copy()
        # Filter items with E3: true and published status (exclude submitted/under review and books)
        filtered_items = [
            item for item in section.get('items', [])
            if item.get('E3') is True
            and item.get('status', 'published') == 'published'
            and item.get('type') != 'book'
        ]
        # Sort items descending by date
        filtered_items.sort(key=get_pub_sort_key, reverse=True)
        new_section['items'] = filtered_items
        home_publications.append(new_section)
    structures['home_publications'] = home_publications

# A publication earns a detail page once it has an `abstract`. Annotate those
# with their slug + pageLink (derived if not set explicitly) so the listing
# template links each such row to /publications/<slug>/. Mutates the shared
# structures dict before any template renders. Publications without an abstract
# are left untouched — they keep the original whole-row publisher link.
if 'publications' in structures:
    for _section in structures['publications']:
        for _item in _section.get('items', []):
            if _item.get('abstract'):
                _item['slug'] = pub_slug(_item)
                _item['pageLink'] = f"/publications/{_item['slug']}/"

# A project earns a detail page once its listing entry has a non-empty `slug`
# AND a matching contents/projects/<slug>/project.json exists (opt-in, mirroring
# publications-by-abstract). Annotate those entries with a pageLink so the
# listing + homepage rows link to /projects/<slug>/.
#
# Bucket the distinct funder strings down to 5 readable categories driven by
# the /projects filter bar — fine-grained enough to be useful, coarse enough to
# scan. An entry can pin its category with an explicit `funderBucket` field in
# projects.json; the string heuristic only runs when no override is present.
# Active-year ranges (the years each project spans) are pre-computed here
# too so the template doesn't recompute on every row.
def _funder_bucket(item):
    """Return one of: NSTC, Gov, NTU, Foundation, Industry.

    Categories follow the funder, not the collaboration mode: bilateral
    NSTC programs (NSTC-ICSSR, NSTC-NWO) stay in NSTC and the TUKUC entry
    folds into Gov under the Ministry of Education. Taipei City Public
    Transportation Office reads as a municipal/government counterpart and
    lands in Gov rather than Industry. Foundation covers 財團法人 sponsors
    that are not companies (MIRDC, Wego private school)."""
    if item.get('funderBucket'):
        return item['funderBucket']
    fz = item.get('fundingAgency') or ''
    fe = item.get('fundingAgencyEn') or ''
    if item.get('grantNumber'):
        return 'NSTC'
    if fz.startswith('國科會') or 'National Science and Technology Council' in fe:
        return 'NSTC'
    if fz.startswith('環境部') or fz.startswith('教育部') \
            or fz.startswith('臺北市') or 'Ministry of' in fe \
            or 'Taipei City' in fe:
        return 'Gov'
    if fz.startswith('國立臺灣大學') or 'National Taiwan University' in fe \
            or fe.startswith('NTU '):
        return 'NTU'
    if fz.startswith('財團法人'):
        return 'Foundation'
    return 'Industry'

if 'projects' in structures:
    for _section in structures['projects']:
        for _item in _section.get('items', []):
            _pslug = _item.get('slug')
            if _pslug and os.path.isfile(
                    os.path.join('contents', 'projects', _pslug, 'project.json')):
                _item['pageLink'] = f"/projects/{_pslug}/"
            _item['funderBucket'] = _funder_bucket(_item)
            _start_yr = (_item.get('startDate') or '')[:4]
            _end_yr   = (_item.get('endDate')   or '')[:4]
            if _start_yr and _end_yr and _start_yr.isdigit() and _end_yr.isdigit():
                _item['activeYears'] = [str(y) for y in
                                        range(int(_start_yr), int(_end_yr) + 1)]
            elif _start_yr:
                _item['activeYears'] = [_start_yr]
            else:
                _item['activeYears'] = []

# Load page-body markdown for each subpage that has one.
articles = {}
_PAGE_BODY_MD = [
    ('about',   'contents/about/about.md'),
    ('contact', 'contents/contact/contact.md'),
]
for _key, _md_path in _PAGE_BODY_MD:
    with open(_md_path, 'r', encoding='utf-8') as _f:
        _md = _f.read()
    articles[_key] = markdown.markdown(_md, extensions=['md_in_html'])
# News article markdown (keyed as 'news/<slug>' — render_news_pages reads
# articles[f'news/{slug}']).
_news_articles_dir = 'contents/news/articles'
if os.path.isdir(_news_articles_dir):
    for _fname in os.listdir(_news_articles_dir):
        if _fname.endswith('.md'):
            _slug = _fname[:-3]
            with open(os.path.join(_news_articles_dir, _fname), 'r', encoding='utf-8') as _f:
                _md = _f.read()
            articles[f'news/{_slug}'] = markdown.markdown(_md, extensions=['md_in_html'])

# Inject the in-memory member markdown (replaces what previously came from the
# disk artifacts under contents/articles/members-{about,position,interest}/).
# The members listing template reads `articles['members-position/<webId>']`.
for _wid, _mds in _member_data['members_md'].items():
    if _mds.get('about'):
        articles[f'members-about/{_wid}'] = _mds['about']
    if _mds.get('position'):
        articles[f'members-position/{_wid}'] = _mds['position']
    if _mds.get('interest'):
        articles[f'members-interest/{_wid}'] = _mds['interest']

# Function to render templates into correct directories
def render_templates():
    def process_pages(pages, base_path=""):
        for template_name, page_data in pages.items():
            if isinstance(page_data, dict) and "path" in page_data:
                template_file = page_data.get("template", template_name)
                template = env.get_template(f"{template_file}.html")
                path_segment = page_data["path"]
                canonical = f"{SITE_URL}/{path_segment}/" if path_segment else f"{SITE_URL}/"
                render_args = {
                    "pages": pages,
                    "title": page_data.get("title"),
                    "subpageTitle": page_data.get("subpageTitle"),
                    "suppressSrH1": page_data.get("suppressSrH1", False),
                    "canonicalLink": canonical,
                    "updated_time": datetime.now().strftime("%Y. %m. %d"),
                    "year": datetime.now().year,
                    "structures": structures,
                    "articles": articles
                }
                if "description" in page_data:
                    render_args["description"] = page_data["description"]
                output = template.render(**render_args)
                
                # Define full output path (subdirectories)
                page_dir = os.path.join(output_dir, base_path, page_data["path"])
                os.makedirs(page_dir, exist_ok=True)
                
                # Save the rendered HTML inside index.html
                with open(os.path.join(page_dir, "index.html"), "w", encoding="utf-8") as f:
                    f.write(output)
            elif isinstance(page_data, dict):  # If it's a nested structure without "path"
                process_pages(page_data, os.path.join(base_path, template_name))
    
    process_pages(pages)
    print("Templates rendered successfully!")


# Function to render individual member pages from the in-memory members_by_id dict
def render_member_pages():
    if not members_by_id:
        print("No member data in memory, skipping member pages.")
        return

    # Build research lookup dict: researchId -> topic data
    research_by_id = {}
    for section in structures.get('research', []):
        for topic in section.get('topics', []):
            research_by_id[topic['researchId']] = topic

    # Build publication lookup dict: citationId -> publication data.
    # Require a non-empty citationId AND status == 'published' — working /
    # in-review entries (which often share an empty citationId) would otherwise
    # all collide on the same dict key and pollute member pages.
    pub_by_id = {}
    for section in structures.get('publications', []):
        for item in section.get('items', []):
            if item.get('citationId') and item.get('status') == 'published':
                pub_by_id[item['citationId']] = item

    template = env.get_template('pages/member/member.html')

    for group in structures.get('members', []) + structures.get('students', []):
        for member_base in group.get('members', []):
            page_link = member_base.get('pageLink', '')
            if not page_link:
                continue
            # webId is the last path segment of /members/{web_id}
            web_id = page_link.rstrip('/').rsplit('/', 1)[-1]
            if not web_id:
                continue

            member_details = members_by_id.get(web_id)
            if member_details is None:
                continue

            # Merge member details, using base data as defaults
            member = member_details.copy()
            member.update(member_base)

            if 'pageLink' not in member:
                continue

            # Place pre-rendered HTML directly onto pageContent so the template
            # can render it without a path-keyed lookup.
            md_for_member = _member_data['members_md'].get(web_id, {})
            page_content = member.get('pageContent', {})

            for section in page_content.get('aboutSection', []):
                section['content'] = md_for_member.get('about', '')
            if page_content.get('positionSection'):
                page_content['positionSection']['content'] = md_for_member.get('position', '')

            # Member interest (pre-rendered HTML from members_md)
            interest_html = md_for_member.get('interest', '')
            if interest_html:
                member['interest_content'] = interest_html

            # Auto-populate Journal Publications by matching authorList[].webId
            matching_items = []
            for pub_section in structures.get('publications', []):
                for item in pub_section.get('items', []):
                    author_web_ids = {a.get('webId') for a in item.get('authorList', []) if a.get('webId')}
                    if (item.get('citationId')
                            and item.get('status') == 'published'
                            and web_id in author_web_ids):
                        matching_items.append(item)

            # Sort by issue date, newest first, via the shared sort key. Routing
            # through get_pub_sort_key (instead of re-parsing year/month here)
            # keeps a single source of truth and normalizes the "'YY" string,
            # plain int, and missing-date forms to a uniform (int, int) tuple.
            # A raw (year, month) sort would raise TypeError the moment a str
            # year and an int-default year were compared.
            matching_items.sort(key=get_pub_sort_key, reverse=True)
            matching_citations = [item['citationId'] for item in matching_items]

            pub_sections = page_content.setdefault('PublicationSection', [])
            journal_section = next((s for s in pub_sections if s.get('sectionTitle') == 'Journal Publications'), None)

            if not journal_section:
                journal_section = {
                    "sectionTitle": "Journal Publications",
                    "publications": []
                }
                pub_sections.insert(0, journal_section)

            journal_section['publications'] = matching_citations

            output = template.render(
                pages=pages,
                member=member,
                research_by_id=research_by_id,
                pub_by_id=pub_by_id,
                structures=structures,
                year=datetime.now().year,
            )

            page_dir = os.path.join(output_dir, member['pageLink'].lstrip('/'))
            os.makedirs(page_dir, exist_ok=True)
            with open(os.path.join(page_dir, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"Member page generated: {member['pageLink']}")

    print("Member pages rendered successfully!")


# Function to render individual news item pages from news.json
def render_news_pages():
    template = env.get_template('pages/news/news-item.html')
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}

    rendered_slugs = set()
    for section in structures.get('news', []):
        # Newest-first: when two listing items point at the same pageLink
        # (e.g. two news mentions of one story), the most recent item supplies
        # the page metadata and earlier duplicates are skipped.
        for item in section.get('items', [])[::-1]:
            page_link = item.get('pageLink')
            if not page_link:
                continue

            # Derive slug from pageLink (e.g. /news/2026-foo/ → 2026-foo)
            slug = news_slug_from_pagelink(page_link)
            if slug in rendered_slugs:
                continue
            rendered_slugs.add(slug)

            # Load markdown article content. Rewrite inline image references
            # (e.g. /assets/news/{slug}/3.jpg) to the 1200w WebP variant since
            # the raw originals are no longer copied to docs/.
            article_key = f'news/{slug}'
            news_content = articles.get(article_key)
            if news_content:
                # Match the same extensions compress_and_convert_images()
                # converts, so an inline .gif/.bmp/.tiff isn't left pointing at
                # an original that never gets copied to docs/.
                news_content = re.sub(
                    r'src="(/assets/news/[^"]+?)\.(?:jpe?g|png|gif|bmp|tiff)"',
                    r'src="\1-1200w.webp"',
                    news_content
                )

            # Discover images from contents/news/images/{slug}/. Output paths
            # point at WebP variants generated by compress_and_convert_images.
            img_folder = os.path.join('contents', 'news', 'images', slug)
            news_images = []
            if os.path.exists(img_folder):
                files = sorted(
                    [f for f in os.listdir(img_folder)
                     if not f.startswith('.') and
                     any(f.lower().endswith(ext) for ext in image_extensions)],
                    key=lambda x: (0, int(x.rsplit('.', 1)[0])) if x.rsplit('.', 1)[0].isdigit() else (1, x)
                )
                # Pass stems (no extension, no -Nw suffix); template builds srcset.
                news_images = [f'/assets/news/{slug}/{os.path.splitext(f)[0]}' for f in files]

            item = dict(item)
            if news_content:
                item['content'] = news_content

            # If a 0.{ext} source exists, use it as the hero. imgPath points at
            # the 1200w WebP variant — used by OG/Twitter meta and JSON-LD.
            # imgPathFront is the bare stem so the template can build srcset.
            if not item.get('imgPath') and os.path.exists(img_folder):
                for ext in image_extensions:
                    if os.path.exists(os.path.join(img_folder, f'0{ext}')):
                        item['imgPathFront'] = f'/assets/news/{slug}/0'
                        item['imgPath'] = f"{item['imgPathFront']}-1200w.webp"
                        news_images = [img for img in news_images
                                       if img.rsplit('/', 1)[-1] != '0']
                        break

            md_path = f"contents/news/articles/{slug}.md"
            try:
                date_modified = datetime.fromtimestamp(os.path.getmtime(md_path)).strftime("%Y-%m-%dT%H:%M:%S")
            except OSError:
                date_modified = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

            output = template.render(
                news=item,
                news_images=news_images,
                pages=pages,
                structures=structures,
                year=datetime.now().year,
                date_modified=date_modified,
            )

            page_dir = os.path.join(output_dir, page_link.lstrip('/'))
            os.makedirs(page_dir, exist_ok=True)
            with open(os.path.join(page_dir, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(output)
            print(f"News item page generated: {page_link}")

    print("News item pages rendered successfully!")


# Function to render individual publication detail pages from publications.json.
# A page is generated for every entry with an `abstract` (detail pages are
# opt-in). The slug and BibTeX are derived from the structured fields. Authors
# in `authorList` whose webId matches an E3 member are linked to that member's
# page by the template (member_ids gates the link).
def render_publication_pages():
    template = env.get_template('pages/publications/publication-item.html')
    member_ids = set(members_by_id.keys())

    count = 0
    for section in structures.get('publications', []):
        for item in section.get('items', []):
            if not item.get('abstract'):
                continue

            pub = dict(item)
            slug = pub.get('slug') or pub_slug(pub)
            pub['slug'] = slug
            pub['pageLink'] = pub.get('pageLink') or f"/publications/{slug}/"
            # BibTeX is derived from the structured fields (single source of
            # truth); an explicit `bibtex` field, if present, overrides it.
            pub['bibtex'] = generate_bibtex(pub)

            output = template.render(
                pub=pub,
                member_ids=member_ids,
                pages=pages,
                structures=structures,
                year=datetime.now().year,
            )

            page_dir = os.path.join(output_dir, pub['pageLink'].lstrip('/'))
            os.makedirs(page_dir, exist_ok=True)
            with open(os.path.join(page_dir, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(output)
            count += 1
            print(f"Publication page generated: {pub['pageLink']}")

    print(f"Publication detail pages rendered successfully! ({count})")


# Function to render individual project detail pages from projects.json + the
# per-slug folder under contents/projects/<slug>/. A page is generated for every
# listing entry whose `slug` is set AND whose folder has a project.json (detail
# pages are opt-in). Core metadata comes from the listing entry; rich fields come
# from project.json; narrative from about.md / about.zh.md. relatedPublications
# resolve by citationId, team by webId.
def render_project_pages():
    template = env.get_template('pages/projects/project-item.html')

    # Publication lookup by citationId (published only) — mirrors render_member_pages.
    pub_by_id = {}
    for section in structures.get('publications', []):
        for item in section.get('items', []):
            if item.get('citationId') and item.get('status') == 'published':
                pub_by_id[item['citationId']] = item

    count = 0
    for section in structures.get('projects', []):
        for item in section.get('items', []):
            slug = item.get('slug')
            if not slug:
                continue
            folder = os.path.join('contents', 'projects', slug)
            detail_path = os.path.join(folder, 'project.json')
            if not os.path.isfile(detail_path):
                continue

            try:
                with open(detail_path, 'r', encoding='utf-8') as f:
                    detail = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                # A malformed detail file is treated like a missing one: skip
                # this project's page with a loud, located warning rather than
                # crashing the entire build on a single content typo.
                print(f"  ⚠ Skipping project '{slug}': cannot read {detail_path} ({e})")
                continue

            proj = dict(item)
            proj.update(detail)
            proj['slug'] = slug
            proj['pageLink'] = item.get('pageLink') or f"/projects/{slug}/"

            # Narrative bodies (EN + optional ZH) → HTML
            proj['narrativeHtml'] = ''
            proj['narrativeZhHtml'] = ''
            about_en = os.path.join(folder, 'about.md')
            about_zh = os.path.join(folder, 'about.zh.md')
            if os.path.isfile(about_en):
                with open(about_en, 'r', encoding='utf-8') as f:
                    proj['narrativeHtml'] = markdown.markdown(f.read(), extensions=['md_in_html'])
            if os.path.isfile(about_zh):
                with open(about_zh, 'r', encoding='utf-8') as f:
                    proj['narrativeZhHtml'] = markdown.markdown(f.read(), extensions=['md_in_html'])

            # Resolve related publications by citationId (skip unknown/unpublished).
            resolved_pubs = []
            for ref in detail.get('relatedPublications', []):
                cid = ref.get('citationId')
                if cid and cid in pub_by_id:
                    resolved_pubs.append(pub_by_id[cid])
            proj['relatedPubsResolved'] = resolved_pubs

            # Resolve team → link to member pages where webId matches a member.
            resolved_team = []
            for person in detail.get('team', []):
                wid = person.get('webId')
                resolved_team.append({
                    'name': person.get('name', ''),
                    'role': person.get('role', ''),
                    'pageLink': f"/members/{wid}/" if (wid and wid in members_by_id) else '',
                })
            proj['teamResolved'] = resolved_team

            # Gallery: keep only entries whose source file exists; build asset stem.
            resolved_gallery = []
            img_folder = os.path.join(folder, 'images')
            for shot in detail.get('gallery', []):
                fname = shot.get('file')
                if fname and os.path.isfile(os.path.join(img_folder, fname)):
                    stem = os.path.splitext(fname)[0]
                    resolved_gallery.append({
                        'stem': f"/assets/projects/{slug}/images/{stem}",
                        'caption': shot.get('caption', ''),
                        'captionZh': shot.get('captionZh', ''),
                    })
            proj['galleryResolved'] = resolved_gallery

            output = template.render(
                proj=proj,
                pages=pages,
                structures=structures,
                year=datetime.now().year,
            )

            page_dir = os.path.join(output_dir, proj['pageLink'].lstrip('/'))
            os.makedirs(page_dir, exist_ok=True)
            with open(os.path.join(page_dir, 'index.html'), 'w', encoding='utf-8') as f:
                f.write(output)
            count += 1
            print(f"Project page generated: {proj['pageLink']}")

    print(f"Project detail pages rendered successfully! ({count})")


# Function to generate sitemap.xml with all indexable pages
def generate_sitemap():
    from xml.etree.ElementTree import Element, SubElement, ElementTree, indent
    today = datetime.now().strftime("%Y-%m-%d")

    def file_mtime(path):
        """Return ISO date of file's last modification, or today if file is missing.
        Logs a warning for non-FileNotFoundError OSErrors so real issues surface."""
        try:
            return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        except FileNotFoundError:
            return today
        except OSError as e:
            print(f"Warning: cannot stat {path}: {e}")
            return today

    def latest_mtime(*paths):
        """Return ISO date of the most recent mtime across the given files/dirs.
        Directories are walked recursively. Missing paths are silently skipped.
        Falls back to today if nothing is found."""
        latest = 0.0
        for p in paths:
            if not os.path.exists(p):
                continue
            if os.path.isfile(p):
                latest = max(latest, os.path.getmtime(p))
            else:
                for root, _, files in os.walk(p):
                    for name in files:
                        try:
                            latest = max(latest, os.path.getmtime(os.path.join(root, name)))
                        except OSError:
                            pass
        if latest == 0.0:
            return today
        return datetime.fromtimestamp(latest).strftime("%Y-%m-%d")

    BASE = SITE_URL

    urlset = Element("urlset")
    urlset.set("xmlns", "http://www.sitemaps.org/schemas/sitemap/0.9")

    def add_url(loc, changefreq="monthly", priority="0.7", lastmod=today):
        url_el = SubElement(urlset, "url")
        SubElement(url_el, "loc").text = loc
        SubElement(url_el, "lastmod").text = lastmod
        SubElement(url_el, "changefreq").text = changefreq
        SubElement(url_el, "priority").text = priority

    # Static pages — lastmod reflects the most recent change to that page's
    # actual sources (data files + templates), not the build date.
    add_url(f"{BASE}/", changefreq="weekly", priority="1.0",
            lastmod=latest_mtime("contents/about", "contents/contact", "contents/group-life",
                                 "contents/news", "contents/publications", "contents/research",
                                 "contents/videos", "contents/projects",
                                 "templates/home", "templates/index.html", "templates/base.html"))
    add_url(f"{BASE}/about/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/about", "templates/pages/about.html"))
    add_url(f"{BASE}/members/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/members/member-info.xlsx", "contents/members",
                                 "templates/pages/members.html"))
    add_url(f"{BASE}/students/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/members/member-info.xlsx", "contents/members",
                                 "templates/pages/students.html"))
    add_url(f"{BASE}/publications/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/publications/publications.json",
                                 "templates/pages/publications.html"))
    add_url(f"{BASE}/news/", changefreq="weekly", priority="0.8",
            lastmod=latest_mtime("contents/news/news.json",
                                 "templates/pages/news.html"))
    add_url(f"{BASE}/research/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/research/research.json",
                                 "templates/pages/research.html"))
    add_url(f"{BASE}/projects/", changefreq="monthly", priority="0.8",
            lastmod=latest_mtime("contents/projects/projects.json",
                                 "templates/pages/projects.html"))
    add_url(f"{BASE}/contact/", changefreq="yearly", priority="0.6",
            lastmod=latest_mtime("contents/contact", "templates/pages/contact.html"))
    add_url(f"{BASE}/group-life/", changefreq="monthly", priority="0.6",
            lastmod=latest_mtime("contents/group-life/group-life.json"))

    # Member pages — lastmod reflects the most recent change to that member's
    # own folder, the roster spreadsheet, or the publications list (since pubs
    # auto-populate onto the member page).
    seen_member_links = set()
    for group in structures.get('members', []) + structures.get('students', []):
        for member in group.get('members', []):
            link = member.get('pageLink')
            if link and link not in seen_member_links:
                seen_member_links.add(link)
                web_id = link.rstrip('/').split('/')[-1]
                add_url(f"{BASE}{link}/", changefreq="monthly", priority="0.7",
                        lastmod=latest_mtime(f"contents/members/{web_id}",
                                             "contents/members/member-info.xlsx",
                                             "contents/publications/publications.json"))

    # News item pages — lastmod reflects the article body, its images, and the
    # news.json entry (which supplies title/date shown on the page).
    for section in structures.get('news', []):
        for item in section.get('items', []):
            link = item.get('pageLink')
            if link:
                slug = news_slug_from_pagelink(link)
                add_url(f"{BASE}{link}", changefreq="yearly", priority="0.6",
                        lastmod=latest_mtime(f"contents/news/articles/{slug}.md",
                                             f"contents/news/images/{slug}",
                                             "contents/news/news.json"))

    # Publication detail pages — one per entry with an abstract. lastmod tracks
    # the publications data file (the page is rendered entirely from it).
    for section in structures.get('publications', []):
        for item in section.get('items', []):
            if item.get('abstract'):
                add_url(f"{BASE}/publications/{item.get('slug') or pub_slug(item)}/",
                        changefreq="yearly", priority="0.6",
                        lastmod=latest_mtime("contents/publications/publications.json"))

    # Project detail pages — one per flagship entry (slug + folder). lastmod tracks
    # the project's own folder and the projects listing data file.
    for section in structures.get('projects', []):
        for item in section.get('items', []):
            if item.get('pageLink'):
                slug = item['slug']
                add_url(f"{BASE}{item['pageLink']}", changefreq="monthly", priority="0.6",
                        lastmod=latest_mtime(f"contents/projects/{slug}",
                                             "contents/projects/projects.json"))

    tree = ElementTree(urlset)
    indent(tree, space="  ")
    sitemap_path = os.path.join(output_dir, "sitemap.xml")
    with open(sitemap_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)
    print(f"Sitemap written to {sitemap_path}")


# ── SEO validation ────────────────────────────────────────────────────────────
def validate_seo():
    """Walk rendered docs/ HTML files; emit warnings for SEO regressions.

    Non-fatal — warnings print in yellow, build does not fail. Implemented as
    a skeleton in Task 6.1; checks added in Task 6.2.
    """
    from bs4 import BeautifulSoup
    YELLOW = "\033[33m"
    RESET = "\033[0m"
    warnings = []

    def warn(msg):
        warnings.append(msg)
        print(f"{YELLOW}[SEO] {msg}{RESET}")

    # Collected per-page descriptions for duplicate detection (used in Task 6.2)
    descriptions = {}  # description -> list of page paths

    for root, _dirs, files in os.walk(output_dir):
        for fname in files:
            if fname != "index.html":
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, output_dir)
            with open(path, "r", encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
            _check_page(soup, rel, warn, descriptions)

    # Duplicate-description warnings (after full walk; populated in Task 6.2)
    for desc, paths in descriptions.items():
        if len(paths) >= 2:
            warn(f"duplicate description on {len(paths)} pages: {paths[:3]}{'…' if len(paths) > 3 else ''}")

    print(f"\nSEO check: {len(warnings)} warnings (0 errors).")


def _check_page(soup, rel, warn, descriptions):
    """All per-page checks live here. Implemented in Task 6.2."""
    # 0. Pages marked noindex (e.g. the /editor dashboard) are invisible to
    # search engines, so none of the SEO checks below apply to them.
    robots = soup.find("meta", attrs={"name": "robots"})
    if robots and "noindex" in (robots.get("content") or "").lower():
        return

    # 1. Description length + duplicate detection
    desc_tag = soup.find("meta", attrs={"name": "description"})
    desc = (desc_tag.get("content", "") if desc_tag else "").strip()
    if not desc:
        warn(f"{rel}: missing meta description")
    else:
        n = len(desc)
        if n < 70:
            warn(f"{rel}: description too short ({n} chars, want ≥70)")
        elif n > 160:
            warn(f"{rel}: description too long ({n} chars, want ≤160)")
        descriptions.setdefault(desc, []).append(rel)

    # 2. Title length
    title_tag = soup.find("title")
    title = (title_tag.string or "").strip() if title_tag else ""
    if not title:
        warn(f"{rel}: missing <title>")
    elif len(title) < 30:
        warn(f"{rel}: title too short ({len(title)} chars, want ≥30)")
    elif len(title) > 60:
        warn(f"{rel}: title too long ({len(title)} chars, want ≤60)")

    # 3. Missing canonical
    if not soup.find("link", attrs={"rel": "canonical"}):
        warn(f"{rel}: missing <link rel=\"canonical\">")

    # 4. JSON-LD parse errors
    for i, block in enumerate(soup.find_all("script", attrs={"type": "application/ld+json"})):
        try:
            json.loads(block.string or "")
        except (json.JSONDecodeError, TypeError) as e:
            warn(f"{rel}: JSON-LD block #{i+1} invalid: {e}")

    # 5. News body word count (only for /news/{slug}/ subsubpages, not the listing)
    if rel.startswith("news/") and rel != "news/index.html":
        article = soup.find("div", class_="news-item-body")
        if article:
            words = len(article.get_text(" ", strip=True).split())
            if words < 200:
                warn(f"{rel}: thin news body ({words} words, want ≥200)")


# Function to copy static assets directly into docs/
def copy_static():
    static_src = "static"
    if os.path.exists(static_src):
        for item in os.listdir(static_src):
            src_path = os.path.join(static_src, item)
            dst_path = os.path.join(output_dir, item)

            if os.path.isdir(src_path):
                if os.path.exists(dst_path):
                    shutil.rmtree(dst_path)
                shutil.copytree(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)

    print("Static assets copied directly into docs/")


# Function to copy videos directly into docs/
def copy_videos():
    videos_src = "contents/videos"
    video_output_dir = os.path.join(output_dir, "assets/videos")
    if os.path.exists(videos_src):
        os.makedirs(video_output_dir, exist_ok=True)
        for item in os.listdir(videos_src):
            # videos.json is data, not an asset — skip it.
            if item == 'videos.json':
                continue
            src_path = os.path.join(videos_src, item)
            dst_path = os.path.join(video_output_dir, item)

            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dst_path)

    print("Videos copied directly into docs/")


# Compress images and convert to WebP format. Each subpage's image source
# folder is now self-contained; the (source_root, output_folder, sizes) tuples
# describe what to process.
members_img_sizes = MEMBER_IMG_WIDTHS
lazy_img_sizes    = LAZY_IMG_WIDTHS

# SUBPAGE_IMG_WIDTHS / MEMBER_IMG_WIDTHS / LAZY_IMG_WIDTHS live in config.py —
# a single source of truth kept in sync with the srcset ladders in templates.
_SUBPAGE_IMAGE_SOURCES = [
    # (source_root,                  docs/assets/<folder>, sizes)
    ('contents/news/images',         'news',               SUBPAGE_IMG_WIDTHS),
    ('contents/group-life/images',   'group-life',         SUBPAGE_IMG_WIDTHS),
    # Projects: walks contents/projects/<slug>/images/* → docs/assets/projects/<slug>/images/*
    ('contents/projects',            'projects',           SUBPAGE_IMG_WIDTHS),
]

def convert_to_webp(path, dst_path, sizes, compression_quality=WEBP_QUALITY, basename=None, target_aspect=None):
    """Resize `path` to each width in `sizes` and save WebP variants under
    `dst_path` as `{basename}-{size}w.webp`. When `basename` is None it is
    derived from the source filename; pass it explicitly when the source
    filename doesn't match the desired output stem (e.g. per-member photos
    are all named `photo.{ext}` but must output as `{webId}-{size}w.webp`).

    When `target_aspect=(w, h)` is given (e.g. (3, 4)), each output is
    center-cropped to that aspect ratio before resizing. This lets templates
    declare matching width/height attributes for CLS reservation."""
    if basename is None:
        basename = os.path.splitext(os.path.basename(path))[0]
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        src_w = img.width
        for size in sizes:
            # Never upscale: when the requested width exceeds the source
            # width, cap at the source. Pillow's resize can't add detail —
            # upscaled WebPs look soft on retina screens (see Jun '26
            # group-life: 1477-px source upscaled to 2000w rendered blurry).
            effective_size = min(size, src_w)
            if target_aspect:
                w_aspect, h_aspect = target_aspect
                target_size = (effective_size, int(effective_size * h_aspect / w_aspect))
                img_resized = ImageOps.fit(img, target_size, centering=(0.5, 0.5))
            else:
                img_resized = img.resize((effective_size, int(effective_size * img.height / img.width)))
            # Keep the original {basename}-{size}w.webp naming so srcset
            # references don't break; the file just stops growing past
            # the source resolution.
            webp_output_path = f"{dst_path}/{basename}-{size}w.webp"
            img_resized.save(webp_output_path, "WEBP", quality=compression_quality)


def compress_and_convert_images():
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff'}

    for src_root, dst_folder, sizes in _SUBPAGE_IMAGE_SOURCES:
        if not os.path.isdir(src_root):
            continue
        print(f"--- Images found in '{dst_folder}' ---")
        dst_root = os.path.join(output_dir, "assets", dst_folder)
        for root, _, files in os.walk(src_root):
            for fname in files:
                lower = fname.lower()
                path = os.path.join(root, fname)
                # Preserve subdirectory layout under src_root (e.g.
                # contents/news/images/{slug}/0.jpg → docs/assets/news/{slug}/0-Nw.webp).
                rel_dir = os.path.relpath(os.path.dirname(path), src_root)
                dst_subdir = dst_root if rel_dir == '.' else os.path.join(dst_root, rel_dir)
                # SVGs (e.g. partner logos) are vector — copy verbatim instead of
                # rasterising to WebP, so they stay crisp at any size.
                if lower.endswith('.svg'):
                    os.makedirs(dst_subdir, exist_ok=True)
                    shutil.copy2(path, dst_subdir)
                    print(f"{path} → {dst_subdir}/ (svg)")
                    continue
                if not any(lower.endswith(ext) for ext in image_extensions):
                    continue
                os.makedirs(dst_subdir, exist_ok=True)
                convert_to_webp(path, dst_subdir, sizes, compression_quality=WEBP_QUALITY)
                convert_to_webp(path, dst_subdir, lazy_img_sizes, compression_quality=WEBP_LAZY_QUALITY)
                print(f"{path} → {dst_subdir}/")


def compress_member_images():
    """Convert per-member photos to WebP variants.

    Source: contents/members/{webId}/photo.{jpg,jpeg,png}
    Output: docs/assets/members/{webId}-{size}w.webp

    The output keeps the {webId} basename so member templates' srcset
    references are byte-identical to the legacy contents/images/members/
    pipeline — only the SOURCE location moved into the per-member folder."""
    members_root = 'contents/members'
    dst_root = os.path.join(output_dir, "assets", "members")
    photo_exts = ('.jpg', '.jpeg', '.png')

    if not os.path.isdir(members_root):
        return
    os.makedirs(dst_root, exist_ok=True)
    print("--- Member photos in 'contents/members/*/' ---")

    for web_id in sorted(os.listdir(members_root)):
        member_dir = os.path.join(members_root, web_id)
        if not os.path.isdir(member_dir):
            continue
        photo = None
        for fname in sorted(os.listdir(member_dir)):
            stem, ext = os.path.splitext(fname)
            if stem == 'photo' and ext.lower() in photo_exts and not fname.startswith('.'):
                photo = os.path.join(member_dir, fname)
                break
        if not photo:
            continue
        convert_to_webp(photo, dst_root, members_img_sizes, compression_quality=WEBP_QUALITY, basename=web_id, target_aspect=(3, 4))
        convert_to_webp(photo, dst_root, lazy_img_sizes, compression_quality=WEBP_LAZY_QUALITY, basename=web_id, target_aspect=(3, 4))
        print(f"{photo} → {dst_root}/{web_id}-*.webp")
        

# Run the build process
if __name__ == "__main__":
    # Start from a clean output dir so content removed from contents/ (a deleted
    # article, a dropped abstract, a renamed slug) can't leave a stale page or a
    # dangling sitemap entry behind. docs/ is gitignored and fully regenerated
    # by the steps below.
    if os.path.isdir(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("Rendering templates...")
    render_templates()
    print("Rendering member pages...")
    render_member_pages()
    print("Rendering news item pages...")
    render_news_pages()
    print("Rendering publication detail pages...")
    render_publication_pages()
    print("Rendering project detail pages...")
    render_project_pages()
    print("Copying static assets...")
    copy_static()
    print("Copying videos...")
    copy_videos()
    print("Generating sitemap...")
    generate_sitemap()
    print("\nValidating SEO...")
    validate_seo()
    print("Compressing images and converting to WebP format...")
    compress_and_convert_images()
    compress_member_images()
    print("Build complete!")
