# SpiritFinder

Search for a bottle across multiple liquor store websites and instantly see which store has the lowest price.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Running

```bash
uvicorn main:app --reload
```

Then open **http://localhost:8000** in your browser.

## Searching

Type a bottle name or distillery into the search box and press **Search**. All configured stores are searched in parallel. Results are sorted lowest price first; the best price is highlighted in green.

## Managing Stores

Click **Manage Stores** at the bottom of the page to expand the store panel.

### Adding a store automatically

1. Paste the store's homepage URL into the URL field.
2. Optionally type a display name in the **Store name** field — if left blank the name is auto-detected from the site.
3. Click **Add Store**.

The app visits the site, finds its search form, detects the product card layout, generates a scraper, and activates it immediately (no restart needed). This takes 20–40 seconds. If a store's auto-detected name looks wrong (e.g. a marketing tagline instead of the store name), use the rename button to fix it.

### Renaming a store

Click the **✎** button on any store chip. The name becomes an editable field — type the new name and press **Enter** to save, or **Escape** to cancel. The change takes effect immediately and persists across restarts.

### Adding a store manually

If auto-detection fails, copy the template and fill it in:

```bash
cp scrapers/_template.py scrapers/my_store.py
```

Open the new file and fill in three things:

| Field | What to put |
|---|---|
| `name` | Display name shown in results |
| `_search_url` | The store's search URL with `{query}` as the placeholder |
| CSS selectors | Match the site's product card, name, and price elements |

The scraper is picked up automatically on the next server restart.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Could not find a search form" | The site may use a non-standard search. Add the store manually. |
| "No prices found on results page" | The site may require JavaScript beyond what the auto-detector waits for. Add manually. |
| Store returns 0 results for a search | The store may not carry that product, or the selectors need tuning in `scrapers/<store>.py`. |
| Store errors on every search | Open the scraper file and check the `_search_url` and CSS selectors against the live site. |
