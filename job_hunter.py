#!/usr/bin/env python3
"""
Job Hunter - Daily Internship Digest
Sends email digests at 10am and 10pm with new internship postings.
Sources: LinkedIn (via scraping), Indeed, Handshake, GitHub/Simplify repos
"""

import os
import json
import time
import hashlib
import smtplib
import logging
import requests
import schedule
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
RECIPIENT_EMAIL = "adityasunke04@gmail.com"
SENDER_EMAIL    = os.environ.get("SENDER_EMAIL", "adityasunke04@gmail.com")
SENDER_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # Gmail App Password

SEARCH_KEYWORDS = ["intern", "quantum software", "AI intern", "ML intern", "research intern"]
LOCATIONS       = ["United States", "India", "Remote"]

SEEN_JOBS_FILE  = Path(__file__).parent / "seen_jobs.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ─────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────
def load_seen_jobs() -> set:
    if SEEN_JOBS_FILE.exists():
        with open(SEEN_JOBS_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen_jobs(seen: set):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)

def job_id(title: str, company: str, url: str) -> str:
    raw = f"{title.lower().strip()}{company.lower().strip()}{url.strip()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ─────────────────────────────────────────────
# SOURCE 1: INDEED
# ─────────────────────────────────────────────
def fetch_indeed(keyword: str, location: str) -> list[dict]:
    jobs = []
    query = keyword.replace(" ", "+")
    loc   = location.replace(" ", "+")
    url   = f"https://www.indeed.com/jobs?q={query}&l={loc}&fromage=1&sort=date"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.job_seen_beacon")
        for card in cards[:10]:
            title_el   = card.select_one("h2.jobTitle span")
            company_el = card.select_one("span.companyName")
            loc_el     = card.select_one("div.companyLocation")
            link_el    = card.select_one("h2.jobTitle a")
            if not (title_el and company_el and link_el):
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True),
                "location": loc_el.get_text(strip=True) if loc_el else location,
                "url":      "https://www.indeed.com" + link_el.get("href", ""),
                "source":   "Indeed",
            })
    except Exception as e:
        log.warning(f"Indeed fetch failed ({keyword}, {location}): {e}")
    return jobs


# ─────────────────────────────────────────────
# SOURCE 2: LINKEDIN (public job search)
# ─────────────────────────────────────────────
def fetch_linkedin(keyword: str, location: str) -> list[dict]:
    jobs = []
    query = keyword.replace(" ", "%20")
    loc   = location.replace(" ", "%20")
    url   = (
        f"https://www.linkedin.com/jobs/search/?keywords={query}"
        f"&location={loc}&f_TPR=r86400&sortBy=DD"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.base-card")
        for card in cards[:10]:
            title_el   = card.select_one("h3.base-search-card__title")
            company_el = card.select_one("h4.base-search-card__subtitle")
            loc_el     = card.select_one("span.job-search-card__location")
            link_el    = card.select_one("a.base-card__full-link")
            if not (title_el and company_el and link_el):
                continue
            jobs.append({
                "title":    title_el.get_text(strip=True),
                "company":  company_el.get_text(strip=True),
                "location": loc_el.get_text(strip=True) if loc_el else location,
                "url":      link_el.get("href", "").split("?")[0],
                "source":   "LinkedIn",
            })
    except Exception as e:
        log.warning(f"LinkedIn fetch failed ({keyword}, {location}): {e}")
    return jobs


# ─────────────────────────────────────────────
# SOURCE 3: HANDSHAKE (public listings via search)
# ─────────────────────────────────────────────
def fetch_handshake(keyword: str) -> list[dict]:
    """
    Handshake requires login for full listings.
    We pull from their public-facing search page as best-effort.
    """
    jobs = []
    query = keyword.replace(" ", "%20")
    url   = f"https://app.joinhandshake.com/jobs?query={query}&job_type=intern"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Handshake renders via JS, so we get minimal static content.
        # We include the search URL as a convenience link instead.
        jobs.append({
            "title":    f'Handshake search: "{keyword}"',
            "company":  "Multiple companies",
            "location": "US / India / Remote",
            "url":      f"https://app.joinhandshake.com/jobs?query={query}&job_type=intern",
            "source":   "Handshake",
        })
    except Exception as e:
        log.warning(f"Handshake fetch failed ({keyword}): {e}")
    return jobs


# ─────────────────────────────────────────────
# SOURCE 4: SIMPLIFY / GITHUB REPOS
# ─────────────────────────────────────────────
SIMPLIFY_README_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md"
)

def fetch_simplify_github() -> list[dict]:
    """Parse Simplify's community-maintained internship table from GitHub README."""
    jobs = []
    try:
        resp = requests.get(SIMPLIFY_README_URL, timeout=15)
        lines = resp.text.splitlines()
        target_keywords = {"software", "quantum", "ml", "ai", "machine learning",
                           "research", "intern", "data"}
        for line in lines:
            if not line.startswith("|"):
                continue
            cols = [c.strip() for c in line.split("|") if c.strip()]
            if len(cols) < 4:
                continue
            # Typical columns: Company | Role | Location | Application Link | Date
            company  = cols[0]
            role     = cols[1] if len(cols) > 1 else ""
            location = cols[2] if len(cols) > 2 else ""
            link_col = cols[3] if len(cols) > 3 else ""

            # Filter by keyword relevance
            combined = (company + role).lower()
            if not any(kw in combined for kw in target_keywords):
                continue

            # Filter by location
            loc_lower = location.lower()
            if not any(l.lower() in loc_lower for l in ["us", "united states", "india", "remote", "new york", "san francisco", "seattle", "boston", "anywhere"]):
                if location not in ("", "—", "N/A"):
                    continue

            # Extract URL from markdown link syntax [text](url)
            url = ""
            if "](http" in link_col:
                start = link_col.find("](") + 2
                end   = link_col.find(")", start)
                url   = link_col[start:end]
            elif link_col.startswith("http"):
                url = link_col

            if not url or company in ("Company", "---"):
                continue

            jobs.append({
                "title":    role,
                "company":  company,
                "location": location,
                "url":      url,
                "source":   "Simplify/GitHub",
            })
    except Exception as e:
        log.warning(f"Simplify GitHub fetch failed: {e}")

    log.info(f"Simplify/GitHub: found {len(jobs)} matching listings")
    return jobs


# ─────────────────────────────────────────────
# AGGREGATOR
# ─────────────────────────────────────────────
def fetch_all_jobs() -> list[dict]:
    all_jobs = []

    # Simplify GitHub (best signal, no scraping issues)
    all_jobs += fetch_simplify_github()

    # Indeed + LinkedIn per keyword/location combo
    for keyword in SEARCH_KEYWORDS:
        for location in LOCATIONS:
            log.info(f"Fetching Indeed: '{keyword}' in '{location}'")
            all_jobs += fetch_indeed(keyword, location)
            time.sleep(1.5)  # polite delay

            log.info(f"Fetching LinkedIn: '{keyword}' in '{location}'")
            all_jobs += fetch_linkedin(keyword, location)
            time.sleep(1.5)

    # Handshake convenience links (one per keyword)
    for keyword in SEARCH_KEYWORDS:
        all_jobs += fetch_handshake(keyword)

    return all_jobs


def filter_new_jobs(all_jobs: list[dict], seen: set) -> tuple[list[dict], set]:
    new_jobs = []
    new_seen = set()
    for job in all_jobs:
        jid = job_id(job["title"], job["company"], job["url"])
        if jid not in seen:
            new_jobs.append(job)
            new_seen.add(jid)
    return new_jobs, new_seen


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
SOURCE_COLORS = {
    "Indeed":          "#003A9B",
    "LinkedIn":        "#0A66C2",
    "Handshake":       "#E8562A",
    "Simplify/GitHub": "#238636",
}

def build_email_html(jobs: list[dict], run_time: str) -> str:
    if not jobs:
        body_content = """
        <div class="empty">
            <div class="empty-icon">🔍</div>
            <p>No new internship postings found since the last check.</p>
            <p>Check back at the next digest!</p>
        </div>
        """
    else:
        cards_html = ""
        for job in jobs:
            color = SOURCE_COLORS.get(job["source"], "#555")
            cards_html += f"""
            <div class="job-card">
                <div class="job-header">
                    <div>
                        <div class="job-title">{job['title']}</div>
                        <div class="job-company">{job['company']}</div>
                    </div>
                    <span class="source-badge" style="background:{color}">{job['source']}</span>
                </div>
                <div class="job-meta">
                    <span>📍 {job['location']}</span>
                </div>
                <a href="{job['url']}" class="apply-btn">View & Apply →</a>
            </div>
            """
        body_content = cards_html

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Internship Digest</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap');

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #f0f2f5;
    font-family: 'DM Sans', sans-serif;
    color: #1a1a2e;
    padding: 32px 16px;
  }}
  .wrapper {{ max-width: 680px; margin: 0 auto; }}

  .header {{
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
    border-radius: 16px 16px 0 0;
    padding: 36px 40px;
    color: white;
  }}
  .header-label {{
    font-family: 'DM Mono', monospace;
    font-size: 11px;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: #a78bfa;
    margin-bottom: 10px;
  }}
  .header h1 {{ font-size: 28px; font-weight: 700; line-height: 1.2; }}
  .header h1 span {{ color: #a78bfa; }}
  .header-meta {{
    margin-top: 14px;
    font-size: 13px;
    color: #c4b5fd;
    display: flex;
    gap: 20px;
  }}
  .header-meta strong {{ color: white; }}

  .body {{
    background: white;
    padding: 32px 40px;
    border-left: 1px solid #e8eaf0;
    border-right: 1px solid #e8eaf0;
  }}

  .section-title {{
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #8b8fa8;
    margin-bottom: 20px;
    padding-bottom: 10px;
    border-bottom: 1px solid #f0f2f5;
  }}

  .job-card {{
    border: 1px solid #e8eaf0;
    border-radius: 10px;
    padding: 20px;
    margin-bottom: 14px;
    transition: box-shadow 0.2s;
  }}
  .job-card:hover {{ box-shadow: 0 4px 20px rgba(0,0,0,0.08); }}

  .job-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    margin-bottom: 10px;
  }}
  .job-title {{ font-size: 16px; font-weight: 600; color: #1a1a2e; margin-bottom: 3px; }}
  .job-company {{ font-size: 13px; color: #6366f1; font-weight: 500; }}
  .source-badge {{
    font-family: 'DM Mono', monospace;
    font-size: 10px;
    padding: 4px 8px;
    border-radius: 4px;
    color: white;
    white-space: nowrap;
    flex-shrink: 0;
  }}
  .job-meta {{ font-size: 13px; color: #6b7280; margin-bottom: 14px; }}

  .apply-btn {{
    display: inline-block;
    background: #6366f1;
    color: white;
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
    padding: 8px 16px;
    border-radius: 6px;
  }}

  .empty {{
    text-align: center;
    padding: 48px 24px;
    color: #8b8fa8;
  }}
  .empty-icon {{ font-size: 40px; margin-bottom: 14px; }}
  .empty p {{ font-size: 14px; line-height: 1.7; }}

  .footer {{
    background: #f8f9fc;
    border: 1px solid #e8eaf0;
    border-top: none;
    border-radius: 0 0 16px 16px;
    padding: 24px 40px;
    text-align: center;
    font-size: 12px;
    color: #9ca3af;
  }}
  .footer a {{ color: #6366f1; text-decoration: none; }}
</style>
</head>
<body>
<div class="wrapper">
  <div class="header">
    <div class="header-label">Internship Digest</div>
    <h1>Your <span>Daily</span> Internship Feed</h1>
    <div class="header-meta">
      <div>🕐 <strong>{run_time}</strong></div>
      <div>📬 <strong>{len(jobs)} new listing{'s' if len(jobs) != 1 else ''}</strong></div>
      <div>🌐 <strong>US · India · Remote</strong></div>
    </div>
  </div>
  <div class="body">
    <div class="section-title">New Postings Since Last Check</div>
    {body_content}
  </div>
  <div class="footer">
    Sources: Indeed · LinkedIn · Handshake · Simplify/GitHub<br/>
    Keywords: intern · quantum software · AI · ML · research intern<br/><br/>
    <a href="https://github.com/SimplifyJobs/Summer2026-Internships">Browse full Simplify list →</a>
  </div>
</div>
</body>
</html>"""


def send_email(jobs: list[dict]):
    run_time = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    subject  = f"🎯 Internship Digest — {len(jobs)} New Posting{'s' if len(jobs) != 1 else ''} [{datetime.now().strftime('%b %d')}]"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = RECIPIENT_EMAIL

    html = build_email_html(jobs, run_time)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
        log.info(f"✅ Email sent: {len(jobs)} new jobs at {run_time}")
    except Exception as e:
        log.error(f"❌ Email failed: {e}")


# ─────────────────────────────────────────────
# MAIN JOB
# ─────────────────────────────────────────────
def run_digest():
    log.info("=" * 50)
    log.info("Running internship digest...")
    seen      = load_seen_jobs()
    all_jobs  = fetch_all_jobs()
    new_jobs, new_seen = filter_new_jobs(all_jobs, seen)

    log.info(f"Total fetched: {len(all_jobs)} | New: {len(new_jobs)}")

    send_email(new_jobs)
    save_seen_jobs(seen | new_seen)
    log.info("Done.")


# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Job Hunter started. Scheduled at 10:00 AM and 10:00 PM daily.")
    schedule.every().day.at("10:00").do(run_digest)
    schedule.every().day.at("22:00").do(run_digest)

    # Uncomment to run once immediately on startup (useful for testing):
    # run_digest()

    while True:
        schedule.run_pending()
        time.sleep(30)
