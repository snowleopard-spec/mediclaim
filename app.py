"""
MedClaims — a local, single-user medical claims tracker & invoice manager.

Run:  python app.py
Then open http://localhost:8765 in your browser.

Everything lives in this folder:
  medclaims.db          SQLite database (your data)
  invoices/             the actual invoice files
  app.py                this file
  static/index.html     the UI

To back up: copy this whole folder. That's it.
"""

import os
import re
import json
import sqlite3
import shutil
import datetime
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "medclaims.db"
INVOICE_DIR = BASE / "invoices"
STATIC_DIR = BASE / "static"
CLAIMANTS_PATH = BASE / "claimants.json"
INSTITUTIONS_PATH = BASE / "institutions.json"
INVOICE_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# Defaults used only when claimants.json is missing on first boot.
DEFAULT_CLAIMANTS = ["Self", "Spouse", "Child 1", "Child 2"]


# ---------------------------------------------------------------------------
# Claimants & institutions (JSON-backed)
# ---------------------------------------------------------------------------

def _read_json_list(path: Path) -> list:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise HTTPException(500, f"{path.name} must be a JSON array of strings")
    return data


def _write_json_list(path: Path, items: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load_claimants() -> list:
    return _read_json_list(CLAIMANTS_PATH)


def load_institutions() -> list:
    items = _read_json_list(INSTITUTIONS_PATH)
    return sorted(items, key=str.casefold)


def save_institutions(items: list) -> None:
    _write_json_list(INSTITUTIONS_PATH, sorted(items, key=str.casefold))


def seed_files_if_missing() -> None:
    if not CLAIMANTS_PATH.exists():
        _write_json_list(CLAIMANTS_PATH, DEFAULT_CLAIMANTS)
    if not INSTITUTIONS_PATH.exists():
        # Seed from whatever distinct institutions are already in the DB,
        # so existing claims keep working with the new dropdown.
        seed = []
        if DB_PATH.exists():
            with closing(get_db()) as db:
                rows = db.execute(
                    "SELECT DISTINCT institution FROM claims "
                    "ORDER BY institution COLLATE NOCASE"
                ).fetchall()
            seed = [r["institution"] for r in rows if r["institution"]]
        _write_json_list(INSTITUTIONS_PATH, seed)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                claimant        TEXT    NOT NULL,
                institution     TEXT    NOT NULL,
                amount          REAL    NOT NULL DEFAULT 0,
                currency        TEXT    NOT NULL DEFAULT 'SGD',
                date_incurred   TEXT    NOT NULL,
                invoice_received INTEGER NOT NULL DEFAULT 0,
                claimed         INTEGER NOT NULL DEFAULT 0,
                rebated         INTEGER NOT NULL DEFAULT 0,
                amount_rebated  REAL    NOT NULL DEFAULT 0,
                invoice_file    TEXT,
                notes           TEXT    NOT NULL DEFAULT '',
                archived        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL,
                updated_at      TEXT    NOT NULL
            )
        """)
        # Idempotent migration: add `excluded` if older DBs predate it.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(claims)").fetchall()}
        if "excluded" not in cols:
            db.execute("ALTER TABLE claims ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        db.execute("""
            CREATE TABLE IF NOT EXISTS claim_files (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id      INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
                kind          TEXT    NOT NULL DEFAULT 'other',
                filename      TEXT    NOT NULL,
                original_name TEXT    NOT NULL,
                created_at    TEXT    NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_claim_files_claim ON claim_files(claim_id)")
        db.commit()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def compute_status(row):
    """Derive workflow stage from the flags. Single source of truth."""
    # Excluded short-circuits everything: insurance won't cover it, end of story.
    if bool(row["excluded"]):
        return "Excluded"
    received = bool(row["invoice_received"])
    claimed = bool(row["claimed"])
    rebated = bool(row["rebated"])

    # Anomaly: rebated without having claimed — surface it so it can be fixed.
    if rebated and not claimed:
        return "Check: rebated but not claimed"
    if rebated:
        return "Complete"
    if claimed:
        return "Claim submitted"
    if received:
        return "Ready to claim"
    return "Awaiting invoice"


def _other_files_for(db, claim_id: int) -> list:
    rows = db.execute(
        "SELECT id, filename, original_name FROM claim_files "
        "WHERE claim_id=? AND kind='other' ORDER BY id",
        (claim_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def row_to_dict(row, *, db=None):
    d = dict(row)
    d["status"] = compute_status(row)
    d["outstanding"] = round(d["amount"] - d["amount_rebated"], 2) if d["rebated"] else None
    d["other_files"] = _other_files_for(db, row["id"]) if db is not None else []
    return d


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    init_db()
    seed_files_if_missing()
    yield


app = FastAPI(title="MedClaims", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def config():
    return {"claimants": load_claimants()}


@app.get("/api/institutions")
def institutions():
    return load_institutions()


@app.post("/api/institutions")
async def add_institution(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Institution name is required")
    current = load_institutions()
    if any(name.casefold() == x.casefold() for x in current):
        raise HTTPException(400, f"{name!r} is already in the list")
    current.append(name)
    save_institutions(current)
    return load_institutions()


@app.get("/api/claims")
def list_claims():
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT * FROM claims WHERE archived=0 "
            "ORDER BY date_incurred DESC, id DESC"
        ).fetchall()
        return [row_to_dict(r, db=db) for r in rows]


def _parse_common(form):
    """Pull and validate the shared fields from a form dict."""
    claimant = (form.get("claimant") or "").strip()
    if claimant not in load_claimants():
        raise HTTPException(400, f"Unknown claimant: {claimant!r}")
    institution = (form.get("institution") or "").strip()
    if not institution:
        raise HTTPException(400, "Institution is required")
    known = load_institutions()
    match = next((x for x in known if x.casefold() == institution.casefold()), None)
    if match is None:
        raise HTTPException(400, f"Unknown institution: {institution!r}. Add it first.")
    institution = match  # canonicalize to stored casing
    date_incurred = (form.get("date_incurred") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_incurred):
        raise HTTPException(400, "date_incurred must be YYYY-MM-DD")
    try:
        amount = float(form.get("amount") or 0)
        amount_rebated = float(form.get("amount_rebated") or 0)
    except ValueError:
        raise HTTPException(400, "Amount fields must be numbers")
    return {
        "claimant": claimant,
        "institution": institution,
        "amount": amount,
        "currency": (form.get("currency") or "SGD").strip() or "SGD",
        "date_incurred": date_incurred,
        "invoice_received": 1 if form.get("invoice_received") in ("1", "true", "on", True) else 0,
        "claimed": 1 if form.get("claimed") in ("1", "true", "on", True) else 0,
        "rebated": 1 if form.get("rebated") in ("1", "true", "on", True) else 0,
        "excluded": 1 if form.get("excluded") in ("1", "true", "on", True) else 0,
        "amount_rebated": amount_rebated,
        "notes": (form.get("notes") or "").strip(),
    }


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "invoice")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:120] or "invoice"


def _store_invoice(claim_id: int, upload: UploadFile) -> str:
    """Copy the upload into invoices/ as {id}_{safename}. Returns stored name."""
    stored = f"{claim_id}_{_safe_name(upload.filename)}"
    dest = INVOICE_DIR / stored
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return stored


@app.post("/api/claims")
async def create_claim(
    claimant: str = Form(...),
    institution: str = Form(...),
    amount: str = Form("0"),
    currency: str = Form("SGD"),
    date_incurred: str = Form(...),
    invoice_received: str = Form("0"),
    claimed: str = Form("0"),
    rebated: str = Form("0"),
    excluded: str = Form("0"),
    amount_rebated: str = Form("0"),
    notes: str = Form(""),
    invoice: UploadFile = File(None),
):
    fields = _parse_common(locals())
    ts = now_iso()
    with closing(get_db()) as db:
        cur = db.execute(
            """INSERT INTO claims
               (claimant, institution, amount, currency, date_incurred,
                invoice_received, claimed, rebated, excluded, amount_rebated,
                notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fields["claimant"], fields["institution"], fields["amount"],
             fields["currency"], fields["date_incurred"], fields["invoice_received"],
             fields["claimed"], fields["rebated"], fields["excluded"], fields["amount_rebated"],
             fields["notes"], ts, ts),
        )
        claim_id = cur.lastrowid
        if invoice is not None and invoice.filename:
            stored = _store_invoice(claim_id, invoice)
            db.execute("UPDATE claims SET invoice_file=? WHERE id=?", (stored, claim_id))
        db.commit()
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return row_to_dict(row, db=db)


@app.put("/api/claims/{claim_id}")
async def update_claim(
    claim_id: int,
    claimant: str = Form(...),
    institution: str = Form(...),
    amount: str = Form("0"),
    currency: str = Form("SGD"),
    date_incurred: str = Form(...),
    invoice_received: str = Form("0"),
    claimed: str = Form("0"),
    rebated: str = Form("0"),
    excluded: str = Form("0"),
    amount_rebated: str = Form("0"),
    notes: str = Form(""),
    invoice: UploadFile = File(None),
):
    fields = _parse_common(locals())
    with closing(get_db()) as db:
        existing = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Claim not found")
        db.execute(
            """UPDATE claims SET
               claimant=?, institution=?, amount=?, currency=?, date_incurred=?,
               invoice_received=?, claimed=?, rebated=?, excluded=?, amount_rebated=?,
               notes=?, updated_at=?
               WHERE id=?""",
            (fields["claimant"], fields["institution"], fields["amount"],
             fields["currency"], fields["date_incurred"], fields["invoice_received"],
             fields["claimed"], fields["rebated"], fields["excluded"], fields["amount_rebated"],
             fields["notes"], now_iso(), claim_id),
        )
        if invoice is not None and invoice.filename:
            stored = _store_invoice(claim_id, invoice)
            db.execute("UPDATE claims SET invoice_file=? WHERE id=?", (stored, claim_id))
        db.commit()
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return row_to_dict(row, db=db)


@app.post("/api/claims/{claim_id}/toggle")
def toggle_flag(claim_id: int, field: str = Form(...)):
    """Quick toggle of a single boolean flag from the table view."""
    if field not in ("invoice_received", "claimed", "rebated", "excluded"):
        raise HTTPException(400, "Bad field")
    with closing(get_db()) as db:
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Claim not found")
        newval = 0 if row[field] else 1
        db.execute(f"UPDATE claims SET {field}=?, updated_at=? WHERE id=?",
                   (newval, now_iso(), claim_id))
        db.commit()
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return row_to_dict(row, db=db)


@app.delete("/api/claims/{claim_id}")
def archive_claim(claim_id: int):
    """Soft delete. The invoice file on disk is never touched."""
    with closing(get_db()) as db:
        db.execute("UPDATE claims SET archived=1, updated_at=? WHERE id=?",
                   (now_iso(), claim_id))
        db.commit()
    return {"ok": True}


@app.get("/api/claims/archived")
def list_archived():
    """The archived (hidden) entries, for the restore view."""
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT * FROM claims WHERE archived=1 "
            "ORDER BY date_incurred DESC, id DESC"
        ).fetchall()
        return [row_to_dict(r, db=db) for r in rows]


@app.post("/api/claims/{claim_id}/restore")
def restore_claim(claim_id: int):
    """Un-archive: bring a hidden entry back into the main list."""
    with closing(get_db()) as db:
        db.execute("UPDATE claims SET archived=0, updated_at=? WHERE id=?",
                   (now_iso(), claim_id))
        db.commit()
    return {"ok": True}


@app.delete("/api/claims/{claim_id}/permanent")
def delete_claim_permanently(claim_id: int):
    """Hard delete: remove the database row AND every file (invoice + other docs)
    from disk. Irreversible, hence the separate, explicit endpoint."""
    with closing(get_db()) as db:
        row = db.execute("SELECT invoice_file FROM claims WHERE id=?",
                         (claim_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Claim not found")
        # Collect every on-disk filename owned by this claim, then unlink.
        # Done before the DB row so we don't lose the names to cascade-delete.
        names = []
        if row["invoice_file"]:
            names.append(row["invoice_file"])
        extra = db.execute(
            "SELECT filename FROM claim_files WHERE claim_id=?", (claim_id,)
        ).fetchall()
        names.extend(r["filename"] for r in extra)
        for n in names:
            path = INVOICE_DIR / n
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass  # already gone or locked; carry on
        db.execute("DELETE FROM claims WHERE id=?", (claim_id,))
        db.commit()
    return {"ok": True, "deleted": claim_id}


@app.get("/api/claims/{claim_id}/invoice")
def get_invoice(claim_id: int):
    with closing(get_db()) as db:
        row = db.execute("SELECT invoice_file FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row or not row["invoice_file"]:
        raise HTTPException(404, "No invoice on file")
    path = INVOICE_DIR / row["invoice_file"]
    if not path.exists():
        raise HTTPException(404, "Invoice file missing from disk")
    return FileResponse(path, filename=row["invoice_file"])


@app.post("/api/claims/{claim_id}/invoice")
async def upload_invoice(claim_id: int, file: UploadFile = File(...)):
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    with closing(get_db()) as db:
        if not db.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone():
            raise HTTPException(404, "Claim not found")
        stored = _store_invoice(claim_id, file)
        db.execute("UPDATE claims SET invoice_file=?, updated_at=? WHERE id=?",
                   (stored, now_iso(), claim_id))
        db.commit()
        return {"ok": True, "invoice_file": stored}


# ---------------------------------------------------------------------------
# Other-document attachments (kind='other' for now)
# ---------------------------------------------------------------------------

def _store_other_file(claim_id: int, upload: UploadFile) -> str:
    stored = f"{claim_id}_other_{_safe_name(upload.filename)}"
    # If a same-named file already exists (re-upload of identical name),
    # disambiguate with a counter so we don't overwrite an older attachment.
    base = INVOICE_DIR / stored
    if base.exists():
        stem, dot, ext = stored.partition(".")
        i = 2
        while (INVOICE_DIR / f"{stem}_{i}{('.' + ext) if ext else ''}").exists():
            i += 1
        stored = f"{stem}_{i}{('.' + ext) if ext else ''}"
    dest = INVOICE_DIR / stored
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return stored


@app.post("/api/claims/{claim_id}/files")
async def upload_claim_file(claim_id: int, file: UploadFile = File(...)):
    if not file or not file.filename:
        raise HTTPException(400, "No file uploaded")
    with closing(get_db()) as db:
        if not db.execute("SELECT 1 FROM claims WHERE id=?", (claim_id,)).fetchone():
            raise HTTPException(404, "Claim not found")
        stored = _store_other_file(claim_id, file)
        cur = db.execute(
            "INSERT INTO claim_files (claim_id, kind, filename, original_name, created_at) "
            "VALUES (?, 'other', ?, ?, ?)",
            (claim_id, stored, file.filename, now_iso()),
        )
        db.commit()
        return {"id": cur.lastrowid, "filename": stored, "original_name": file.filename}


@app.get("/api/claims/{claim_id}/files/{file_id}")
def get_claim_file(claim_id: int, file_id: int):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT filename, original_name FROM claim_files WHERE id=? AND claim_id=?",
            (file_id, claim_id),
        ).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    path = INVOICE_DIR / row["filename"]
    if not path.exists():
        raise HTTPException(404, "File missing from disk")
    return FileResponse(path, filename=row["original_name"])


@app.delete("/api/claims/{claim_id}/files/{file_id}")
def delete_claim_file(claim_id: int, file_id: int):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT filename FROM claim_files WHERE id=? AND claim_id=?",
            (file_id, claim_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "File not found")
        path = INVOICE_DIR / row["filename"]
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        db.execute("DELETE FROM claim_files WHERE id=?", (file_id,))
        db.commit()
    return {"ok": True, "deleted": file_id}


if __name__ == "__main__":
    import uvicorn
    print("\n  MedClaims running →  http://localhost:8765\n")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
