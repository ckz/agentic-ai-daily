#!/usr/bin/env python3
"""
build_manifest.py — Scans daily/*.md files and generates docs/manifest.json
for the Agentic AI Daily GitHub Pages site.

Usage:
    python3 build_manifest.py

The script reads each markdown file in daily/, extracts metadata (date, summary,
tweet count, topics), and writes a combined manifest.json used by the site's
JavaScript to render reports and topics.
"""

import json
import os
import re
import glob
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DAILY_DIR = REPO_ROOT / "daily"
MANIFEST_PATH = REPO_ROOT / "docs" / "manifest.json"


def parse_frontmatter(text: str) -> dict:
    """Extract YAML-like frontmatter from markdown text."""
    meta = {}
    match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if match:
        fm = match.group(1)
        for line in fm.splitlines():
            if ':' in line:
                key, _, value = line.partition(':')
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")
                if key == 'topics':
                    # Handle list-style or comma-separated
                    if value.startswith('['):
                        value = [v.strip().strip('"').strip("'") for v in value.strip('[]').split(',') if v.strip()]
                    else:
                        value = [v.strip() for v in value.split(',') if v.strip()]
                meta[key] = value
    return meta


def extract_date_from_filename(filename: str) -> str:
    """Extract YYYY-MM-DD from filename like 2025-01-15.md"""
    basename = os.path.basename(filename)
    match = re.match(r'(\d{4}-\d{2}-\d{2})', basename)
    return match.group(1) if match else ""


def extract_summary(text: str) -> str:
    """Try to extract a summary from the markdown content."""
    # Remove frontmatter
    content = re.sub(r'^---\s*\n.*?\n---', '', text, flags=re.DOTALL).strip()
    lines = content.splitlines()

    # Look for a summary line after the first heading
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('# ') and i + 1 < len(lines):
            # Skip the heading, find next non-empty line
            for j in range(i + 1, min(i + 5, len(lines))):
                candidate = lines[j].strip()
                if candidate and not candidate.startswith('#') and not candidate.startswith('!') and not candidate.startswith('|'):
                    return candidate[:200]
    # Fallback: first non-heading, non-empty line
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith('!') and not stripped.startswith('|') and not stripped.startswith('---'):
            return stripped[:200]
    return "Daily agentic AI research digest."


def extract_tweet_count(text: str) -> int | None:
    """Try to extract tweet count from the markdown content."""
    # Look for patterns like "XX tweets" or "tweets: XX" or "## Summary (XX tweets)"
    patterns = [
        r'(\d+)\s*tweets?',
        r'tweets?\s*[:=]\s*(\d+)',
        r'summary\s*\((\d+)\s*tweets?\)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def extract_topics(text: str) -> list[str]:
    """Extract topics from frontmatter or content."""
    meta = parse_frontmatter(text)
    if 'topics' in meta and isinstance(meta['topics'], list):
        return meta['topics']

    # Try to find topic-like headings
    topics = []
    content = re.sub(r'^---\s*\n.*?\n---', '', text, flags=re.DOTALL)
    for match in re.finditer(r'^##\s+(.+)$', content, re.MULTILINE):
        topic = match.group(1).strip()
        # Clean emoji and extra text
        topic = re.sub(r'^[\U0001F300-\U0001F9FF]+\s*', '', topic).strip()
        if topic and len(topic) < 50:
            topics.append(topic)
    return topics


def build_manifest():
    """Scan daily/*.md and build the manifest."""
    reports = []
    all_topics = {}

    md_files = sorted(glob.glob(str(DAILY_DIR / "*.md")))

    for filepath in md_files:
        filename = os.path.basename(filepath)
        date = extract_date_from_filename(filename)
        if not date:
            continue

        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()

        meta = parse_frontmatter(text)
        summary = meta.get('summary', '') or extract_summary(text)
        tweet_count = meta.get('tweet_count') or extract_tweet_count(text)
        if tweet_count is not None:
            try:
                tweet_count = int(tweet_count)
            except (ValueError, TypeError):
                tweet_count = None

        topics = meta.get('topics', []) or extract_topics(text)

        # Track all topics
        for t in topics:
            all_topics[t] = all_topics.get(t, 0) + 1

        report = {
            "date": date,
            "filename": filename,
            "url": f"../daily/{filename}",
            "summary": summary,
            "tweet_count": tweet_count,
            "topics": topics if isinstance(topics, list) else [],
        }
        reports.append(report)

    # Sort reports by date descending
    reports.sort(key=lambda r: r['date'], reverse=True)

    # Calculate stats
    total_reports = len(reports)
    topics_tracked = len(all_topics)
    if reports:
        first_date = min(r['date'] for r in reports)
        last_date = max(r['date'] for r in reports)
        try:
            d1 = datetime.strptime(first_date, "%Y-%m-%d")
            d2 = datetime.strptime(last_date, "%Y-%m-%d")
            days_active = (d2 - d1).days + 1
        except ValueError:
            days_active = total_reports
    else:
        days_active = 0

    manifest = {
        "reports": reports,
        "topics": [{"name": name, "count": count} for name, count in
                   sorted(all_topics.items(), key=lambda x: -x[1])],
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": {
            "total_reports": total_reports,
            "topics_tracked": topics_tracked,
            "days_active": days_active
        }
    }

    # Ensure docs/ directory exists
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"✅ Manifest generated: {MANIFEST_PATH}")
    print(f"   Reports: {total_reports}, Topics: {topics_tracked}, Days active: {days_active}")
    return manifest


if __name__ == "__main__":
    build_manifest()
