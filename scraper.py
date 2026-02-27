#!/usr/bin/env python3
"""
LinkedIn People Scraper — Direct Voyager API approach.

Uses your LinkedIn session cookies (li_at + JSESSIONID) to call LinkedIn's
internal API directly, just like Apify actors do.  No browser needed.

Usage:
    1. Copy .env.example → .env and fill in your cookies
    2. pip install -r requirements.txt
    3. python scraper.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

LI_AT = os.getenv("LI_AT", "")
JSESSIONID = os.getenv("JSESSIONID", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
GEO_URN = os.getenv("GEO_URN", "")
COMPANY_URLS = [
    u.strip()
    for u in os.getenv("COMPANY_URLS", "").split(",")
    if u.strip()
]
TAGS = [
    t.strip()
    for t in os.getenv("TAGS", "brand").split(",")
    if t.strip()
]
SEARCH_MODE = os.getenv("SEARCH_MODE", "title").strip().lower()  # 'title' or 'keywords'
MAX_PAGES = int(os.getenv("MAX_PAGES", "5"))
DELAY = float(os.getenv("DELAY_BETWEEN_REQUESTS", "2"))

# LinkedIn Voyager API base
API_BASE = "https://www.linkedin.com/voyager/api"

# Decoration IDs to try (LinkedIn rotates these periodically)
DECORATION_IDS = [
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-186",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-187",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-165",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-174",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-175",
]

# Known GraphQL query IDs for people search (LinkedIn changes these)
GRAPHQL_QUERY_IDS = [
    "voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0",
    "voyagerSearchDashClusters.21e82f0e3a53042e2ae85e46b9265195",
    "voyagerSearchDashClusters.13e85e43f6e4f51a8f35b2138f68b917",
]


# ─── LinkedIn API Client ────────────────────────────────────────────────────

class LinkedInClient:
    """Thin wrapper around LinkedIn's Voyager REST API."""

    def __init__(self, li_at: str, jsessionid: str):
        if not li_at or not jsessionid:
            sys.exit(
                "ERROR: LI_AT and JSESSIONID must be set in .env\n"
                "See README.md for how to get these cookies."
            )

        self.session = requests.Session()

        # Strip surrounding quotes if present (JSESSIONID often has them)
        clean_jsessionid = jsessionid.strip('"')

        self.session.cookies.set("li_at", li_at, domain=".linkedin.com")
        self.session.cookies.set("JSESSIONID", f'"{clean_jsessionid}"', domain=".linkedin.com")

        self.session.headers.update({
            "csrf-token": clean_jsessionid,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/vnd.linkedin.normalized+json+2.1",
            "Accept-Language": "en-US,en;q=0.9",
            "x-li-lang": "en_US",
            "x-li-page-instance": "urn:li:page:d_flagship3_search_srp_people;",
            "x-restli-protocol-version": "2.0.0",
        })

        self._working_decoration_id = None

    # ── Company ID resolution ────────────────────────────────────────────

    def get_company_id(self, company_url: str) -> dict:
        """Resolve a company LinkedIn URL to its numeric ID and name."""
        slug = self._extract_slug(company_url)
        if not slug:
            return {"ok": False, "error": f"Cannot extract slug from: {company_url}"}

        url = f"{API_BASE}/organization/companies?q=universalName&universalName={slug}"
        log(f"Resolving company: {slug}")

        resp = self._get(url)
        if resp is None:
            return {"ok": False, "error": f"API request failed for company: {slug}"}

        # Parse response
        elements = resp.get("elements", [])
        if elements:
            company = elements[0]
            company_id = company.get("entityUrn", "").split(":")[-1]
            name = company.get("name", slug)
            return {"ok": True, "id": company_id, "name": name, "slug": slug}

        # Fallback: try the newer dash endpoint
        url2 = f"{API_BASE}/organization/dash/companies?q=universalName&universalName={slug}"
        resp2 = self._get(url2)
        if resp2:
            for key, val in resp2.get("included", [{}])[0].items() if isinstance(resp2.get("included"), list) and resp2["included"] else []:
                pass
            # Try extracting from included entities
            for entity in resp2.get("included", []):
                if isinstance(entity, dict) and entity.get("name"):
                    eid = entity.get("entityUrn", "").split(":")[-1]
                    return {"ok": True, "id": eid, "name": entity["name"], "slug": slug}

        # Last resort: scrape the company page HTML for the ID
        return self._get_company_id_from_html(company_url, slug)

    def _get_company_id_from_html(self, company_url: str, slug: str) -> dict:
        """Fallback: load company page HTML and extract ID from embedded data."""
        log(f"Falling back to HTML scrape for company ID: {slug}")
        try:
            resp = self.session.get(company_url, timeout=15)
            if resp.status_code != 200:
                return {"ok": False, "error": f"HTTP {resp.status_code} loading company page"}

            html = resp.text

            # Look for companyId in various patterns
            patterns = [
                r'"companyId":(\d+)',
                r'"objectUrn":"urn:li:company:(\d+)"',
                r'company:(\d+)',
                r'/company/(\d+)',
            ]
            for pat in patterns:
                m = re.search(pat, html)
                if m:
                    cid = m.group(1)
                    log(f"Found company ID from HTML: {cid}")
                    # Try to get name from HTML too
                    name_match = re.search(r'"name":"([^"]+)"', html)
                    name = name_match.group(1) if name_match else slug
                    return {"ok": True, "id": cid, "name": name, "slug": slug}

            return {"ok": False, "error": f"Could not find company ID in page HTML for: {slug}"}
        except Exception as e:
            return {"ok": False, "error": f"HTML fallback failed: {e}"}

    # ── People search ────────────────────────────────────────────────────

    def _build_query_string(self, company_id, keywords, geo_urn):
        """Build the LinkedIn API query parameter string."""
        qp_parts = [
            f"(key:currentCompany,value:List({company_id}))",
            "(key:resultType,value:List(PEOPLE))",
        ]
        if geo_urn:
            qp_parts.append(f"(key:geoUrn,value:List({geo_urn}))")

        # 'title' mode = strict job title filter (matches LinkedIn UI)
        # 'keywords' mode = broad search across entire profile
        if keywords and SEARCH_MODE == "title":
            qp_parts.append(f"(key:title,value:List({keywords}))")
            qp_str = ",".join(qp_parts)
            return f"(flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp_str}))"

        qp_str = ",".join(qp_parts)
        if keywords:
            return f"(keywords:{keywords},flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp_str}))"
        return f"(flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp_str}))"

    def search_people(
        self,
        company_id: str,
        keywords: str = "",
        geo_urn: str = "",
        start: int = 0,
        count: int = 10,
    ) -> dict:
        """Search for people at a company. Tries multiple API strategies."""
        log(f"Search: keywords='{keywords}' start={start} count={count}")

        query = self._build_query_string(company_id, keywords, geo_urn)

        # Strategy 1: GraphQL endpoint (what modern LinkedIn frontend uses)
        result = self._try_graphql_search(query, start, count)
        if result:
            return self._parse_search_response(result, start, count)

        # Strategy 2: REST endpoint with decoration IDs
        result = self._try_rest_search(query, start, count)
        if result:
            return self._parse_search_response(result, start, count)

        # Strategy 3: Older blended search endpoint
        result = self._try_blended_search(company_id, keywords, geo_urn, start, count)
        if result:
            return self._parse_search_response(result, start, count)

        # Strategy 4: HTML scrape of search results page
        log("All API strategies failed — falling back to HTML scrape...")
        return self._scrape_search_html(company_id, keywords, geo_urn, start, count)

    def _try_graphql_search(self, query, start, count):
        """Try LinkedIn's GraphQL search endpoint."""
        variables = f"(start:{start},origin:FACETED_SEARCH,query:{query},count:{count})"

        for qid in GRAPHQL_QUERY_IDS:
            params = {"variables": variables, "queryId": qid}
            url = f"{API_BASE}/graphql?{urlencode(params, safe='():,')}"
            log(f"  Trying GraphQL: queryId=...{qid[-12:]}")
            resp = self._get(url)
            if resp is not None:
                log(f"  ✓ GraphQL worked with queryId: {qid}")
                return resp
        return None

    def _try_rest_search(self, query, start, count):
        """Try the REST search/dash/clusters endpoint with various decoration IDs."""
        for did in DECORATION_IDS:
            params = {
                "decorationId": did,
                "origin": "FACETED_SEARCH",
                "q": "all",
                "query": query,
                "start": start,
                "count": count,
            }
            url = f"{API_BASE}/search/dash/clusters?{urlencode(params, safe='():,')}"
            log(f"  Trying REST: decorationId=...{did[-4:]}")
            resp = self._get(url)
            if resp is not None:
                log(f"  ✓ REST worked with: {did}")
                return resp

        # Also try without decoration
        params = {
            "origin": "FACETED_SEARCH",
            "q": "all",
            "query": query,
            "start": start,
            "count": count,
        }
        url = f"{API_BASE}/search/dash/clusters?{urlencode(params, safe='():,')}"
        log("  Trying REST without decorationId...")
        resp = self._get(url)
        if resp is not None:
            return resp
        return None

    def _try_blended_search(self, company_id, keywords, geo_urn, start, count):
        """Try the older /search/blended endpoint."""
        params = {
            "origin": "FACETED_SEARCH",
            "q": "all",
            "count": count,
            "start": start,
            "filters": f"List(currentCompany->{company_id},resultType->PEOPLE)",
        }
        if keywords:
            params["keywords"] = keywords
        if geo_urn:
            params["filters"] += f",geoUrn->{geo_urn}"

        url = f"{API_BASE}/search/blended?{urlencode(params, safe='():,->'+quote(','))}"
        log("  Trying blended search endpoint...")
        resp = self._get(url)
        if resp is not None:
            log("  ✓ Blended search worked")
            return resp
        return None

    def _scrape_search_html(self, company_id, keywords, geo_urn, start, count):
        """Fallback: load the actual search results HTML page and parse profiles from it."""
        page_num = (start // 10) + 1
        params = {
            "currentCompany": f'["{company_id}"]',
            "origin": "FACETED_SEARCH",
        }
        if keywords:
            params["keywords"] = keywords
        if geo_urn:
            params["geoUrn"] = f'["{geo_urn}"]'
        if page_num > 1:
            params["page"] = str(page_num)

        url = f"https://www.linkedin.com/search/results/people/?{urlencode(params)}"
        log(f"  Scraping HTML: {url[:80]}...")

        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                return {"ok": False, "profiles": [], "error": f"HTML scrape HTTP {resp.status_code}"}

            html = resp.text
            profiles = []

            # Extract profiles from embedded JSON in <code> tags
            # LinkedIn embeds search data as JSON inside <code> elements
            import json as json_mod
            code_blocks = re.findall(r'<code[^>]*>(.*?)</code>', html, re.DOTALL)
            for block in code_blocks:
                try:
                    data = json_mod.loads(block)
                    if isinstance(data, dict) and "included" in data:
                        return self._parse_search_response(data, start, count)
                except (json.JSONDecodeError, ValueError):
                    continue

            # Fallback: extract /in/ profile links and names from HTML
            profile_links = re.findall(
                r'href="(https://www\.linkedin\.com/in/[^"?]+)"[^>]*>([^<]+)',
                html
            )
            seen_urls = set()
            for link_url, link_text in profile_links:
                clean_url = link_url.split("?")[0]
                if clean_url.endswith("/"):
                    clean_url = clean_url
                else:
                    clean_url += "/"
                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)
                name = link_text.strip()
                # Skip non-name text
                if len(name) < 2 or len(name) > 60 or name.startswith("View"):
                    continue
                # Split on "•" to remove connection degree
                name = name.split("\u2022")[0].strip()
                if name:
                    profiles.append({
                        "name": name,
                        "designation": "",
                        "location": "",
                        "profileUrl": clean_url,
                    })

            has_more = len(profiles) >= count
            return {
                "ok": True,
                "profiles": profiles,
                "total": 0,
                "has_more": has_more,
                "count_on_page": len(profiles),
            }
        except Exception as e:
            return {"ok": False, "profiles": [], "error": f"HTML scrape failed: {e}"}

    def _parse_search_response(self, data: dict, start: int, count: int) -> dict:
        """Extract profile info from Voyager search API response."""
        profiles = []

        # The response has "included" array with all entity data
        # and "data" with the cluster structure
        included = {
            item.get("entityUrn", ""): item
            for item in data.get("included", [])
            if isinstance(item, dict)
        }

        # Find profile entities
        for urn, entity in included.items():
            if "urn:li:fsd_profile:" not in urn and "urn:li:fs_miniProfile:" not in urn:
                continue

            profile = self._extract_profile_from_entity(entity, included)
            if profile and profile.get("name"):
                profiles.append(profile)

        # Determine pagination
        total = 0
        paging = data.get("data", {}).get("paging", {})
        if paging:
            total = paging.get("total", 0)
        else:
            # Try to find paging in nested structure
            for key in ("paging", "metadata"):
                if key in data:
                    total = data[key].get("total", data[key].get("totalResultCount", 0))
                    break

        has_more = (start + count) < total if total else len(profiles) >= count

        return {
            "ok": True,
            "profiles": profiles,
            "total": total,
            "has_more": has_more,
            "count_on_page": len(profiles),
        }

    def _extract_profile_from_entity(self, entity: dict, included: dict) -> dict:
        """Pull name, headline, location, profile URL from an entity."""
        name = ""
        headline = ""
        location = ""
        profile_url = ""
        public_id = ""

        # Name
        first = entity.get("firstName", "")
        last = entity.get("lastName", "")
        if first or last:
            name = f"{first} {last}".strip()

        # Headline / occupation
        headline = entity.get("headline", "") or entity.get("occupation", "")

        # Location
        location = entity.get("location", "") or entity.get("geoLocationName", "")

        # Public identifier → profile URL
        public_id = entity.get("publicIdentifier", "") or entity.get("publicId", "")
        if public_id:
            profile_url = f"https://www.linkedin.com/in/{public_id}/"

        # If name is empty, try title/text fields (varies by response format)
        if not name:
            title = entity.get("title", {})
            if isinstance(title, dict):
                name = title.get("text", "")
            elif isinstance(title, str):
                name = title

        # If headline is empty, try subtitle
        if not headline:
            subtitle = entity.get("subtitle", entity.get("primarySubtitle", {}))
            if isinstance(subtitle, dict):
                headline = subtitle.get("text", "")
            elif isinstance(subtitle, str):
                headline = subtitle

        # If location is empty, try secondary subtitle
        if not location:
            sec = entity.get("secondarySubtitle", entity.get("summary", {}))
            if isinstance(sec, dict):
                location = sec.get("text", "")
            elif isinstance(sec, str):
                location = sec

        # Try to get profile URL from navigation context
        if not profile_url:
            nav = entity.get("navigationUrl", "") or entity.get("navigationContext", {}).get("url", "")
            if "/in/" in nav:
                profile_url = nav.split("?")[0]
                if not profile_url.endswith("/"):
                    profile_url += "/"

        if not name:
            return None

        return {
            "name": name,
            "designation": headline,
            "location": location,
            "profileUrl": profile_url,
        }

    # ── HTTP helpers ─────────────────────────────────────────────────────

    def _get(self, url: str) -> Optional[dict]:
        """Make a GET request, return JSON or None on failure."""
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                log(f"⚠ Rate limited (429). Waiting 60s...")
                time.sleep(60)
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
            log(f"✗ HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            log(f"✗ Request error: {e}")
            return None

    @staticmethod
    def _extract_slug(url: str) -> str:
        """Extract company slug from a LinkedIn URL."""
        m = re.search(r"/company/([^/?#]+)", url)
        return m.group(1).rstrip("/") if m else ""


# ─── Webhook ─────────────────────────────────────────────────────────────────

def send_to_webhook(webhook_url: str, payload: dict) -> bool:
    """POST payload to webhook. Returns True on success."""
    if not webhook_url:
        log("No webhook URL configured — skipping send.")
        return False

    log(f"Sending {payload.get('profileCount', '?')} profiles to webhook...")
    try:
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        log(f"Webhook response: HTTP {resp.status_code}")
        return resp.status_code < 400
    except Exception as e:
        log(f"✗ Webhook error: {e}")
        return False


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("LinkedIn People Scraper")
    print("=" * 60)

    if not COMPANY_URLS:
        sys.exit("ERROR: Set COMPANY_URLS in .env")
    if not LI_AT or LI_AT == "your_li_at_cookie_here":
        sys.exit("ERROR: Set your LI_AT cookie in .env")

    client = LinkedInClient(LI_AT, JSESSIONID)

    for company_url in COMPANY_URLS:
        print(f"\n{'─' * 60}")
        print(f"Company: {company_url}")
        print(f"{'─' * 60}")

        # 1. Resolve company ID
        cdata = client.get_company_id(company_url)
        if not cdata["ok"]:
            log(f"✗ {cdata['error']}")
            continue

        company_id = cdata["id"]
        company_name = cdata["name"]
        log(f"✓ {company_name} (ID: {company_id})")

        all_profiles = []

        # 2. Search for each tag
        for tag in TAGS:
            log(f"\nSearching tag: \"{tag}\"")
            start = 0
            page = 0

            while page < MAX_PAGES:
                result = client.search_people(
                    company_id=company_id,
                    keywords=tag,
                    geo_urn=GEO_URN,
                    start=start,
                    count=10,
                )

                if not result["ok"]:
                    log(f"  ✗ {result.get('error', 'Unknown error')}")
                    break

                page_profiles = result["profiles"]
                log(f"  Page {page + 1}: {len(page_profiles)} profiles (total available: {result.get('total', '?')})")

                if not page_profiles:
                    break

                for p in page_profiles:
                    p["tag"] = tag
                    p["companyName"] = company_name
                    p["companyUrl"] = company_url
                    all_profiles.append(p)

                if not result["has_more"]:
                    log("  No more pages.")
                    break

                page += 1
                start += 10
                time.sleep(DELAY)

        # 3. Deduplicate by profileUrl
        seen = set()
        unique_profiles = []
        for p in all_profiles:
            key = p.get("profileUrl") or p.get("name", "")
            if key and key not in seen:
                seen.add(key)
                unique_profiles.append(p)

        log(f"\n✓ Total: {len(unique_profiles)} unique profiles for {company_name}")

        # 4. Build payload and send
        payload = {
            "type": "linkedin_people_extract",
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "company": {
                "name": company_name,
                "id": company_id,
                "linkedinUrl": company_url,
            },
            "tags": TAGS,
            "profileCount": len(unique_profiles),
            "profiles": unique_profiles,
        }

        # Save locally as JSON backup
        outfile = f"output_{cdata['slug']}.json"
        with open(os.path.join(os.path.dirname(__file__), outfile), "w") as f:
            json.dump(payload, f, indent=2)
        log(f"Saved to {outfile}")

        # Send to webhook
        if WEBHOOK_URL:
            send_to_webhook(WEBHOOK_URL, payload)

        # Print summary
        print(f"\n{'─' * 40}")
        print(f"  Company : {company_name}")
        print(f"  Profiles: {len(unique_profiles)}")
        print(f"  Tags    : {', '.join(TAGS)}")
        print(f"{'─' * 40}")
        for i, p in enumerate(unique_profiles[:10], 1):
            print(f"  {i}. {p['name']} — {p.get('designation', 'N/A')}")
        if len(unique_profiles) > 10:
            print(f"  ... and {len(unique_profiles) - 10} more")

    print(f"\n{'=' * 60}")
    print("Done!")
    print(f"{'=' * 60}")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}")


if __name__ == "__main__":
    main()
