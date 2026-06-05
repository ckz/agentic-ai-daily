#!/usr/bin/env python3
"""
Agentic AI Daily Research Script
=================================
Runs daily via cron to:
1. Search X/Twitter via twitterapi.io for agentic AI content
2. Check novelty against previous daily reports
3. Write findings to daily/YYYY-MM-DD.md
4. Update topic tracking in topics/*.md
5. Git commit and push
"""

import os
import re
import sys
import glob
import time
import json
import signal
import logging
import textwrap
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
DAILY_DIR = REPO_DIR / "daily"
TOPICS_DIR = REPO_DIR / "topics"
ENV_FILE = Path.home() / ".openclaw" / ".env"

API_BASE = "https://api.twitterapi.io"
API_TIMEOUT = 30          # seconds per request
MAX_RETRIES = 10          # on 429 / transient errors
BACKOFF_BASE = 5          # seconds, doubles each retry

TODAY = date.today().isoformat()  # YYYY-MM-DD

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("daily_research")

# ---------------------------------------------------------------------------
# Env-file parsing (no dotenv dependency)
# ---------------------------------------------------------------------------

def parse_env(path: Path) -> dict:
    """Parse a simple KEY=VALUE .env file (ignores comments & blanks)."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


# ---------------------------------------------------------------------------
# Twitter API helpers
# ---------------------------------------------------------------------------

def search_tweets(api_key: str, query: str, query_type: str = "Top") -> list:
    """Run one advanced search query against twitterapi.io. Returns list of tweet dicts."""
    url = f"{API_BASE}/twitter/tweet/advanced_search"
    headers = {"x-api-key": api_key}
    params = {"query": query, "queryType": query_type}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=API_TIMEOUT)
            if resp.status_code == 429:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning("Rate-limited (429). Retry %d/%d — sleeping %ds", attempt, MAX_RETRIES, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            tweets = data.get("tweets", [])
            log.info("Query '%s' returned %d tweets", query[:60], len(tweets))
            return tweets
        except requests.exceptions.RequestException as exc:
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            log.error("Request error (attempt %d/%d): %s — retrying in %ds", attempt, MAX_RETRIES, exc, wait)
            if attempt == MAX_RETRIES:
                raise
            time.sleep(wait)

    return []


def run_all_searches(api_key: str) -> list:
    """Execute all search queries and return deduplicated, engagement-sorted tweets."""
    queries = [
        "agentic AI agent launch OR release OR announce min_faves:200",
        "AI agent new product OR startup OR funding min_faves:100",
        "Claude OR OpenAI OR Google agent tool OR coding agent min_faves:500",
        "AI agents future OR MCP OR computer-use OR browser-use min_faves:300",
        "agentic framework OR agent platform OR autonomous agent min_faves:100",
    ]

    all_tweets = {}  # id -> tweet dict  (dedup by ID)

    for query in queries:
        try:
            tweets = search_tweets(api_key, query, query_type="Top")
            for t in tweets:
                tid = t.get("id")
                if tid and tid not in all_tweets:
                    all_tweets[tid] = t
        except Exception as exc:
            log.error("Search failed for query '%s': %s", query[:50], exc)

    # Sort by likeCount descending
    sorted_tweets = sorted(all_tweets.values(), key=lambda t: int(t.get("likeCount") or 0), reverse=True)
    log.info("Total deduplicated tweets: %d", len(sorted_tweets))
    return sorted_tweets


# ---------------------------------------------------------------------------
# Novelty checking against previous daily reports
# ---------------------------------------------------------------------------

def load_previous_reports() -> dict:
    """
    Scan all daily/*.md files (excluding today's) and return a dict with:
      - urls: set of previously mentioned tweet URLs
      - usernames: set of @usernames mentioned
      - headings: set of heading phrases (## / ### lines)
    """
    prev = {"urls": set(), "usernames": set(), "headings": set()}

    pattern = str(DAILY_DIR / "*.md")
    for filepath in sorted(glob.glob(pattern)):
        fname = os.path.basename(filepath)
        if fname == f"{TODAY}.md":
            continue  # skip today's if it exists already
        try:
            text = Path(filepath).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Extract URLs (twitter.com / x.com)
        for url in re.findall(r'https?://(?:twitter|x)\.com/\S+', text):
            prev["urls"].add(url.rstrip(")")  .rstrip("]"))

        # Extract @usernames
        for user in re.findall(r'@([A-Za-z0-9_]{1,15})', text):
            prev["usernames"].add(user.lower())

        # Extract heading phrases
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("##"):
                # Remove leading #'s and whitespace
                phrase = re.sub(r'^#+\s*', '', stripped).strip()
                if phrase and not phrase.startswith("📊") and not phrase.startswith("🆕") and not phrase.startswith("🔁") and not phrase.startswith("♻️") and not phrase.startswith("📈") and not phrase.startswith("🔗"):
                    prev["headings"].add(phrase.lower())

    log.info("Previous reports: %d urls, %d usernames, %d headings",
             len(prev["urls"]), len(prev["usernames"]), len(prev["headings"]))
    return prev


def classify_novelty(tweet: dict, previous: dict) -> str:
    """
    Classify a tweet as:
      🆕 NEW     — URL and author never seen before
      🔁 UPDATE  — same author/topic seen before, but new URL (new development)
      ♻️ KNOWN   — exact URL already covered
    """
    url = (tweet.get("url") or "").rstrip(")").rstrip("]")
    author = (tweet.get("author", {}).get("userName") or "").lower()

    if url and url in previous["urls"]:
        return "♻️ KNOWN"

    if author and author in previous["usernames"]:
        return "🔁 UPDATE"

    # Check if tweet text overlaps heavily with any previous heading
    text_lower = (tweet.get("text") or "").lower()
    for heading in previous["headings"]:
        # Simple overlap: if >60% of heading words appear in tweet text
        heading_words = set(heading.split())
        if len(heading_words) > 2:
            overlap = heading_words & set(text_lower.split())
            if len(overlap) / len(heading_words) > 0.6:
                return "🔁 UPDATE"

    return "🆕 NEW"


# ---------------------------------------------------------------------------
# Topic tracking
# ---------------------------------------------------------------------------

def extract_topics(tweets: list) -> dict:
    """
    Naive topic extraction: scan tweet text for common agentic-AI keywords/phrases
    and group tweets by topic. Returns {topic_slug: {"name": ..., "tweets": [...]}}
    """
    TOPIC_PATTERNS = [
        ("mcp", [r'\bmcp\b', r'\bmodel context protocol\b']),
        ("coding-agents", [r'\bcoding agent\b', r'\bcode agent\b', r'\bswe-agent\b', r'\bswe agent\b', r'\bdevin\b', r'\bcursor agent\b']),
        ("computer-use", [r'\bcomputer.use\b', r'\bcomputer use\b', r'\bui agent\b', r'\bdesktop agent\b']),
        ("browser-use", [r'\bbrowser.use\b', r'\bbrowser use\b', r'\bweb agent\b', r'\bweb browsing agent\b']),
        ("openai-agents", [r'\bopenai\b.*\bagent\b', r'\bagent\b.*\bopenai\b', r'\bgpt\b.*\bagent\b', r'\boperators?\b.*\bopenai\b']),
        ("claude-agents", [r'\bclaude\b.*\bagent\b', r'\bagent\b.*\bclaude\b', r'\banthropic\b.*\bagent\b']),
        ("google-agents", [r'\bgoogle\b.*\bagent\b', r'\bagent\b.*\bgoogle\b', r'\bgemini\b.*\bagent\b', r'\bproject mariner\b']),
        ("autonomous-agents", [r'\bautonomo?us\b', r'\bself.directing\b', r'\bagentic\b']),
        ("agent-frameworks", [r'\bframework\b', r'\blanggraph\b', r'\bcrewai\b', r'\bautogen\b', r'\bagent builder\b', r'\bagent platform\b']),
        ("funding-startups", [r'\bfunding\b', r'\braised?\b.*\$\d', r'\bseed\b', r'\bseries [abc]\b', r'\bstartup\b']),
        ("multi-agent", [r'\bmulti.agent\b', r'\bswarm\b', r'\bagent.team\b', r'\bagent collaborat']),
    ]

    topics = {}
    for tweet in tweets:
        text = (tweet.get("text") or "").lower()
        for slug, patterns in TOPIC_PATTERNS:
            if any(re.search(p, text) for p in patterns):
                if slug not in topics:
                    topics[slug] = {"name": slug.replace("-", " ").title(), "tweets": []}
                topics[slug]["tweets"].append(tweet)

    return topics


def update_topic_files(topics: dict):
    """Create or update topics/<slug>.md files."""
    TOPICS_DIR.mkdir(parents=True, exist_ok=True)

    for slug, info in topics.items():
        topic_path = TOPICS_DIR / f"{slug}.md"

        # Load existing data if present
        existing_tweets = set()
        first_seen = TODAY
        last_seen = TODAY
        total_mentions = 0

        if topic_path.exists():
            try:
                existing_text = topic_path.read_text(encoding="utf-8", errors="replace")
                # Parse existing metadata
                for line in existing_text.splitlines():
                    if line.startswith("first_seen:"):
                        first_seen = line.split(":", 1)[1].strip()
                    elif line.startswith("last_seen:"):
                        last_seen = line.split(":", 1)[1].strip()
                    elif line.startswith("total_mentions:"):
                        total_mentions = int(line.split(":", 1)[1].strip())
                    elif line.startswith("- "):
                        # Extract tweet URL for dedup
                        url_match = re.search(r'https?://\S+', line)
                        if url_match:
                            existing_tweets.add(url_match.group().rstrip(")").rstrip("]"))
            except Exception:
                pass

        # Merge new tweets
        new_count = 0
        tweet_lines = []
        for t in info["tweets"]:
            url = (t.get("url") or "").strip()
            if url and url not in existing_tweets:
                author = t.get("author", {}).get("userName", "unknown")
                tweet_lines.append(f"- [{author}]({url}) ({TODAY})")
                existing_tweets.add(url)
                new_count += 1

        total_mentions += new_count
        last_seen = TODAY

        # Rebuild file
        name = info["name"]
        tweet_list = "\n".join(sorted(existing_tweets))
        # We need to reconstruct from scratch to keep it clean
        all_tweet_entries = []
        if topic_path.exists():
            try:
                for line in topic_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("- "):
                        all_tweet_entries.append(line)
            except Exception:
                pass
        all_tweet_entries.extend(tweet_lines)

        content = textwrap.dedent(f"""\
        # {name}

        first_seen: {first_seen}
        last_seen: {last_seen}
        total_mentions: {total_mentions}

        ## Related Tweets
        """)
        for entry in sorted(set(all_tweet_entries)):
            content += entry + "\n"

        topic_path.write_text(content, encoding="utf-8")
        log.info("Updated topic: %s (%d total mentions, +%d new)", slug, total_mentions, new_count)


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_summary(tweets_by_novelty: dict) -> str:
    """Generate a brief text summary of the day's themes."""
    new_tweets = tweets_by_novelty.get("🆕 NEW", [])
    if not new_tweets:
        return "No major new findings today. The agentic AI space was relatively quiet."

    # Collect key phrases from new tweets
    themes = set()
    for t in new_tweets[:10]:
        text = (t.get("text") or "")[:200]
        # Simple: grab first sentence or 80 chars
        sentence = text.split(".")[0][:100].strip()
        if sentence:
            themes.add(sentence)

    lines = ["Key themes today:\n"]
    for i, theme in enumerate(sorted(themes)[:8], 1):
        lines.append(f"{i}. {theme}")
    return "\n".join(lines)


def extract_title(tweet: dict) -> str:
    """Extract a short title from a tweet's text."""
    text = (tweet.get("text") or "").strip()
    # Take first line or first 100 chars
    first_line = text.split("\n")[0].strip()
    if len(first_line) > 100:
        return first_line[:97] + "..."
    return first_line or "Untitled"


def extract_tags(tweet: dict) -> str:
    """Extract hashtags from tweet text."""
    text = tweet.get("text") or ""
    tags = re.findall(r'#(\w+)', text)
    # Also add some auto-tags based on content
    text_lower = text.lower()
    if "mcp" in text_lower:
        tags.append("MCP")
    if "agent" in text_lower:
        tags.append("AIagents")
    if "claude" in text_lower:
        tags.append("Claude")
    if "openai" in text_lower:
        tags.append("OpenAI")
    if "coding" in text_lower or "code" in text_lower:
        tags.append("CodingAgent")
    # Dedupe and format
    seen = set()
    unique = []
    for t in tags:
        t_lower = t.lower()
        if t_lower not in seen:
            seen.add(t_lower)
            unique.append(f"#{t}")
    return " ".join(unique[:6]) if unique else "#AgenticAI"


def why_it_matters(tweet: dict) -> str:
    """Generate a brief 'why it matters' note from tweet content."""
    text = (tweet.get("text") or "")
    likes = int(tweet.get("likeCount") or 0)
    author = tweet.get("author", {}).get("name", "")

    if likes >= 5000:
        return f"High-impact signal from {author} with {likes:,} likes — indicates strong community interest."
    elif likes >= 1000:
        return f"Significant engagement ({likes:,} likes) — suggests this is resonating with the AI community."
    elif likes >= 200:
        return f"Notable mention from {author} — worth tracking for emerging trends."
    return f"Shared by {author} — adds to the daily agentic AI discourse."


def build_markdown_report(tweets: list, novelty_map: dict, previous: dict) -> str:
    """Build the full daily report markdown."""
    # Classify tweets
    new_tweets = []
    update_tweets = []
    known_tweets = []
    for t in tweets:
        tag = novelty_map.get(t.get("id", ""), "🆕 NEW")
        if tag == "🆕 NEW":
            new_tweets.append(t)
        elif tag == "🔁 UPDATE":
            update_tweets.append(t)
        else:
            known_tweets.append(t)

    total = len(tweets)
    new_count = len(new_tweets)
    known_count = len(update_tweets) + len(known_tweets)
    novelty_score = round((new_count / total * 100) if total > 0 else 0, 1)

    tweets_by_novelty = {"🆕 NEW": new_tweets, "🔁 UPDATE": update_tweets, "♻️ KNOWN": known_tweets}
    summary = generate_summary(tweets_by_novelty)

    # --- Build markdown ---
    lines = []
    lines.append("---")
    lines.append(f"date: {TODAY}")
    lines.append(f"total_tweets: {total}")
    lines.append(f"new_findings: {new_count}")
    lines.append(f"known_findings: {known_count}")
    lines.append(f"novelty_score: {novelty_score}%")
    lines.append("queries_run: 5")
    lines.append("---")
    lines.append("")
    lines.append(f"# 🤖 Agentic AI Daily — {TODAY}")
    lines.append("")
    lines.append("## 📊 Daily Summary")
    lines.append(summary)
    lines.append("")

    # --- New Findings ---
    lines.append("## 🆕 New Findings")
    lines.append("")
    if new_tweets:
        for i, t in enumerate(new_tweets, 1):
            title = extract_title(t)
            author = t.get("author", {})
            username = author.get("userName", "unknown")
            name = author.get("name", username)
            created = t.get("createdAt", "")
            likes = int(t.get("likeCount") or 0)
            rts = int(t.get("retweetCount") or 0)
            views = int(t.get("viewCount") or 0)
            url = t.get("url", "")
            tags = extract_tags(t)
            matters = why_it_matters(t)

            lines.append(f"### {i}. {title}")
            lines.append(f"- **Source:** @{username} ({name}) — {created}")
            lines.append(f"- **Engagement:** ❤️ {likes:,}  🔄 {rts:,}  👁 {views:,}")
            lines.append(f"- **Link:** {url}")
            lines.append(f"- **Why it matters:** {matters}")
            lines.append(f"- **Tags:** {tags}")
            lines.append("")
    else:
        lines.append("_No brand-new findings today._")
        lines.append("")

    # --- Updates ---
    lines.append("## 🔁 Updates (known topics with new developments)")
    lines.append("")
    if update_tweets:
        for i, t in enumerate(update_tweets, 1):
            title = extract_title(t)
            author = t.get("author", {})
            username = author.get("userName", "unknown")
            name = author.get("name", username)
            created = t.get("createdAt", "")
            likes = int(t.get("likeCount") or 0)
            rts = int(t.get("retweetCount") or 0)
            views = int(t.get("viewCount") or 0)
            url = t.get("url", "")
            tags = extract_tags(t)
            matters = why_it_matters(t)

            lines.append(f"### {i}. {title}")
            lines.append(f"- **Source:** @{username} ({name}) — {created}")
            lines.append(f"- **Engagement:** ❤️ {likes:,}  🔄 {rts:,}  👁 {views:,}")
            lines.append(f"- **Link:** {url}")
            lines.append(f"- **Why it matters:** {matters}")
            lines.append(f"- **Tags:** {tags}")
            lines.append("")
    else:
        lines.append("_No updates on previously tracked topics._")
        lines.append("")

    # --- Known / Ongoing ---
    lines.append("## ♻️ Known / Ongoing")
    lines.append("")
    if known_tweets:
        for t in known_tweets:
            author = t.get("author", {}).get("userName", "unknown")
            url = t.get("url", "")
            title = extract_title(t)
            lines.append(f"- @{author}: [{title}]({url})")
        lines.append("")
    else:
        lines.append("_No previously covered items reappeared today._")
        lines.append("")

    # --- Trending Topics ---
    lines.append("## 📈 Trending Topics Today")
    lines.append("")
    topics = extract_topics(tweets)
    if topics:
        lines.append("| Topic | Mentions | Sentiment |")
        lines.append("|-------|----------|-----------|")
        for slug, info in sorted(topics.items(), key=lambda x: len(x[1]["tweets"]), reverse=True):
            count = len(info["tweets"])
            # Very naive sentiment: count positive words vs negative
            all_text = " ".join(t.get("text", "").lower() for t in info["tweets"])
            pos = sum(all_text.count(w) for w in ["exciting", "amazing", "great", "love", "awesome", "breakthrough", "launch", "release", "impressive"])
            neg = sum(all_text.count(w) for w in ["fail", "bad", "broken", "hype", "disappointed", "overrated"])
            if pos > neg * 2:
                sentiment = "😊 Positive"
            elif neg > pos:
                sentiment = "😟 Negative"
            else:
                sentiment = "😐 Neutral"
            lines.append(f"| {info['name']} | {count} | {sentiment} |")
        lines.append("")
    else:
        lines.append("_No specific topics identified._")
        lines.append("")

    # --- All Sources ---
    lines.append("## 🔗 All Sources")
    lines.append("")
    if tweets:
        lines.append("| # | Author | Tweet | Likes |")
        lines.append("|---|--------|-------|-------|")
        for i, t in enumerate(tweets, 1):
            author = t.get("author", {}).get("userName", "unknown")
            url = t.get("url", "")
            title = extract_title(t)[:60]
            likes = int(t.get("likeCount") or 0)
            lines.append(f"| {i} | @{author} | [{title}]({url}) | {likes:,} |")
        lines.append("")

    return "\n".join(lines)


def build_error_report(error_msg: str) -> str:
    """Build a minimal report when the API fails."""
    lines = [
        "---",
        f"date: {TODAY}",
        "total_tweets: 0",
        "new_findings: 0",
        "known_findings: 0",
        "novelty_score: 0%",
        "queries_run: 0",
        "---",
        "",
        f"# 🤖 Agentic AI Daily — {TODAY}",
        "",
        "## ⚠️ Error",
        "",
        f"The daily research script encountered an error:",
        "",
        f"```",
        f"{error_msg}",
        f"```",
        "",
        f"_This report was auto-generated on {datetime.now().isoformat()}._",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def git_commit_and_push(report_path: str, new_count: int):
    """Stage, commit, and push the daily report and any topic changes."""
    import subprocess

    cwd = str(REPO_DIR)

    def run(cmd: str):
        log.info("git: %s", cmd)
        result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log.warning("git stderr: %s", result.stderr.strip())
        return result

    # Ensure identity
    run("git config user.name ckz")
    run("git config user.email ck@seeice.com")

    run("git add -A")

    # Check if there's anything to commit
    result = run("git status --porcelain")
    if not result.stdout.strip():
        log.info("Nothing to commit.")
        return

    commit_msg = f"📅 daily report: {TODAY} — {new_count} new findings"
    run(f'git commit -m "{commit_msg}"')

    # Push using PAT
    env = parse_env(ENV_FILE)
    pat = env.get("GITHUB_PAT", "")
    if pat:
        # Set remote URL with PAT for push
        run(f'git remote set-url origin https://ckz:{pat}@github.com/ckz/agentic-ai-daily.git')

    result = run("git push origin main")
    if result.returncode == 0:
        log.info("Pushed successfully.")
    else:
        log.error("Push failed: %s", result.stderr.strip())

    # Clean PAT from remote URL for security
    run("git remote set-url origin https://github.com/ckz/agentic-ai-daily.git")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Agentic AI Daily Research — %s", TODAY)
    log.info("=" * 60)

    # Load API key
    env = parse_env(ENV_FILE)
    api_key = env.get("TWITTERAPI_IO_KEY")
    if not api_key:
        error_msg = "TWITTERAPI_IO_KEY not found in ~/.openclaw/.env"
        log.error(error_msg)
        report = build_error_report(error_msg)
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        report_path = DAILY_DIR / f"{TODAY}.md"
        report_path.write_text(report, encoding="utf-8")
        git_commit_and_push(str(report_path), 0)
        sys.exit(1)

    # Ensure directories exist
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    TOPICS_DIR.mkdir(parents=True, exist_ok=True)

    # Load previous reports for novelty checking
    previous = load_previous_reports()

    # Run searches
    try:
        tweets = run_all_searches(api_key)
    except Exception as exc:
        error_msg = f"API search failed: {exc}"
        log.error(error_msg)
        report = build_error_report(error_msg)
        report_path = DAILY_DIR / f"{TODAY}.md"
        report_path.write_text(report, encoding="utf-8")
        git_commit_and_push(str(report_path), 0)
        sys.exit(1)

    if not tweets:
        log.warning("No tweets found today.")
        report = build_error_report("No tweets returned from any search query.")
        report_path = DAILY_DIR / f"{TODAY}.md"
        report_path.write_text(report, encoding="utf-8")
        git_commit_and_push(str(report_path), 0)
        sys.exit(0)

    # Classify novelty
    novelty_map = {}
    for t in tweets:
        tid = t.get("id", "")
        novelty_map[tid] = classify_novelty(t, previous)

    new_count = sum(1 for v in novelty_map.values() if v == "🆕 NEW")

    # Generate report
    report = build_markdown_report(tweets, novelty_map, previous)
    report_path = DAILY_DIR / f"{TODAY}.md"
    report_path.write_text(report, encoding="utf-8")
    log.info("Report written to %s", report_path)

    # Update topic files
    topics = extract_topics(tweets)
    update_topic_files(topics)

    # Git commit and push
    git_commit_and_push(str(report_path), new_count)

    log.info("Done! %d tweets processed, %d new findings.", len(tweets), new_count)


if __name__ == "__main__":
    main()
