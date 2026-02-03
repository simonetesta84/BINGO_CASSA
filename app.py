import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

# ---------------------------
# Config
# ---------------------------
st.set_page_config(page_title="BINGO CASSA", layout="wide")
DB_PATH = "incassi_app.sqlite3"

# Business day: 12:00 -> 12:00 (Europe/Zurich)
TZ = ZoneInfo("Europe/Zurich")
CUTOFF_HOUR = 12

# ---------------------------
# Business day helpers (12->12)
# ---------------------------
def business_day_for(dt: datetime, cutoff_hour: int = CUTOFF_HOUR) -> date:
    """Ritorna la 'giornata operativa' associata al datetime dt (12->12)."""
    local_dt = dt.astimezone(TZ)
    if local_dt.hour < cutoff_hour:
        return local_dt.date() - timedelta(days=1)
    return local_dt.date()

def business_day_range(day_: date, cutoff_hour: int = CUTOFF_HOUR) -> Tuple[datetime, datetime]:
    """Intervallo [start, end) della giornata operativa: day_ 12:00 -> day_+1 12:00."""
    start = datetime.combine(day_, time(hour=cutoff_hour, minute=0, second=0), tzinfo=TZ)
    end = start + timedelta(days=1)
    return start, end

def fmt_time_from_iso(iso_str: str) -> str:
    """Estrae HH:MM:SS da created_at ISO (con o senza timezone)."""
    if not iso_str:
        return ""
    # 2026-01-31T10:12:33+01:00 -> slice [11:19]
    if len(iso_str) >= 19 and "T" in iso_str:
        return iso_str[11:19]
    return iso_str[-8:]

# ---------------------------
# DB
# ---------------------------
def conn():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("PRAGMA foreign_keys = ON;")
    return c

def now():
    # timestamp coerente con timezone (utile per scavalcare mezzanotte senza confusione)
    return datetime.now(TZ).isoformat(timespec="seconds")

def init_db():
    c = conn()
    cur = c.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS waiters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,            -- YYYY-MM-DD  (giornata operativa 12->12)
            waiter_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (waiter_id) REFERENCES waiters(id)
        );
    """)

    # Migrazione: colonna settled (0=aperto, 1=incassato)
    try:
        cur.execute("ALTER TABLE transactions ADD COLUMN settled INTEGER NOT NULL DEFAULT 0;")
    except sqlite3.OperationalError:
        pass

    # Migrazione: colonna voided (0=valida, 1=annullata/tagliata)
    try:
        cur.execute("ALTER TABLE transactions ADD COLUMN voided INTEGER NOT NULL DEFAULT 0;")
    except sqlite3.OperationalError:
        pass

    # Tabella: camerieri in servizio per giorno (selezione manuale salvata a DB)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shift_waiters (
            day TEXT NOT NULL,            -- YYYY-MM-DD (giornata operativa 12->12)
            waiter_id INTEGER NOT NULL,
            PRIMARY KEY (day, waiter_id),
            FOREIGN KEY (waiter_id) REFERENCES waiters(id)
        );
    """)

    c.commit()
    c.close()

def get_waiters(active_only: bool = True) -> List[Tuple[int, str, int]]:
    c = conn()
    cur = c.cursor()
    if active_only:
        cur.execute("SELECT id, name, active FROM waiters WHERE active=1 ORDER BY name;")
    else:
        cur.execute("SELECT id, name, active FROM waiters ORDER BY active DESC, name;")
    rows = cur.fetchall()
    c.close()
    return rows

def add_waiter(name: str):
    name = (name or "").strip()
    if not name:
        raise ValueError("Inserisci un nome valido.")
    c = conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO waiters(name, active, created_at) VALUES (?, 1, ?);",
        (name, now())
    )
    c.commit()
    c.close()

def set_waiter_active(waiter_id: int, active: bool):
    c = conn()
    cur = c.cursor()
    cur.execute("UPDATE waiters SET active=? WHERE id=?;", (1 if active else 0, waiter_id))
    c.commit()
    c.close()

def add_tx(day_str: str, waiter_id: int, amount: float):
    if amount <= 0:
        raise ValueError("L'importo deve essere > 0.")
    c = conn()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO transactions(day, waiter_id, amount, created_at, settled, voided) VALUES (?,?,?,?,0,0);",
        (day_str, waiter_id, float(amount), now())
    )
    c.commit()
    c.close()

def void_tx(tx_id: int):
    """Annulla/Taglia una battuta: non si cancella, si marca voided=1."""
    c = conn()
    cur = c.cursor()
    cur.execute("UPDATE transactions SET voided=1 WHERE id=?;", (tx_id,))
    c.commit()
    c.close()

def settle_unsettled_for_waiter_day(day_str: str, waiter_id: int):
    """INCASSO PARZIALE: marca come incassate solo quelle non incassate (settled=0) e non void."""
    c = conn()
    cur = c.cursor()
    cur.execute("""
        UPDATE transactions
        SET settled=1
        WHERE day=? AND waiter_id=? AND settled=0 AND voided=0;
    """, (day_str, waiter_id))
    c.commit()
    c.close()

def totals_by_waiter_for_day(day_str: str) -> Dict[int, float]:
    """Totale giornata (incassato + aperto) ESCLUDENDO voided."""
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND voided=0
        GROUP BY waiter_id;
    """, (day_str,))
    d = {int(wid): float(tot) for wid, tot in cur.fetchall()}
    c.close()
    return d

def totals_unsettled_by_waiter_for_day(day_str: str) -> Dict[int, float]:
    """Totale DA INCASSARE (solo settled=0) ESCLUDENDO voided."""
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND settled=0 AND voided=0
        GROUP BY waiter_id;
    """, (day_str,))
    d = {int(wid): float(tot) for wid, tot in cur.fetchall()}
    c.close()
    return d

def totals_settled_by_waiter_for_day(day_str: str) -> Dict[int, float]:
    """Totale INCASSATO (solo settled=1) ESCLUDENDO voided."""
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT waiter_id, COALESCE(SUM(amount),0)
        FROM transactions
        WHERE day=? AND settled=1 AND voided=0
        GROUP BY waiter_id;
    """, (day_str,))
    d = {int(wid): float(tot) for wid, tot in cur.fetchall()}
    c.close()
    return d

def tx_by_waiter_for_day(day_str: str, waiter_id: int) -> List[Tuple[int, float, str, int, int]]:
    """Lista battute: (id, amount, created_at, settled, voided)"""
    c = conn()
    cur = c.cursor()
    cur.execute("""
        SELECT id, amount, created_at, settled, voided
        FROM transactions
        WHERE day=? AND waiter_id=?
        ORDER BY id DESC;
    """, (day_str, waiter_id))
    rows = cur.fetchall()
    c.close()
    return [(int(i), float(a), str(t), int(s), int(v)) for i, a, t, s, v in rows]

def history_df(date_from: str, date_to: str, waiter_id: Optional[int]) -> pd.DataFrame:
    c = conn()
    q = """
    SELECT t.id, t.day, w.name AS cameriere, t.amount, t.created_at, t.settled, t.voided
    FROM transactions t
    JOIN waiters w ON w.id=t.waiter_id
    WHERE t.day BETWEEN ? AND ?
    """
    params = [date_from, date_to]
    if waiter_id:
        q += " AND t.waiter_id=?"
        params.append(waiter_id)
    q += " ORDER BY t.day DESC, t.id DESC;"
    df = pd.read_sql_query(q, c, params=params)
    c.close()
    return df

# ---------- Shift waiters (selezione in DB) ----------
def get_shift_waiters(day_str: str) -> List[int]:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT waiter_id FROM shift_waiters WHERE day=? ORDER BY waiter_id;", (day_str,))
    ids = [int(r[0]) for r in cur.fetchall()]
    c.close()
    return ids

def set_shift_waiters(day_str: str, waiter_ids: List[int]):
    # salva esattamente la selezione del giorno (resetta sostituendo)
    c = conn()
    cur = c.cursor()
    cur.execute("DELETE FROM shift_waiters WHERE day=?;", (day_str,))
    cur.executemany(
        "INSERT OR IGNORE INTO shift_waiters(day, waiter_id) VALUES (?,?);",
        [(day_str, int(wid)) for wid in waiter_ids]
    )
    c.commit()
    c.close()
    
def ui_status(settled: int, voided: int) -> str:
    if voided == 1:
        return "Annullato"
    if settled == 1:
        return "Incassato"
    return "Aperto"
# ---------------------------
# CSS (tasti grandi + mini list + SIDEBAR custom)
# ---------------------------
st.markdown(
    """
<style>
/* Bottoni grandi e comodi */
[data-testid="stButton"] > button{
  height: 64px;
  font-size: 20px;
  border-radius: 10px;
}

/* Card border arrotondato */
[data-testid="stVerticalBlockBorderWrapper"]{
  border-radius: 18px;
}

/* Mini list accanto al tastierino */
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

h3 {
  margin-top: 0 !important;
  margin-bottom: 4px !important;
}

/* ---------------------------
   SIDEBAR custom (stile screenshot)
   --------------------------- */
[data-testid="stSidebar"]{
  background: #ffffff;
  border-right: 1px solid rgba(15, 23, 42, 0.06);
}
[data-testid="stSidebar"] .stSidebarContent{
  padding-top: 18px;
}

/* Brand title */
.sb-title{
  font-weight: 800;
  font-size: 18px;
  margin: 6px 0 14px 0;
  color: rgba(15, 23, 42, 0.88);
  padding: 0 10px;
}

/* Nav list */
.sb-nav{
  display:flex;
  flex-direction:column;
  gap:10px;
  padding: 0 10px;
}

.sb-item{
  display:flex;
  align-items:center;
  gap:12px;
  padding: 14px 14px;
  border-radius: 14px;
  text-decoration:none !important;
  user-select:none;
  transition: all .12s ease-in-out;
  color: rgba(15, 23, 42, 0.60);
  font-weight: 700;
}

.sb-item:hover{
  background: rgba(59, 130, 246, 0.08);
  color: rgba(15, 23, 42, 0.75);
}

.sb-item svg{
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  stroke: rgba(15, 23, 42, 0.50);
}

/* Active */
.sb-item.active{
  background: #2563eb;
  color: #ffffff;
  box-shadow: 0 10px 24px rgba(37, 99, 235, 0.22);
}
.sb-item.active svg{
  stroke: rgba(255,255,255,0.95);
}

/* pulizia margini */
[data-testid="stSidebar"] .element-container { margin-bottom: 0.35rem; }

/* ---- CTA sopra il titolo: INCASSI ---- */
.sb-cta-wrap{
  padding: 0 10px;
  margin-top: 4px;
  margin-bottom: 12px;
}

.sb-cta{
  display:flex;
  align-items:center;
  gap:12px;
  padding: 16px 16px;
  border-radius: 16px;
  text-decoration:none !important;
  user-select:none;
  background: #2563eb;
  color: #ffffff !important;
  font-weight: 900;
  box-shadow: 0 14px 30px rgba(37, 99, 235, 0.35);
  transition: all .12s ease-in-out;
}

.sb-cta:hover{
  background:#1d4ed8;
  transform: translateY(-1px);
}

.sb-cta svg{
  width:20px;
  height:20px;
  stroke: rgba(255,255,255,0.95);
}

/* se sei già su Incassi, leggermente più scuro */
.sb-cta.active{
  background:#1e40af;
}

</style>
""",
    unsafe_allow_html=True,
)

# ---------------------------
# Tastierino numerico
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
            if s == "0":
                st.session_state[buf_key] = ch
            else:
                st.session_state[buf_key] = s + ch

    # CSS solo per questo keypad (wrapper)
    st.markdown("""
        <style>
        /* --- PRIMARY (Aggiungi, Incasso) lasciali come vuoi --- */
        button[kind="primary"]{
          height: 64px;
          font-size: 60px;
          border-radius: 10px;
        }

        /* --- SECONDARY (tastierino) più grande --- */
        button[kind="secondary"]{
          height: 82px;       /* altezza tasti */
          font-size: 60px;    /* font tasti */
          font-weight: 900;
          border-radius: 14px;
        }

        /* Fallback per versioni Streamlit diverse */
        div[data-testid^="baseButton-secondary"] > button{
          height: 82px;
          font-size: 60px;
          font-weight: 900;
          border-radius: 14px;
        }
        
        div[class^="st-key-kp_"] [data-testid="stButton"] > button,
            div[class*=" st-key-kp_"] [data-testid="stButton"] > button{
              height: 86px !important;
              font-size: 50px !important;
              font-weight: 900 !important;
              border-radius: 16px !important;
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

    # Wrapper (fondamentale per targettare solo questi bottoni)
    st.markdown(f'<div data-kp-wrap="{prefix}">', unsafe_allow_html=True)

    grid = [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["7", "8", "9"],
        [".", "0", "Reset"],
    ]
    for row in grid:
        cols = st.columns(3, gap="small")
        for i, ch in enumerate(row):
            if cols[i].button(ch, key=f"{prefix}_k_{ch}", width='stretch'):
                press(ch)
                st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)

    return _get_value()


# ---------------------------
# Init
# ---------------------------
init_db()

# Seed demo se DB vuoto
if "seed_done" not in st.session_state:
    if len(get_waiters(active_only=False)) == 0:
        for n in ["Anna Bianchi", "Luigi Verdi", "Mario Rossi"]:
            try:
                add_waiter(n)
            except Exception:
                pass
    st.session_state["seed_done"] = True

# ---------------------------
# Sidebar nav (custom, stile screenshot)
#   usa query param ?page=...
# ---------------------------
# ---------------------------
# ---------------------------
# Sidebar nav (custom, stile screenshot) - FIX
# usa query param ?page=...
# ---------------------------

# query param corrente
qp = st.query_params
current = qp.get("page", "incassi")

KEY_TO_PAGE = {
    "dashboard": "Dashboard",
    "incassi": "Registra Incassi",
    "camerieri": "Camerieri",
    "storico": "Storico Incassi",
}

# Icona Incassi (CTA)
ICON_INCASSI = '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="12" rx="2"></rect><path d="M7 12h2"></path><path d="M15 12h2"></path></svg>'

# MENU (senza Incassi)
NAV_MENU = [
    ("dashboard", "Dashboard",
     '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 13h8V3H3v10z"></path><path d="M13 21h8V11h-8v10z"></path><path d="M13 3h8v6h-8V3z"></path><path d="M3 17h8v4H3v-4z"></path></svg>'),
    ("camerieri", "Camerieri",
     '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>'),
    ("storico", "Storico",
     '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"></path><path d="M7 14l4-4 3 3 6-6"></path></svg>'),
]

# Variabile page per il resto del codice
page = KEY_TO_PAGE.get(current, "Registra Incassi")

# ---------------------------
# Sidebar: CTA + (CALENDARIO SOTTO CTA) + Titolo + Menu
# ---------------------------
with st.sidebar:
    # CTA Incassi sopra al titolo
    cta_active = "active" if current == "incassi" else ""
    st.markdown(
        "<div class='sb-cta-wrap'>"
        f"<a class='sb-cta {cta_active}' href='?page=incassi'>"
        f"{ICON_INCASSI}"
        "<span>Incassi</span>"
        "</a>"
        "</div>",
        unsafe_allow_html=True
    )

    # ✅ Calendario sotto il bottone Incassi (solo Dashboard e Incassi)
    if page in ["Dashboard", "Registra Incassi"]:
        default_bd = business_day_for(datetime.now(TZ), cutoff_hour=CUTOFF_HOUR)
        day = st.date_input(
            "Giornata (12:00 → 12:00)",
            value=default_bd,
            key="sidebar_day"
        )
        day_str = day.isoformat()

        start_dt, end_dt = business_day_range(day, cutoff_hour=CUTOFF_HOUR)
        st.caption(
            f"Intervallo: {start_dt.strftime('%d.%m.%Y %H:%M')} → {end_dt.strftime('%d.%m.%Y %H:%M')}"
        )
    else:
        day_str = date.today().isoformat()

    # Titolo come prima (sotto calendario)
    st.markdown("<div class='sb-title'>BINGO CASSA</div>", unsafe_allow_html=True)

    # Menu identico ma senza Incassi
    html = ["<div class='sb-nav'>"]
    for key, label, icon_svg in NAV_MENU:
        is_active = "active" if key == current else ""
        html.append(
            f"<a class='sb-item {is_active}' href='?page={key}'>"
            f"{icon_svg}"
            f"<span>{label}</span>"
            f"</a>"
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


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

    st.divider()
    st.subheader("Riepilogo per cameriere")

    if not waiters:
        st.info("Nessun cameriere attivo.")
    else:
        cols = st.columns(min(4, len(waiters)))
        for i, (wid, name, _) in enumerate(waiters):
            with cols[i % len(cols)]:
                open_amt = tot_open.get(wid, 0.0)
                paid_amt = tot_paid.get(wid, 0.0)
                st.metric(
                    name,
                    f"Da incassare: €{open_amt:,.2f}".replace(",", " "),
                    delta=f" Totale incassato: €{paid_amt:,.2f}".replace(",", " ")
                )

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

    # ---- carico selezione dal DB per il giorno ----
    sel_key = f"selected_names_{day_str}"
    if sel_key not in st.session_state:
        shift_ids = get_shift_waiters(day_str)
        st.session_state[sel_key] = [id_to_name[wid] for wid in shift_ids if wid in id_to_name]

    selected_names = st.session_state[sel_key]
    selected_ids = [name_to_id[n] for n in selected_names if n in name_to_id]

    # Totali (le funzioni DB escludono già i voided)
    tot_open_map = totals_unsettled_by_waiter_for_day(day_str)
    tot_paid_map = totals_settled_by_waiter_for_day(day_str)

    # Cards camerieri
    if not selected_ids:
        st.info("Seleziona i camerieri in servizio in fondo alla pagina per iniziare.")
    else:
        cols = st.columns(len(selected_ids), gap="large")

        for i, wid in enumerate(selected_ids):
            name = id_to_name[wid]

            with cols[i]:
                txs = tx_by_waiter_for_day(day_str, wid)
                tot_open = tot_open_map.get(wid, 0.0)
                tot_paid = tot_paid_map.get(wid, 0.0)

                has_unsettled = any((settled == 0 and voided == 0) for _, _, _, settled, voided in txs)

                with st.container(border=True):
                    # Header
                    h_name, h_btn, h_tot = st.columns([3.8, 2.2, 2.7], gap="small")

                    with h_name:
                        st.markdown(f"### {name}")

                    with h_btn:
                        confirm_key = f"confirm_settle_{day_str}_{wid}"

                        # stato conferma (per singolo cameriere/giornata)
                        if confirm_key not in st.session_state:
                            st.session_state[confirm_key] = False

                        if not st.session_state[confirm_key]:
                            # PRIMO CLICK: chiede conferma
                            st.markdown("<div class='incasso-red'>", unsafe_allow_html=True)
                            ask = st.button(
                                "INCASSO",
                                type="primary",
                                use_container_width=True,
                                key=f"settle_btn_{day_str}_{wid}",
                                disabled=(not has_unsettled),
                                help="Clicca per incassare tutte le battute aperte"
                            )
                            st.markdown("</div>", unsafe_allow_html=True)

                            if ask and has_unsettled:
                                st.session_state[confirm_key] = True
                                st.rerun()

                        else:
                            st.warning("Confermi INCASSO?")

                            c_ok, c_no = st.columns(2, gap="small")

                            with c_ok:
                                st.markdown("<div class='incasso-red'>", unsafe_allow_html=True)
                                do_it = st.button(
                                    "✅ SI",
                                    type="primary",
                                    width='stretch',
                                    key=f"settle_confirm_{day_str}_{wid}",
                                )
                                st.markdown("</div>", unsafe_allow_html=True)

                            with c_no:
                                cancel = st.button(
                                    "NO",
                                    width='stretch',
                                    key=f"settle_cancel_{day_str}_{wid}",
                                )

                            if do_it:
                                settle_unsettled_for_waiter_day(day_str, wid)
                                st.session_state[confirm_key] = False
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

                    # Incassato oggi + Incasso
                    bb1, bb2 = st.columns([3, 1], gap="small")

                    # Tastierino + Ultime 8
                    left_pad, right_last = st.columns([2.9, 1.3], gap="large")

                    with left_pad:
                        amount = keypad_widget(prefix=f"kp_{day_str}_{wid}")

                        if st.button(
                            "Aggiungi",
                            type="primary",
                            width='stretch',
                            key=f"add_{day_str}_{wid}"
                        ):
                            try:
                                add_tx(day_str, wid, amount)
                                st.session_state[f"kp_{day_str}_{wid}_buf"] = ""
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))

                    with right_last:
                        st.markdown("<div class='mini-box-title'>Ultime 9</div>", unsafe_allow_html=True)
                        last9 = txs[:9]
                        if not last9:
                            st.caption("—")
                        else:
                            for _, amt, _, _, voided in last9:
                                if voided == 1:
                                    st.markdown(
                                        f"""
                                        <div class='mini-tx' style="
                                            background: rgba(239,68,68,0.12);
                                            border: 1px solid rgba(239,68,68,0.35);
                                            color: #991b1b;
                                            font-size:20px;
                                            text-decoration: line-through;">
                                            € {amt:,.2f}
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
                                            € {amt:,.2f}
                                        </div>
                                        """.replace(",", " "),
                                        unsafe_allow_html=True
                                    )

                    st.divider()

                    # Lista completa (scroll)
                    if not txs:
                        st.caption("Nessun incasso oggi")
                    else:
                        with st.container(height=2000):
                            for tx_id, amt, created_at, settled, voided in txs:

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
                                            {fmt_time_from_iso(created_at)}
                                          </span>
                                        </div>
                                        """.replace(",", " "),
                                        unsafe_allow_html=True,
                                    )

                                with r2:
                                    if voided == 1:
                                        st.button("⛔", disabled=True, width='stretch', key=f"voided_{tx_id}")
                                    elif settled == 1:
                                        st.button("✅", disabled=True, width='stretch', key=f"ok_{tx_id}")
                                    else:
                                        if st.button("🗑️", key=f"void_{tx_id}", width='stretch', help="Annulla battuta"):
                                            void_tx(tx_id)
                                            st.rerun()

        # Totali in fondo (solo selezionati)
        st.divider()
        overall_open = sum(tot_open_map.get(wid, 0.0) for wid in selected_ids)
        overall_paid = sum(tot_paid_map.get(wid, 0.0) for wid in selected_ids)
        a, b = st.columns(2)
        a.metric("Totale da incassare (selezionati)", f"€{overall_open:,.2f}".replace(",", " "))
        # b.metric("Totale incassato (selezionati)", f"€{overall_paid:,.2f}".replace(",", " "))

    # Camerieri in servizio oggi – IN BASSO PAGINA (MANUALE + DB)
    st.divider()
    st.subheader("Camerieri in servizio oggi")

    new_selected = st.multiselect(
        "Seleziona i camerieri presenti oggi",
        options=names,
        default=st.session_state[sel_key],
    )

    if st.button("✅ Applica selezione", type="primary", width='stretch'):
        st.session_state[sel_key] = new_selected
        set_shift_waiters(day_str, [name_to_id[n] for n in new_selected if n in name_to_id])
        st.rerun()

# ---------------------------
# Camerieri
# ---------------------------
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
                    st.success("Aggiunto.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Nome già presente.")
                except Exception as e:
                    st.error(str(e))

    with right:
        st.subheader("Elenco")
        rows = get_waiters(active_only=False)
        if not rows:
            st.info("Nessun cameriere.")
        else:
            for wid, name, active in rows:
                c1, c2, c3 = st.columns([1, 1, 1], gap="small")
                c1.write(f"**{name}**")
                c2.write("✅" if active else "❌")
                new_active = c3.toggle("Attivo", value=bool(active), key=f"act_{wid}")
                if new_active != bool(active):
                    set_waiter_active(wid, new_active)
                    st.rerun()

# ---------------------------
# Storico Incassi
# ---------------------------
else:
    st.title("Storico Incassi")

    def ui_status(settled: int, voided: int) -> str:
        # Annullato vince sempre
        if int(voided) == 1:
            return "Annullato"
        if int(settled) == 1:
            return "Incassato"
        return "Aperto"

    waiters_all = get_waiters(active_only=False)
    id_to_name = {wid: name for wid, name, _ in waiters_all}

    f1, f2, f3 = st.columns([1, 1, 1.2], gap="small")
    with f1:
        d_from = st.date_input("Da", value=date.today().replace(day=1))
    with f2:
        d_to = st.date_input("A", value=date.today())
    with f3:
        options = ["Tutti"] + [id_to_name[wid] for wid in id_to_name]
        choice = st.selectbox("Cameriere", options=options, index=0)

    waiter_id = None
    if choice != "Tutti":
        # più semplice: ricava id dal nome scelto
        name_to_id_all = {name: wid for wid, name, _ in waiters_all}
        waiter_id = name_to_id_all.get(choice)

    df = history_df(d_from.isoformat(), d_to.isoformat(), waiter_id)

    if df.empty:
        st.info("Nessun dato nel periodo selezionato.")
    else:
        # Aggiungi colonna Stato (leggibile)
        if "settled" in df.columns and "voided" in df.columns:
            df["Stato"] = df.apply(lambda r: ui_status(r["settled"], r["voided"]), axis=1)
        else:
            df["Stato"] = "—"

        # Riordina colonne e rimuovi le tecniche
        cols = [c for c in ["id", "day", "cameriere", "amount", "created_at", "Stato"] if c in df.columns]
        df_view = df[cols].copy()

        # Mostra tabella pulita
        st.dataframe(df_view, width='stretch')

        # Totale periodo (esclude annullati) + conteggi utili
        valid_mask = (df["voided"] == 0) if "voided" in df.columns else pd.Series([True] * len(df))
        total = df.loc[valid_mask, "amount"].sum()

        c1, c2, c3 = st.columns(3)
        c1.metric("Totale periodo (esclude annullati)", f"€{total:,.2f}".replace(",", " "))
        c2.metric("N. battute valide", int(valid_mask.sum()))
        c3.metric("N. annullate", int((~valid_mask).sum()))

        # CSV: esporta la vista pulita
        csv = df_view.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Scarica CSV",
            data=csv,
            file_name="storico_incassi.csv",
            mime="text/csv"
        )
