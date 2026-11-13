import sqlite3
import json
import io
import csv
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
import os

DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads.db")

def get_db_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS law_firms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        search_query TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT,
        relevance TEXT DEFAULT 'high',
        relevance_reason TEXT,
        phone TEXT,
        website TEXT,
        email TEXT,
        email_type TEXT,
        mx_valid INTEGER,
        secondary_emails TEXT,
        address TEXT,
        city TEXT,
        state TEXT,
        postal_code TEXT,
        google_maps_url TEXT,
        place_id TEXT UNIQUE,
        rating REAL,
        reviews_count INTEGER,
        enrichment_status TEXT DEFAULT 'pending',
        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS affiliated_people (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        firm_id INTEGER NOT NULL,
        run_id TEXT,
        name TEXT NOT NULL,
        title TEXT,
        seniority TEXT,
        email TEXT,
        linkedin_url TEXT,
        source TEXT DEFAULT 'website_directory',
        discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (firm_id) REFERENCES law_firms(id) ON DELETE CASCADE
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        market TEXT NOT NULL,
        requested_count INTEGER NOT NULL,
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP,
        end_state TEXT DEFAULT 'running',
        found_total INTEGER DEFAULT 0,
        new_count INTEGER DEFAULT 0,
        emails_found INTEGER DEFAULT 0
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS run_firms (
        run_id TEXT NOT NULL,
        firm_id INTEGER NOT NULL,
        was_new INTEGER DEFAULT 1,
        PRIMARY KEY (run_id, firm_id),
        FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE,
        FOREIGN KEY (firm_id) REFERENCES law_firms(id) ON DELETE CASCADE
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_run_firms_run ON run_firms(run_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_run_firms_firm ON run_firms(firm_id);")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS undo_snapshots (
        token TEXT PRIMARY KEY,
        action TEXT NOT NULL,
        run_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        data_json TEXT NOT NULL
    );
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_people_firm_id ON affiliated_people(firm_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_people_name ON affiliated_people(name);")

    # Apply automatic migration for any columns added in updates
    existing_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(law_firms)").fetchall()}
    columns_to_add = [
        ("run_id", "TEXT"),
        ("category", "TEXT"),
        ("relevance", "TEXT DEFAULT 'high'"),
        ("relevance_reason", "TEXT"),
        ("email_type", "TEXT"),
        ("mx_valid", "INTEGER"),
        ("city", "TEXT"),
        ("state", "TEXT"),
        ("postal_code", "TEXT"),
        ("contact_form_url", "TEXT"),
        ("source_url", "TEXT")
    ]
    for col_name, col_type in columns_to_add:
        if col_name not in existing_cols:
            try:
                cursor.execute(f"ALTER TABLE law_firms ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

    existing_people_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(affiliated_people)").fetchall()}
    people_columns_to_add = [
        ("is_direct_linkedin", "INTEGER DEFAULT 0"),
        ("source_url", "TEXT"),
        ("profile_url", "TEXT"),
        ("source_layer", "TEXT DEFAULT 'other'"),
        ("confidence", "TEXT DEFAULT 'high'"),
        ("score", "INTEGER DEFAULT 0"),
        ("evidence", "TEXT")
    ]
    for col_name, col_type in people_columns_to_add:
        if col_name not in existing_people_cols:
            try:
                cursor.execute(f"ALTER TABLE affiliated_people ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

    existing_run_cols = {row["name"] for row in cursor.execute("PRAGMA table_info(runs)").fetchall()}
    if "session_id" not in existing_run_cols:
        try:
            cursor.execute("ALTER TABLE runs ADD COLUMN session_id TEXT;")
        except Exception:
            pass

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_firm_name ON law_firms(name);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_firm_email ON law_firms(email);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_firm_run_id ON law_firms(run_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_firm_place_id ON law_firms(place_id);")

    # Backfill runs and run_firms from existing data if empty
    cursor.execute("""
        INSERT OR IGNORE INTO run_firms (run_id, firm_id, was_new)
        SELECT run_id, id, 1 FROM law_firms WHERE run_id IS NOT NULL AND run_id != ''
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO runs (id, market, requested_count, started_at, end_state, found_total)
        SELECT run_id, COALESCE(city, 'Directory Market'), COUNT(*), MIN(scraped_at), 'completed', COUNT(*)
        FROM law_firms
        WHERE run_id IS NOT NULL AND run_id != ''
        GROUP BY run_id
    """)

    cursor.execute("SELECT value FROM app_state WHERE key = 'latest_run_id'")
    if not cursor.fetchone():
        cursor.execute("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
        latest = cursor.fetchone()
        if latest:
            cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (latest[0],))

    conn.commit()
    conn.close()

def upsert_law_firm(firm_data: Dict[str, Any], current_run_id: str) -> Tuple[int, bool]:
    """
    Insert or update a law firm record.
    Returns (record_id, is_already_in_db).
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    sec_emails_json = json.dumps(firm_data.get("secondary_emails") or [])
    place_id = firm_data.get("place_id") or None
    name = firm_data.get("name") or "Unknown Firm"

    existing_id = None
    if place_id:
        cursor.execute("SELECT id FROM law_firms WHERE place_id = ?", (place_id,))
        row = cursor.fetchone()
        if row:
            existing_id = row["id"]

    if not existing_id and firm_data.get("phone"):
        cursor.execute("SELECT id FROM law_firms WHERE name = ? AND phone = ?", (name, firm_data.get("phone")))
        row = cursor.fetchone()
        if row:
            existing_id = row["id"]

    if existing_id:
        cursor.execute("""
            UPDATE law_firms SET
                run_id = ?,
                search_query = COALESCE(?, search_query),
                name = COALESCE(?, name),
                category = COALESCE(?, category),
                relevance = COALESCE(?, relevance),
                relevance_reason = COALESCE(?, relevance_reason),
                phone = COALESCE(?, phone),
                website = COALESCE(?, website),
                address = COALESCE(?, address),
                city = COALESCE(?, city),
                state = COALESCE(?, state),
                postal_code = COALESCE(?, postal_code),
                google_maps_url = COALESCE(?, google_maps_url),
                rating = COALESCE(?, rating),
                reviews_count = COALESCE(?, reviews_count)
            WHERE id = ?
        """, (
            current_run_id,
            firm_data.get("search_query"),
            name,
            firm_data.get("category"),
            firm_data.get("relevance", "high"),
            firm_data.get("relevance_reason"),
            firm_data.get("phone"),
            firm_data.get("website"),
            firm_data.get("address"),
            firm_data.get("city"),
            firm_data.get("state"),
            firm_data.get("postal_code"),
            firm_data.get("google_maps_url"),
            firm_data.get("rating"),
            firm_data.get("reviews_count"),
            existing_id
        ))
        conn.commit()
        if current_run_id:
            cursor.execute("""
                INSERT OR REPLACE INTO run_firms (run_id, firm_id, was_new)
                VALUES (?, ?, 0)
            """, (current_run_id, existing_id))
            conn.commit()
        conn.close()
        return existing_id, True
    else:
        cursor.execute("""
            INSERT INTO law_firms (
                run_id, search_query, name, category, relevance, relevance_reason,
                phone, website, email, email_type, mx_valid, secondary_emails,
                address, city, state, postal_code, google_maps_url, place_id,
                rating, reviews_count, enrichment_status, scraped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            current_run_id,
            firm_data.get("search_query", "Law firms"),
            name,
            firm_data.get("category", "Directory Listing"),
            firm_data.get("relevance", "high"),
            firm_data.get("relevance_reason", ""),
            firm_data.get("phone"),
            firm_data.get("website"),
            firm_data.get("email"),
            firm_data.get("email_type"),
            1 if firm_data.get("mx_valid") else (0 if firm_data.get("mx_valid") is False else None),
            sec_emails_json,
            firm_data.get("address"),
            firm_data.get("city"),
            firm_data.get("state"),
            firm_data.get("postal_code"),
            firm_data.get("google_maps_url"),
            place_id,
            firm_data.get("rating"),
            firm_data.get("reviews_count"),
            firm_data.get("enrichment_status", "pending"),
            datetime.utcnow().isoformat()
        ))
        conn.commit()
        record_id = cursor.lastrowid
        if current_run_id:
            cursor.execute("""
                INSERT OR REPLACE INTO run_firms (run_id, firm_id, was_new)
                VALUES (?, ?, 1)
            """, (current_run_id, record_id))
            conn.commit()
        conn.close()
        return record_id, False

def insert_affiliated_person(
    firm_id: int,
    run_id: str,
    name: str,
    title: Optional[str] = None,
    seniority: Optional[str] = None,
    email: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    source: str = "website_directory",
    is_direct_linkedin: bool = False,
    source_url: Optional[str] = None,
    profile_url: Optional[str] = None,
    source_layer: str = "other",
    confidence: str = "high",
    score: int = 0,
    evidence: Optional[str] = None
) -> int:
    """Inserts a person associated with a firm, deduplicating by firm_id and name."""
    conn = get_db_connection()
    cursor = conn.cursor()
    direct_li_int = 1 if is_direct_linkedin else 0

    cursor.execute("SELECT id FROM affiliated_people WHERE firm_id = ? AND name = ?", (firm_id, name))
    existing = cursor.fetchone()
    if existing:
        person_id = existing["id"]
        cursor.execute("""
            UPDATE affiliated_people SET
                title = COALESCE(?, title),
                seniority = COALESCE(?, seniority),
                email = COALESCE(?, email),
                linkedin_url = COALESCE(?, linkedin_url),
                is_direct_linkedin = ?,
                source_url = COALESCE(?, source_url),
                profile_url = COALESCE(?, profile_url),
                source_layer = COALESCE(?, source_layer),
                confidence = COALESCE(?, confidence),
                score = CASE WHEN ? > 0 THEN ? ELSE score END,
                evidence = COALESCE(?, evidence),
                run_id = ?
            WHERE id = ?
        """, (
            title, seniority, email, linkedin_url, direct_li_int,
            source_url, profile_url, source_layer, confidence, score, score, evidence,
            run_id, person_id
        ))
    else:
        cursor.execute("""
            INSERT INTO affiliated_people (
                firm_id, run_id, name, title, seniority, email, linkedin_url,
                source, is_direct_linkedin, source_url, profile_url,
                source_layer, confidence, score, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            firm_id, run_id, name, title, seniority, email, linkedin_url,
            source, direct_li_int, source_url, profile_url,
            source_layer, confidence, score, evidence
        ))
        person_id = cursor.lastrowid

    conn.commit()
    conn.close()
    return person_id

def get_firm_people(firm_id: int) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM affiliated_people WHERE firm_id = ? ORDER BY id ASC", (firm_id,))
    rows = cursor.fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return results

def delete_firm(firm_id: int) -> bool:
    """Deletes a firm and cascade deletes its affiliated people."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM affiliated_people WHERE firm_id = ?", (firm_id,))
    cursor.execute("DELETE FROM law_firms WHERE id = ?", (firm_id,))
    conn.commit()
    conn.close()
    return True

def delete_person(person_id: int) -> bool:
    """Deletes an individual person record."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM affiliated_people WHERE id = ?", (person_id,))
    conn.commit()
    conn.close()
    return True

def update_firm_enrichment(
    record_id: int,
    email: Optional[str],
    email_type: Optional[str],
    mx_valid: Optional[bool],
    secondary_emails: List[Dict[str, Any]],
    status: str,
    resolved_website: Optional[str] = None,
    contact_form_url: Optional[str] = None
):
    conn = get_db_connection()
    cursor = conn.cursor()
    mx_int = 1 if mx_valid else (0 if mx_valid is False else None)
    if resolved_website:
        cursor.execute("""
            UPDATE law_firms SET
                email = ?,
                email_type = ?,
                mx_valid = ?,
                secondary_emails = ?,
                enrichment_status = ?,
                website = ?,
                contact_form_url = ?
            WHERE id = ?
        """, (email, email_type, mx_int, json.dumps(secondary_emails), status, resolved_website, contact_form_url, record_id))
    else:
        cursor.execute("""
            UPDATE law_firms SET
                email = ?,
                email_type = ?,
                mx_valid = ?,
                secondary_emails = ?,
                enrichment_status = ?,
                contact_form_url = ?
            WHERE id = ?
        """, (email, email_type, mx_int, json.dumps(secondary_emails), status, contact_form_url, record_id))
    conn.commit()
    conn.close()

def get_existing_firm_enrichment(record_id: int) -> Optional[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    """Returns cached enrichment data and practitioners if already resolved."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT email, email_type, mx_valid, secondary_emails, enrichment_status, website, contact_form_url
        FROM law_firms WHERE id = ?
    """, (record_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    d = dict(row)
    if not d.get("enrichment_status") or d["enrichment_status"] == "pending":
        conn.close()
        return None

    try:
        d["secondary_emails"] = json.loads(d["secondary_emails"]) if d.get("secondary_emails") else []
    except Exception:
        d["secondary_emails"] = []
    d["mx_valid"] = bool(d["mx_valid"]) if d.get("mx_valid") is not None else None

    cursor.execute("SELECT * FROM affiliated_people WHERE firm_id = ? ORDER BY id ASC", (record_id,))
    people = [dict(p) for p in cursor.fetchall()]
    conn.close()
    return d, people

def create_run(run_id: str, market: str, requested_count: int, session_id: Optional[str] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO runs (id, market, requested_count, session_id, started_at, end_state)
        VALUES (?, ?, ?, ?, ?, 'running')
    """, (run_id, market, requested_count, session_id, datetime.utcnow().isoformat()))
    cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (run_id,))
    if session_id:
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (f"latest_run_{session_id}", run_id))
    conn.commit()
    conn.close()

def finish_run(run_id: str, end_state: str, found_total: int, new_count: int, emails_found: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE runs SET
            ended_at = ?,
            end_state = ?,
            found_total = ?,
            new_count = ?,
            emails_found = ?
        WHERE id = ?
    """, (datetime.utcnow().isoformat(), end_state, found_total, new_count, emails_found, run_id))
    conn.commit()
    conn.close()

def record_run_firm(run_id: str, firm_id: int, was_new: bool):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO run_firms (run_id, firm_id, was_new)
        VALUES (?, ?, ?)
    """, (run_id, firm_id, 1 if was_new else 0))
    conn.commit()
    conn.close()

def get_latest_run_info(session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()

    latest_id = None
    if session_id:
        cursor.execute("SELECT value FROM app_state WHERE key = ?", (f"latest_run_{session_id}",))
        row = cursor.fetchone()
        if row:
            latest_id = row["value"]
        else:
            cursor.execute("SELECT id FROM runs WHERE session_id = ? ORDER BY started_at DESC LIMIT 1", (session_id,))
            s_row = cursor.fetchone()
            if s_row:
                latest_id = s_row["id"]
            else:
                cursor.execute("SELECT value FROM app_state WHERE key = 'latest_run_id'")
                g_row = cursor.fetchone()
                if g_row and g_row["value"]:
                    latest_id = g_row["value"]
                else:
                    cursor.execute("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
                    r_row = cursor.fetchone()
                    if r_row:
                        latest_id = r_row["id"]
                    else:
                        conn.close()
                        return None
    else:
        cursor.execute("SELECT value FROM app_state WHERE key = 'latest_run_id'")
        row = cursor.fetchone()
        latest_id = row["value"] if row else None

    if latest_id == "empty_session" or not latest_id:
        conn.close()
        return None

    cursor.execute("SELECT * FROM runs WHERE id = ?", (latest_id,))
    run_row = cursor.fetchone()
    if not run_row:
        conn.close()
        return None

    run_dict = dict(run_row)
    conn.close()
    run_dict["stats"] = get_db_stats(run_id=latest_id, session_id=session_id)
    return run_dict

def set_latest_run_id(run_id: str, session_id: Optional[str] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (run_id,))
    if session_id:
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, ?)", (f"latest_run_{session_id}", run_id))
    conn.commit()
    conn.close()

def reset_session(session_id: Optional[str] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', 'empty_session')")
    if session_id:
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES (?, 'empty_session')", (f"latest_run_{session_id}",))
    conn.commit()
    conn.close()
    conn.commit()
    conn.close()

import uuid

def clear_latest_run(run_id: str) -> Tuple[int, str]:
    """
    Clears run_firms links for run_id.
    Deletes any law_firms that belong to no other run.
    Stores snapshot for 10s undo.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT firm_id, was_new FROM run_firms WHERE run_id = ?", (run_id,))
    linked = cursor.fetchall()
    if not linked:
        conn.close()
        return 0, ""

    firm_ids = [r["firm_id"] for r in linked]
    placeholders = ','.join(['?'] * len(firm_ids))

    cursor.execute(f"SELECT * FROM law_firms WHERE id IN ({placeholders})", firm_ids)
    snapshot_firms = [dict(f) for f in cursor.fetchall()]

    cursor.execute(f"SELECT * FROM affiliated_people WHERE firm_id IN ({placeholders})", firm_ids)
    snapshot_people = [dict(p) for p in cursor.fetchall()]

    cursor.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
    run_row = cursor.fetchone()
    snapshot_run = dict(run_row) if run_row else None

    token = str(uuid.uuid4())[:12]
    snapshot_data = {
        "run_id": run_id,
        "run": snapshot_run,
        "run_firms": [dict(r) for r in linked],
        "firms": snapshot_firms,
        "people": snapshot_people
    }

    cursor.execute("""
        INSERT INTO undo_snapshots (token, action, run_id, data_json)
        VALUES (?, 'clear_run', ?, ?)
    """, (token, run_id, json.dumps(snapshot_data)))

    cursor.execute("DELETE FROM run_firms WHERE run_id = ?", (run_id,))

    # Delete firms that belong to NO other run_firms
    cursor.execute(f"""
        DELETE FROM law_firms
        WHERE id IN ({placeholders})
        AND id NOT IN (SELECT DISTINCT firm_id FROM run_firms)
    """, firm_ids)

    cursor.execute("SELECT id FROM runs WHERE id != ? ORDER BY started_at DESC LIMIT 1", (run_id,))
    next_run = cursor.fetchone()
    if next_run:
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (next_run["id"],))
    else:
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', 'empty_session')")

    conn.commit()
    conn.close()
    return len(firm_ids), token

def delete_everything() -> Tuple[int, str]:
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM law_firms")
    all_firms = [dict(f) for f in cursor.fetchall()]

    cursor.execute("SELECT * FROM affiliated_people")
    all_people = [dict(p) for p in cursor.fetchall()]

    cursor.execute("SELECT * FROM runs")
    all_runs = [dict(r) for r in cursor.fetchall()]

    cursor.execute("SELECT * FROM run_firms")
    all_rf = [dict(rf) for rf in cursor.fetchall()]

    token = str(uuid.uuid4())[:12]
    snapshot_data = {
        "runs": all_runs,
        "run_firms": all_rf,
        "firms": all_firms,
        "people": all_people
    }

    cursor.execute("""
        INSERT INTO undo_snapshots (token, action, run_id, data_json)
        VALUES (?, 'delete_everything', NULL, ?)
    """, (token, json.dumps(snapshot_data)))

    cursor.execute("DELETE FROM run_firms")
    cursor.execute("DELETE FROM affiliated_people")
    cursor.execute("DELETE FROM law_firms")
    cursor.execute("DELETE FROM runs")
    cursor.execute("DELETE FROM app_state WHERE key LIKE 'latest_run_%'")
    cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', 'empty_session')")

    conn.commit()
    conn.close()
    return len(all_firms), token

def undo_action(token: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM undo_snapshots WHERE token = ?", (token,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False

    snapshot = json.loads(row["data_json"])
    action = row["action"]

    if action == "clear_run":
        for f in snapshot.get("firms", []):
            cols = list(f.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO law_firms ({','.join(cols)}) VALUES ({placeholders})", [f[c] for c in cols])
        for p in snapshot.get("people", []):
            cols = list(p.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO affiliated_people ({','.join(cols)}) VALUES ({placeholders})", [p[c] for c in cols])
        for rf in snapshot.get("run_firms", []):
            cursor.execute("INSERT OR REPLACE INTO run_firms (run_id, firm_id, was_new) VALUES (?, ?, ?)", (rf["run_id"], rf["firm_id"], rf["was_new"]))
        if snapshot.get("run"):
            r = snapshot["run"]
            cols = list(r.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO runs ({','.join(cols)}) VALUES ({placeholders})", [r[c] for c in cols])
        cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (snapshot["run_id"],))

    elif action == "delete_everything":
        for r in snapshot.get("runs", []):
            cols = list(r.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO runs ({','.join(cols)}) VALUES ({placeholders})", [r[c] for c in cols])
        for f in snapshot.get("firms", []):
            cols = list(f.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO law_firms ({','.join(cols)}) VALUES ({placeholders})", [f[c] for c in cols])
        for p in snapshot.get("people", []):
            cols = list(p.keys())
            placeholders = ','.join(['?'] * len(cols))
            cursor.execute(f"INSERT OR REPLACE INTO affiliated_people ({','.join(cols)}) VALUES ({placeholders})", [p[c] for c in cols])
        for rf in snapshot.get("run_firms", []):
            cursor.execute("INSERT OR REPLACE INTO run_firms (run_id, firm_id, was_new) VALUES (?, ?, ?)", (rf["run_id"], rf["firm_id"], rf["was_new"]))
        if snapshot.get("runs"):
            latest = sorted(snapshot["runs"], key=lambda x: x.get("started_at", ""), reverse=True)[0]
            cursor.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('latest_run_id', ?)", (latest["id"],))

    cursor.execute("DELETE FROM undo_snapshots WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return True

def purge_undo_snapshot(token: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM undo_snapshots WHERE token = ?", (token,))
    conn.commit()
    conn.close()

def get_law_firms(
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    search: Optional[str] = None,
    only_with_email: bool = False,
    hide_low_relevance: bool = False,
    limit: int = 500,
    offset: int = 0
) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()

    conditions = []
    params: List[Any] = []

    if only_with_email:
        conditions.append("(lf.email IS NOT NULL AND lf.email != '')")

    if hide_low_relevance:
        conditions.append("lf.relevance != 'low'")

    if search:
        search_pattern = f"%{search}%"
        conditions.append("(lf.name LIKE ? OR lf.phone LIKE ? OR lf.email LIKE ? OR lf.address LIKE ? OR lf.city LIKE ? OR lf.website LIKE ? OR lf.category LIKE ?)")
        params.extend([search_pattern, search_pattern, search_pattern, search_pattern, search_pattern, search_pattern, search_pattern])

    if run_id and run_id != "all" and run_id != "empty_session":
        cond_sql = (" AND " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT lf.*, rf.was_new
            FROM law_firms lf
            INNER JOIN run_firms rf ON lf.id = rf.firm_id
            WHERE rf.run_id = ?
            {cond_sql}
            ORDER BY lf.id DESC
            LIMIT ? OFFSET ?
        """
        all_params = [run_id] + params + [limit, offset]
        cursor.execute(sql, all_params)
    elif run_id == "empty_session":
        conn.close()
        return []
    else:
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"""
            SELECT lf.*, NULL as was_new
            FROM law_firms lf
            {where_clause}
            ORDER BY lf.id DESC
            LIMIT ? OFFSET ?
        """
        all_params = params + [limit, offset]
        cursor.execute(sql, all_params)

    rows = cursor.fetchall()
    results = []
    firm_ids = []
    for r in rows:
        d = dict(r)
        try:
            d["secondary_emails"] = json.loads(d["secondary_emails"]) if d.get("secondary_emails") else []
        except Exception:
            d["secondary_emails"] = []
        d["mx_valid"] = bool(d["mx_valid"]) if d.get("mx_valid") is not None else None
        d["was_new"] = bool(d["was_new"]) if d.get("was_new") is not None else None
        d["people"] = []
        results.append(d)
        firm_ids.append(d["id"])

    # Load associated practitioners in batch
    if firm_ids:
        placeholders = ','.join(['?'] * len(firm_ids))
        cursor.execute(f"SELECT * FROM affiliated_people WHERE firm_id IN ({placeholders}) ORDER BY id ASC", firm_ids)
        people_rows = cursor.fetchall()
        people_by_firm = {}
        for p in people_rows:
            p_dict = dict(p)
            f_id = p_dict["firm_id"]
            if f_id not in people_by_firm:
                people_by_firm[f_id] = []
            people_by_firm[f_id].append(p_dict)

        for res in results:
            res["people"] = people_by_firm.get(res["id"], [])

    conn.close()
    return results

def get_db_stats(run_id: Optional[str] = None, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Calculates statistics for a specific run_id or all-time."""
    conn = get_db_connection()
    cursor = conn.cursor()

    if run_id and run_id != "all" and run_id != "empty_session":
        base_from = "FROM law_firms lf INNER JOIN run_firms rf ON lf.id = rf.firm_id WHERE rf.run_id = ?"
        params = [run_id]

        cursor.execute(f"SELECT COUNT(*) {base_from}", params)
        total_leads = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND lf.email IS NOT NULL AND lf.email != ''", params)
        with_email = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND lf.phone IS NOT NULL AND lf.phone != ''", params)
        with_phone = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND lf.website IS NOT NULL AND lf.website != ''", params)
        with_website = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND lf.relevance != 'low'", params)
        high_rel = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND lf.relevance = 'low'", params)
        low_rel = cursor.fetchone()[0]

        cursor.execute(f"SELECT COUNT(*) {base_from} AND rf.was_new = 1", params)
        new_count = cursor.fetchone()[0]
    elif run_id == "empty_session":
        total_leads = 0
        with_email = 0
        with_phone = 0
        with_website = 0
        high_rel = 0
        low_rel = 0
        new_count = 0
    else:
        total_leads = cursor.execute("SELECT COUNT(*) FROM law_firms").fetchone()[0]
        with_email = cursor.execute("SELECT COUNT(*) FROM law_firms WHERE email IS NOT NULL AND email != ''").fetchone()[0]
        with_phone = cursor.execute("SELECT COUNT(*) FROM law_firms WHERE phone IS NOT NULL AND phone != ''").fetchone()[0]
        with_website = cursor.execute("SELECT COUNT(*) FROM law_firms WHERE website IS NOT NULL AND website != ''").fetchone()[0]
        high_rel = cursor.execute("SELECT COUNT(*) FROM law_firms WHERE relevance != 'low'").fetchone()[0]
        low_rel = cursor.execute("SELECT COUNT(*) FROM law_firms WHERE relevance = 'low'").fetchone()[0]
        new_count = total_leads

    conn.close()

    rate = round((with_email / total_leads * 100), 1) if total_leads > 0 else 0.0

    return {
        "scope": run_id if (run_id and run_id != "all" and run_id != "empty_session") else ("empty_session" if run_id == "empty_session" else "all_time"),
        "total_leads": total_leads,
        "with_email": with_email,
        "with_phone": with_phone,
        "with_website": with_website,
        "email_success_rate": rate,
        "high_relevance_count": high_rel,
        "low_relevance_count": low_rel,
        "new_count": new_count
    }

def export_leads_csv(
    run_id: Optional[str] = None,
    only_with_email: bool = False,
    hide_low_relevance: bool = False
) -> str:
    leads = get_law_firms(
        run_id=run_id,
        only_with_email=only_with_email,
        hide_low_relevance=hide_low_relevance,
        limit=50000
    )
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID", "Firm Name", "Primary Email", "Email Classification", "Domain Accepts Mail (MX)",
        "Phone", "Website", "Category", "Relevance", "Relevance Reason",
        "Street Address", "City", "State", "Postal Code", "Rating", "Reviews Count",
        "Enrichment Status", "Google Maps URL", "Run ID", "Search Query", "Cataloged At"
    ])

    for row in leads:
        sec_emails_formatted = "; ".join([e.get("email", "") for e in (row.get("secondary_emails") or []) if isinstance(e, dict)])
        mx_text = "Yes" if row.get("mx_valid") is True else ("No" if row.get("mx_valid") is False else "N/A")

        writer.writerow([
            row.get("id"),
            row.get("name"),
            row.get("email") or "",
            row.get("email_type") or "",
            mx_text,
            row.get("phone") or "",
            row.get("website") or "",
            row.get("category") or "",
            row.get("relevance") or "",
            row.get("relevance_reason") or "",
            row.get("address") or "",
            row.get("city") or "",
            row.get("state") or "",
            row.get("postal_code") or "",
            row.get("rating") or "",
            row.get("reviews_count") or "",
            row.get("enrichment_status") or "",
            row.get("google_maps_url") or "",
            row.get("run_id") or "",
            row.get("search_query") or "",
            row.get("scraped_at") or ""
        ])

    return output.getvalue()

def export_contacts_csv(run_id: Optional[str] = None) -> str:
    """Exports each affiliated practitioner / partner as an individual row."""
    conn = get_db_connection()
    cursor = conn.cursor()

    if run_id and run_id != "all" and run_id != "empty_session":
        sql = """
            SELECT
                p.id, p.name, p.title, p.seniority, p.email, p.linkedin_url, p.source,
                f.name AS firm_name, f.phone AS firm_phone, f.website AS firm_website,
                f.city AS firm_city, f.state AS firm_state
            FROM affiliated_people p
            JOIN law_firms f ON p.firm_id = f.id
            JOIN run_firms rf ON f.id = rf.firm_id
            WHERE rf.run_id = ?
            ORDER BY f.name ASC, p.id ASC
        """
        params = [run_id]
    elif run_id == "empty_session":
        conn.close()
        return ""
    else:
        sql = """
            SELECT
                p.id, p.name, p.title, p.seniority, p.email, p.linkedin_url, p.source,
                f.name AS firm_name, f.phone AS firm_phone, f.website AS firm_website,
                f.city AS firm_city, f.state AS firm_state
            FROM affiliated_people p
            JOIN law_firms f ON p.firm_id = f.id
            ORDER BY f.name ASC, p.id ASC
        """
        params = []
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Contact ID", "Person Name", "Title / Role", "Seniority", "Direct Email",
        "LinkedIn Profile URL", "Firm Name", "Firm Phone", "Firm Website", "Market City", "Market State"
    ])

    for r in rows:
        writer.writerow([
            r["id"],
            r["name"],
            r["title"] or "",
            r["seniority"] or "",
            r["email"] or "",
            r["linkedin_url"] or "",
            r["firm_name"],
            r["firm_phone"] or "",
            r["firm_website"] or "",
            r["firm_city"] or "",
            r["firm_state"] or ""
        ])

    return output.getvalue()
