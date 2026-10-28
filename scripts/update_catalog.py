#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Product Hunt daily catalog updater with Turso database sync.
Uses the official libsql package for remote access (HTTP).
Reads credentials from env: TURSO_DB_URL, TURSO_AUTH_TOKEN.
"""

from __future__ import annotations

import html
import json
import os
import random
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import libsql

PH_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"
README_PATH = "README.md"

START_TODAY = "<!-- START:PH_TODAY -->"
END_TODAY = "<!-- END:PH_TODAY -->"
START_ARCHIVE = "<!-- START:ARCHIVE -->"
END_ARCHIVE = "<!-- END:ARCHIVE -->"

# --- Retry / rate-limit tuning ---------------------------------------------

MAX_RETRIES = 8

# Product Hunt's rate-limit window is 15 minutes = 900s. Allow waiting for
# the full window rather than failing at 840s. Combined with the workflow's
# timeout-minutes=30, this lets a single 429 wait out the window and resume
# pagination from the last cursor.
RETRY_AFTER_FAIL_THRESHOLD = int(os.getenv("PH_RETRY_AFTER_FAIL_THRESHOLD", "900"))

# Cap total accumulated sleep inside one ph_call_with_retry invocation.
# Guards against a pathological loop that repeatedly trips 429 with a full
# 900s reset, which would otherwise keep the job alive for hours.
MAX_TOTAL_WAIT_SECONDS = int(os.getenv("PH_MAX_TOTAL_WAIT_SECONDS", "900"))

# Cap on how many pagination pages fetch_posts_for_day will ever request.
# Prevents an infinite loop if PH ever returns hasNextPage=true with a
# non-advancing cursor. 200 pages × 50 posts = 10 000 posts, well above any
# realistic day; exceeding it is a bug signal, not a legitimate day size.
MAX_PAGES = int(os.getenv("PH_MAX_PAGES", "200"))

# Client-side sliding window for request spacing. The 450/15min request
# budget applies to non-GraphQL endpoints; the /graphql endpoint is bounded
# by complexity instead (see ph_call_with_retry docstring). We still enforce
# a minimum spacing to keep bursts tame on CI runners.
REQUEST_BUDGET_WINDOW = 15 * 60  # seconds
REQUEST_BUDGET_MAX = int(os.getenv("PH_REQUEST_BUDGET_MAX", "400"))
MIN_REQUEST_INTERVAL = float(os.getenv("PH_MIN_REQUEST_INTERVAL", "0.6"))

# --- Sliding-window rate limiter -------------------------------------------

_request_times: list[float] = []
_request_lock = threading.Lock()
_last_request_at: float = 0.0


def _throttle() -> None:
    """Block until it is safe to issue another Product Hunt request."""
    global _last_request_at
    with _request_lock:
        now = time.monotonic()

        since_last = now - _last_request_at
        if since_last < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - since_last)
            now = time.monotonic()

        cutoff = now - REQUEST_BUDGET_WINDOW
        while _request_times and _request_times[0] < cutoff:
            _request_times.pop(0)

        if len(_request_times) >= REQUEST_BUDGET_MAX:
            sleep_for = _request_times[0] + REQUEST_BUDGET_WINDOW - now + 0.5
            if sleep_for > 0:
                print(
                    f"Local rate budget reached "
                    f"({REQUEST_BUDGET_MAX} req / {REQUEST_BUDGET_WINDOW}s); "
                    f"sleeping {sleep_for:.1f}s before next PH call.",
                    file=sys.stderr,
                )
                time.sleep(sleep_for)
                now = time.monotonic()
                cutoff = now - REQUEST_BUDGET_WINDOW
                while _request_times and _request_times[0] < cutoff:
                    _request_times.pop(0)

        _request_times.append(now)
        _last_request_at = now


# --- GraphQL query ----------------------------------------------------------
# `topics(first: 5)` caps the nested topic connection per post, which is the
# dominant complexity cost after the base per-page charge. Posts with >5
# topics keep their first 5 categories in the catalog — a deliberate trade
# to fit a busy day inside one 6250-point window.

QUERY_POSTS = r"""
query Posts($first: Int, $after: String, $postedAfter: DateTime, $postedBefore: DateTime) {
  posts(first: $first, after: $after, postedAfter: $postedAfter, postedBefore: $postedBefore) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        name
        tagline
        url
        website
        votesCount
        commentsCount
        media { videoUrl }
        topics(first: 5) { edges { node { name } } }
      }
    }
  }
}
"""


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def md_escape_text(s: str) -> str:
    return (s or "").replace("\n", " ").replace("|", "\\|").strip()


def safe_link(label: str, url: str) -> str:
    label = md_escape_text(label)
    url = (url or "").strip()
    if not url:
        return label
    return f"[{label}](<{url}>)"


def html_compact(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = html.escape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.replace("\n", "<br>")


def website_icon_link(website: str) -> str:
    w = (website or "").strip()
    if not w:
        return "—"
    return f"[🔗](<{w}>)"


def build_description_cell(tagline: str) -> str:
    short = html_compact(tagline)
    return short if short else "—"


# --- Rate-limit header helpers ---------------------------------------------
# Product Hunt returns rate-limit info under these EXACT header names.
# Note the hyphens: `X-Rate-Limit-Reset` — NOT `X-RateLimit-Reset`.

_RATE_HEADERS = (
    "X-Rate-Limit-Limit",
    "X-Rate-Limit-Remaining",
    "X-Rate-Limit-Reset",
    "Retry-After",
)


def _header_int(headers, name: str) -> int | None:
    """Return a header value as int, or None if absent / non-numeric."""
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (ValueError, TypeError):
        return None


def _log_rate_headers(headers, prefix: str = "") -> None:
    """Print whatever rate-limit headers PH exposed (when present)."""
    if headers is None:
        return
    parts = []
    for k in _RATE_HEADERS:
        v = headers.get(k)
        if v is not None:
            parts.append(f"{k}={v}")
    if parts:
        print(f"{prefix}rate-limit: " + " ".join(parts), file=sys.stderr)


def ph_call_with_retry(token: str, query: str, variables: dict) -> dict:
    """Call the PH GraphQL API with robust 429 / 5xx / network retry.

    Rate-limit behaviour:
    - On 429, prefers `X-Rate-Limit-Reset` (seconds until the current
      complexity window resets) and sleeps exactly that long + jitter.
    - Falls back to `Retry-After` if present.
    - Falls back to exponential backoff only if neither header is present.
    - Fails fast if the required wait exceeds RETRY_AFTER_FAIL_THRESHOLD
      (indicates the next scheduled run should pick this up instead).
    - Caps total accumulated wait at MAX_TOTAL_WAIT_SECONDS so a pathologically
      rate-limited run can't hang the job for hours.
    - Retries transient 5xx responses and URLError / timeouts.
    - Applies a client-side sliding-window budget before every request.
    """
    last_exc: Exception | None = None
    total_waited = 0.0

    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
            req = Request(
                PH_ENDPOINT,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urlopen(req, timeout=45) as resp:
                _log_rate_headers(resp.headers, prefix="")
                raw = resp.read().decode("utf-8")

            out = json.loads(raw)
            if out.get("errors"):
                raise RuntimeError(json.dumps(out["errors"], ensure_ascii=False))
            return out.get("data") or {}

        except HTTPError as e:
            last_exc = e

            if e.code == 429:
                _log_rate_headers(e.headers, prefix="429 ")

                reset = _header_int(e.headers, "X-Rate-Limit-Reset")
                retry_after = _header_int(e.headers, "Retry-After")

                if reset is not None and reset > 0:
                    delay = float(reset)
                    hint = f"X-Rate-Limit-Reset={reset}"
                elif retry_after is not None and retry_after > 0:
                    delay = float(retry_after)
                    hint = f"Retry-After={retry_after}"
                else:
                    delay = min(60.0 * (2 ** attempt), 900.0)
                    hint = "exponential fallback"

                delay += random.uniform(1, 5)

                print(
                    f"Rate limited (429). attempt={attempt + 1}/{MAX_RETRIES} "
                    f"source={hint} sleeping {delay:.1f}s "
                    f"(total_waited={total_waited:.0f}s)",
                    file=sys.stderr,
                )

                if delay > RETRY_AFTER_FAIL_THRESHOLD:
                    print(
                        f"Required wait ({delay:.0f}s) exceeds "
                        f"PH_RETRY_AFTER_FAIL_THRESHOLD="
                        f"{RETRY_AFTER_FAIL_THRESHOLD}s — likely quota exhaustion; "
                        f"not retrying (next scheduled run will pick this up).",
                        file=sys.stderr,
                    )
                    raise

                if total_waited + delay > MAX_TOTAL_WAIT_SECONDS:
                    print(
                        f"Total wait would reach {total_waited + delay:.0f}s, "
                        f"exceeding PH_MAX_TOTAL_WAIT_SECONDS="
                        f"{MAX_TOTAL_WAIT_SECONDS}s — giving up.",
                        file=sys.stderr,
                    )
                    raise

                if attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(delay)
                total_waited += delay
                continue

            if 500 <= e.code < 600 and attempt < MAX_RETRIES - 1:
                delay = min(2.0 ** attempt, 60.0) + random.uniform(0, 2)
                print(
                    f"Transient HTTP {e.code} on attempt {attempt + 1}; "
                    f"sleeping {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue

            raise

        except (URLError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = min(2.0 ** attempt, 60.0) + random.uniform(0, 2)
                print(
                    f"Network error ({type(e).__name__}: {e}); "
                    f"sleeping {delay:.1f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("ph_call_with_retry: exhausted retries")


def get_target_day(tz_name: str):
    tz = ZoneInfo(tz_name)
    override = (os.getenv("DATE") or "").strip()
    if override:
        y, m, d = [int(x) for x in override.split("-")]
        start = datetime(y, m, d, 0, 0, 0, tzinfo=tz)
    else:
        now = datetime.now(tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    label = start.strftime("%d-%m-%Y")
    year = start.strftime("%Y")
    month = start.strftime("%m")
    return start, end, label, year, month


def fetch_posts_for_day(token: str, start_local: datetime, end_local: datetime) -> list[dict]:
    """Paginate `posts` for the given local day.

    Emits one log line per page so the size of the day's post set is visible
    in the CI log. Guards against non-advancing cursors and runaway page
    counts, both of which would otherwise look identical to a "really big
    day" until the rate limit trips.
    """
    after = None
    items: list[dict] = []
    pages = 0
    seen_cursors: set[str] = set()

    while True:
        pages += 1
        if pages > MAX_PAGES:
            print(
                f"WARNING: exceeded MAX_PAGES={MAX_PAGES}; truncating results.",
                file=sys.stderr,
            )
            break

        vars_ = {
            "first": 50,
            "after": after,
            "postedAfter": iso_z(start_local),
            "postedBefore": iso_z(end_local),
        }
        data = ph_call_with_retry(token, QUERY_POSTS, vars_)
        conn = data.get("posts") or {}
        edges = conn.get("edges") or []

        for e in edges:
            node = e.get("node")
            if not node:
                continue
            if "media" not in node or node["media"] is None:
                node["media"] = []
            video_url = ""
            media = node.get("media", [])
            if media and len(media) > 0:
                video_url = media[0].get("videoUrl") or ""
            if not video_url:
                continue
            node["video_url"] = video_url
            topics_conn = node.get("topics") or {}
            topic_edges = topics_conn.get("edges") or []
            node["topics"] = []
            for te in topic_edges:
                tn = te.get("node")
                if tn and tn.get("name"):
                    node["topics"].append({"name": tn["name"]})
            items.append(node)

        page = conn.get("pageInfo") or {}
        has_next = bool(page.get("hasNextPage"))
        print(
            f"[page {pages}] edges={len(edges)} kept_total={len(items)} "
            f"has_next={has_next}",
            file=sys.stderr,
        )

        if not has_next:
            break

        next_cursor = page.get("endCursor")
        if not next_cursor:
            print(
                "WARNING: hasNextPage=true but endCursor is empty; stopping.",
                file=sys.stderr,
            )
            break
        if next_cursor == after:
            print(
                f"WARNING: endCursor did not advance ({next_cursor!r}); stopping.",
                file=sys.stderr,
            )
            break
        if next_cursor in seen_cursors:
            print(
                f"WARNING: repeated endCursor {next_cursor!r} detected; stopping.",
                file=sys.stderr,
            )
            break

        seen_cursors.add(next_cursor)
        after = next_cursor

    print(
        f"[pagination done] pages={pages} posts_with_video={len(items)}",
        file=sys.stderr,
    )
    return items


@dataclass
class DailyStats:
    launches: int
    total_votes: int
    avg_votes: float
    median_votes: float
    total_comments: int
    avg_comments: float
    median_comments: float


def compute_daily_stats(posts):
    votes = [int(p.get("votesCount") or 0) for p in posts]
    comments = [int(p.get("commentsCount") or 0) for p in posts]
    n = len(posts)
    votes_sorted = sorted(votes)
    comments_sorted = sorted(comments)
    total_votes = sum(votes)
    total_comments = sum(comments)
    avg_votes = (total_votes / n) if n else 0.0
    avg_comments = (total_comments / n) if n else 0.0
    if n == 0:
        median_votes = median_comments = 0.0
    elif n % 2 == 1:
        median_votes = float(votes_sorted[n // 2])
        median_comments = float(comments_sorted[n // 2])
    else:
        median_votes = (votes_sorted[n // 2 - 1] + votes_sorted[n // 2]) / 2.0
        median_comments = (comments_sorted[n // 2 - 1] + comments_sorted[n // 2]) / 2.0
    return DailyStats(
        launches=n,
        total_votes=total_votes,
        avg_votes=avg_votes,
        median_votes=median_votes,
        total_comments=total_comments,
        avg_comments=avg_comments,
        median_comments=median_comments,
    )


def render_posts_table(posts):
    lines = [
        "| # | App | Description | Votes | Comments | Website | Video | Categories |",
        "|---:|---|---|---:|---:|---|---|---|",
    ]
    posts_sorted = sorted(posts, key=lambda p: int(p.get("votesCount") or 0), reverse=True)
    for i, p in enumerate(posts_sorted, 1):
        name = md_escape_text(p.get("name") or "")
        ph_url = (p.get("url") or "").strip()
        app_cell = safe_link(name, ph_url) if ph_url else name
        desc_cell = build_description_cell(p.get("tagline") or "")
        votes = int(p.get("votesCount") or 0)
        comments = int(p.get("commentsCount") or 0)
        website_cell = website_icon_link(p.get("website") or "")
        video_url = p.get("video_url", "")
        video_cell = safe_link("🎥 Watch", video_url) if video_url else "—"
        topics = p.get("topics") or []
        topic_names = [t.get("name", "") for t in topics if isinstance(t, dict)]
        categories_cell = ", ".join(topic_names) if topic_names else "—"
        lines.append(
            f"| {i} | {app_cell} | {desc_cell} | {votes} | {comments} | "
            f"{website_cell} | {video_cell} | {categories_cell} |"
        )
    return "\n".join(lines) + "\n"


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def write_text(path, content):
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def replace_block(text, start, end, new_block):
    start_idx = text.find(start)
    if start_idx == -1:
        return text.rstrip() + "\n\n" + start + "\n" + new_block + "\n" + end + "\n"
    end_idx = text.find(end, start_idx)
    if end_idx == -1:
        return text.rstrip() + "\n\n" + start + "\n" + new_block + "\n" + end + "\n"
    before = text[:start_idx]
    after = text[end_idx + len(end):]
    return before + start + "\n" + new_block + "\n" + end + after


def scan_archive_nav():
    years = [d for d in os.listdir(".") if d.isdigit() and len(d) == 4 and os.path.isdir(d)]
    years.sort(reverse=True)
    if not years:
        return "_No reports yet._"
    lines = []
    for y in years:
        months = [
            m for m in os.listdir(y)
            if m.isdigit() and len(m) == 2 and os.path.isdir(os.path.join(y, m))
        ]
        months.sort(reverse=True)
        lines.append(f"<details>\n<summary>{y}</summary>\n")
        for m in months:
            month_dir = os.path.join(y, m)
            files = [f for f in os.listdir(month_dir) if f.lower().endswith(".md")]

            def sort_key(fn):
                base = fn[:-3]
                try:
                    d, mm, yy = base.split("-")
                    return datetime(int(yy), int(mm), int(d))
                except Exception:
                    return datetime.min

            files.sort(key=sort_key, reverse=True)
            lines.append(f"  <details>\n  <summary>{m}</summary>\n")
            if not files:
                lines.append("  _Empty_\n")
            else:
                for fn in files:
                    rel = f"{y}/{m}/{fn}"
                    title = fn[:-3]
                    lines.append(f"  - [{title}]({rel})")
            lines.append("\n  </details>\n")
        lines.append("</details>\n")
    return "\n".join(lines).rstrip() + "\n"


def build_daily_report_md(tz_name, label, stats, posts, rel_link):
    header = f"# Product Hunt — video launches for {label}\n"
    sub = f"_Timezone for “today”: `{tz_name}`. Source: Product Hunt API._\n\n"
    follow_me = (
        "[![Follow me on Product Hunt](https://img.shields.io/badge/"
        "Follow%20me%20on%20Product%20Hunt-@nbox-orange?style=for-the-badge)]"
        "(https://www.producthunt.com/@nbox)\n\n"
    )
    summary = [
        "## Summary\n",
        f"- Launches with video: **{stats.launches}**",
        f"- Total votes: **{stats.total_votes}**",
        f"- Avg / Median votes: **{stats.avg_votes:.2f} / {stats.median_votes:.2f}**",
        f"- Total comments: **{stats.total_comments}**",
        f"- Avg / Median comments: **{stats.avg_comments:.2f} / {stats.median_comments:.2f}**",
        f"- Report file: {safe_link(label, rel_link)}",
        "\n",
    ]
    launches = ["## Launches with video (sorted by votes)\n", render_posts_table(posts)]
    return header + follow_me + "\n".join(summary) + "\n" + "\n".join(launches)


def build_today_readme_block(tz_name, label, stats, posts, rel_link):
    lines = [
        f"### {label} ({tz_name})\n",
        f"- Launches with video: **{stats.launches}**",
        f"- Total votes: **{stats.total_votes}**",
        f"- Avg / Median votes: **{stats.avg_votes:.2f} / {stats.median_votes:.2f}**",
        f"- Total comments: **{stats.total_comments}**",
        f"- Avg / Median comments: **{stats.avg_comments:.2f} / {stats.median_comments:.2f}**",
        f"- Full report: {safe_link(label, rel_link)}\n",
        render_posts_table(posts),
    ]
    return "\n".join(lines).rstrip() + "\n"


def upsert_to_turso(posts: list[dict], date_str: str) -> None:
    """Insert/update records using the official libsql package.

    Reads credentials from env: TURSO_DB_URL, TURSO_AUTH_TOKEN.
    All rows are written in a single transaction to keep the run fast.
    """
    db_url = os.getenv("TURSO_DB_URL")
    auth_token = os.getenv("TURSO_AUTH_TOKEN")
    if not db_url or not auth_token:
        print(
            "Missing TURSO_DB_URL or TURSO_AUTH_TOKEN. Skipping database sync.",
            file=sys.stderr,
        )
        return

    conn = libsql.connect(
        database=db_url,
        auth_token=auth_token,
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_when_added TEXT NOT NULL,
            video_url TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            source_name TEXT DEFAULT '',
            source_link TEXT DEFAULT '',
            categories TEXT DEFAULT '',
            full_description TEXT DEFAULT '',
            votes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            UNIQUE (video_url, time_when_added)
        );
        """
    )
    conn.commit()

    upsert_sql = """
    INSERT INTO videos (
        time_when_added, video_url, title, description, source_name,
        source_link, categories, full_description, votes, comments
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(video_url, time_when_added) DO UPDATE SET
        votes = excluded.votes,
        comments = excluded.comments,
        categories = excluded.categories;
    """

    rows: list[tuple] = []
    for p in posts:
        video_url = p.get("video_url") or ""
        if not video_url:
            continue

        title = p.get("name") or ""
        description_short = p.get("tagline") or ""
        votes = int(p.get("votesCount") or 0)
        comments = int(p.get("commentsCount") or 0)
        # `p["url"]` is the Product Hunt product page.
        # `p["website"]` is the PH redirect to the product's own site.
        source_link = p.get("url") or ""
        categories_list = p.get("topics") or []
        categories_str = ", ".join(
            t.get("name", "") for t in categories_list if isinstance(t, dict)
        )

        rows.append(
            (
                date_str,
                video_url,
                title,
                description_short,
                "PH",
                source_link,
                categories_str,
                "",
                votes,
                comments,
            )
        )

    if not rows:
        conn.close()
        print("Upserted 0 records into Turso database.")
        return

    try:
        conn.executemany(upsert_sql, rows)
        conn.commit()
    except Exception as e:
        print(f"Batch upsert failed: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()

    print(f"Upserted {len(rows)} records into Turso database.")


def main() -> int:
    token = (os.getenv("PRODUCTHUNT_TOKEN") or "").strip()
    if not token:
        print("Missing env PRODUCTHUNT_TOKEN", file=sys.stderr)
        return 2

    tz_name = (os.getenv("PH_TZ") or "America/Los_Angeles").strip()
    start_local, end_local, label, year, month = get_target_day(tz_name)

    posts = fetch_posts_for_day(token, start_local, end_local)
    if not posts:
        print("No launches with video found for today. Skipping all updates.")
        return 0

    date_str = start_local.strftime("%Y-%m-%d")
    upsert_to_turso(posts, date_str)

    stats = compute_daily_stats(posts)
    daily_filename = f"{label}.md"
    daily_path = os.path.join(year, month, daily_filename)
    rel_link = f"{year}/{month}/{daily_filename}"
    daily_md = build_daily_report_md(tz_name, label, stats, posts, rel_link)
    write_text(daily_path, daily_md)
    print(f"Wrote/updated: {daily_path}")

    if not os.path.exists(README_PATH):
        print("README.md not found. Create it first.", file=sys.stderr)
        return 3

    with open(README_PATH, "r", encoding="utf-8") as f:
        readme = f.read()

    today_block = build_today_readme_block(tz_name, label, stats, posts, rel_link)
    archive_block = scan_archive_nav()
    readme = replace_block(readme, START_TODAY, END_TODAY, today_block)
    readme = replace_block(readme, START_ARCHIVE, END_ARCHIVE, archive_block)
    write_text(README_PATH, readme)
    print("README updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())