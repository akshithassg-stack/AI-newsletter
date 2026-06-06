from __future__ import annotations

import json
import logging
import os
import textwrap
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests
from bs4 import BeautifulSoup

import anthropic

from config import MAX_TOKENS, MODEL
from models import ResearchInput
from utils import RESEARCH_SYSTEM_PROMPT
from utils.helpers import extract_json

logger = logging.getLogger(__name__)

# Only accept articles published on or after this date
CUTOFF_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Category search terms — used by both NewsAPI and Google News
# ---------------------------------------------------------------------------
CATEGORY_SEARCH_TERMS: dict[str, str] = {
    "Renewable Energy":                        "green steel renewable energy wind solar power",
    "Hydrogen Production & Technology":        "hydrogen steel H2 DRI electrolyser",
    "Green Iron & Low-Carbon Feedstocks":      "green iron DRI direct reduction low carbon",
    "Circular Economy (Scrap)":                "steel scrap recycling electric arc furnace",
    "CCS & CCUS":                              "steel carbon capture CCS CCUS",
    "Steel Demand, Procurement & End Markets": "steel demand procurement automotive construction",
    "Steel Prices & Green Premiums":           "steel price green premium low carbon",
    "Raw Material Prices":                     "iron ore coking coal steel raw material price",
    "Clean Energy Logistics & Storage":        "green hydrogen storage clean energy logistics",
    "Project Finance & Investment":            "green steel investment financing fund",
    "Trade, Tariffs & Regulations":            "steel trade tariff regulation carbon border",
    "Climate Policy & Environment":            "steel decarbonization climate policy net zero",
    "Corporate Offtake":                       "green steel offtake agreement corporate",
    "Partnerships & M&A":                      "steel merger acquisition partnership joint venture",
    "Green Steel Projects & Plant Development":"green steel plant project development construction",
}
FALLBACK_SEARCH_TERMS = "green steel hydrogen decarbonization"

# Google News RSS
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

# Category RSS feeds — last-resort fallback
CATEGORY_FEEDS: dict[str, list[str]] = {
    "Renewable Energy":                        ["https://cleantechnica.com/feed/", "https://www.renewableenergyworld.com/feed/"],
    "Hydrogen Production & Technology":        ["https://www.hydrogeninsight.com/rss", "https://www.fuelcellsworks.com/feed/"],
    "Green Iron & Low-Carbon Feedstocks":      ["https://www.mining.com/feed/"],
    "Circular Economy (Scrap)":                ["https://www.recyclingtoday.com/rss/all-news.rss"],
    "CCS & CCUS":                              ["https://www.globalccsinstitute.com/feed/", "https://carbonbrief.org/feed"],
    "Steel Demand, Procurement & End Markets": ["https://www.worldsteel.org/rss.xml"],
    "Steel Prices & Green Premiums":           ["https://www.worldsteel.org/rss.xml"],
    "Raw Material Prices":                     ["https://www.mining.com/feed/"],
    "Clean Energy Logistics & Storage":        ["https://cleantechnica.com/feed/"],
    "Project Finance & Investment":            ["https://www.hydrogeninsight.com/rss"],
    "Trade, Tariffs & Regulations":            ["https://carbonbrief.org/feed"],
    "Climate Policy & Environment":            ["https://carbonbrief.org/feed", "https://steelwatch.org/feed/"],
    "Corporate Offtake":                       ["https://www.worldsteel.org/rss.xml"],
    "Partnerships & M&A":                      ["https://www.hydrogeninsight.com/rss", "https://www.mining.com/feed/"],
    "Green Steel Projects & Plant Development":["https://steelwatch.org/feed/", "https://www.hydrogeninsight.com/rss"],
}

RELEVANCE_KEYWORDS = [
    "steel", "hydrogen", "green", "iron", "decarboni", "carbon",
    "renewable", "scrap", "electric arc", "DRI", "blast furnace",
    "CCUS", "CCS", "offtake", "net zero", "emission", "energy",
    "investment", "plant", "project", "fund", "tariff", "trade",
]


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------
def _parse_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    for parser in (
        lambda s: parsedate_to_datetime(s).replace(tzinfo=timezone.utc),
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
    ):
        try:
            return parser(raw)
        except Exception:
            pass
    return None


def _is_recent(pub_dt: Optional[datetime], days: int = None) -> bool:
    """Return True only if article is from 2026 or has unknown date."""
    if pub_dt is None:
        return True  # keep if date unknown
    return pub_dt >= CUTOFF_FROM


def _age_label(pub_dt: Optional[datetime]) -> str:
    if pub_dt is None:
        return "unknown date"
    delta = datetime.now(timezone.utc) - pub_dt
    if delta.days == 0:
        return "today"
    if delta.days == 1:
        return "yesterday"
    if delta.days <= 7:
        return f"{delta.days} days ago"
    if delta.days <= 30:
        return f"{delta.days // 7} weeks ago"
    if delta.days <= 90:
        return f"{delta.days // 30} months ago"
    return f"{pub_dt.strftime('%B %Y')} [OLDER SOURCE]"


def _clean_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(separator=" ").strip()


# ---------------------------------------------------------------------------
# SOURCE 1 — NewsAPI  (primary, requires NEWS_API_KEY in .env)
# ---------------------------------------------------------------------------
NEWSAPI_URL = "https://newsapi.org/v2/everything"

def fetch_newsapi(topic: str, category: str, max_items: int = 15) -> list[dict]:
    """
    Fetch articles from NewsAPI using topic + category terms.
    Returns [] silently if key is missing or quota exceeded.
    """
    api_key = os.environ.get("NEWS_API_KEY", "")
    if not api_key:
        logger.info("[ResearchAgent] NEWS_API_KEY not set — skipping NewsAPI")
        return []

    cat_terms = CATEGORY_SEARCH_TERMS.get(category, FALLBACK_SEARCH_TERMS)
    query = f"{topic} OR ({cat_terms})"
    from_date = CUTOFF_FROM.strftime("%Y-%m-%d")  # 2026-01-01

    params = {
        "q":        query,
        "from":     from_date,
        "sortBy":   "publishedAt",
        "language": "en",
        "pageSize": max_items,
        "apiKey":   api_key,
    }

    try:
        resp = requests.get(NEWSAPI_URL, params=params, timeout=10)
        if resp.status_code == 426:
            logger.warning("[ResearchAgent] NewsAPI free plan limit hit — falling back to Google News")
            return []
        if resp.status_code == 401:
            logger.warning("[ResearchAgent] NewsAPI key invalid — falling back to Google News")
            return []
        if resp.status_code != 200:
            logger.warning("[ResearchAgent] NewsAPI returned %d — falling back", resp.status_code)
            return []

        data = resp.json()
        articles = data.get("articles", [])
        entries = []
        for art in articles:
            pub_dt = _parse_date(art.get("publishedAt", ""))
            if not _is_recent(pub_dt):
                continue
            entries.append({
                "title":     (art.get("title") or "").strip(),
                "summary":   _clean_html(art.get("description") or art.get("content") or "")[:600],
                "link":      art.get("url", ""),
                "published": pub_dt.strftime("%B %d, %Y") if pub_dt else "",
                "pub_dt":    pub_dt,
                "source":    art.get("source", {}).get("name", "NewsAPI"),
            })

        logger.info("[ResearchAgent] NewsAPI returned %d articles", len(entries))
        return entries

    except Exception as exc:
        logger.warning("[ResearchAgent] NewsAPI error: %s — falling back", exc)
        return []


# ---------------------------------------------------------------------------
# SOURCE 2 — Google News RSS  (free fallback, no key needed)
# ---------------------------------------------------------------------------
def _fetch_feed(url: str, max_items: int = 15) -> list[dict]:
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        entries = []
        for entry in feed.entries[:max_items]:
            pub_dt = _parse_date(entry.get("published", "") or entry.get("updated", ""))
            item = {
                "title":     entry.get("title", "").strip(),
                "summary":   _clean_html(entry.get("summary", entry.get("description", "")))[:600],
                "link":      entry.get("link", ""),
                "published": pub_dt.strftime("%B %d, %Y") if pub_dt else entry.get("published", ""),
                "pub_dt":    pub_dt,
                "source":    "RSS",
            }
            if item["title"]:
                entries.append(item)
        return entries
    except Exception as exc:
        logger.debug("Feed fetch failed (%s): %s", url, exc)
        return []


def fetch_google_news(topic: str, category: str, max_items: int = 20) -> list[dict]:
    cat_terms = CATEGORY_SEARCH_TERMS.get(category, FALLBACK_SEARCH_TERMS)
    entries: list[dict] = []
    for query in [f"{topic} green steel", f"{cat_terms} green steel 2026"]:
        url = GOOGLE_NEWS_RSS.format(query=urllib.parse.quote(query))
        batch = _fetch_feed(url, max_items=max_items)
        entries.extend(batch)
        logger.info("[ResearchAgent] Google News '%s' -> %d entries", query[:50], len(batch))
    return entries


# ---------------------------------------------------------------------------
# SOURCE 3 — Category RSS feeds  (last resort)
# ---------------------------------------------------------------------------
def fetch_category_rss(category: str) -> list[dict]:
    entries: list[dict] = []
    for url in CATEGORY_FEEDS.get(category, []):
        entries.extend(_fetch_feed(url, max_items=8))
    return entries


# ---------------------------------------------------------------------------
# Scoring + dedup
# ---------------------------------------------------------------------------
def _score(entry: dict, topic_words: set[str]) -> int:
    text      = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
    relevance = sum(1 for kw in RELEVANCE_KEYWORDS if kw in text)
    topic_hit = sum(2 for w in topic_words if w in text)
    pub       = entry.get("pub_dt")
    recency   = (
        10 if pub and pub >= datetime.now(timezone.utc) - timedelta(days=7)  else
        5  if pub and pub >= datetime.now(timezone.utc) - timedelta(days=30) else
        0
    )
    return relevance + topic_hit + recency


def _dedup(entries: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for e in entries:
        if e["title"] and e["title"] not in seen:
            seen.add(e["title"])
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# Main gather function — tries NewsAPI first, falls back automatically
# ---------------------------------------------------------------------------
def gather_live_news(category: str, topic: str, max_articles: int = 15) -> list[dict]:
    topic_words = {w.lower() for w in topic.split() if len(w) > 3}
    all_entries: list[dict] = []
    source_used = "none"

    # --- Tier 1: NewsAPI ---
    newsapi_entries = fetch_newsapi(topic, category, max_items=20)
    if newsapi_entries:
        all_entries.extend(newsapi_entries)
        source_used = "NewsAPI"
        logger.info("[ResearchAgent] Using NewsAPI as primary source (%d articles)", len(newsapi_entries))

    # --- Tier 2: Google News RSS (always run as supplement) ---
    google_entries = fetch_google_news(topic, category, max_items=15)
    all_entries.extend(google_entries)
    if not newsapi_entries:
        source_used = "Google News RSS"

    # --- Tier 3: Category RSS (if still thin) ---
    if len(all_entries) < 8:
        rss_entries = fetch_category_rss(category)
        all_entries.extend(rss_entries)
        if not newsapi_entries and not google_entries:
            source_used = "Category RSS"

    # Filter to recent only
    all_entries = [e for e in all_entries if _is_recent(e.get("pub_dt"))]

    # Dedup + rank
    unique = _dedup(all_entries)
    unique.sort(key=lambda e: _score(e, topic_words), reverse=True)
    result = unique[:max_articles]

    logger.info(
        "[ResearchAgent] Final: %d articles (primary source: %s)",
        len(result), source_used
    )
    return result


# ---------------------------------------------------------------------------
# Format for Claude context — includes age label on every article
# ---------------------------------------------------------------------------
def _format_news_context(entries: list[dict]) -> str:
    lines = []
    for i, e in enumerate(entries, 1):
        age = _age_label(e.get("pub_dt"))
        src = e.get("source", "")
        lines.append(
            f"{i}. [{age}] {e['title']}\n"
            f"   Source:  {src} | {e['link']}\n"
            f"   Summary: {e['summary']}"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# ResearchAgent
# ---------------------------------------------------------------------------
class ResearchAgent:
    """
    Agent 1 — Research & intelligence gathering.

    Source priority:
      1. NewsAPI (real-time, last 29 days) — if NEWS_API_KEY is set
      2. Google News RSS (live search, free, always runs as supplement)
      3. Category RSS feeds (fallback if fewer than 8 articles gathered)

    Each article is labelled with its age (today / X days ago / [OLDER SOURCE])
    so Claude can clearly distinguish fresh vs stale information.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to your .env file or set it as an environment variable."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def run(self, topic: str, category: Optional[str] = None) -> ResearchInput:
        logger.info("[ResearchAgent] Starting research: %s", topic)

        cat          = category or "Green Steel Projects & Plant Development"
        live_entries = gather_live_news(cat, topic)
        news_context = _format_news_context(live_entries)
        today        = datetime.now(timezone.utc).strftime("%B %d, %Y")

        has_newsapi  = bool(os.environ.get("NEWS_API_KEY", ""))
        source_note  = "NewsAPI (real-time) + Google News RSS" if has_newsapi else "Google News RSS"

        if not live_entries:
            logger.warning("[ResearchAgent] No live articles found — Claude will use training knowledge.")
            news_context = (
                "No live articles retrieved. Use your most recent knowledge of the green steel "
                "industry, but clearly note where figures may be from your training data."
            )

        user_message = textwrap.dedent(f"""
            Today's date: {today}
            Topic: {topic}
            Category: {cat}
            News source: {source_note}

            LIVE NEWS ARTICLES (each labelled with age — prioritise the freshest ones):
            {news_context}

            STRICT INSTRUCTIONS - 2026 SOURCES ONLY:
            - Use ONLY facts from articles labelled "today", "yesterday", or "X days ago" as your
              primary sources. These are confirmed recent news.
            - Facts from articles labelled "[OLDER SOURCE]" may be used as background context only.
              Mark them clearly in the facts array with [OLDER SOURCE] prefix.
            - Do NOT invent figures. If a number is not in the articles above, do not include it.
            - The suggested_angle must be written as a wire-service news hook (active voice, present
              tense, specific — not a summary).
            - Return ONLY a valid JSON object as specified.
        """).strip()

        message = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=RESEARCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = message.content[0].text.strip()
        data     = extract_json(raw_text)
        data["topic"] = topic

        live_urls        = [e["link"] for e in live_entries if e.get("link")][:6]
        data["sources"]  = list(dict.fromkeys(data.get("sources", []) + live_urls))[:8]

        research = ResearchInput(**data)
        logger.info(
            "[ResearchAgent] Brief ready — %d facts, %d sources, %d key players.",
            len(research.facts), len(research.sources), len(research.key_players),
        )
        return research
