"""
Auto-post Hindi news articles to thenewswala.com (WordPress).

Flow (one run):
  1. Check WordPress login (fails fast before spending any AI credits).
  2. Collect fresh headlines from Google News Hindi RSS (Deoria / Gorakhpur /
     Kushinagar / UP searches + top headlines).
  3. Claude picks N stories the site has not covered yet (local Deoria first).
  4. For each story Claude researches it with web search and writes an
     original Hindi article in the site's house style.
  5. The main image (og:image) of the source news article is downloaded,
     uploaded to the WordPress media library and set as the featured image.
  6. The article is published through the WordPress REST API.

Configuration comes from environment variables (see README.md).
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

import feedparser
import requests


def load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    """Read KEY=VALUE lines from .env (local testing). Real environment variables win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip().strip('"').strip("'")
                if value and not os.getenv(key.strip()):
                    os.environ[key.strip()] = value


load_dotenv()

# ---------------------------------------------------------------- settings
WP_URL = os.getenv("WP_URL", "https://thenewswala.com").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_AUTH = (WP_USER, WP_APP_PASSWORD) if WP_USER and WP_APP_PASSWORD else None

POSTS_PER_RUN = int(os.getenv("POSTS_PER_RUN") or 1)

# Daily posts per section. Override with e.g. TOPIC_QUOTAS="national=12,education=5,state=7,sports=3,entertainment=3"
TOPICS = {
    "national": {
        "label": "national (देश)",
        "category": "देश",
        "queries": ["देश", "केंद्र सरकार", "भारत"],
        "top_headlines": True,
        "brief": "Major national news of India: central government and ministries, Parliament, Supreme Court and big court rulings, "
                 "national politics, big accidents or disasters, weather alerts, economy and prices that affect common people. "
                 "Not sports, entertainment or exam/education news, and not small local crime.",
    },
    "education": {
        "label": "education (शिक्षा)",
        "category": "शिक्षा",
        "queries": ["शिक्षा", "परीक्षा रिजल्ट", "यूपी बोर्ड", "CBSE", "भर्ती परीक्षा"],
        "brief": "Education and careers: board exams (UP Board, CBSE), results, admit cards, exam dates, admissions, universities, "
                 "scholarships, recruitment exams and government job notifications. Prefer news useful to students in UP and Bihar.",
    },
    "state": {
        "label": "state / Uttar Pradesh (उत्तर प्रदेश)",
        "category": "उत्तर प्रदेश",
        "queries": ["देवरिया", "गोरखपुर", "कुशीनगर", "उत्तर प्रदेश", "लखनऊ"],
        "brief": "Uttar Pradesh news. Prefer Deoria district first, then Gorakhpur, Kushinagar and Purvanchal, then the rest of UP: "
                 "state government decisions, CM Yogi, police and administration, crime, accidents, development projects.",
    },
    "sports": {
        "label": "sports (स्पोर्ट्स)",
        "category": "स्पोर्ट्स",
        "queries": ["क्रिकेट", "खेल"],
        "brief": "Sports news, mainly cricket (Team India, IPL, results, squads) and other big sports with Indian interest. "
                 "Prefer match results and confirmed news over previews, fantasy tips and predictions.",
    },
    "entertainment": {
        "label": "entertainment (मनोरंजन)",
        "category": "मनोरंजन",
        "queries": ["बॉलीवुड", "मनोरंजन"],
        "brief": "Entertainment: Bollywood and Bhojpuri films, box office, OTT releases, TV and music. "
                 "Prefer confirmed news (releases, box office, announcements) over gossip, rumours and personal-life speculation.",
    },
}
for item in (os.getenv("TOPIC_QUOTAS") or "national=12,education=5,state=7,sports=3,entertainment=3").split(","):
    key, _, num = item.partition("=")
    if key.strip() in TOPICS:
        TOPICS[key.strip()]["quota"] = int(num or 0)
MAX_POSTS_PER_DAY = sum(t.get("quota", 0) for t in TOPICS.values())  # 30, counted per IST calendar day
POST_STATUS = os.getenv("POST_STATUS") or "publish"  # publish | draft | pending

# AI provider: "gemini", "openai" or "claude". Auto-detected from whichever API key is set.
AI_PROVIDER = (
    os.getenv("AI_PROVIDER")
    or ("gemini" if os.getenv("GEMINI_API_KEY") else "openai" if os.getenv("OPENAI_API_KEY") else "claude")
).lower()
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.5-flash"
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-5.5"
MODEL = os.getenv("CLAUDE_MODEL") or "claude-opus-5-5"
EFFORT = os.getenv("CLAUDE_EFFORT") or "medium"  # low | medium | high
DRY_RUN = (os.getenv("DRY_RUN") or "false").lower() == "true"
REQUIRE_IMAGE = (os.getenv("REQUIRE_IMAGE") or "false").lower() == "true"

# Category IDs added to every post (site convention: Newsbeat=598, Recent=6).
ALWAYS_CATEGORY_IDS = [
    int(x) for x in (os.getenv("ALWAYS_CATEGORY_IDS") or "598,6").split(",") if x.strip()
]
# Categories Claude must never choose.
EXCLUDED_CATEGORIES = {
    "uncategorized", "live tv", "covid19", "recent", "newsbeat", "stories", "कहानी / कविता",
}
MAX_HEADLINE_AGE_HOURS = 24
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "posted.json")

GN = "hl=hi&gl=IN&ceid=IN:hi"


def topic_feeds(topic):
    feeds = [f"https://news.google.com/rss/search?q={quote(q + ' when:1d')}&{GN}" for q in topic["queries"]]
    if topic.get("top_headlines"):
        feeds.insert(0, f"https://news.google.com/rss?{GN}")
    return feeds
SKIP_SOURCES = {"facebook", "youtube", "instagram", "x", "twitter", "dailyhunt", "the newswala"}

IST = timezone(timedelta(hours=5, minutes=30))

# Models that accept server-side refusal fallbacks.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1"}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HTTP = requests.Session()
HTTP.headers["User-Agent"] = BROWSER_UA

AI_ERRORS = ()
if AI_PROVIDER == "gemini":
    from google import genai
    from google.genai import errors as gerrors
    from google.genai import types as gtypes

    gemini_client = genai.Client()  # reads GEMINI_API_KEY
    AI_ERRORS = (gerrors.APIError,)
elif AI_PROVIDER == "openai":
    import openai

    openai_client = openai.OpenAI()  # reads OPENAI_API_KEY
    AI_ERRORS = (openai.OpenAIError,)
else:
    import anthropic

    claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    AI_ERRORS = (anthropic.APIError,)


def log(*args):
    print(*args, flush=True)


# --------------------------------------------------------------- WordPress
def wp_raw(method, route, **kw):
    url = f"{WP_URL}/index.php?rest_route={route}"
    return HTTP.request(method, url, auth=WP_AUTH, timeout=90, **kw)


def wp(method, route, **kw):
    r = wp_raw(method, route, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"WordPress {method} {route} -> HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


def wp_check_login():
    me = wp("GET", "/wp/v2/users/me", params={"context": "edit", "_fields": "id,name,roles"})
    log(f"WordPress login OK: {me.get('name')} roles={me.get('roles')}")


def wp_categories():
    cats = wp("GET", "/wp/v2/categories", params={"per_page": 100, "_fields": "id,name"})
    return {html.unescape(c["name"]).strip(): c["id"] for c in cats}


def wp_recent_titles(n=60):
    params = {"per_page": n, "_fields": "title"}
    if WP_AUTH:
        params["status"] = "publish,draft,pending,future"
    posts = wp("GET", "/wp/v2/posts", params=params)
    return [html.unescape(p["title"]["rendered"]) for p in posts]


def wp_tag_id(name):
    name = name.strip().strip("#")[:60]
    if not name:
        return None
    for t in wp("GET", "/wp/v2/tags", params={"search": name, "per_page": 20, "_fields": "id,name"}):
        if html.unescape(t["name"]).strip().lower() == name.lower():
            return t["id"]
    r = wp_raw("POST", "/wp/v2/tags", json={"name": name})
    data = r.json() if r.content else {}
    if r.ok:
        return data["id"]
    if data.get("code") == "term_exists":
        return data["data"]["term_id"]
    log(f"  could not create tag {name!r}: {r.status_code}")
    return None


def wp_upload_image(img, alt, caption):
    r = wp_raw(
        "POST",
        "/wp/v2/media",
        data=img["bytes"],
        headers={"Content-Type": img["type"], "Content-Disposition": f'attachment; filename="{img["filename"]}"'},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"media upload -> HTTP {r.status_code}: {r.text[:300]}")
    media_id = r.json()["id"]
    wp("POST", f"/wp/v2/media/{media_id}", json={"alt_text": alt, "caption": caption, "title": alt})
    return media_id


# ------------------------------------------------------------------ images
IMG_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
META_IMAGE_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']', re.I),
]
BAD_IMAGE_HINTS = ("logo", "default", "placeholder", "favicon", "og-image-common", "share-image", "fallback")


def page_image_url(page_url):
    """Returns (image_url, final_article_url) - redirect links are followed to the real article."""
    r = HTTP.get(page_url, timeout=30, headers={"Accept-Language": "hi,en;q=0.8"})
    host = urlparse(r.url).netloc
    if r.status_code >= 400 or "news.google.com" in host or "vertexaisearch" in host:
        return None, r.url
    for rx in META_IMAGE_RES:
        for m in rx.finditer(r.text[:300_000]):
            url = urljoin(r.url, html.unescape(m.group(1)).strip())
            filename = urlparse(url).path.rsplit("/", 1)[-1].lower()  # check file name only, folders can say "logo"
            if url.startswith("http") and not any(h in filename for h in BAD_IMAGE_HINTS):
                return url, r.url
    return None, r.url


def download_image(img_url, referer, slug):
    # Don't advertise AVIF: CDNs would send it and not every WordPress server can process it.
    r = HTTP.get(img_url, timeout=30, headers={"Referer": referer, "Accept": "image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5"})
    ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if r.status_code >= 400 or ctype not in IMG_EXT or len(r.content) < 15_000:
        log(f"  image rejected: HTTP {r.status_code}, {ctype or 'no type'}, {len(r.content)} bytes")
        return None  # error, not a supported image, or too small (likely a logo/icon)
    return {"bytes": r.content, "type": ctype, "filename": f"{slug or 'news'}-{int(time.time())}.{IMG_EXT[ctype]}"}


def find_source_image(urls, slug):
    """Try the source article first, then other search results, until one has a usable main image."""
    tried = set()
    for page in urls:
        if not page or page in tried or not page.startswith("http"):
            continue
        tried.add(page)
        try:
            img_url, real_page = page_image_url(page)
            if not img_url:
                continue
            img = download_image(img_url, real_page, slug)
            if img:
                img["source_page"] = real_page
                log(f"  image from {real_page}")
                return img
        except requests.RequestException as e:
            log(f"  image fetch failed for {page}: {e}")
        if len(tried) >= 6:
            break
    return None


def source_name(url):
    host = urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    known = {
        "jagran.com": "दैनिक जागरण", "amarujala.com": "अमर उजाला", "livehindustan.com": "लाइव हिंदुस्तान",
        "bhaskar.com": "दैनिक भास्कर", "aajtak.in": "आज तक", "abplive.com": "एबीपी न्यूज़",
        "navbharattimes.indiatimes.com": "नवभारत टाइम्स", "ndtv.in": "एनडीटीवी", "zeenews.india.com": "ज़ी न्यूज़",
        "news18.com": "न्यूज़18", "hindi.news18.com": "न्यूज़18", "tv9hindi.com": "टीवी9 भारतवर्ष",
        "jansatta.com": "जनसत्ता", "patrika.com": "पत्रिका", "indiatv.in": "इंडिया टीवी",
    }
    for domain, name in known.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host


def source_credit_html(url):
    """Source line added at the end of every post."""
    url = re.sub(r"\.amp\.html$", ".html", url).replace("/amp/", "/")  # link the normal page, not AMP
    return (f'<p><strong>स्रोत:</strong> <a href="{html.escape(url)}" target="_blank" '
            f'rel="nofollow noopener">{html.escape(source_name(url))}</a></p>')


# ---------------------------------------------------------- source article
def gnews_decode(link):
    """Turn a news.google.com/rss/articles/... link into the real article URL."""
    if "news.google.com" not in link:
        return link
    gid = urlparse(link).path.split("/")[-1]
    page = HTTP.get(f"https://news.google.com/rss/articles/{gid}", timeout=30).text
    sig = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sig and ts):
        return None
    inner = (
        '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
        '"X","X",1,[1,1,1],1,1,null,0,0,null,0],"%s",%s,"%s"]' % (gid, ts.group(1), sig.group(1))
    )
    r = HTTP.post(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data={"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])},
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        timeout=30,
    )
    data = json.loads(r.text.split("\n\n")[1])[:-2]
    return json.loads(data[0][2])[1]


def article_text(page_html):
    """Main text of a news article: JSON-LD articleBody if present, else the Hindi <p> paragraphs."""
    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", page_html, re.S | re.I):
        try:
            d = json.loads(m.group(1).strip(), strict=False)  # allow raw newlines inside strings
        except ValueError:
            continue
        items = d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []
        for it in items:
            if isinstance(it, dict) and len(str(it.get("articleBody", ""))) > 300:
                return html.unescape(re.sub(r"<[^>]+>", " ", str(it["articleBody"]))).strip()
    paras = [html.unescape(re.sub(r"<[^>]+>", "", p)).strip()
             for p in re.findall(r"<p[^>]*>(.*?)</p>", page_html, re.S | re.I)]
    # real paragraphs are short; huge blocks are menus/scripts swallowed by unclosed <p> tags
    return "\n".join(p for p in paras if 60 < len(p) < 2500 and re.search(r"[ऀ-ॿ]", p))


def fetch_source(story):
    """Returns {"url", "text"} of the original article, or None."""
    try:
        url = gnews_decode(story.get("link", ""))
        if not url or any(s in urlparse(url).netloc for s in ("facebook.", "youtube.", "instagram.", "x.com")):
            return None
        r = HTTP.get(url, timeout=30, headers={"Accept-Language": "hi,en;q=0.8"})
        if r.status_code >= 400:
            return None
        text = article_text(r.text)
        log(f"  source: {r.url} ({len(text)} chars)")
        return {"url": r.url, "text": text[:12000]}
    except (requests.RequestException, ValueError, IndexError, KeyError, TypeError) as e:
        log(f"  could not read source article: {type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posted": []}


def save_state(state):
    state["posted"] = state["posted"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def norm(s):
    return re.sub(r"\W+", " ", s.lower()).strip()


# --------------------------------------------------------------- headlines
def fetch_candidates(topic, already_used):
    seen, out = set(already_used), []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_HEADLINE_AGE_HOURS)
    feeds = topic_feeds(topic)
    for url in feeds:
        try:
            feed = feedparser.parse(HTTP.get(url, timeout=30).content)
        except requests.RequestException as e:
            log(f"feed failed: {url}: {e}")
            continue
        for e in feed.entries:
            title = html.unescape(e.get("title", "")).strip()
            source = e.get("source", {}).get("title", "")
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3].strip()
            if not title or norm(title) in seen or source.lower() in SKIP_SOURCES:
                continue
            published = e.get("published_parsed")
            if published:
                when = datetime(*published[:6], tzinfo=timezone.utc)
                if when < cutoff:
                    continue
                when_str = when.astimezone(IST).strftime("%d %b %H:%M IST")
            else:
                when_str = "unknown"
            seen.add(norm(title))
            out.append({"headline": title, "source": source, "published": when_str, "link": e.get("link", "")})
    log(f"Collected {len(out)} fresh headlines from {len(feeds)} feeds")
    return out[:100]


def wp_ensure_category(name, categories):
    """Category ID by name; creates the category if the site doesn't have it yet."""
    for n, i in categories.items():
        if n.lower() == name.lower():
            return i
    cid = wp("POST", "/wp/v2/categories", json={"name": name})["id"]
    categories[name] = cid
    log(f"Created category {name!r} (#{cid})")
    return cid


def pick_topic(posted_today):
    """Section that is furthest behind its daily quota, so sections mix through the day."""
    best, best_ratio = None, None
    for key, t in TOPICS.items():
        quota, done = t.get("quota", 0), posted_today.get(key, 0)
        if quota <= 0 or done >= quota:
            continue
        ratio = done / quota
        if best is None or ratio < best_ratio or (ratio == best_ratio and quota > TOPICS[best]["quota"]):
            best, best_ratio = key, ratio
    return best


# ---------------------------------------------------------------------- AI
def ask_ai(system, user, *, web_search=False, schema=None, max_tokens=16000):
    """Returns (text, list_of_source_urls). text is None if the model refused."""
    if AI_PROVIDER == "gemini":
        return ask_gemini(system, user, web_search=web_search, schema=schema, max_tokens=max_tokens)
    if AI_PROVIDER == "openai":
        return ask_openai(system, user, web_search=web_search, schema=schema, max_tokens=max_tokens)
    output_format = {"type": "json_schema", "schema": schema} if schema else None
    text, blocks = ask_claude(system, user, tools=[WEB_SEARCH] if web_search else None,
                              output_format=output_format, max_tokens=max_tokens)
    return text, search_result_urls(blocks)


def ask_gemini(system, user, *, web_search=False, schema=None, max_tokens=16000):
    cfg = {"system_instruction": system, "max_output_tokens": max_tokens}
    if web_search:
        cfg["tools"] = [gtypes.Tool(google_search=gtypes.GoogleSearch())]
    if schema:
        cfg["response_mime_type"] = "application/json"
    resp = None
    for attempt in range(4):
        try:
            resp = gemini_client.models.generate_content(
                model=GEMINI_MODEL, contents=user, config=gtypes.GenerateContentConfig(**cfg)
            )
            break
        except gerrors.APIError as e:
            # Free tier has per-minute limits: wait and retry on 429 / temporary 503.
            if e.code in (429, 500, 503) and attempt < 3:
                log(f"  gemini busy/limited ({e.code}), waiting 45s...")
                time.sleep(45)
                continue
            raise
    u = resp.usage_metadata
    cand = resp.candidates[0] if resp.candidates else None
    urls = []
    meta = getattr(cand, "grounding_metadata", None)
    for chunk in (getattr(meta, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        if web and web.uri:
            urls.append(web.uri)  # Google redirect link; it resolves to the real article
    searches = len(getattr(meta, "web_search_queries", None) or [])
    finish = getattr(cand, "finish_reason", None)
    log(f"  gemini {GEMINI_MODEL}: in={getattr(u, 'prompt_token_count', '?')} "
        f"out={getattr(u, 'candidates_token_count', '?')} searches={searches} finish={finish}")
    text = resp.text if cand else None
    if not text:
        log(f"  gemini returned no text (finish={finish}, feedback={resp.prompt_feedback})")
        return None, urls
    return text, list(dict.fromkeys(urls))


def ask_openai(system, user, *, web_search=False, schema=None, max_tokens=16000):
    kw = dict(model=OPENAI_MODEL, instructions=system, input=user, max_output_tokens=max_tokens)
    if web_search:
        kw["tools"] = [{
            "type": "web_search",
            "user_location": {"type": "approximate", "country": "IN", "region": "Uttar Pradesh",
                              "city": "Deoria", "timezone": "Asia/Kolkata"},
        }]
        kw["include"] = ["web_search_call.action.sources"]
    if schema:
        kw["text"] = {"format": {"type": "json_schema", "name": "result", "schema": schema, "strict": True}}
    resp = openai_client.responses.create(**kw)
    u = resp.usage
    urls, searches = [], 0
    for item in resp.output:
        if item.type == "web_search_call":
            searches += 1
            for s in getattr(getattr(item, "action", None), "sources", None) or []:
                if getattr(s, "url", None):
                    urls.append(s.url)
        elif item.type == "message":
            for part in item.content:
                if getattr(part, "type", "") == "refusal":
                    log(f"  openai refused: {part.refusal}")
                    return None, urls
                for ann in getattr(part, "annotations", None) or []:
                    if getattr(ann, "url", None):
                        urls.append(ann.url)
    log(f"  openai {resp.model}: in={u.input_tokens} out={u.output_tokens} searches={searches} status={resp.status}")
    # citation URLs come first: those are the pages the article is actually based on
    cited = [x for x in urls if "utm_source=openai" in x]
    return resp.output_text, list(dict.fromkeys(cited + urls))


def ask_claude(system, user, *, tools=None, output_format=None, max_tokens=16000):
    """Run one Claude request (resuming pause_turn). Returns (text, content_blocks); text is None on refusal."""
    messages = [{"role": "user", "content": user}]
    output_config = {"effort": EFFORT}
    if output_format:
        output_config["format"] = output_format
    kwargs = dict(model=MODEL, max_tokens=max_tokens, system=system, output_config=output_config)
    if tools:
        kwargs["tools"] = tools
    use_fallback = MODEL in FALLBACK_MODELS
    if use_fallback:
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"

    texts, blocks = [], []
    for _ in range(5):
        if use_fallback:
            resp = claude.beta.messages.create(messages=messages, **kwargs)
        else:
            resp = claude.messages.create(messages=messages, **kwargs)
        u = resp.usage
        searches = getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
        log(f"  claude: in={u.input_tokens} out={u.output_tokens} searches={searches} stop={resp.stop_reason}")
        if resp.stop_reason == "refusal":
            log(f"  claude refused: {getattr(resp, 'stop_details', None)}")
            return None, blocks
        blocks += resp.content
        texts += [b.text for b in resp.content if b.type == "text"]
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
    return "".join(texts), blocks


def search_result_urls(blocks):
    urls = []
    for b in blocks:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            urls += [r.url for r in b.content if getattr(r, "url", None)]
    return urls


SELECT_SYSTEM = """You are the news desk editor of The Newswala (thenewswala.com), a Hindi news portal from Deoria, Uttar Pradesh, read mostly in UP and Bihar.

Right now the site needs a story for its {label} section. What belongs there: {brief}

From the numbered candidate headlines, choose stories for this section only, best first.
Rules:
- Pick only headlines that clearly belong to this section.
- Skip any story the site has already covered. Compare against the recently published titles: the same event counts as covered even if it is worded differently.
- Every pick must be a different event (headlines from different outlets about one event count once).
- Prefer important, fresh, confirmed news that many readers care about, and events reported by several outlets.
- "Deoria" means Deoria district of Uttar Pradesh. Skip stories about similarly named places in other states (e.g. "Muzaffarpur News: देवरिया..." is in Bihar).
- Skip live blogs, opinion pieces, horoscopes, quizzes, photo galleries, sponsored or advertising items, stock tips, and vague or clickbait headlines."""


def select_stories(topic, candidates, recent_titles, n):
    listing = "\n".join(f"{i}. {c['headline']} ({c['source']}, {c['published']})" for i, c in enumerate(candidates))
    recent = "\n".join(f"- {t}" for t in recent_titles) or "(none)"
    user = (
        f"Recently published titles on our site:\n{recent}\n\n"
        f"Candidate headlines:\n{listing}\n\n"
        f"Return the indexes of up to {n} stories to cover, best first, as JSON: {{\"picks\": [3, 0, 7]}}"
    )
    schema = {
        "type": "object",
        "properties": {"picks": {"type": "array", "items": {"type": "integer"}}},
        "required": ["picks"],
        "additionalProperties": False,
    }
    system = SELECT_SYSTEM.replace("{label}", topic["label"]).replace("{brief}", topic["brief"])
    text, _ = ask_ai(system, user, schema=schema, max_tokens=8000)
    if not text:
        return []
    m = re.search(r"\{.*\}", text, re.S)
    picks = []
    for i in json.loads(m.group(0) if m else text).get("picks", []):
        if 0 <= i < len(candidates) and i not in picks:
            picks.append(i)
    return [candidates[i] for i in picks[:n]]


RESEARCH_WITH_SEARCH = """Research first. Use web search to find at least two reliable reports on the story and confirm the key facts: who, what, when, where, numbers, police station or block names, and official statements. Report only facts you found in the search results or in the source article text given to you. Never invent names, quotes, numbers, dates or official statements. If a detail is not confirmed, leave it out or attribute it ("पुलिस के अनुसार", "जानकारी के अनुसार", "परिजनों का आरोप है")."""

RESEARCH_FROM_SOURCE = """The facts come only from the source article text given in the message. It is your only source: never add names, numbers, quotes, dates, places or any detail that is not in it, and do not guess. Ignore website menus, ads, "और पढ़ें" links, bylines and unrelated text mixed into it. Attribute claims as the source does ("पुलिस के अनुसार", "जानकारी के अनुसार", "परिजनों का आरोप है"). If the source text is too thin, is about a different event than the headline, or is not news, reply only with <skip>reason</skip>."""

WRITE_SYSTEM = """You are a senior reporter for The Newswala (thenewswala.com), a Hindi news portal from Deoria, Uttar Pradesh. You write one original Hindi news article about the story you are assigned, in the site's house style.

{research}

House style (follow it closely):
- Simple, everyday Hindi in Devanagari, as in a Hindi newspaper. Common words like FIR, DM, SP, BJP, IPL, CBSE may stay as they are.
- The first paragraph starts with the dateline in bold: the city or district where the news happened, followed by "।", e.g. <p><strong>देवरिया।</strong> ...</p>, <p><strong>लखनऊ।</strong> ...</p>, <p><strong>नई दिल्ली।</strong> ...</p>, <p><strong>मुंबई।</strong> ...</p>. It answers who, what, when and where; for local crime news it names the थाना क्षेत्र or block and village when known.
- Then 4 to 7 short paragraphs with details, what officials or people involved said, and what happens next. Put one or two short bold sub-headings between paragraphs as <p><strong>sub-heading</strong></p>.
- 300 to 500 words. Write it fresh in your own words, like a reporter retelling the story to readers: new sentences, a different order of points, your own headline and sub-headings. Never reuse more than five words in a row from the source, except names, designations, place names and official terms (e.g. scheme or exam names).
- Neutral and factual. Use "आरोप" or "कथित" for accusations. In sexual-offence cases never name or otherwise identify the victim, and never identify minors involved in crimes. For celebrities, report only what the source confirms; no gossip or speculation.
- For exams and jobs, give dates, eligibility and official website names exactly as in the source. For sports, give scores and names exactly as in the source.
- Headline style: specific and factual, like "देवरिया: बनकटा पुलिस ने 50 पेटी अवैध शराब बरामद की, चार गिरफ्तार", "यूपी बोर्ड परीक्षा 2027: ..." or "IND vs AUS: ...". No invented details, no exaggeration beyond the facts.
- Do not mention AI, web searches or these instructions. No source list or links in the article body.

Reply with exactly these tags and nothing after them:
<title>Hindi headline, at most about 100 characters</title>
<slug>short-english-url-slug-in-lowercase</slug>
<excerpt>One or two sentence Hindi summary, at most 160 characters</excerpt>
<categories>0 to 2 extra category names copied exactly from the allowed list, only if clearly fitting (e.g. the district like देवरिया / गोरखपुर, or राजनीति / अपराध / रोजगार / बिहार). The section category is added automatically.</categories>
<tags>4 to 8 short Hindi tags: places, people in public roles, organisations, topic</tags>
<image_source>URL of the news article you used that is most about this exact story (or "source")</image_source>
<content>Article body as HTML using only <p> and <strong> (and <ul>/<li> if a list is really needed). Do not repeat the headline. No <h1>, no markdown.</content>

If you cannot confirm the story from reliable sources, or it is already outdated, reply only with <skip>reason</skip>."""

WEB_SEARCH = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 4,
    "user_location": {"type": "approximate", "city": "Deoria", "region": "Uttar Pradesh", "country": "IN", "timezone": "Asia/Kolkata"},
}


def tag(text, name):
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.S)
    return m.group(1).strip() if m else ""


MAX_COPY_OVERLAP = 0.08  # share of 8-word phrases allowed to match the source word for word


def copy_overlap(source_text, article_html):
    """Share of the article's 8-word phrases that appear word for word in the source (0.0 - 1.0)."""
    words = lambda t: re.findall(r"[ऀ-ॿ\w]+", re.sub(r"<[^>]+>", " ", t))
    grams = lambda w: {tuple(w[i:i + 8]) for i in range(len(w) - 7)}
    ours = grams(words(article_html))
    return len(ours & grams(words(source_text))) / len(ours) if ours else 0.0


def write_article(story, topic, category_names):
    now = datetime.now(IST).strftime("%d %B %Y, %H:%M IST")
    # Gemini free tier has no Google Search, so it writes only from the source article text.
    use_search = AI_PROVIDER != "gemini"
    source = fetch_source(story)
    if not use_search and (not source or len(source["text"]) < 400):
        log("  source article text not available, skipping (no facts to write from)")
        return None
    user = (
        f"Current date and time: {now}\n"
        f"Section: {topic['label']}\n\n"
        f"Story to cover:\nHeadline: {story['headline']}\nReported by: {story['source']}\nPublished: {story['published']}\n\n"
        f"Allowed categories: {', '.join(category_names)}"
    )
    if source and source["text"]:
        user += f"\n\nSource article ({source['url']}):\n<source_text>\n{source['text']}\n</source_text>"
    system = WRITE_SYSTEM.replace("{research}", RESEARCH_WITH_SEARCH if use_search else RESEARCH_FROM_SOURCE)
    text, urls = ask_ai(system, user, web_search=use_search)
    if text and "<skip>" not in text and source and source["text"]:
        overlap = copy_overlap(source["text"], tag(text, "content"))
        log(f"  copied phrases vs source: {overlap:.0%}")
        if overlap > MAX_COPY_OVERLAP:
            # Too close to the source: ask for a complete rewrite once.
            retry_user = user + (
                "\n\nIMPORTANT: your previous draft reused too many exact phrases from the source. "
                "Rewrite the whole article completely in your own words with different sentence structure and order. "
                "Keep only names, numbers, places and official terms the same."
            )
            text, more_urls = ask_ai(system, retry_user, web_search=use_search)
            urls += more_urls
            if text and "<skip>" not in text:
                overlap = copy_overlap(source["text"], tag(text, "content"))
                log(f"  after rewrite: {overlap:.0%}")
                if overlap > MAX_COPY_OVERLAP * 2:
                    log("  still too close to the source, skipping")
                    return None
    if source:
        urls = [source["url"]] + urls
    if not text:
        return None
    if "<skip>" in text:
        log(f"  skipped by writer: {tag(text, 'skip')}")
        return None
    article = {k: tag(text, k) for k in ("title", "slug", "excerpt", "categories", "tags", "image_source", "content")}
    article["content"] = re.sub(r"^```(?:html)?|```$", "", article["content"]).strip()
    article["search_urls"] = urls
    article["source_url"] = source["url"] if source else (urls[0] if urls else "")
    if not article["title"] or len(article["content"]) < 800:
        log("  article output incomplete, skipping")
        return None
    return article


# -------------------------------------------------------------------- main
def main():
    model = {"gemini": GEMINI_MODEL, "openai": OPENAI_MODEL}.get(AI_PROVIDER, MODEL)
    log(f"AI provider: {AI_PROVIDER} ({model}), dry_run={DRY_RUN}")
    if WP_AUTH:
        wp_check_login()
    elif not DRY_RUN:
        log("WP_USER / WP_APP_PASSWORD are required to publish.")
        return 1
    else:
        log("No WordPress login set: dry run uses public data only.")
    state = load_state()
    today = datetime.now(IST).date().isoformat()
    posted_today = {}
    for p in state["posted"]:
        if p.get("time", "").startswith(today):
            key = p.get("topic", "state")  # older entries were all UP/Deoria news
            posted_today[key] = posted_today.get(key, 0) + 1
    total_today = sum(posted_today.values())
    log("Posted today: " + ", ".join(f"{k} {posted_today.get(k, 0)}/{t.get('quota', 0)}" for k, t in TOPICS.items())
        + f" (total {total_today}/{MAX_POSTS_PER_DAY})")
    topic_key = os.getenv("FORCE_TOPIC") or pick_topic(posted_today)
    to_post = min(POSTS_PER_RUN, MAX_POSTS_PER_DAY - total_today)
    if not topic_key or to_post <= 0:
        log("Daily quota reached, nothing to do.")
        return 0
    topic = TOPICS[topic_key]
    log(f"Section for this run: {topic['label']}")

    categories = wp_categories()
    topic_cat_id = 0
    if not DRY_RUN:
        topic_cat_id = wp_ensure_category(topic["category"], categories)
    allowed = sorted(n for n, i in categories.items() if n.lower() not in EXCLUDED_CATEGORIES and i not in ALWAYS_CATEGORY_IDS)
    cat_lookup = {n.lower(): i for n, i in categories.items()}
    topic_cat_id = topic_cat_id or cat_lookup.get(topic["category"].lower(), 0)

    site_titles = wp_recent_titles()
    used = [p["headline"] for p in state["posted"]] + [p["title"] for p in state["posted"]]
    candidates = fetch_candidates(topic, [norm(t) for t in used + site_titles])
    if not candidates:
        log("No fresh headlines found.")
        return 1

    recent_titles = site_titles + [p["title"] for p in state["posted"][-60:]]
    try:
        stories = select_stories(topic, candidates, recent_titles, to_post + 2)  # 2 backups
    except AI_ERRORS as e:
        log(f"AI error while choosing stories: {e}")
        return 1
    log(f"Selected {len(stories)} stories: " + " | ".join(s["headline"] for s in stories))

    posted = 0
    for story in stories:
        if posted >= to_post:
            break
        log(f"\n=== {story['headline']}")
        try:
            art = write_article(story, topic, allowed)
            if not art:
                continue
            cat_ids = list(ALWAYS_CATEGORY_IDS) + ([topic_cat_id] if topic_cat_id else [])
            for name in art["categories"].split(","):
                cid = cat_lookup.get(name.strip().lower())
                if cid and cid not in cat_ids:
                    cat_ids.append(cid)
            slug = re.sub(r"[^a-z0-9-]+", "-", art["slug"].lower()).strip("-")[:80]

            # Photo from the source article first, then the other pages used.
            img = find_source_image(art["search_urls"][:1] + [art["image_source"]] + art["search_urls"][1:], slug)
            if not img:
                log("  no usable image found")
                if REQUIRE_IMAGE:
                    continue

            if DRY_RUN:
                log(f"[DRY RUN] {art['title']}\ncategories={cat_ids} tags={art['tags']}")
                log(f"image: {img['source_page'] + ' (' + str(len(img['bytes'])) + ' bytes)' if img else 'none'}")
                log(art["content"][:1500])
                posted += 1
                continue

            media_id = 0
            if img:
                try:
                    media_id = wp_upload_image(img, alt=art["title"], caption=f"साभार: {source_name(img['source_page'])}")
                except RuntimeError as e:
                    log(f"  {e}")

            tag_ids = [t for t in (wp_tag_id(x) for x in art["tags"].split(",")[:8]) if t]
            content = art["content"]
            if art["source_url"]:
                content += "\n" + source_credit_html(art["source_url"])
            payload = {
                "title": art["title"],
                "content": content,
                "excerpt": art["excerpt"],
                "status": POST_STATUS,
                "categories": cat_ids,
                "tags": tag_ids,
            }
            if slug:
                payload["slug"] = slug
            if media_id:
                payload["featured_media"] = media_id
            post = wp("POST", "/wp/v2/posts", json=payload)
            log(f"  POSTED #{post['id']} (image={media_id or 'none'}): {post.get('link')}")
            state["posted"].append({
                "headline": story["headline"],
                "title": art["title"],
                "wp_id": post["id"],
                "topic": topic_key,
                "source_url": art["source_url"],
                "time": datetime.now(IST).isoformat(timespec="seconds"),
            })
            save_state(state)
            posted += 1
        except (*AI_ERRORS, RuntimeError, requests.RequestException, ValueError, KeyError) as e:
            log(f"  failed: {type(e).__name__}: {e}")
            time.sleep(3)

    log(f"\nDone: {posted}/{to_post} articles {'generated (dry run)' if DRY_RUN else 'posted'}.")
    return 0 if posted else 1


if __name__ == "__main__":
    sys.exit(main())
