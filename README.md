# MediClaim — local medical claims tracker

A single-user, fully-local tracker for family medical invoices and rebate claims.
Your data and invoice files never leave your machine.

## What it does

- One row per invoice: claimant, institution, amount, date, four workflow flags,
  amount rebated, a linked invoice file, additional supporting documents, and notes.
- A **status** is derived automatically from the flags — you never set it by hand:
  - *Awaiting invoice* → *Ready to claim* → *Claim submitted* → *Complete*
  - *Excluded* — short-circuits the workflow for items insurance won't cover
  - *Check: rebated but not claimed* — anomaly flag
- Rows are **colour-coded by status** so you can scan the table at a glance.
- Filter by **date range**, claimant, status, or free-text search.
- The list always re-sorts to newest-first whenever you add, edit, or toggle
  anything — column headers still work for ad-hoc sorting.
- Click a row's flag dots (Inv / Clm / Reb / Exc) to toggle them inline.
- Click **View ↗** on the invoice column to open the invoice file.
- The **Other Docs** column holds any number of supporting documents per claim
  (referral letters, lab results, etc.) — click **+ Add** to attach, click a
  chip to view, click **×** to remove.
- The three tiles at the top — *Total amount incurred*, *Total rebate*,
  *Shortfall* — recompute live as you change the date range.

## Requirements

- Python 3.10+
- One-time install of dependencies:

```bash
pip install fastapi "uvicorn[standard]" python-multipart
```

## Running it

From this folder:

```bash
python app.py
```

Then open <http://localhost:8765> in any browser. Stop it with Ctrl-C.

(Optional convenience: `./start.sh` does the same.)

## Where your data lives

Everything is inside this one folder:

| Item                 | What it is                                              |
|----------------------|---------------------------------------------------------|
| `mediclaim.db`       | SQLite database — all your entries and file metadata    |
| `invoices/`          | Invoice files **and** other attached documents on disk  |
| `claimants.json`     | The list of people that show up in the Claimant dropdown |
| `institutions.json`  | The list of medical institutions in the dropdown         |
| `app.py`             | The server                                              |
| `static/`            | The web UI                                              |

**To back up:** copy the whole folder (or just `mediclaim.db` + `invoices/` +
the two JSON files).

**To inspect data directly:** open `mediclaim.db` in any SQLite browser, or
`sqlite3 mediclaim.db "SELECT * FROM claims;"`. Attached documents live in the
`claim_files` table, joined by `claim_id`.

## Customising the claimant list

Edit `claimants.json` directly — it's a JSON array of names. Save and reload
the page — no restart needed. Existing entries keep whatever claimant they
were saved with even if you rename someone later.

## Adding & managing institutions

Two ways:

- **From the browser:** the Institution dropdown in the new-entry form ends
  with **+ Add new institution…** — pick it, type a name, save. The new
  institution joins the list and the JSON file immediately.
- **By editing the file:** add/remove names in `institutions.json` directly.
  Reload the page to see changes.

Duplicate names are rejected case-insensitively, and a typed name is
canonicalised to the stored casing when saved (so "raffles medical" stored as
"Raffles Medical" stays consistent).

## Notes on design choices

- **Archive vs. permanent delete.** *Archive* hides a row but never touches
  the file on disk — safe by default. *Delete* (the explicit red action) is
  a hard delete: the row, its invoice, and every attached other-doc are
  removed from disk in one cascading operation. Both actions confirm first.
- **Relative file paths only.** The DB stores just the filename, so the whole
  folder is portable — move it anywhere and links still resolve.
- **Localhost only.** The server binds to `127.0.0.1`, so it's not reachable
  from other devices on your network.
- **Single-user, single-currency.** Defaults to SGD everywhere; you can store
  individual claims in another currency but the summary tiles assume SGD.
- **JSON files are re-read per request.** No restart is needed after editing
  `claimants.json` or `institutions.json` by hand.

## Possible next steps

- CSV export for tax/FSA season.
- Per-claimant breakdown alongside the family-wide totals.
- A "remind me" view for claims submitted >N days ago with no rebate yet.
- Multi-currency aware summaries.
