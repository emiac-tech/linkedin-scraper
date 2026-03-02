# LinkedIn People Scraper & API

A powerful LinkedIn scraper that uses your session cookies to fetch people profiles directly from LinkedIn's Voyager API (no browser automation needed).

## Features

- **Direct API Access**: Uses `li_at` and `JSESSIONID` cookies to mimic legitimate requests.
- **Fast & Lightweight**: No Selenium/Puppeteer overhead.
- **API Server**: Built-in HTTP server to trigger scrapes remotely.
- **Unlimited Pagination**: Scrape all available results for a company/role.
- **Webhook Support**: Automatically send scraped data to a webhook (e.g., n8n, Zapier).

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get your LinkedIn cookies

1. Open **Chrome** and go to [linkedin.com](https://www.linkedin.com)
2. Make sure you're **logged in**
3. Open **DevTools** (`F12` or `Cmd+Option+I`)
4. Go to **Application** tab → **Cookies** → `https://www.linkedin.com`
5. Copy these two values:
   - **`li_at`**: Your session token.
   - **`JSESSIONID`**: Your CSRF token (remove quotes if present, e.g., `ajax:...`).

### 3. Configure `.env`

Create a `.env` file based on the example:

```env
LI_AT="AQEDATtq..."
JSESSIONID="ajax:1234..."
WEBHOOK_URL="https://your-webhook.com/endpoint"
GEO_URN="102713980"   # India (optional)
SEARCH_MODE="title"   # 'title' or 'keywords'
MAX_PAGES=0           # 0 for unlimited
DELAY_BETWEEN_REQUESTS=2
PORT=8000
```

## Usage

### Option 1: Run the API Server (Recommended)

Start the server:
```bash
python server.py
```

Send a scraping request:
```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "company_url": "https://www.linkedin.com/company/google",
    "tags": ["software engineer"],
    "max_pages": 0,
    "li_at": "YOUR_LI_AT_COOKIE",
    "jsessionid": "YOUR_JSESSIONID_COOKIE"
  }'
```

> **Note**: If you provide `li_at` and `jsessionid` in the request body, they will override the environment variables.

### Option 2: Run as CLI Script

Edit `.env` with your target `COMPANY_URLS` and `TAGS`, then run:
```bash
python scraper.py
```

## Deployment

### Docker

Build and run the container:

```bash
docker build -t linkedin-scraper .
docker run -p 8000:8000 --env-file .env linkedin-scraper
```

### Cloud (Render/Railway/Heroku)

1. **Push to GitHub** (see instructions below).
2. Connect your repo to a cloud provider.
3. Add your **Environment Variables** (`LI_AT`, `JSESSIONID`, etc.) in the cloud dashboard.
4. Deploy!
