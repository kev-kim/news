"""
Naver News Scraper
------------------
Reads queries from ../data/news_keywords.yaml and writes results to
../data/naver_news.json (both paths relative to this script's location).

Naver News renders results via a "Fender" JS framework. The first 10 articles
are bootstrapped as JSON inside a <script> tag. Additional pages are loaded
via an infinite-scroll API endpoint whose URL is embedded in the same page.
This scraper:
  1. Loads the search page with Playwright and extracts the first 10 articles
     plus the "more" API URL.
  2. Calls the "more" API directly (incrementing &start= by 10 each time)
     until fewer than 10 articles are returned, collecting all results.

Setup:
    pip install playwright pyyaml
    playwright install chromium

Usage:
    python naver_news_scraper.py
"""

import json
import re
import time
import playwright
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, parse_qs, urljoin

import yaml
from playwright.sync_api import sync_playwright


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL          = "https://search.naver.com/search.naver"
PAGE_LOAD_TIMEOUT = 15_000   # ms
RENDER_SETTLE     = 2.0      # seconds
REQUEST_DELAY     = 2.0      # seconds between queries
PAGE_DELAY        = 0.8      # seconds between paginated API calls


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def build_search_url(query: str) -> str:
    """'dog cat'  ->  ...query=dog+%7C+cat... (OR search, past 1 day)"""
    or_query = " | ".join(query.strip().split())
    params = {
        "ssc":   "tab.news.all",
        "where": "news",
        "query": or_query,
        "sm":    "tab_dgs",
        "nso":   "so:r,p:1d",
        "qdt":   "1",
    }
    qs = "&".join(f"{k}={quote(v, safe=':,')}" for k, v in params.items())
    return f"{BASE_URL}?{qs}"


# ---------------------------------------------------------------------------
# Balanced-brace JSON extractor
# ---------------------------------------------------------------------------

def _extract_bootstrap_json(html: str) -> dict | None:
    """
    Find the second argument of `entry.bootstrap(element, <JSON>, ...)`.
    Uses a balanced-brace walk that respects strings and escape sequences.
    """
    marker = "entry.bootstrap("
    idx = html.find(marker)
    if idx == -1:
        return None

    # skip first arg: document.getElementById("...")
    i     = idx + len(marker)
    depth = 0
    while i < len(html):
        if html[i] == "(":
            depth += 1
        elif html[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    i += 1  # past the )

    # skip comma + whitespace
    while i < len(html) and html[i] in " ,\n\r\t":
        i += 1

    if i >= len(html) or html[i] != "{":
        return None

    brace_start = i
    depth       = 0
    in_string   = False
    escape_next = False

    for j in range(brace_start, len(html)):
        c = html[j]
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace_start : j + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Recursive newsItem finder
# ---------------------------------------------------------------------------

def _find_news_items(node) -> list[dict]:
    """Recursively collect every dict with templateId == 'newsItem'."""
    found = []
    if isinstance(node, dict):
        if node.get("templateId") == "newsItem":
            found.append(node)
        else:
            for v in node.values():
                found.extend(_find_news_items(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_news_items(item))
    return found


# ---------------------------------------------------------------------------
# Article extraction from Fender JSON
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"전$|^\d{4}\.")

def _articles_from_fender_data(data: dict) -> list[dict]:
    articles = []
    for node in _find_news_items(data):
        props = node.get("props", {})
        title = props.get("title", "").strip()
        link  = props.get("titleHref", "").strip()
        if not title or not link:
            continue
        published_at = ""
        for st in props.get("sourceProfile", {}).get("subTexts", []):
            text = st.get("text", "")
            if _TIME_RE.search(text):
                published_at = text
                break
        articles.append({"title": title, "link": link, "published_at": published_at})
    return articles


def extract_articles(html: str) -> list[dict]:
    data = _extract_bootstrap_json(html)
    if data is None:
        return []
    return _articles_from_fender_data(data)


# ---------------------------------------------------------------------------
# "More" API URL extractor
# ---------------------------------------------------------------------------

_MORE_URL_RE = re.compile(r'"url"\s*:\s*"(https?:\\/\\/s\.search\.naver\.com\\/p\\/newssearch[^"]+)"')

def extract_more_base_url(html: str) -> str | None:
    """
    Extract the infinite-scroll API base URL from the page HTML.
    Returns the URL with &start= stripped so we can append it ourselves,
    or None if not found.
    """
    m = _MORE_URL_RE.search(html)
    if not m:
        return None
    # Unescape JSON string (\\/ -> /, \u0026 -> &, etc.)
    raw = m.group(1)
    url = raw.replace("\\/", "/").encode().decode("unicode_escape")
    # Remove the trailing &start=N so we control pagination
    url = re.sub(r"&start=\d+", "", url)
    return url


# ---------------------------------------------------------------------------
# Fetch paginated results from the "more" API
# ---------------------------------------------------------------------------

def fetch_more_page(browser_page, base_url: str, start: int) -> list[dict]:
    """Call the infinite-scroll API for a given start offset."""
    url = f"{base_url}&start={start}"
    resp = browser_page.request.get(
        url,
        headers={
            "Accept": "text/html,*/*",
            "Referer": "https://search.naver.com/",
        },
    )
    if not resp.ok:
        return []
    html = resp.text()
    # The API response contains the same Fender bootstrap JSON
    data = _extract_bootstrap_json(html)
    if data is None:
        return []
    return _articles_from_fender_data(data)


# ---------------------------------------------------------------------------
# Full query scraper
# ---------------------------------------------------------------------------

def scrape_query(browser_page, query: str) -> list[dict]:
    or_query = " | ".join(query.strip().split())
    print(f'  Searching: "{or_query}"')

    url = build_search_url(query)
    print(f"    URL: {url}")

    # --- page 1: load in browser to get rendered HTML ---
    browser_page.goto(url, wait_until="domcontentloaded")
    try:
        browser_page.wait_for_function(
            "() => document.body.innerHTML.includes('entry.bootstrap')",
            timeout=PAGE_LOAD_TIMEOUT,
        )
    except Exception:
        print("    [WARNING] entry.bootstrap not found — page may be empty or blocked.")
        return []
    time.sleep(RENDER_SETTLE)

    html = browser_page.content()
    articles = extract_articles(html)
    print(f"    Page 1: {len(articles)} article(s)")

    # --- subsequent pages: call the "more" API directly ---
    more_base = extract_more_base_url(html)
    if not more_base:
        print("    [WARNING] Could not find more-API URL; returning page 1 only.")
        return articles

    start = 11
    while True:
        time.sleep(PAGE_DELAY)
        page_articles = fetch_more_page(browser_page, more_base, start)
        print(f"    start={start}: {len(page_articles)} article(s)")
        articles.extend(page_articles)
        if len(page_articles) < 10:
            break
        start += 10

    return articles


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def load_queries(config_path: Path) -> list[str]:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, list):
        queries = data
    elif isinstance(data, dict) and "queries" in data:
        queries = data["queries"]
    else:
        raise ValueError("YAML must be a list or a dict with a 'queries' key.")
    if not queries:
        raise ValueError("No queries found in the YAML file.")
    return [str(q) for q in queries]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    script_dir  = Path(__file__).parent
    config_path = script_dir / "../data/news_keywords.yaml"
    output_path = script_dir / "../data/naver_news.json"

    print(f"Loading queries from: {config_path.resolve()}")
    queries = load_queries(config_path)
    print(f"Found {len(queries)} query/queries: {queries}\n")

    results: dict = {
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
        "queries":    [],
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        bpage = context.new_page()

        for raw_query in queries:
            print(f"Processing query: '{raw_query}'")
            articles = scrape_query(bpage, raw_query)
            results["queries"].append({
                "query":    raw_query,
                "or_query": " | ".join(raw_query.strip().split()),
                "total":    len(articles),
                "articles": articles,
            })
            print(f"  Total articles for '{raw_query}': {len(articles)}\n")
            time.sleep(REQUEST_DELAY)

        browser.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Results saved to: {output_path.resolve()}")
    total = sum(q["total"] for q in results["queries"])
    print(f"Grand total articles scraped: {total}")


if __name__ == "__main__":
    main()