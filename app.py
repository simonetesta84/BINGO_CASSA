# ============================================================
# BINGO CASSA — Streamlit + TURSO (libSQL) — FAST + STABLE
# - Auth con password + TTL
# - Turso via SQLAlchemy (sqlite+libsql://...)
# - Reads: text()+execute() (evita bug pandas+params su libsql)
# - 1 query al giorno per tx + aggregazioni in RAM
# - Rendering limitato (ultime 30 + expander)
#
# SECRETS (Streamlit -> Settings -> Secrets):
# APP_PASSWORD="..."
# TURSO_DATABASE_URL="libsql://bingo-cassa-xxxx.turso.io"
# TURSO_AUTH_TOKEN="xxxxx"
#
# requirements.txt:
# streamlit
# pandas
# sqlalchemy>=2.0
# sqlalchemy-libsql
# ============================================================

from __future__ import annotations

import time as pytime
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ---------------------------
# Streamlit config (UNA sola volta)
# ---------------------------
st.set_page_config(page_title="BINGO CASSA", layout="wide")

# ---------------------------
# AUTH
# ---------------------------
SESSION_TTL_SECONDS = 60 * 60 * 6  # 6 ore

def require_password():
    expected = st.secrets.get("APP_PASSWORD")
    if not expected:
        st.error("❌ APP_PASSWORD mancante. Impostalo in Settings → Secrets.")
        st.stop()

    now_ts = pytime.time()
    expires_at = st.session_state.get("auth_expires_at", 0)
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

# ---------------------------
# Config
# ---------------------------
TZ = ZoneInfo("Europe/Zurich")
CUTOFF_HOUR = 12  # business day 12:00 -> 12:00

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

# ---------------------------
# Turso engine (CORRETTO)
# ---------------------------
@st.cache_resource
def get_engine():
    url = st.secrets.get("TURSO_DATABASE_URL")  # es: libsql://...turso.io
    token = st.secrets.get("TURSO_AUTH_TOKEN")

    if not url or not token:
        st.error("❌ Turso non configurato. Aggiungi TURSO_DATABASE_URL e TURSO_AUTH_TOKEN nei Secrets.")
        st.stop()

    # forza stringa valida per SQLAlchemy: sqlite+libsql://HOST
    db_host = url.replace("libsql://", "").strip()

    engine = create_engine(
        f"sqlite+libsql://{db_host}",
        connect_args={"auth_token": token},
        pool_pre_ping=True,
    )
    return engine

ENGINE = get_engine()

# ---------------------------
# Business day helpers (12->12)
# ---------------------------
def business_day_for(dt: datetime, cutoff_hour: int = CUTOFF_HOUR) -> date:
    local_dt = dt.astimezone(TZ)
    if local_dt.hour < cutoff_hour:
        return local_dt.date() - timedelta(days=1)
    return local_dt.date()

def business_day_range(day_: date, cutoff_hour: int = CUTOFF_HOUR) -> Tuple[datetime, datetime]:
    start = datetime.combine(day_, time(hour=cutoff_hour, minute=0, second=0), tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

def fmt_time_from_iso(iso_str: str) -> str:
    if not iso_str:
        return ""
    if len(iso_str) >= 19 and "T" in iso_str:
        return iso_str[11:19]
    return iso_str[-8:]

# ---------------------------
# DB init + CRUD (writes)
# ---------------------------
def init_db():
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")

        c.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS waiters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
        """)

        c.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                waiter_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0,
                voided INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (waiter_id) REFERENCES waiters(id)
            );
        """)

        c.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS shift_waiters (
                day TEXT NOT NULL,
                waiter_id INTEGER NOT NULL,
                PRIMARY KEY (day, waiter_id),
                FOREIGN KEY (waiter_id) REFERENCES waiters(id)
            );
        """)

def add_waiter(name: str):
    name = (name or "").strip()
    if not name:
        raise ValueError("Inserisci un nome valido.")
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql(
            "INSERT INTO waiters(name, active, created_at) VALUES (?, 1, ?);",
            (name, now_iso())
        )

def set_waiter_active(waiter_id: int, active: bool):
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql("UPDATE waiters SET active=? WHERE id=?;", (1 if active else 0, int(waiter_id)))

def add_tx(day_str: str, waiter_id: int, amount: float):
    if amount <= 0:
        raise ValueError("L'importo deve essere > 0.")
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql(
            "INSERT INTO transactions(day, waiter_id, amount, created_at, settled, voided) VALUES (?,?,?,?,0,0);",
            (day_str, int(waiter_id), float(amount), now_iso())
        )

def void_tx(tx_id: int):
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql("UPDATE transactions SET voided=1 WHERE id=?;", (int(tx_id),))

def settle_unsettled_for_waiter_day(day_str: str, waiter_id: int):
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql("""
            UPDATE transactions
            SET settled=1
            WHERE day=? AND waiter_id=? AND settled=0 AND voided=0;
        """, (day_str, int(waiter_id)))

def set_shift_waiters(day_str: str, waiter_ids: List[int]):
    with ENGINE.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys = ON;")
        c.exec_driver_sql("DELETE FROM shift_waiters WHERE day=?;", (day_str,))
        for wid in waiter_ids:
            c.exec_driver_sql(
                "INSERT OR IGNORE INTO shift_waiters(day, waiter_id) VALUES (?,?);",
                (day_str, int(wid))
            )

# ---------------------------
# READS (caching) — STABLE: text()+execute()
# ---------------------------
@st.cache_data(ttl=2)
def get_waiters_df(active_only: bool) -> pd.DataFrame:
    if active_only:
        q = text("SELECT id, name, active FROM waiters WHERE active=1 ORDER BY name;")
        params = {}
    else:
        q = text("SELECT id, name, active FROM waiters ORDER BY active DESC, name;")
        params = {}

    with ENGINE.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    return pd.DataFrame(rows)

@st.cache_data(ttl=2)
def get_shift_waiters_ids(day_str: str) -> List[int]:
    q = text("SELECT waiter_id FROM shift_waiters WHERE day=:day ORDER BY waiter_id;")
    with ENGINE.connect() as conn:
        rows = conn.execute(q, {"day": day_str}).all()
    return [int(r[0]) for r in rows]

@st.cache_data(ttl=2)
def get_tx_day_df(day_str: str) -> pd.DataFrame:
    q = text("""
        SELECT t.id, t.day, t.waiter_id, w.name AS cameriere,
               t.amount, t.created_at, t.settled, t.voided
        FROM transactions t
        JOIN waiters w ON w.id=t.waiter_id
        WHERE t.day = :day
        ORDER BY t.id DESC;
    """)
    with ENGINE.connect() as conn:
        rows = conn.execute(q, {"day": day_str}).mappings().all()

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["id"] = df["id"].astype(int)
    df["waiter_id"] = df["waiter_id"].astype(int)
    df["amount"] = df["amount"].astype(float)
    df["settled"] = df["settled"].astype(int)
    df["voided"] = df["voided"].astype(int)
    return df

def invalidate_caches():
    st.cache_data.clear()

def ui_status(settled: int, voided: int) -> str:
    if int(voided) == 1:
        return "Annullato"
    if int(settled) == 1:
        return "Incassato"
    return "Aperto"

# ---------------------------
# CSS (tuo)
# ---------------------------
st.markdown(
    """
<style>
[data-testid="stButton"] > button{
  height: 64px;
  font-size: 20px;
  border-radius: 10px;
}
[data-testid="stVerticalBlockBorderWrapper"]{
  border-radius: 18px;
}
.mini-tx {
  padding: 10px 12px;
  border-radius: 12px;
  background: rgba(49,51,63,0.04);
  font-weight: 900;
  margin-bottom: 8px;
}
.mini-box-title {
  font-weight: 900;
  margin: 4px 0 10px 0;
  color: rgba(49,51,63,0.70);
}
h3 { margin-top: 0 !important; margin-bottom: 4px !important; }
[data-testid="stSidebar"]{
  background: #ffffff;
  border-right: 1px solid rgba(15, 23, 42, 0.06);
}
[data-testid="stSidebar"] .stSidebarContent{ padding-top: 18px; }
.sb-title{
  font-weight: 800;
  font-size: 18px;
  margin: 6px 0 14px 0;
  color: rgba(15, 23, 42, 0.88);
  padding: 0 10px;
}
[data-testid="stSidebar"] .element-container { margin-bottom: 0.35rem; }
.sb-cta-wrap{ padding: 0 10px; margin-top: 4px; margin-bottom: 12px; }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------
# Keypad (FAST: nessun st.rerun qui)
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

    st.markdown("""
        <style>
        button[kind="primary"]{ height: 64px; font-size: 60px; border-radius: 10px; }
        button[kind="secondary"]{ height: 82px; font-size: 60px; font-weight: 900; border-radius: 14px; }
        div[data-testid^="baseButton-secondary"] > button{ height: 82px; font-size: 60px; font-weight: 900; border-radius: 14px; }
        div[class^="st-key-kp_"] [data-testid="stButton"] > button,
        div[class*=" st-key-kp_"] [data-testid="stButton"] > button{
          height: 86px !important; font-size: 50px !important; font-weight: 900 !important; border-radius: 16px !important;
        }
        </style>
    """, unsafe_allow_html=True)

    val = _get_value()
    st.markdown(
        f"""
        <div style="
          width:100%;
          font-size:60px;
          font-weight:900;
          padding:8px 14px;
          border-radius:14px;
          border:1px solid rgba(49,51,63,0.18);
          background: rgba(49,51,63,0.02);
          text-align:left;
          margin: -34px 0 0 0;">
          {currency} {val:,.2f}
        </div>
        """.replace(",", " "),
        unsafe_allow_html=True,
    )

    grid = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        [".", "0", "Reset"],
    ]
    for row in grid:
        cols = st.columns(3, gap="small")
        for i, ch in enumerate(row):
            if cols[i].button(ch, key=f"{prefix}_k_{ch}", width="stretch"):
                press(ch)

    return _get_value()

# ---------------------------
# Init DB + seed
# ---------------------------
init_db()

if "seed_done" not in st.session_state:
    wdf_all = get_waiters_df(active_only=False)
    if wdf_all.empty:
        for n in ["Anna Bianchi", "Luigi Verdi", "Mario Rossi"]:
            try:
                add_waiter(n)
            except Exception:
                pass
        invalidate_caches()
    st.session_state["seed_done"] = True

# ---------------------------
# Sidebar nav
# ---------------------------
qp = st.query_params
current = qp.get("page", "incassi")

KEY_TO_PAGE = {
    "dashboard": "Dashboard",
    "incassi": "Registra Incassi",
    "camerieri": "Camerieri",
    "storico": "Storico Incassi",
}
NAV_MENU = [("dashboard", "Dashboard"), ("camerieri", "Camerieri"), ("storico", "Storico")]

def _goto(page_key: str):
    st.query_params["page"] = page_key
    st.rerun()

page = KEY_TO_PAGE.get(current, "Registra Incassi")

with st.sidebar:
    if st.button("Incassi", key="nav_incassi", width="stretch"):
        _goto("incassi")

    if page in ["Dashboard", "Registra Incassi"]:
        default_bd = business_day_for(datetime.now(TZ), cutoff_hour=CUTOFF_HOUR)
        day = st.date_input("Giornata (12:00 → 12:00)", value=default_bd, key="sidebar_day")
        day_str = day.isoformat()
        start_dt, end_dt = business_day_range(day, cutoff_hour=CUTOFF_HOUR)
        st.caption(f"Intervallo: {start_dt.strftime('%d.%m.%Y %H:%M')} → {end_dt.strftime('%d.%m.%Y %H:%M')}")
    else:
        day_str = date.today().isoformat()

    st.markdown("<div class='sb-title'>BINGO CASSA</div>", unsafe_allow_html=True)
    for key, label in NAV_MENU:
        if st.button(label, key=f"nav_{key}", width="stretch"):
            _goto(key)

# ---------------------------
# Preload dati
# ---------------------------
waiters_active_df = get_waiters_df(active_only=True)
waiters_all_df = get_waiters_df(active_only=False)
tx_day_df = get_tx_day_df(day_str) if page in ["Dashboard", "Registra Incassi"] else pd.DataFrame()

# ---------------------------
# Aggregazioni in Python (fast)
# ---------------------------
def totals_maps_from_df(df: pd.DataFrame) -> Tuple[Dict[int, float], Dict[int, float], Dict[int, float]]:
    if df.empty:
        return {}, {}, {}
    valid = df[df["voided"] == 0]
    all_map = valid.groupby("waiter_id")["amount"].sum().to_dict()
    open_map = valid[valid["settled"] == 0].groupby("waiter_id")["amount"].sum().to_dict()
    paid_map = valid[valid["settled"] == 1].groupby("waiter_id")["amount"].sum().to_dict()
    return (
        {int(k): float(v) for k, v in all_map.items()},
        {int(k): float(v) for k, v in open_map.items()},
        {int(k): float(v) for k, v in paid_map.items()},
    )

tot_all_map, tot_open_map, tot_paid_map = totals_maps_from_df(tx_day_df)

# ============================================================
# PAGES
# ============================================================

if page == "Dashboard":
    st.title("Dashboard")
    if waiters_active_df.empty:
        st.info("Nessun cameriere attivo.")
        st.stop()

    waiters = [(int(r["id"]), str(r["name"]), int(r["active"])) for _, r in waiters_active_df.iterrows()]
    overall_all = sum(tot_all_map.get(wid, 0.0) for wid, _, _ in waiters)
    overall_open = sum(tot_open_map.get(wid, 0.0) for wid, _, _ in waiters)
    overall_paid = sum(tot_paid_map.get(wid, 0.0) for wid, _, _ in waiters)

    a, b, c = st.columns(3)
    a.metric("Totale giornata (tutto)", f"€{overall_all:,.2f}".replace(",", " "))
    b.metric("Da incassare (aperto)", f"€{overall_open:,.2f}".replace(",", " "))
    c.metric("Incassato", f"€{overall_paid:,.2f}".replace(",", " "))

    st.divider()
    st.subheader("Riepilogo per cameriere")
    cols = st.columns(min(4, len(waiters)))
    for i, (wid, name, _) in enumerate(waiters):
        with cols[i % len(cols)]:
            st.metric(
                name,
                f"Da incassare: €{tot_open_map.get(wid, 0.0):,.2f}".replace(",", " "),
                delta=f" Totale incassato: €{tot_paid_map.get(wid, 0.0):,.2f}".replace(",", " ")
            )

elif page == "Registra Incassi":
    if waiters_active_df.empty:
        st.warning("Nessun cameriere attivo. Vai in 'Camerieri' e aggiungine uno.")
        st.stop()

    waiters_active = [(int(r["id"]), str(r["name"]), int(r["active"])) for _, r in waiters_active_df.iterrows()]
    id_to_name = {wid: name for wid, name, _ in waiters_active}
    names = sorted([name for _, name, _ in waiters_active], key=lambda x: x.lower())
    name_to_id = {name: wid for wid, name, _ in waiters_active}

    sel_key = f"selected_names_{day_str}"
    if sel_key not in st.session_state:
        shift_ids = get_shift_waiters_ids(day_str)
        st.session_state[sel_key] = [id_to_name[wid] for wid in shift_ids if wid in id_to_name]

    selected_names = st.session_state[sel_key]
    selected_ids = [name_to_id[n] for n in selected_names if n in name_to_id]

    if not selected_ids:
        st.info("Seleziona i camerieri in servizio in fondo alla pagina per iniziare.")
    else:
        cols = st.columns(len(selected_ids), gap="large")

        for i, wid in enumerate(selected_ids):
            name = id_to_name[wid]
            with cols[i]:
                df_w = tx_day_df[tx_day_df["waiter_id"] == wid].copy() if not tx_day_df.empty else pd.DataFrame()
                txs = df_w[["id", "amount", "created_at", "settled", "voided"]].values.tolist() if not df_w.empty else []

                tot_open = float(tot_open_map.get(wid, 0.0))
                has_unsettled = bool(((df_w["settled"] == 0) & (df_w["voided"] == 0)).any()) if not df_w.empty else False

                with st.container(border=True):
                    h_name, h_btn, h_tot = st.columns([3.8, 2.2, 2.7], gap="small")

                    with h_name:
                        st.markdown(f"### {name}")

                    with h_btn:
                        confirm_key = f"confirm_settle_{day_str}_{wid}"
                        if confirm_key not in st.session_state:
                            st.session_state[confirm_key] = False

                        if not st.session_state[confirm_key]:
                            ask = st.button(
                                "INCASSO",
                                type="primary",
                                use_container_width=True,
                                key=f"settle_btn_{day_str}_{wid}",
                                disabled=(not has_unsettled),
                                help="Clicca per incassare tutte le battute aperte"
                            )
                            if ask and has_unsettled:
                                st.session_state[confirm_key] = True
                                st.rerun()
                        else:
                            st.warning("Confermi INCASSO?")
                            c_ok, c_no = st.columns(2, gap="small")
                            with c_ok:
                                do_it = st.button("✅ SI", type="primary", width="stretch", key=f"settle_confirm_{day_str}_{wid}")
                            with c_no:
                                cancel = st.button("NO", width="stretch", key=f"settle_cancel_{day_str}_{wid}")

                            if do_it:
                                settle_unsettled_for_waiter_day(day_str, wid)
                                st.session_state[confirm_key] = False
                                invalidate_caches()
                                st.rerun()
                            if cancel:
                                st.session_state[confirm_key] = False
                                st.rerun()

                    with h_tot:
                        st.markdown(
                            f"""
                            <div style="
                                display:flex;
                                justify-content:flex-end;
                                align-items:flex-end;
                                font-weight:900;
                                color:#1f6feb;
                                font-size:24px;
                                white-space:nowrap;
                                padding-top:7px;">
                                Tot:&nbsp;€ {tot_open:,.2f}
                            </div>
                            """.replace(",", " "),
                            unsafe_allow_html=True,
                        )

                    left_pad, right_last = st.columns([2.9, 1.3], gap="large")

                    with left_pad:
                        amount = keypad_widget(prefix=f"kp_{day_str}_{wid}")

                        if st.button("Aggiungi", type="primary", width="stretch", key=f"add_{day_str}_{wid}"):
                            try:
                                add_tx(day_str, wid, amount)
                                st.session_state[f"kp_{day_str}_{wid}_buf"] = ""
                                invalidate_caches()
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                    with right_last:
                        st.markdown("<div class='mini-box-title'>Ultime 9</div>", unsafe_allow_html=True)
                        if not txs:
                            st.caption("—")
                        else:
                            for _id, amt, _created_at, _settled, voided in txs[:9]:
                                if int(voided) == 1:
                                    st.markdown(
                                        f"""
                                        <div class='mini-tx' style="
                                            background: rgba(239,68,68,0.12);
                                            border: 1px solid rgba(239,68,68,0.35);
                                            color: #991b1b;
                                            font-size:20px;
                                            text-decoration: line-through;">
                                            € {float(amt):,.2f}
                                        </div>
                                        """.replace(",", " "),
                                        unsafe_allow_html=True
                                    )
                                else:
                                    st.markdown(
                                        f"""
                                        <div class='mini-tx' style="
                                            background: rgba(239,68,68,0.12);
                                            border: 1px solid rgba(239,68,68,0.35);
                                            font-size:20px;">
                                            € {float(amt):,.2f}
                                        </div>
                                        """.replace(",", " "),
                                        unsafe_allow_html=True
                                    )

                    st.divider()

                    if not txs:
                        st.caption("Nessun incasso oggi")
                    else:
                        show_n = 30

                        def render_rows(rows):
                            for tx_id, amt, created_at, settled, voided in rows:
                                voided = int(voided)
                                settled = int(settled)
                                amt = float(amt)

                                if voided == 1:
                                    bg = "rgba(239,68,68,0.15)"
                                    border = "1px solid rgba(239,68,68,0.60)"
                                    time_color = "#991b1b"
                                    deco = "line-through"
                                elif settled == 1:
                                    bg = "rgba(34,197,94,0.18)"
                                    border = "1px solid rgba(34,197,94,0.55)"
                                    time_color = "#065f46"
                                    deco = "none"
                                else:
                                    bg = "rgba(49,51,63,0.04)"
                                    border = "1px solid rgba(49,51,63,0.08)"
                                    time_color = "#6b7280"
                                    deco = "none"

                                r1, r2 = st.columns([4, 1], gap="small")
                                with r1:
                                    st.markdown(
                                        f"""
                                        <div style="
                                          display:flex;
                                          justify-content:space-between;
                                          align-items:center;
                                          padding:12px;
                                          border-radius:14px;
                                          background:{bg};
                                          border:{border};
                                          font-weight:900;
                                          font-size:20px;
                                          margin-bottom:8px;
                                          text-decoration:{deco};">
                                          <span>€ {amt:,.2f}</span>
                                          <span style="font-size:18px; font-weight:700; color:{time_color};">
                                            {fmt_time_from_iso(str(created_at))}
                                          </span>
                                        </div>
                                        """.replace(",", " "),
                                        unsafe_allow_html=True,
                                    )

                                with r2:
                                    if voided == 1:
                                        st.button("⛔", disabled=True, width="stretch", key=f"voided_{tx_id}")
                                    elif settled == 1:
                                        st.button("✅", disabled=True, width="stretch", key=f"ok_{tx_id}")
                                    else:
                                        if st.button("🗑️", key=f"void_{tx_id}", width="stretch", help="Annulla battuta"):
                                            void_tx(int(tx_id))
                                            invalidate_caches()
                                            st.rerun()

                        render_rows(txs[:show_n])
                        if len(txs) > show_n:
                            with st.expander(f"Mostra tutte ({len(txs)})"):
                                render_rows(txs[show_n:])

        st.divider()
        overall_open = sum(float(tot_open_map.get(wid, 0.0)) for wid in selected_ids)
        st.metric("Totale da incassare (selezionati)", f"€{overall_open:,.2f}".replace(",", " "))

    st.divider()
    st.subheader("Camerieri in servizio oggi")

    new_selected = st.multiselect(
        "Seleziona i camerieri presenti oggi",
        options=names,
        default=st.session_state[sel_key],
    )

    if st.button("✅ Applica selezione", type="primary", width="stretch"):
        st.session_state[sel_key] = new_selected
        set_shift_waiters(day_str, [name_to_id[n] for n in new_selected if n in name_to_id])
        invalidate_caches()
        st.rerun()

elif page == "Camerieri":
    st.title("Camerieri")
    left, right = st.columns([1, 2], gap="large")

    with left:
        st.subheader("Aggiungi cameriere")
        with st.form("add_waiter", clear_on_submit=True):
            name = st.text_input("Nome", placeholder="Es. Giovanni")
            ok = st.form_submit_button("Salva")
            if ok:
                try:
                    add_waiter(name)
                    invalidate_caches()
                    st.success("Aggiunto.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with right:
        st.subheader("Elenco")
        if waiters_all_df.empty:
            st.info("Nessun cameriere.")
        else:
            for _, r in waiters_all_df.iterrows():
                wid = int(r["id"])
                name = str(r["name"])
                active = bool(int(r["active"]))

                c1, c2, c3 = st.columns([1, 1, 1], gap="small")
                c1.write(f"**{name}**")
                c2.write("✅" if active else "❌")
                new_active = c3.toggle("Attivo", value=active, key=f"act_{wid}")
                if new_active != active:
                    set_waiter_active(wid, new_active)
                    invalidate_caches()
                    st.rerun()

else:
    st.title("Storico Incassi")

    f1, f2, f3 = st.columns([1, 1, 1.2], gap="small")
    with f1:
        d_from = st.date_input("Da", value=date.today().replace(day=1))
    with f2:
        d_to = st.date_input("A", value=date.today())
    with f3:
        options = ["Tutti"] + [str(r["name"]) for _, r in waiters_all_df.iterrows()] if not waiters_all_df.empty else ["Tutti"]
        choice = st.selectbox("Cameriere", options=options, index=0)

    waiter_id = None
    if choice != "Tutti":
        name_to_id_all = {str(r["name"]): int(r["id"]) for _, r in waiters_all_df.iterrows()}
        waiter_id = name_to_id_all.get(choice)

    # STORICO: text()+execute() (no pandas params)
    base_sql = """
        SELECT t.id, t.day, w.name AS cameriere, t.amount, t.created_at, t.settled, t.voided
        FROM transactions t
        JOIN waiters w ON w.id=t.waiter_id
        WHERE t.day BETWEEN :dfrom AND :dto
    """
    if waiter_id:
        base_sql += " AND t.waiter_id = :wid "
    base_sql += " ORDER BY t.day DESC, t.id DESC;"

    q = text(base_sql)
    params = {"dfrom": d_from.isoformat(), "dto": d_to.isoformat()}
    if waiter_id:
        params["wid"] = int(waiter_id)

    with ENGINE.connect() as conn:
        rows = conn.execute(q, params).mappings().all()
    df = pd.DataFrame(rows)

    if df.empty:
        st.info("Nessun dato nel periodo selezionato.")
    else:
        df["Stato"] = df.apply(lambda r: ui_status(r["settled"], r["voided"]), axis=1)
        cols = [c for c in ["id", "day", "cameriere", "amount", "created_at", "Stato"] if c in df.columns]
        df_view = df[cols].copy()

        st.dataframe(df_view, width="stretch")

        valid_mask = (df["voided"].astype(int) == 0) if "voided" in df.columns else pd.Series([True] * len(df))
        total = df.loc[valid_mask, "amount"].sum()

        c1, c2, c3 = st.columns(3)
        c1.metric("Totale periodo (esclude annullati)", f"€{total:,.2f}".replace(",", " "))
        c2.metric("N. battute valide", int(valid_mask.sum()))
        c3.metric("N. annullate", int((~valid_mask).sum()))

        csv = df_view.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Scarica CSV",
            data=csv,
            file_name="storico_incassi.csv",
            mime="text/csv"
        )
