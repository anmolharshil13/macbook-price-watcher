# MacBook Price Watcher India

Tracks **Apple MacBook Air M5 · 13-inch · 24GB · 1TB** across Amazon India, Flipkart, Croma,
Vijay Sales, Reliance Digital and Imagine Online, and pushes alerts to your iPhone. Target: **₹1,85,000**.

## How it works

```
 iPhone (Safari / Home Screen app)          GitHub (free)                         ntfy (free)
 ┌──────────────────────────────┐   read   ┌─────────────────────────────┐  push  ┌──────────────┐
 │ Dashboard · History · Alerts │◀─────────│ data/history.json           │        │ ntfy iOS app │
 │ Settings: product links      │──commit─▶│ data/config.json (links)    │        │  on iPhone   │
 └──────────────────────────────┘          │ data/alerts.json            │        └──────▲───────┘
         hosted on GitHub Pages            │                             │               │
                                           │ Actions, every 3 h:         │               │
                                           │  checker/check_prices.py ───┼───────────────┘
                                           │  fetch → extract → VERIFY   │
                                           │  → alert → commit results   │
                                           └─────────────────────────────┘
```

* **No server, no database, ₹0.** GitHub Actions runs the checker on a schedule, commits results
  to `data/`, GitHub Pages serves the app, ntfy delivers the push.
* **Only verified prices alert.** A check counts as verified only if the page loaded (not a
  captcha/block page), a price was found in structured data or a known price element, it is
  between ₹1,00,000 and ₹4,00,000, the listing confirms **M5 + 13″ + 24GB + 1TB + MacBook Air** (or names one of the four Apple part numbers for that exact config — `model_numbers` in `data/config.json`),
  and it isn't marked out of stock. Everything else is recorded with a reason but never alerts.
* **Alerts** (each sent once, at the moment it happens):
  * 📉 **Price drop** — verified price is lower than that retailer's previous verified price.
  * 🎯 **Target reached** — verified price moves to ₹1,85,000 or below (from above, or on the first check).
  * ⚠️ **Needs attention** — a retailer has failed verification 4 checks in a row (≈12 h), e.g. a
    dead link or the retailer blocking the checker.

## One-time setup (≈15 minutes)

1. **Create the repo.** On github.com create a new **public** repository, e.g. `macbook-price-watcher`
   (Pages is free for public repos; nothing secret is stored in it). Upload/push the contents of this folder.
2. **Turn on Pages.** Repo → Settings → Pages → *Deploy from a branch* → `main` / `(root)`.
   Your app is at `https://<your-username>.github.io/macbook-price-watcher/`.
3. **Allow the bot to save results.** Repo → Settings → Actions → General → Workflow permissions →
   *Read and write permissions*.
4. **ntfy on the iPhone.** Install *ntfy* from the App Store, allow notifications, tap **+** and subscribe
   to a long random topic, e.g. `mbp-watch-7f3k9q2x` (anyone who knows the name can read it).
   Then repo → Settings → Secrets and variables → Actions → **New repository secret**
   `NTFY_TOPIC` = that topic name.
5. **Token for the app's Settings screen.** github.com → Settings → Developer settings →
   Fine-grained tokens → *Generate*: repository access = **only this repo**; permissions
   **Contents: Read and write** and **Actions: Read and write**. It is stored only in Safari on your iPhone.
6. **On the iPhone:** open the Pages URL in Safari → Share → **Add to Home Screen**. Open it →
   Settings → paste the five product links + token → **Save & check now**. Tap **Test push** to confirm
   notifications arrive.

Saving links commits `data/config.json`, which immediately triggers a check. After that it runs every
3 hours (cron in `.github/workflows/check-prices.yml`; change `17 */3 * * *` to taste — GitHub may delay
scheduled runs by several minutes).

## Honest limitations

* **Amazon and Flipkart often block cloud servers.** You'll see a ⛔ *Blocked* status (never a false
  alert). If a retailer stays blocked, run the same checker from a home PC on a schedule —
  `python checker/check_prices.py` with `NTFY_TOPIC` set, then commit `data/` — residential IPs are
  blocked far less.
* Retailer page markup changes. The checker tries JSON-LD → meta tags → retailer-specific patterns
  (Croma also via its pricing API). If a retailer starts showing *No price found*, the pattern in
  `RETAILER_PATTERNS` needs updating.
* Bank-card offers and exchange discounts aren't counted — it tracks the listed selling price.
* Scheduled workflows in a repo with no activity for 60 days get paused by GitHub; the bot's own
  commits count as activity, so this only bites if every check fails to change anything for 60 days.

## Files

| Path | What |
|---|---|
| `index.html`, `app.js`, `styles.css`, `manifest.webmanifest`, `icons/` | iPhone web app (installable to Home Screen, light/dark) |
| `checker/check_prices.py` | the cloud checker (Python standard library only) |
| `checker/test_check_prices.py` | tests — `python -m unittest discover -s checker` |
| `.github/workflows/check-prices.yml` | schedule, manual run, test push, auto-commit |
| `data/config.json` | links + target price (edited from the app) |
| `data/history.json`, `data/alerts.json` | written by the checker |
