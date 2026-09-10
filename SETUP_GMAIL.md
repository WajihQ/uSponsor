# Gmail CRM sync — one-time setup

This connects the CRM to your Gmail so it can stamp **initial contact**, **last
contact**, and **follow-up count** on leads automatically, by reading your
**Sent** mail. It reads *headers only* (To/Cc/Bcc/Date) — never message bodies —
and everything stays on your PC.

You do this once. Budget ~10 minutes.

## 1. Create a Google Cloud project + enable Gmail

1. Go to <https://console.cloud.google.com/> and create a project (any name,
   e.g. "uSponsor CRM"). Use the account that owns your Workspace domain.
2. **APIs & Services → Library →** search **Gmail API → Enable**.

## 2. Configure the OAuth consent screen (Internal = no verification)

1. **APIs & Services → OAuth consent screen.**
2. Choose **Internal** (available because you're on Google Workspace). Internal
   apps need **no Google verification** and have no test-user limit — they just
   work for accounts on your domain.
3. Fill in the app name + your email where required, save. You can skip scopes
   here; the app requests them at sign-in.

## 3. Create a Desktop OAuth client

1. **APIs & Services → Credentials → Create credentials → OAuth client ID.**
2. Application type: **Desktop app**. Create.
3. **Download JSON**. Save it into `scripts/gmail/` (next to `connect_gmail.py`)
   as **`client_secret.json`**. (It's gitignored — never commit it.)

## 4. Install the libraries + connect each account

```bash
pip install -r requirements.txt
python scripts/gmail/connect_gmail.py
```

A browser opens — sign in with a **sending** account and approve. The token is
saved to `gmail_tokens/<address>.json`. **Run `python scripts/gmail/connect_gmail.py` again
for every account you send from** — both Instantly mailboxes *and* the account
you send manual outreach from. (Instantly sends through your Workspace
mailboxes, so those sends are in Sent mail and get picked up too.)

## 5. Sync

1. Start the app (`python app.py`) and open **Influencer CRM** or **Brand CRM**.
2. In the **Gmail sync** card, click **Full resync** once. This reads all your
   sent history across every connected account and fills in the true first/last
   contact dates + follow-up counts.
3. After that, **Sync now** (or the automatic 30-minute background sync) only
   reads new mail and folds it in.

## Notes

- **Scope:** `gmail.metadata` — the app can only read message headers, not
  bodies. Matching is by email address, so a lead with no email won't be
  stamped until you add one.
- **The automatic sync only runs while `python app.py` is running.** When you
  open the app it catches up on anything sent since last time. To keep it
  current hands-off, point Windows Task Scheduler at `python scan.py`'s sibling
  (ask and I'll add a headless `sync.py`), or just leave the app running.
- Change the cadence with the `USPONSOR_GMAIL_INTERVAL` environment variable
  (minutes; `0` disables the background sync).
- **How the fields reconcile:** *Full resync* overwrites the three contact
  fields with Gmail's truth (across all accounts). *Sync now* / the interval add
  only newer mail: the earliest send stays as initial contact, the latest send
  becomes last contact, and each additional send bumps the follow-up count.
  Values you typed or imported are treated as estimates until the first Full
  resync makes Gmail authoritative.
```
