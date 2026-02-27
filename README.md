# LinkedIn People Scraper

Scrapes LinkedIn people search results using direct API calls (like Apify actors do). No browser automation needed — just your session cookies.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get your LinkedIn cookies

1. Open **Chrome** and go to [linkedin.com](https://www.linkedin.com)
2. Make sure you're **logged in**
3. Open **DevTools** → `F12` or `Cmd+Option+I`
4. Go to **Application** tab → **Cookies** → `https://www.linkedin.com`
5. Find and copy these two cookies:

   - **`li_at`** — your session token (long string)
   - **`JSESSIONID`** — CSRF token (looks like `"ajax:1234567890"` with quotes)

### 3. Configure `.env`

Edit the `.env` file and fill in your values:

```env
LI_AT=AQEDAQxxxxxx...your_li_at_value
JSESSIONID=ajax:1234567890123456789

WEBHOOK_URL=https://your-webhook-url.com/endpoint
GEO_URN=102713980
COMPANY_URLS=https://www.linkedin.com/company/google/,https://www.linkedin.com/company/meta/
TAGS=brand,marketing
MAX_PAGES=5
DELAY_BETWEEN_REQUESTS=2
```

### 4. Run

```bash
python scraper.py
```

## Output

- **Console**: Real-time progress and summary
- **JSON files**: `output_<company-slug>.json` saved in the project directory
- **Webhook**: Results POSTed to your configured webhook URL

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `LI_AT` | LinkedIn session cookie | required |
| `JSESSIONID` | LinkedIn CSRF cookie | required |
| `WEBHOOK_URL` | Webhook endpoint for results | optional |
| `GEO_URN` | LinkedIn geo filter (102713980 = India) | optional |
| `COMPANY_URLS` | Comma-separated company URLs | required |
| `TAGS` | Comma-separated search keywords | `brand` |
| `MAX_PAGES` | Max pages per tag (10 results/page) | `5` |
| `DELAY_BETWEEN_REQUESTS` | Seconds between API calls | `2` |

## Notes

- Cookies expire after some time. If you get auth errors, re-copy fresh cookies.
- Keep `DELAY_BETWEEN_REQUESTS` at 2+ seconds to avoid rate limiting.
- LinkedIn may temporarily block your account if you scrape too aggressively.
