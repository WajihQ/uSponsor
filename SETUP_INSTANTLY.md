# Instantly sync — setup

Pulls each campaign lead's status from Instantly into the CRM's **Instantly**
column — *replied / opened / bounced / unsubscribed / interested / meeting
booked* — and nudges the last-contact date forward. It's read-only against
Instantly and never overwrites a status you set by hand.

Gmail sync already tracks *when* you contacted a lead; this adds *what happened*
in your campaigns, which Gmail can't see.

## 1. Get your API key

1. In Instantly: **Settings → Integrations → API keys** (v2). Your Growth plan
   includes API access.
2. Create a key and copy it (a long base64 `id:token` string).
   **If you pasted a key into a chat, regenerate it first** — treat the old one
   as burned.

## 2. Give it to the app

Create a file named **`instantly.json`** in the project folder (next to
`app.py`) with:

```json
{ "api_key": "PASTE_YOUR_KEY_HERE" }
```

It's gitignored — it never leaves your machine or enters the repo. (You can
instead set an `INSTANTLY_API_KEY` environment variable.)

## 3. Confirm it works

```bash
python diagnose_instantly.py
```

This lists your campaigns, prints one real lead's fields, shows the status the
app derives, and how many Instantly leads match a CRM email. **Paste that output
back to me** — the v2 lead field names vary, and I want to confirm the status
mapping against your real data before you rely on it.

## 4. Sync

Open a CRM page → **Instantly sync** card → **Sync from Instantly**. It reads all
leads across your campaigns and fills the **Instantly** column. Re-run any time
(it's idempotent). You can filter that column like any other — e.g. show only
`replied` to see who to follow up with, or `bounced` to fix bad addresses.

## Notes

- **Matching is by email**, same as Gmail sync. A lead with no email, or an
  Instantly recipient not in your CRM, simply isn't touched.
- Rate limit on Growth is ~100 requests / 10s; the client backs off and retries
  transient errors automatically.
- Not yet included: **pushing** CRM leads into an Instantly campaign (so you can
  stop CSV-uploading). Say the word and that's the next add-on.
```
