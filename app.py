# ============================================================
# BINGO CASSA — SQLite (local cache) + TURSO (master) + sync on write
# - Ogni bottone che scrive -> write_and_sync(...)
# - ID UUID (TEXT) + updated_at (epoch ms) + deleted (soft delete)
# - Migrazione automatica da vecchio schema con INTEGER AUTOINCREMENT
# ============================================================

from __future__ import annotations

import time
import uuid
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Tuple, Optional, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ===========================
# PAGE CONFIG (SOLO UNA VOLTA)
# ===========================
st.set_page_config(page_title="BINGO CASSA", layout="wide")

# ===========================
# AUTH
# ===========================
SESSION_TTL_SECONDS = 60 * 60 * 6  # 6 ore

def require_password():
    expected = st.secrets.get("APP_PASSWORD", "")
    if not expected:
        st.error("❌ APP_PASSWORD mancante. Impostalo in Settings → Secrets.")
        st.stop()

    now_ts = time.time()
    expires_at = st.session_state.get("auth_expires_at", 0.0)
    if expires_at and now_ts < expires_at:
        return

    st.markdown("## 🔒 Bingo Cassa – Accesso riservato")
    pwd = st.text_input("Password", type="password")
    if not pwd:
        st.stop()

    if pwd != expected:
        st.error("Password errata")
        st.stop()

    st.session_state["auth_expires_at"] = now_ts + SESSION_TTL_SECONDS
    st.rerun()

require_password()

# ===========================
# CONFIG
# ===========================
DB_PATH = "incassi_app.sqlite3"  # locale (cache) — su Community Cloud può essere effimero
TZ = ZoneInfo("Europe/Zurich")
CUTOFF_HOUR = 12

TURSO_URL = st.secrets.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = st.secrets.get("TURSO_AUTH_TOKEN", "")

if not TURSO_URL or not TURSO_TOKEN:
    st.error("❌ TURSO_DATABASE_URL / TURSO_AUTH_TOKEN mancanti in Secrets.")
    st.stop()

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

def now_ms() -> int:
    return int(time.time() * 1000)

# ===========================
# TURSO ENGINE
# ===========================
@st.cache_resource
def turso_engine():
    # sqlalchemy-libsql
    return create_engine(
        "sqlite+libsql://",
        connect_args={"url": TURSO_URL, "auth_token": TURSO_TOKEN},
        future=True,
        pool_pre_ping=True,
    )

# ===========================
# LOCAL SQLITE CONN
# ===========================
@st.cache_resource
def local_conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA foreign_keys = ON;")
    c.execute("PRAGMA journal_mode = WAL;")
    return c

# ===========================
# BUSINESS DAY
# ===========================
def business_day_for(dt: datetime, cutoff_hour: int = CUTOFF_HOUR) -> date:
    local_dt = dt.astimezone(TZ)
    return (local_dt.date() - timedelta(days=1)) if local_dt.hour < cutoff_hour else local_dt.date()

def business_day_range(day_: date, cutoff_hour: int = CUTOFF_HOUR) -> Tuple[datetime, datetime]:
    start = datetime.combine(day_, dtime(hour=cutoff_hour), tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

def fmt_time_from_iso(iso_str: str) -> str:
    if not iso_str:
        return ""
    if len(iso_str) >= 19 and "T" in iso_str:
        return iso_str[11:19]
    return iso_str[-8:]

# ===========================
# SCHEMA (NUOVO)
# ===========================
LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS waiters (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  waiter_id TEXT NOT NULL,
  amount REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  settled INTEGER NOT NULL DEFAULT 0,
  voided INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (waiter_id) REFERENCES waiters(id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_day_waiter ON transactions(day, waiter_id);
CREATE INDEX IF NOT EXISTS idx_transactions_updated_at ON transactions(updated_at);

CREATE TABLE IF NOT EXISTS shift_waiters (
  day TEXT NOT NULL,
  waiter_id TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, waiter_id),
  FOREIGN KEY (waiter_id) REFERENCES waiters(id)
);
"""

TURSO_SCHEMA = LOCAL_SCHEMA + """
CREATE TABLE IF NOT EXISTS sync_state (
  k TEXT PRIMARY KEY,
  v INTEGER NOT NULL
);
"""

def exec_many_sqlite(c: sqlite3.Connection, sql: str):
    cur = c.cursor()
    for stmt in sql.strip().split(";"):
        s = stmt.strip()
        if s:
            cur.execute(s)
    c.commit()

def exec_many_turso(engine, sql: str):
    with engine.begin() as con:
        for stmt in sql.strip().split(";"):
            s = stmt.strip()
            if s:
                con.execute(text(s))

# ===========================
# MIGRAZIONE (vecchio schema INTEGER -> nuovo UUID)
# ===========================
def table_has_column_sqlite(c: sqlite3.Connection, table: str, col: str) -> bool:
    cur = c.cursor()
    cur.execute(f"PRAGMA table_info({table});")
    return any(r[1] == col for r in cur.fetchall())

def table_exists_sqlite(c: sqlite3.Connection, table: str) -> bool:
    cur = c.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?;", (table,))
    return cur.fetchone() is not None

def migrate_if_needed(c: sqlite3.Connection):
    # Se esistono tabelle ma NON hanno id TEXT (nuovo schema), migro.
    if not table_exists_sqlite(c, "waiters"):
        return

    # vecchio schema: waiters.id INTEGER e NON esiste updated_at
    is_old = not table_has_column_sqlite(c, "waiters", "updated_at") or table_has_column_sqlite(c, "waiters", "active") and not table_has_column_sqlite(c, "waiters", "deleted")
    if not is_old:
        return

    cur = c.cursor()

    # Backup tables
    cur.execute("ALTER TABLE waiters RENAME TO waiters_old;")
    cur.execute("ALTER TABLE transactions RENAME TO transactions_old;")
    if table_exists_sqlite(c, "shift_waiters"):
        cur.execute("ALTER TABLE shift_waiters RENAME TO shift_waiters_old;")

    c.commit()

    # Create new schema
    exec_many_sqlite(c, LOCAL_SCHEMA)

    # Map old waiter int -> new uuid
    cur.execute("SELECT id, name, active, created_at FROM waiters_old;")
    rows = cur.fetchall()
    waiter_map: Dict[int, str] = {}
    for wid_int, name, active, created_at in rows:
        new_id = str(uuid.uuid4())
        waiter_map[int(wid_int)] = new_id
        cur.execute(
            "INSERT INTO waiters(id,name,active,created_at,updated_at,deleted) VALUES (?,?,?,?,?,0);",
            (new_id, name, int(active), created_at or now_iso(), now_ms())
        )

    # Transactions migrate
    # vecchio schema potrebbe avere settled/voided già aggiunti
    cols = [r[1] for r in cur.execute("PRAGMA table_info(transactions_old);").fetchall()]
    has_settled = "settled" in cols
    has_voided = "voided" in cols

    q = "SELECT id, day, waiter_id, amount, created_at"
    if has_settled: q += ", settled"
    else: q += ", 0 as settled"
    if has_voided: q += ", voided"
    else: q += ", 0 as voided"
    q += " FROM transactions_old;"
    cur.execute(q)
    tx_rows = cur.fetchall()

    for _id_int, day, waiter_id_int, amount, created_at, settled, voided in tx_rows:
        new_tx_id = str(uuid.uuid4())
        new_waiter_id = waiter_map.get(int(waiter_id_int))
        if not new_waiter_id:
            continue
        cur.execute(
            """INSERT INTO transactions
               (id, day, waiter_id, amount, created_at, updated_at, settled, voided, deleted)
               VALUES (?,?,?,?,?,?,?,?,0);""",
            (new_tx_id, day, new_waiter_id, float(amount), created_at or now_iso(), now_ms(), int(settled), int(voided))
        )

    # shift_waiters migrate (se esisteva)
    if table_exists_sqlite(c, "shift_waiters_old"):
        cur.execute("SELECT day, waiter_id FROM shift_waiters_old;")
        for day, wid_int in cur.fetchall():
            new_waiter_id = waiter_map.get(int(wid_int))
            if new_waiter_id:
                cur.execute(
                    "INSERT OR REPLACE INTO shift_waiters(day,waiter_id,updated_at,deleted) VALUES (?,?,?,0);",
                    (day, new_waiter_id, now_ms())
                )

    c.commit()

# ===========================
# INIT DB
# ===========================
def init_dbs():
    lc = local_conn()
    migrate_if_needed(lc)
    exec_many_sqlite(lc, LOCAL_SCHEMA)
    exec_many_turso(turso_engine(), TURSO_SCHEMA)

init_dbs()

# ===========================
# SYNC STATE (su TURSO)
# ===========================
def get_sync_ts(key: str) -> int:
    eng = turso_engine()
    with eng.begin() as con:
        row = con.execute(text("SELECT v FROM sync_state WHERE k=:k;"), {"k": key}).fetchone()
        if row:
            return int(row[0])
        con.execute(text("INSERT INTO sync_state(k,v) VALUES(:k,0);"), {"k": key})
        return 0

def set_sync_ts(key: str, v: int):
    eng = turso_engine()
    with eng.begin() as con:
        con.execute(text("""
            INSERT INTO sync_state(k,v) VALUES(:k,:v)
            ON CONFLICT(k) DO UPDATE SET v=excluded.v;
        """), {"k": key, "v": int(v)})

# ===========================
# SYNC (PUSH/PULL)
# ===========================
UPSERT_WAITER = """
INSERT INTO waiters (id,name,active,created_at,updated_at,deleted)
VALUES (:id,:name,:active,:created_at,:updated_at,:deleted)
ON CONFLICT(id) DO UPDATE SET
  name=excluded.name,
  active=excluded.active,
  created_at=excluded.created_at,
  updated_at=excluded.updated_at,
  deleted=excluded.deleted
WHERE excluded.updated_at >= waiters.updated_at;
"""

UPSERT_TX = """
INSERT INTO transactions (id,day,waiter_id,amount,created_at,updated_at,settled,voided,deleted)
VALUES (:id,:day,:waiter_id,:amount,:created_at,:updated_at,:settled,:voided,:deleted)
ON CONFLICT(id) DO UPDATE SET
  day=excluded.day,
  waiter_id=excluded.waiter_id,
  amount=excluded.amount,
  created_at=excluded.created_at,
  updated_at=excluded.updated_at,
  settled=excluded.settled,
  voided=excluded.voided,
  deleted=excluded.deleted
WHERE excluded.updated_at >= transactions.updated_at;
"""

UPSERT_SHIFT = """
INSERT INTO shift_waiters (day,waiter_id,updated_at,deleted)
VALUES (:day,:waiter_id,:updated_at,:deleted)
ON CONFLICT(day,waiter_id) DO UPDATE SET
  updated_at=excluded.updated_at,
  deleted=excluded.deleted
WHERE excluded.updated_at >= shift_waiters.updated_at;
"""

def push_local_to_turso() -> int:
    lc = local_conn()
    eng = turso_engine()

    last = get_sync_ts("last_push")
    max_ts = last
    pushed = 0

    # WAITERS
    w_df = pd.read_sql_query(
        "SELECT id,name,active,created_at,updated_at,deleted FROM waiters WHERE updated_at > ?;",
        lc,
        params=(last,),
    )
    # TRANSACTIONS
    t_df = pd.read_sql_query(
        "SELECT id,day,waiter_id,amount,created_at,updated_at,settled,voided,deleted FROM transactions WHERE updated_at > ?;",
        lc,
        params=(last,),
    )
    # SHIFT
    s_df = pd.read_sql_query(
        "SELECT day,waiter_id,updated_at,deleted FROM shift_waiters WHERE updated_at > ?;",
        lc,
        params=(last,),
    )

    with eng.begin() as con:
        for _, r in w_df.iterrows():
            con.execute(text(UPSERT_WAITER), r.to_dict())
            max_ts = max(max_ts, int(r["updated_at"]))
            pushed += 1
        for _, r in t_df.iterrows():
            con.execute(text(UPSERT_TX), r.to_dict())
            max_ts = max(max_ts, int(r["updated_at"]))
            pushed += 1
        for _, r in s_df.iterrows():
            con.execute(text(UPSERT_SHIFT), r.to_dict())
            max_ts = max(max_ts, int(r["updated_at"]))
            pushed += 1

    if max_ts > last:
        set_sync_ts("last_push", max_ts)
    return pushed

def pull_turso_to_local() -> int:
    lc = local_conn()
    eng = turso_engine()

    last = get_sync_ts("last_pull")
    max_ts = last
    pulled = 0

    with eng.begin() as con:
        w_rows = con.execute(text("SELECT id,name,active,created_at,updated_at,deleted FROM waiters WHERE updated_at > :ts;"), {"ts": last}).mappings().all()
        t_rows = con.execute(text("SELECT id,day,waiter_id,amount,created_at,updated_at,settled,voided,deleted FROM transactions WHERE updated_at > :ts;"), {"ts": last}).mappings().all()
        s_rows = con.execute(text("SELECT day,waiter_id,updated_at,deleted FROM shift_waiters WHERE updated_at > :ts;"), {"ts": last}).mappings().all()

    cur = lc.cursor()

    for r in w_rows:
        cur.execute("""
            INSERT INTO waiters(id,name,active,created_at,updated_at,deleted)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              active=excluded.active,
              created_at=excluded.created_at,
              updated_at=excluded.updated_at,
              deleted=excluded.deleted
            WHERE excluded.updated_at >= waiters.updated_at;
        """, (r["id"], r["name"], int(r["active"]), r["created_at"], int(r["updated_at"]), int(r["deleted"])))
        max_ts = max(max_ts, int(r["updated_at"]))
        pulled += 1

    for r in t_rows:
        cur.execute("""
            INSERT INTO transactions(id,day,waiter_id,amount,created_at,updated_at,settled,voided,deleted)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              day=excluded.day,
              waiter_id=excluded.waiter_id,
              amount=excluded.amount,
              created_at=excluded.created_at,
              updated_at=excluded.updated_at,
              settled=excluded.settled,
              voided=excluded.voided,
              deleted=excluded.deleted
            WHERE excluded.updated_at >= transactions.updated_at;
        """, (r["id"], r["day"], r["waiter_id"], float(r["amount"]), r["created_at"], int(r["updated_at"]), int(r["settled"]), int(r["voided"]), int(r["deleted"])))
        max_ts = max(max_ts, int(r["updated_at"]))
        pulled += 1

    for r in s_rows:
        cur.execute("""
            INSERT INTO shift_waiters(day,waiter_id,updated_at,deleted)
            VALUES (?,?,?,?)
            ON CONFLICT(day,waiter_id) DO UPDATE SET
              updated_at=excluded.updated_at,
              deleted=excluded.deleted
            WHERE excluded.updated_at >= shift_waiters.updated_at;
        """, (r["day"], r["waiter_id"], int(r["updated_at"]), int(r["deleted"])))
        max_ts = max(max_ts, int(r["updated_at"]))
        pulled += 1

    lc.commit()

    if max_ts > last:
        set_sync_ts("last_pull", max_ts)
    return pulled

# Pull iniziale (una volta per sessione)
if "initial_pull_done" not in st.session_state:
    try:
        pull_turso_to_local()
    except Exception as e:
        st.warning(f"Pull iniziale non riuscito: {e}")
    st.session_state["initial_pull_done"] = True

# ===========================
# WRAPPER: WRITE + SYNC (SU OGNI BOTTONE DI SCRITTURA)
# ===========================
def write_and_sync(action: Callable[[], None], do_pull_after: bool = False):
    """
    1) scrive su SQLite locale
    2) push su Turso
    3) opzionale pull
    4) rerun
    """
    try:
        action()
    except Exception as e:
        st.error(str(e))
        return

    try:
        pushed = push_local_to_turso()
        if do_pull_after:
            pull_turso_to_local()
        st.toast(f"✅ Sync OK (pushed {pushed})", icon="✅")
    except Exception as e:
        st.warning(f"⚠️ Scrittura locale ok, sync fallita: {e}")

    st.rerun()

# ===========================
# CRUD (LOCAL) — SEMPRE updated_at
# ===========================
def get_waiters(active_only: bool = True) -> List[Tuple[str, str, int]]:
    c = local_conn()
    cur = c.cursor()
    if active_only:
        cur.execute("SELECT id,name,active FROM waiters WHERE active=1 AND deleted=0 ORDER BY name;")
    else:
        cur.execute("SELECT id,name,active FROM waiters WHERE deleted=0 ORDER BY active DESC, name;")
    return [(str(i), str(n), int(a)) for i, n, a in cur.fetchall()]

def add_waiter(name: str):
    name = (name or "").strip()
    if not name:
        raise ValueError("Inserisci un nome valido.")
    wid = str(uuid.uuid4())
    c = local_conn()
    c.execute(
        "INSERT INTO waiters(id,name,active,created_at,updated_at,deleted) VALUES (?,?,?,?,?,0);",
        (wid, name, 1, now_iso(), now_ms())
    )
    c.commit()

def set_waiter_active(waiter_id: str, active: bool):
    c = local_conn()
    c.execute("UPDATE waiters SET active=?, updated_at=? WHERE id=?;", (1 if active else 0, now_ms(), waiter_id))
    c.commit()

def add_tx(day_str: str, waiter_id: str, amount: float):
    if amount <= 0:
        raise ValueError("L'importo deve essere > 0.")
    tx_id = str(uuid.uuid4())
    c = local_conn()
    c.execute(
        """INSERT INTO transactions
           (id,day,waiter_id,amount,created_at,updated_at,settled,voided,deleted)
           VALUES (?,?,?,?,?,?,?,?,0);""",
        (tx_id, day_str, waiter_id, float(amount), now_iso(), now_ms(), 0, 0)
    )
    c.commit()

def void_tx(tx_id: str):
    c = local_conn()
    c.execute("UPDATE transactions SET voided=1, updated_at=? WHERE id=?;", (now_ms(), tx_id))
    c.commit()

def settle_unsettled_for_waiter_day(day_str: str, waiter_id: str):
    c = local_conn()
    c.execute("""
        UPDATE transactions
        SET settled=1, updated_at=?
        WHERE day=? AND waiter_id=? AND settled=0 AND voided=0 AND deleted=0;
    """, (now_ms(), day_str, waiter_id))
    c.commit()

def totals_unsettled_by_waiter_for_day(day_str: str) -> Dict[str, float]:
    c = local_conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND settled=0 AND voided=0 AND deleted=0
        GROUP BY waiter_id;
    """, (day_str,))
    return {str(wid): float(tot) for wid, tot in cur.fetchall()}

def totals_settled_by_waiter_for_day(day_str: str) -> Dict[str, float]:
    c = local_conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND settled=1 AND voided=0 AND deleted=0
        GROUP BY waiter_id;
    """, (day_str,))
    return {str(wid): float(tot) for wid, tot in cur.fetchall()}

def totals_by_waiter_for_day(day_str: str) -> Dict[str, float]:
    c = local_conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND voided=0 AND deleted=0
        GROUP BY waiter_id;
    """, (day_str,))
    return {str(wid): float(tot) for wid, tot in cur.fetchall()}

def tx_by_waiter_for_day(day_str: str, waiter_id: str) -> List[Tuple[str, float, str, int, int]]:
    c = local_conn()
    cur = c.cursor()
    cur.execute("""
        SELECT id, amount, created_at, settled, voided
        FROM transactions
        WHERE day=? AND waiter_id=? AND deleted=0
        ORDER BY created_at DESC;
    """, (day_str, waiter_id))
    return [(str(i), float(a), str(t), int(s), int(v)) for i, a, t, s, v in cur.fetchall()]

def history_df(date_from: str, date_to: str, waiter_id: Optional[str]) -> pd.DataFrame:
    c = local_conn()
    q = """
    SELECT t.id, t.day, w.name AS cameriere, t.amount, t.created_at, t.settled, t.voided
    FROM transactions t
    JOIN waiters w ON w.id=t.waiter_id
    WHERE t.day BETWEEN ? AND ? AND t.deleted=0 AND w.deleted=0
    """
    params = [date_from, date_to]
    if waiter_id:
        q += " AND t.waiter_id=?"
        params.append(waiter_id)
    q += " ORDER BY t.day DESC, t.created_at DESC;"
    return pd.read_sql_query(q, c, params=params)

def get_shift_waiters(day_str: str) -> List[str]:
    c = local_conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id
        FROM shift_waiters
        WHERE day=? AND deleted=0
        ORDER BY waiter_id;
    """, (day_str,))
    return [str(r[0]) for r in cur.fetchall()]

def set_shift_waiters(day_str: str, waiter_ids: List[str]):
    c = local_conn()
    cur = c.cursor()
    ts = now_ms()

    # soft-delete tutti quelli del giorno, poi riattivo quelli selezionati
    cur.execute("UPDATE shift_waiters SET deleted=1, updated_at=? WHERE day=?;", (ts, day_str))
    for wid in waiter_ids:
        cur.execute("""
            INSERT INTO shift_waiters(day,waiter_id,updated_at,deleted)
            VALUES (?,?,?,0)
            ON CONFLICT(day,waiter_id) DO UPDATE SET
              updated_at=excluded.updated_at,
              deleted=0;
        """, (day_str, wid, ts))
    c.commit()

def ui_status(settled: int, voided: int) -> str:
    if int(voided) == 1:
        return "Annullato"
    if int(settled) == 1:
        return "Incassato"
    return "Aperto"

# ===========================
# SEED (solo se vuoto)
# ===========================
if "seed_done" not in st.session_state:
    if len(get_waiters(active_only=False)) == 0:
        for n in ["Anna Bianchi", "Luigi Verdi", "Mario Rossi"]:
            try:
                # seed locale + sync
                write_and_sync(lambda nn=n: add_waiter(nn))
            except Exception:
                pass
    st.session_state["seed_done"] = True

# ============================================================
# QUI SOTTO: la tua UI (CSS, sidebar, keypad, pagine)
# Ti mostro solo come cambiano i PUNTI DI SCRITTURA:
# - add_tx, void_tx, settle..., add_waiter, set_waiter_active, set_shift_waiters
# devono passare da write_and_sync(...)
# ============================================================

# ---------------------------
# (OPZIONALE) il tuo CSS qui
# ---------------------------
# st.markdown("...tuo css...", unsafe_allow_html=True)

# ---------------------------
# Tastierino (puoi riusare il tuo)
# ---------------------------
def keypad_widget(prefix: str, currency: str = "€") -> float:
    buf_key = f"{prefix}_buf"
    if buf_key not in st.session_state:
        st.session_state[buf_key] = ""

    def _get_value() -> float:
        s = st.session_state[buf_key]
        if not s or s == ".":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return 0.0

    def press(ch: str):
        s = st.session_state[buf_key]
        if ch == "Reset":
            st.session_state[buf_key] = ""
            return
        if ch == ".":
            if "." in s:
                return
            st.session_state[buf_key] = "0." if s == "" else s + "."
            return
        if ch.isdigit():
            st.session_state[buf_key] = ch if s == "0" else s + ch

    val = _get_value()
    st.write(f"{currency} {val:,.2f}".replace(",", " "))

    grid = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        [".", "0", "Reset"],
    ]
    for row in grid:
        cols = st.columns(3)
        for i, ch in enumerate(row):
            if cols[i].button(ch, key=f"{prefix}_k_{ch}"):
                press(ch)
                st.rerun()

    return _get_value()

# ---------------------------
# Sidebar nav (versione base)
# (se vuoi, rimetti la tua identica)
# ---------------------------
qp = st.query_params
current = qp.get("page", "incassi")

KEY_TO_PAGE = {
    "dashboard": "Dashboard",
    "incassi": "Registra Incassi",
    "camerieri": "Camerieri",
    "storico": "Storico Incassi",
}

def _goto(page_key: str):
    st.query_params["page"] = page_key
    st.rerun()

page = KEY_TO_PAGE.get(current, "Registra Incassi")

with st.sidebar:
    if st.button("Dashboard"): _goto("dashboard")
    if st.button("Incassi"): _goto("incassi")
    if st.button("Camerieri"): _goto("camerieri")
    if st.button("Storico"): _goto("storico")

    if page in ["Dashboard", "Registra Incassi"]:
        default_bd = business_day_for(datetime.now(TZ), cutoff_hour=CUTOFF_HOUR)
        day = st.date_input("Giornata (12:00 → 12:00)", value=default_bd, key="sidebar_day")
        day_str = day.isoformat()
    else:
        day_str = date.today().isoformat()

    # Sync manuale (utile per debug)
    if st.button("🔁 Sync ora (push+pull)"):
        try:
            pushed = push_local_to_turso()
            pulled = pull_turso_to_local()
            st.success(f"OK — pushed {pushed}, pulled {pulled}")
        except Exception as e:
            st.error(str(e))

# ---------------------------
# Dashboard
# ---------------------------
if page == "Dashboard":
    st.title("Dashboard")

    waiters = get_waiters(active_only=True)
    tot_all = totals_by_waiter_for_day(day_str)
    tot_open = totals_unsettled_by_waiter_for_day(day_str)
    tot_paid = totals_settled_by_waiter_for_day(day_str)

    overall_all = sum(tot_all.get(wid, 0.0) for wid, _, _ in waiters)
    overall_open = sum(tot_open.get(wid, 0.0) for wid, _, _ in waiters)
    overall_paid = sum(tot_paid.get(wid, 0.0) for wid, _, _ in waiters)

    a, b, c = st.columns(3)
    a.metric("Totale giornata (tutto)", f"€{overall_all:,.2f}".replace(",", " "))
    b.metric("Da incassare (aperto)", f"€{overall_open:,.2f}".replace(",", " "))
    c.metric("Incassato", f"€{overall_paid:,.2f}".replace(",", " "))

# ---------------------------
# Registra Incassi
# ---------------------------
elif page == "Registra Incassi":
    waiters = get_waiters(active_only=True)
    if not waiters:
        st.warning("Nessun cameriere attivo. Vai in 'Camerieri' e aggiungine uno.")
        st.stop()

    id_to_name = {wid: name for wid, name, _ in waiters}
    names = sorted([name for _, name, _ in waiters], key=lambda x: x.lower())
    name_to_id = {name: wid for wid, name, _ in waiters}

    sel_key = f"selected_names_{day_str}"
    if sel_key not in st.session_state:
        shift_ids = get_shift_waiters(day_str)
        st.session_state[sel_key] = [id_to_name[wid] for wid in shift_ids if wid in id_to_name]

    selected_names = st.session_state[sel_key]
    selected_ids = [name_to_id[n] for n in selected_names if n in name_to_id]

    tot_open_map = totals_unsettled_by_waiter_for_day(day_str)
    tot_paid_map = totals_settled_by_waiter_for_day(day_str)

    if not selected_ids:
        st.info("Seleziona i camerieri in servizio in fondo alla pagina per iniziare.")
    else:
        cols = st.columns(len(selected_ids), gap="large")
        for i, wid in enumerate(selected_ids):
            name = id_to_name[wid]
            with cols[i]:
                txs = tx_by_waiter_for_day(day_str, wid)
                tot_open = tot_open_map.get(wid, 0.0)

                st.subheader(name)
                st.write(f"Tot da incassare: € {tot_open:,.2f}".replace(",", " "))

                amount = keypad_widget(prefix=f"kp_{day_str}_{wid}")

                # ✅ WRITE: Aggiungi -> write_and_sync
                if st.button("Aggiungi", key=f"add_{day_str}_{wid}", type="primary", use_container_width=True):
                    write_and_sync(lambda: add_tx(day_str, wid, amount))
                    st.session_state[f"kp_{day_str}_{wid}_buf"] = ""

                # ✅ WRITE: Incasso -> write_and_sync
                if st.button("INCASSO", key=f"settle_{day_str}_{wid}", use_container_width=True):
                    write_and_sync(lambda: settle_unsettled_for_waiter_day(day_str, wid))

                st.divider()
                for tx_id, amt, created_at, settled, voided in txs[:15]:
                    st.write(f"{fmt_time_from_iso(created_at)} — € {amt:,.2f} — {ui_status(settled, voided)}".replace(",", " "))
                    # ✅ WRITE: void -> write_and_sync
                    if voided == 0 and settled == 0:
                        if st.button("🗑️ Annulla", key=f"void_{tx_id}"):
                            write_and_sync(lambda tid=tx_id: void_tx(tid))

    st.divider()
    st.subheader("Camerieri in servizio oggi")
    new_selected = st.multiselect("Seleziona i camerieri presenti oggi", options=names, default=st.session_state[sel_key])

    # ✅ WRITE: Applica selezione -> write_and_sync
    if st.button("✅ Applica selezione", type="primary", use_container_width=True):
        st.session_state[sel_key] = new_selected
        write_and_sync(lambda: set_shift_waiters(day_str, [name_to_id[n] for n in new_selected if n in name_to_id]))

# ---------------------------
# Camerieri
# ---------------------------
elif page == "Camerieri":
    st.title("Camerieri")

    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("Aggiungi cameriere")
        with st.form("add_waiter_form", clear_on_submit=True):
            name = st.text_input("Nome", placeholder="Es. Giovanni")
            ok = st.form_submit_button("Salva")
            if ok:
                # ✅ WRITE: add_waiter -> write_and_sync
                write_and_sync(lambda: add_waiter(name))

    with right:
        st.subheader("Elenco")
        rows = get_waiters(active_only=False)
        for wid, name, active in rows:
            c1, c2 = st.columns([3, 1])
            c1.write(f"**{name}**")
            new_active = c2.toggle("Attivo", value=bool(active), key=f"act_{wid}")
            if new_active != bool(active):
                # ✅ WRITE: set_waiter_active -> write_and_sync
                write_and_sync(lambda w=wid, a=new_active: set_waiter_active(w, a))

# ---------------------------
# Storico
# ---------------------------
else:
    st.title("Storico Incassi")

    waiters_all = get_waiters(active_only=False)
    id_to_name = {wid: name for wid, name, _ in waiters_all}

    f1, f2, f3 = st.columns([1, 1, 1.2])
    with f1:
        d_from = st.date_input("Da", value=date.today().replace(day=1))
    with f2:
        d_to = st.date_input("A", value=date.today())
    with f3:
        options = ["Tutti"] + [id_to_name[wid] for wid in id_to_name]
        choice = st.selectbox("Cameriere", options=options, index=0)

    waiter_id = None
    if choice != "Tutti":
        name_to_id_all = {name: wid for wid, name, _ in waiters_all}
        waiter_id = name_to_id_all.get(choice)

    df = history_df(d_from.isoformat(), d_to.isoformat(), waiter_id)
    if df.empty:
        st.info("Nessun dato nel periodo selezionato.")
    else:
        df["Stato"] = df.apply(lambda r: ui_status(r["settled"], r["voided"]), axis=1)
        df_view = df[["id", "day", "cameriere", "amount", "created_at", "Stato"]].copy()
        st.dataframe(df_view, use_container_width=True)

        valid_mask = (df["voided"] == 0)
        total = df.loc[valid_mask, "amount"].sum()
        st.metric("Totale periodo (esclude annullati)", f"€{total:,.2f}".replace(",", " "))
