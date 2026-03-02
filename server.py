#!/usr/bin/env python3
"""
LinkedIn People Scraper — HTTP API Server.

Endpoints:
    POST /scrape  — accepts company URL + tags, returns profiles
    GET  /health  — health check

Usage:
    python server.py
    curl -X POST http://localhost:8000/scrape \
      -H "Content-Type: application/json" \
      -d '{"company_url": "https://www.linkedin.com/company/bharatpe/", "tags": "brand,marketing"}'
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
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
SEARCH_MODE = os.getenv("SEARCH_MODE", "title").strip().lower()
MAX_PAGES = int(os.getenv("MAX_PAGES", "0"))
DELAY = float(os.getenv("DELAY_BETWEEN_REQUESTS", "2"))
PORT = int(os.getenv("PORT", "8000"))

API_BASE = "https://www.linkedin.com/voyager/api"

DECORATION_IDS = [
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-186",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-187",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-165",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-174",
    "com.linkedin.voyager.dash.deco.search.SearchClusterCollection-175",
]

GRAPHQL_QUERY_IDS = [
    "voyagerSearchDashClusters.b0928897b71bd00a5a7291755dcd64f0",
    "voyagerSearchDashClusters.21e82f0e3a53042e2ae85e46b9265195",
    "voyagerSearchDashClusters.13e85e43f6e4f51a8f35b2138f68b917",
]


# ─── LinkedIn Client (reused from scraper.py) ───────────────────────────────

class LinkedInClient:
    def __init__(self, li_at, jsessionid):
        if not li_at or not jsessionid:
            # Only fail if these are absolutely required and not present
            # But here we might be initializing the global client which might be empty if ENV vars are missing
            # and that's okay as long as we provide cookies in the request.
            pass
        
        self.session = requests.Session()
        clean_jsessionid = jsessionid.strip('"') if jsessionid else ""
        if li_at:
            self.session.cookies.set("li_at", li_at, domain=".linkedin.com")
        if clean_jsessionid:
            self.session.cookies.set("JSESSIONID", f'"{clean_jsessionid}"', domain=".linkedin.com")
            self.session.headers.update({"csrf-token": clean_jsessionid})
            
        self.session.headers.update({
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
        self._working_graphql_qid = None

    def get_company_id(self, company_url):
        slug = self._extract_slug(company_url)
        if not slug:
            return None, None, slug

        # Try API first
        url = f"{API_BASE}/organization/companies?q=universalName&universalName={slug}"
        resp = self._get(url)
        if resp:
            elements = resp.get("elements", [])
            if elements:
                c = elements[0]
                return c.get("entityUrn", "").split(":")[-1], c.get("name", slug), slug

        # Fallback: try the newer dash endpoint
        url2 = f"{API_BASE}/organization/dash/companies?q=universalName&universalName={slug}"
        resp2 = self._get(url2)
        if resp2:
            # Try extracting from included entities
            for entity in resp2.get("included", []):
                if isinstance(entity, dict) and entity.get("name"):
                    eid = entity.get("entityUrn", "").split(":")[-1]
                    return eid, entity["name"], slug

        # Fallback: HTML scrape
        try:
            resp = self.session.get(company_url, timeout=15)
            if resp.status_code == 200:
                html = resp.text
                patterns = [
                    r'"companyId":(\d+)',
                    r'"objectUrn":"urn:li:company:(\d+)"',
                    r'company:(\d+)',
                    r'/company/(\d+)',
                ]
                for pat in patterns:
                    m = re.search(pat, html)
                    if m:
                        name_m = re.search(r'"name":"([^"]+)"', html)
                        return m.group(1), name_m.group(1) if name_m else slug, slug
        except Exception:
            pass

        return None, None, slug

    def search_people(self, company_id, keywords="", geo_urn="", start=0, count=10):
        query = self._build_query(company_id, keywords, geo_urn)

        # Try GraphQL
        variables = f"(start:{start},origin:FACETED_SEARCH,query:{query},count:{count})"
        qids = ([self._working_graphql_qid] if self._working_graphql_qid else []) + GRAPHQL_QUERY_IDS
        for qid in qids:
            params = {"variables": variables, "queryId": qid}
            url = f"{API_BASE}/graphql?{urlencode(params, safe='():,')}"
            resp = self._get(url)
            if resp is not None:
                self._working_graphql_qid = qid
                return self._parse_response(resp, start, count)

        # Try REST
        for did in DECORATION_IDS:
            params = {
                "decorationId": did, "origin": "FACETED_SEARCH",
                "q": "all", "query": query, "start": start, "count": count,
            }
            url = f"{API_BASE}/search/dash/clusters?{urlencode(params, safe='():,')}"
            resp = self._get(url)
            if resp is not None:
                return self._parse_response(resp, start, count)

        return {"profiles": [], "has_more": False}

    def _build_query(self, company_id, keywords, geo_urn):
        qp = [
            f"(key:currentCompany,value:List({company_id}))",
            "(key:resultType,value:List(PEOPLE))",
        ]
        if geo_urn:
            qp.append(f"(key:geoUrn,value:List({geo_urn}))")
        if keywords and SEARCH_MODE == "title":
            qp.append(f"(key:title,value:List({keywords}))")
            return f"(flagshipSearchIntent:SEARCH_SRP,queryParameters:List({','.join(qp)}))"
        qp_str = ",".join(qp)
        if keywords:
            return f"(keywords:{keywords},flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp_str}))"
        return f"(flagshipSearchIntent:SEARCH_SRP,queryParameters:List({qp_str}))"

    def _parse_response(self, data, start, count):
        profiles = []
        included = data.get("included", [])
        for entity in included:
            if not isinstance(entity, dict):
                continue
            urn = entity.get("entityUrn", "")
            if "fsd_profile:" not in urn and "fs_miniProfile:" not in urn:
                continue
            p = self._extract_profile(entity)
            if p:
                profiles.append(p)

        paging = data.get("data", {}).get("paging", {})
        total = paging.get("total", 0) if paging else 0
        has_more = (start + count) < total if total else len(profiles) >= count

        return {"profiles": profiles, "has_more": has_more, "total": total}

    def _extract_profile(self, e):
        name = f"{e.get('firstName', '')} {e.get('lastName', '')}".strip()
        if not name:
            t = e.get("title", {})
            name = t.get("text", "") if isinstance(t, dict) else str(t) if t else ""

        headline = e.get("headline", "") or e.get("occupation", "")
        if not headline:
            s = e.get("subtitle", e.get("primarySubtitle", {}))
            headline = s.get("text", "") if isinstance(s, dict) else str(s) if s else ""

        location = e.get("location", "") or e.get("geoLocationName", "")
        if not location:
            s = e.get("secondarySubtitle", e.get("summary", {}))
            location = s.get("text", "") if isinstance(s, dict) else str(s) if s else ""

        pub_id = e.get("publicIdentifier", "") or e.get("publicId", "")
        profile_url = f"https://www.linkedin.com/in/{pub_id}/" if pub_id else ""
        if not profile_url:
            nav = e.get("navigationUrl", "") or e.get("navigationContext", {}).get("url", "")
            if "/in/" in nav:
                profile_url = nav.split("?")[0]

        if not name:
            return None
        return {
            "name": name,
            "designation": headline,
            "location": location,
            "profileUrl": profile_url,
        }

    def _get(self, url):
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(60)
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
            return None
        except Exception:
            return None

    @staticmethod
    def _extract_slug(url):
        m = re.search(r"/company/([^/?#]+)", url)
        return m.group(1).rstrip("/") if m else ""


# ─── Scrape Logic ────────────────────────────────────────────────────────────

client = LinkedInClient(LI_AT, JSESSIONID)


def scrape(company_url, tags, geo_urn="", search_mode=None, max_pages=None, li_at=None, jsessionid=None):
    global SEARCH_MODE
    original_mode = SEARCH_MODE
    if search_mode:
        SEARCH_MODE = search_mode

    # Use custom client if cookies are provided, otherwise use global client
    active_client = client
    if li_at and jsessionid:
        active_client = LinkedInClient(li_at, jsessionid)
    
    _max = int(max_pages) if max_pages is not None else MAX_PAGES

    company_id, company_name, slug = active_client.get_company_id(company_url)
    if not company_id:
        SEARCH_MODE = original_mode
        return {"ok": False, "error": f"Could not resolve company: {company_url}"}

    all_profiles = []

    for tag in tags:
        start = 0
        page = 0
        while True:
            # Check for max pages limit if set (0 or less means unlimited)
            if _max > 0 and page >= _max:
                break
                
            result = active_client.search_people(
                company_id=company_id,
                keywords=tag,
                geo_urn=geo_urn or GEO_URN,
                start=start,
                count=10,
            )
            page_profiles = result.get("profiles", [])
            if not page_profiles:
                break
            for p in page_profiles:
                p["tag"] = tag
            all_profiles.extend(page_profiles)
            if not result.get("has_more"):
                break
            page += 1
            start += 10
            time.sleep(DELAY)

    # Deduplicate
    seen = set()
    unique = []
    for p in all_profiles:
        key = p.get("profileUrl") or p.get("name", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(p)

    SEARCH_MODE = original_mode

    payload = {
        "ok": True,
        "extractedAt": datetime.now(timezone.utc).isoformat(),
        "company": {
            "name": company_name,
            "id": company_id,
            "linkedinUrl": company_url,
        },
        "searchMode": search_mode or SEARCH_MODE,
        "tags": tags,
        "profileCount": len(unique),
        "profiles": unique,
    }

    # Also send to webhook if configured
    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json=payload, timeout=30)
        except Exception:
            pass

    return payload


# ─── HTTP Server ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok", "cookies_set": bool(LI_AT)})
        else:
            self._json_response(404, {"error": "Not found. Use POST /scrape"})

    def do_POST(self):
        if self.path != "/scrape":
            self._json_response(404, {"error": "Not found. Use POST /scrape"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, ValueError):
            self._json_response(400, {"error": "Invalid JSON body"})
            return

        company_url = body.get("company_url", "").strip()
        if not company_url:
            self._json_response(400, {"error": "company_url is required"})
            return

        tags_raw = body.get("tags", "")
        if isinstance(tags_raw, list):
            tags = [t.strip() for t in tags_raw if t.strip()]
        else:
            tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

        if not tags:
            self._json_response(400, {"error": "tags is required (comma-separated string or array)"})
            return

        geo_urn = body.get("geo_urn", "")
        search_mode = body.get("search_mode", "")
        max_pages = body.get("max_pages", None)
        li_at = body.get("li_at", "")
        jsessionid = body.get("jsessionid", "")

        result = scrape(company_url, tags, geo_urn, search_mode, max_pages, li_at, jsessionid)
        status = 200 if result.get("ok") else 500
        self._json_response(status, result)

    def _json_response(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_OPTIONS(self):
        self._json_response(204, "")

    def log_message(self, format, *args):
        print(f"  [{time.strftime('%H:%M:%S')}] {args[0]}")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # if not LI_AT or LI_AT == "your_li_at_cookie_here":
    #     print("ERROR: Set LI_AT in .env")
    #     exit(1)

    print(f"LinkedIn Scraper API running on http://0.0.0.0:{PORT}")
    print(f"  Search mode: {SEARCH_MODE}")
    print(f"  Max pages:   {MAX_PAGES}")
    print(f"  Webhook:     {'✓' if WEBHOOK_URL else '✗'}")
    print(f"\nExample:")
    print(f'  curl -X POST http://localhost:{PORT}/scrape \\')
    print(f'    -H "Content-Type: application/json" \\')
    print(f'    -d \'{{"company_url": "https://www.linkedin.com/company/bharatpe/", "tags": "brand"}}\'')
    print()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
