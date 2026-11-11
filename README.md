# The Newswala – auto posting

A GitHub Action that publishes **30 Hindi news posts a day** to https://thenewswala.com, spread evenly from 6:30 AM to 9:00 PM IST (12 national, 5 jobs, 7 UP, 3 sports, 3 entertainment). It is triggered every 15 minutes; because GitHub skips some scheduled runs, each run checks how far behind the plan it is and posts up to 3 to catch up, never more than 30 a day.

Each run:
1. Reads fresh Hindi headlines from Google News (Deoria, Salempur, Barhaj, Gorakhpur, Kushinagar, UP, plus the top headlines).
2. Claude picks stories the site hasn't covered yet, with Deoria first.
3. Claude checks each story with web search and writes an original article in the site's style: a **देवरिया।** dateline, short paragraphs, bold sub-headings, 300–500 words.
4. The script downloads the main photo from the source news article, uploads it to the Media Library with a credit caption ("साभार: <source site>") and sets it as the featured image.
5. The script publishes the post under Newsbeat + Recent + the right district category, with tags.

`posted.json` lists the stories already covered so they aren't posted twice. The workflow commits it after every run.

## Setup (one time)

### 1. Create a WordPress Application Password
1. Open https://thenewswala.com/wp-admin/profile.php
2. Scroll to **Application Passwords**, type the name `github-autopost` and click **Add New Application Password**.
3. Copy the password (looks like `abcd efgh ijkl mnop qrst uvwx`). The user must be an **Editor** or **Administrator**.

### 2. Get a Claude API key
Create one at https://console.anthropic.com/settings/keys and add some credit.

### 3. Put this folder on GitHub
Create a **private** repository and push these files (including `.github/workflows/autopost.yml`).

### 4. Add the secrets
Go to the repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `GEMINI_API_KEY` | your Gemini API key (free from https://aistudio.google.com/apikey). Or use `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` instead |
| `WP_USER` | your WordPress username |
| `WP_APP_PASSWORD` | the application password from step 1 |

### 5. Test with a dry run
Open **Actions → Auto-post news → Run workflow**. Leave **Dry run** ticked and run it.
The log shows the stories picked, the articles written and the image found. Nothing is published.
Then run it again with **Dry run** unticked to publish 1 real post.
After that it runs by itself (30 posts a day, 6:30 AM to 9:00 PM IST).

## Optional settings (Settings → Variables → Actions)

| Variable | Default | Meaning |
|---|---|---|
| `MAX_POSTS_PER_RUN` | `3` | most posts one run may publish when catching up after GitHub skipped runs |
| `TOPIC_QUOTAS` | `national=12,jobs=5,state=7,sports=3,entertainment=3` | posts per day per section (total = daily limit) |
| `POST_STATUS` | `publish` | use `draft` to review posts before they go live |
| `CLAUDE_MODEL` | `claude-opus-5-5` | `claude-sonnet-5-5` or `claude-haiku-5-5` are cheaper |
| `CLAUDE_EFFORT` | `medium` | `low` is cheaper and faster |
| `ALWAYS_CATEGORY_IDS` | `598,6` | categories added to every post (Newsbeat, Recent) |
| `REQUIRE_IMAGE` | `false` | `true` = skip a story if no photo is found |

## Troubleshooting
- **401 / rest_not_logged_in**: wrong username or application password, or a security plugin is blocking the REST API.
- **403 from Hostinger**: the hosting firewall may be blocking GitHub's servers. Allow REST API access in hPanel or your security plugin.
- **Runs late**: GitHub's schedule can run 5–30 minutes late. That's normal.
