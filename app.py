"""
Gov Proposal Monitor  ·  app.py
Single file: Streamlit UI + Anthropic scanner + SQLite + APScheduler
Tweak this file, push to GitHub, Railway auto-deploys.
"""

import os, re, json, time, threading, calendar as cal_mod
import sqlite3
from datetime import date, datetime, timedelta

import streamlit as st
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS  (edit freely)
# ─────────────────────────────────────────────────────────────────────────────
MODEL          = "claude-sonnet-4-6"
DB_PATH        = "monitor.db"
MAX_LOG_ROWS   = 50

DEFAULT_KEYWORDS = [
    "lunar lander", "CLPS", "cislunar", "commercial lunar payload",
    "space domain awareness", "launch services", "spacecraft bus",
    "lunar surface", "moon landing", "deep space navigation",
]
DEFAULT_AGENCIES  = ["NASA", "Space Force", "DARPA"]
ALL_AGENCIES      = ["NASA", "Space Force", "DARPA", "NRO", "DoD", "AFRL", "MDA", "NOAA"]
REFRESH_OPTIONS   = {"6 hours": 6, "12 hours": 12, "1 day": 24}

TYPE_COLORS = {
    "RFP":            "#185FA5",
    "RFI":            "#BA7517",
    "BAA":            "#3B6D11",
    "SBIR/STTR":      "#534AB7",
    "Contract Award": "#A32D2D",
    "Sources Sought": "#5F5E5A",
}

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS opportunities (
                id                TEXT PRIMARY KEY,
                title             TEXT,
                agency            TEXT,
                sub_agency        TEXT,
                type              TEXT,
                solicitation_no   TEXT,
                posted_date       TEXT,
                deadline          TEXT,
                description       TEXT,
                url               TEXT,
                relevance_score   INTEGER DEFAULT 0,
                matched_keywords  TEXT DEFAULT '[]',
                estimated_value   TEXT,
                updated_at        TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS scan_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                message    TEXT,
                level      TEXT DEFAULT 'info',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

def cfg_get(key, default=None):
    row = db().execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    if row:
        try:    return json.loads(row[0])
        except: return row[0]
    return default

def cfg_set(key, value):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (key, json.dumps(value)))

def log(msg, level="info"):
    with db() as c:
        c.execute("INSERT INTO scan_log(message,level) VALUES(?,?)", (msg, level))
        c.execute(f"DELETE FROM scan_log WHERE id NOT IN (SELECT id FROM scan_log ORDER BY id DESC LIMIT {MAX_LOG_ROWS})")

def save_opportunities(opps, sources):
    with db() as c:
        for o in opps:
            c.execute("""
                INSERT OR REPLACE INTO opportunities
                    (id,title,agency,sub_agency,type,solicitation_no,posted_date,deadline,
                     description,url,relevance_score,matched_keywords,estimated_value,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """, (
                o.get("id",""), o.get("title",""), o.get("agency",""), o.get("subAgency"),
                o.get("type",""), o.get("solicitationNumber"), o.get("postedDate"),
                o.get("deadline"), o.get("description",""), o.get("url"),
                o.get("relevanceScore", 0), json.dumps(o.get("matchedKeywords",[])),
                o.get("estimatedValue"),
            ))

def load_opportunities():
    rows = db().execute("SELECT * FROM opportunities").fetchall()
    return [dict(r) for r in rows]

def load_logs(n=30):
    rows = db().execute("SELECT message,level,created_at FROM scan_log ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# JSON REPAIR  (handles truncated / trailing-comma responses)
# ─────────────────────────────────────────────────────────────────────────────
def robust_parse(text: str) -> dict | None:
    text = re.sub(r"```json|```", "", text).strip()

    # 1. Direct parse
    try: return json.loads(text)
    except: pass

    # 2. Find outermost { and strip trailing commas
    start = text.find("{")
    if start == -1: return None
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text[start:])
    try: return json.loads(cleaned)
    except: pass

    # 3. Salvage complete objects from a truncated opportunities array
    idx = cleaned.find('"opportunities"')
    if idx == -1: return None
    arr = cleaned.find("[", idx)
    if arr == -1: return None

    opps, depth, in_str, esc, obj_start = [], 0, False, False, -1
    for i, ch in enumerate(cleaned[arr+1:], arr+1):
        if esc:              esc = False;  continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"':        in_str = not in_str; continue
        if in_str:           continue
        if ch == "{":
            if depth == 0:   obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                seg = re.sub(r",(\s*[}\]])", r"\1", cleaned[obj_start:i+1])
                try:    opps.append(json.loads(seg))
                except: pass
                obj_start = -1
        elif ch == "]" and depth == 0:
            break

    return {"opportunities": opps, "sources": []} if opps else None

# ─────────────────────────────────────────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────────────────────────────────────────
def run_scan(keywords: list[str] | None = None, agencies: list[str] | None = None) -> int:
    keywords = keywords or cfg_get("keywords", DEFAULT_KEYWORDS)
    agencies = agencies or cfg_get("agencies",  DEFAULT_AGENCIES)

    log(f"Scanning {', '.join(agencies)} · {len(keywords)} keywords…")

    api_key = (st.secrets.get("ANTHROPIC_API_KEY") if hasattr(st, "secrets") else None) \
              or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("ANTHROPIC_API_KEY not set", "error")
        return 0

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""Find up to 10 current government contract opportunities for space companies
(e.g. Intuitive Machines, Astrobotic, Firefly Aerospace).
Agencies: {', '.join(agencies)}
Keywords: {', '.join(keywords)}
Search SAM.gov, SBIR.gov, NASA SEWP, SpaceWERX, DARPA BAAs.
Focus on opportunities posted in the last 60 days or with upcoming deadlines.

IMPORTANT: Return ONLY valid JSON — no markdown, no explanation. Keep descriptions under 40 words each.

{{"opportunities":[{{"id":"uid","title":"full title","agency":"NASA","subAgency":"GSFC or null",
"type":"RFP or RFI or BAA or SBIR/STTR or Contract Award or Sources Sought",
"solicitationNumber":"number or null","postedDate":"YYYY-MM-DD or null",
"deadline":"YYYY-MM-DD or null","description":"short description under 40 words",
"url":"https://... or null","relevanceScore":85,
"matchedKeywords":["kw"],"estimatedValue":"$Xm or null"}}],
"sources":["sam.gov"]}}"""

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system="You are a government contract intelligence agent for the space industry. "
                   "Return ONLY valid JSON — no markdown, no code fences, no preamble. "
                   "Keep descriptions under 40 words. Return at most 10 opportunities.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = robust_parse(text)

        if not parsed:
            log("Could not parse API response — try again", "error")
            return 0

        opps    = parsed.get("opportunities", [])
        sources = parsed.get("sources", [])
        save_opportunities(opps, sources)
        cfg_set("last_scanned", datetime.now().isoformat())
        log(f"Found {len(opps)} opportunities · {', '.join(sources) or 'various'}", "success")
        return len(opps)

    except Exception as e:
        log(f"Scan failed: {e}", "error")
        return 0

# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER  (singleton via st.cache_resource)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource
def get_scheduler():
    sched = BackgroundScheduler(daemon=True)
    sched.start()
    return sched

def apply_schedule(hours: int):
    sched = get_scheduler()
    sched.remove_all_jobs()
    if hours > 0:
        sched.add_job(run_scan, "interval", hours=hours, id="scan_job",
                      next_run_time=datetime.now() + timedelta(hours=hours))
    cfg_set("refresh_hours", hours)

# ─────────────────────────────────────────────────────────────────────────────
# HOLIDAYS
# ─────────────────────────────────────────────────────────────────────────────
def _nth(year, month, weekday, n):
    """weekday 0=Mon…6=Sun.  n=-1 → last occurrence."""
    last_day = cal_mod.monthrange(year, month)[1]
    if n == -1:
        d = date(year, month, last_day)
        while d.weekday() != weekday: d -= timedelta(1)
        return d.day
    count = 0
    for day in range(1, last_day+1):
        if date(year, month, day).weekday() == weekday:
            count += 1
            if count == n: return day

def federal_holidays(year: int) -> dict[str, str]:
    h = {}
    def fixed(name, month, day):
        d = date(year, month, day)
        if d.weekday() == 5: d -= timedelta(1)   # Sat → Fri
        if d.weekday() == 6: d += timedelta(1)   # Sun → Mon
        h[d.isoformat()] = name

    fixed("New Year's Day",   1,  1)
    fixed("Juneteenth",       6, 19)
    fixed("Independence Day", 7,  4)
    fixed("Veterans Day",    11, 11)
    fixed("Christmas Day",   12, 25)

    h[date(year,  1, _nth(year,  1, 0, 3)).isoformat()] = "MLK Jr. Day"
    h[date(year,  2, _nth(year,  2, 0, 3)).isoformat()] = "Presidents' Day"
    h[date(year,  5, _nth(year,  5, 0,-1)).isoformat()] = "Memorial Day"
    h[date(year,  9, _nth(year,  9, 0, 1)).isoformat()] = "Labor Day"
    h[date(year, 10, _nth(year, 10, 0, 2)).isoformat()] = "Columbus Day"
    h[date(year, 11, _nth(year, 11, 3, 4)).isoformat()] = "Thanksgiving"   # 3=Thu

    return h

def working_days(deadline_str: str, holidays: dict) -> int | None:
    if not deadline_str: return None
    try:    target = date.fromisoformat(deadline_str)
    except: return None
    today = date.today()
    if target <= today: return 0
    n, d = 0, today + timedelta(1)
    while d <= target:
        if d.weekday() < 5 and d.isoformat() not in holidays: n += 1
        d += timedelta(1)
    return n

# ─────────────────────────────────────────────────────────────────────────────
# UI HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def deadline_info(deadline_str: str) -> tuple[int | None, str, str]:
    """Returns (days_left, label, hex_color)."""
    if not deadline_str:
        return None, "—", "#9ca3af"
    try:    days = (date.fromisoformat(deadline_str) - date.today()).days
    except: return None, "—", "#9ca3af"
    if days < 0:  return days, "Expired",          "#9ca3af"
    if days == 0: return 0,    "Due today",         "#A32D2D"
    if days <= 7: return days, f"{days}d left 🔴",  "#A32D2D"
    if days <= 21:return days, f"{days}d 🟡",       "#BA7517"
    return days, f"{days}d 🟢", "#3B6D11"

def badge(text: str, color: str, size=12) -> str:
    bg = color + "22"
    return (f'<span style="background:{bg};color:{color};border:1px solid {color}55;'
            f'padding:3px 10px;border-radius:20px;font-size:{size}px;font-weight:700">{text}</span>')

def render_calendar(opps: list[dict], holidays: dict, show_wdays: bool):
    if "cy" not in st.session_state:
        st.session_state.cy = date.today().year
        st.session_state.cm = date.today().month

    dl_map: dict[str, list] = {}
    for o in opps:
        if o.get("deadline"):
            dl_map.setdefault(o["deadline"], []).append(o)

    c1, c2, c3 = st.columns([1, 3, 1])
    with c1:
        if st.button("◀", key="prev_mo"):
            if st.session_state.cm == 1: st.session_state.cy -= 1; st.session_state.cm = 12
            else: st.session_state.cm -= 1
    with c2:
        mo_label = date(st.session_state.cy, st.session_state.cm, 1).strftime("%B %Y")
        st.markdown(f"<h4 style='text-align:center;margin:4px 0'>{mo_label}</h4>", unsafe_allow_html=True)
    with c3:
        if st.button("▶", key="next_mo"):
            if st.session_state.cm == 12: st.session_state.cy += 1; st.session_state.cm = 1
            else: st.session_state.cm += 1

    y, m = st.session_state.cy, st.session_state.cm
    today_str = date.today().isoformat()
    first_dow = (date(y, m, 1).weekday() + 1) % 7   # shift so Sun=0
    dim = cal_mod.monthrange(y, m)[1]
    cells = [None]*first_dow + list(range(1, dim+1))
    while len(cells) % 7: cells.append(None)

    dot_css = "display:inline-block;width:7px;height:7px;border-radius:50%;margin:1px;"
    html = """
    <style>
      .gcal{width:100%;border-collapse:separate;border-spacing:3px}
      .gcal th{font-size:11px;font-weight:700;color:#9ca3af;text-align:center;padding:4px}
      .gcal td{text-align:center;vertical-align:top;border-radius:8px;
               min-height:46px;padding:4px 2px;font-size:13px;width:14.28%}
      .day-num{font-size:13px;line-height:1.2}
      .g-past {color:#d1d5db;background:#fafafa}
      .g-today{background:#E6F1FB;border:2px solid #185FA5;font-weight:700;color:#185FA5}
      .g-hol  {background:#FAEEDA;color:#BA7517}
      .g-wknd {color:#ef4444;background:#fafafa}
      .g-norm {background:#fff;border:1px solid #f3f4f6}
    </style>
    <table class='gcal'>
    <tr><th>Su</th><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th></tr>
    """
    for week in [cells[i:i+7] for i in range(0, len(cells), 7)]:
        html += "<tr>"
        for col, day in enumerate(week):
            if day is None: html += "<td></td>"; continue
            ds     = date(y, m, day).isoformat()
            is_today  = ds == today_str
            is_wknd   = col in (0, 6)
            is_hol    = ds in holidays
            is_past   = ds < today_str
            day_opps  = dl_map.get(ds, [])

            if   is_today:                   css = "g-today"
            elif is_hol:                     css = "g-hol"
            elif is_past and not day_opps:   css = "g-past"
            elif is_wknd:                    css = "g-wknd"
            else:                            css = "g-norm"

            dots = "".join(
                f'<span style="{dot_css}background:{TYPE_COLORS.get(o["type"],"#888")}"></span>'
                for o in day_opps[:4]
            ) + (f'<span style="font-size:9px">+{len(day_opps)-4}</span>' if len(day_opps)>4 else "")

            hol_tip = f'title="{holidays[ds]}"' if is_hol else ""
            wday_tip = ""
            if show_wdays and ds > today_str and day_opps:
                wd = working_days(ds, holidays)
                wday_tip = f' ({wd} work days)' if wd is not None else ""

            html += f'<td class="{css}" {hol_tip}><div class="day-num">{day}{wday_tip}</div><div>{dots}</div></td>'
        html += "</tr>"
    html += "</table>"

    # Legend
    html += "<div style='margin-top:10px;display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#4b5563'>"
    for t, c in TYPE_COLORS.items():
        html += f'<span><span style="{dot_css}background:{c}"></span>{t}</span>'
    html += ('<span><span style="display:inline-block;width:9px;height:9px;border-radius:3px;'
             'background:#FAEEDA;border:2px solid #BA7517;margin-right:3px"></span>US Holiday</span>')
    html += "</div>"

    st.markdown(html, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="Gov Proposal Monitor", page_icon="🛰️", layout="wide")
    st.markdown("""
        <style>
        .block-container{padding-top:1.2rem;max-width:1400px}
        [data-testid="metric-container"]{background:#f9fafb;border-radius:10px;
            padding:14px;border:1px solid #e5e7eb}
        </style>
    """, unsafe_allow_html=True)

    init_db()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## ⚙️ Configure")

        # Keywords
        st.markdown("**Keywords** (one per line)")
        saved_kw = cfg_get("keywords", DEFAULT_KEYWORDS)
        new_kw_raw = st.text_area("keywords", "\n".join(saved_kw), height=220,
                                  label_visibility="collapsed")
        new_kw = [k.strip() for k in new_kw_raw.splitlines() if k.strip()]
        if new_kw != saved_kw:
            cfg_set("keywords", new_kw)

        # Agencies
        st.markdown("**Agencies**")
        saved_ag = cfg_get("agencies", DEFAULT_AGENCIES)
        new_ag = []
        cols = st.columns(2)
        for i, a in enumerate(ALL_AGENCIES):
            if cols[i % 2].checkbox(a, value=a in saved_ag, key=f"ag_{a}"):
                new_ag.append(a)
        if new_ag != saved_ag:
            cfg_set("agencies", new_ag)

        st.divider()

        # Schedule
        st.markdown("**Auto-scan**")
        saved_hrs = cfg_get("refresh_hours", 24)
        freq_label = st.selectbox("Frequency", list(REFRESH_OPTIONS.keys()),
            index=list(REFRESH_OPTIONS.values()).index(saved_hrs)
                  if saved_hrs in REFRESH_OPTIONS.values() else 2,
            label_visibility="collapsed")
        auto_on = st.toggle("Enable auto-scan", value=bool(saved_hrs))
        chosen_hrs = REFRESH_OPTIONS[freq_label] if auto_on else 0
        if chosen_hrs != saved_hrs:
            apply_schedule(chosen_hrs)

        st.divider()
        show_wdays = st.toggle("Show working days", value=False)
        if st.button("🔍  Scan Now", use_container_width=True, type="primary"):
            with st.spinner("Searching federal procurement databases…"):
                n = run_scan(new_kw, new_ag)
            st.success(f"✅ Found {n} opportunities")
            st.rerun()

        # Sources legend
        st.markdown("**Sources**")
        for s in ["SAM.gov","SBIR.gov","NASA SEWP","SpaceWERX","DARPA.mil","USASpending"]:
            st.caption(f"• {s}")

    # ── Header ───────────────────────────────────────────────────────────────
    last_ts = cfg_get("last_scanned")
    status  = ("Auto-scan on" if chosen_hrs else "Auto-scan off") if True else ""
    last_lbl = (datetime.fromisoformat(last_ts).strftime("Last scan %b %d at %H:%M") + f"  ·  {status}"
                if last_ts else "Not yet scanned — click Scan Now")

    st.markdown("## 🛰️  Gov Proposal Monitor")
    st.caption(last_lbl)

    # ── Load data ─────────────────────────────────────────────────────────────
    opps = load_opportunities()
    today = date.today()
    all_holidays: dict[str, str] = {}
    for yr in [today.year - 1, today.year, today.year + 1]:
        all_holidays.update(federal_holidays(yr))

    if not opps:
        st.info("No opportunities yet.  Configure keywords → click **Scan Now**.")
        return

    # ── Stat cards ────────────────────────────────────────────────────────────
    def days_left(o):
        if not o.get("deadline"): return None
        try:    return (date.fromisoformat(o["deadline"]) - today).days
        except: return None

    urgent = sum(1 for o in opps if (d := days_left(o)) is not None and 0 < d <= 14)
    high   = sum(1 for o in opps if (o.get("relevance_score") or 0) >= 80)
    rfps   = sum(1 for o in opps if o.get("type") == "RFP")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tracked",    len(opps))
    c2.metric("Open RFPs",  rfps)
    c3.metric("Due < 14d",  urgent,  delta=f"{urgent} urgent" if urgent else None, delta_color="inverse")
    c4.metric("High match", high)

    st.divider()

    # ── Quick-lookup table ────────────────────────────────────────────────────
    st.markdown("### 📋 Quick Lookup")

    import pandas as pd

    def sort_key(o):
        return (o.get("deadline") or "9999-12-31", -(o.get("relevance_score") or 0))

    rows = []
    for o in sorted(opps, key=sort_key):
        dl, lbl, col = deadline_info(o.get("deadline",""))
        wd = working_days(o.get("deadline"), all_holidays) if show_wdays and o.get("deadline") else None
        dl_cell = f"{o['deadline']}  ({dl}d)" if dl and dl > 0 else (o.get("deadline","—") or "—")
        if wd is not None: dl_cell += f"  /  {wd}wd"
        rows.append({
            "Title":    (o.get("title","") or "")[:75] + ("…" if len(o.get("title",""))>75 else ""),
            "Type":     o.get("type",""),
            "Agency":   o.get("agency",""),
            "Score":    o.get("relevance_score") or 0,
            "Deadline": dl_cell,
            "_days":    dl if dl is not None else 9999,
        })

    df = pd.DataFrame(rows)

    def style_type(v):
        c = TYPE_COLORS.get(v, "#5F5E5A")
        return f"color:{c};font-weight:700"

    def style_deadline(v):
        if "—" in v or not v: return "color:#9ca3af"
        # Extract days from cell "(Xd)" pattern
        m = re.search(r"\((\d+)d\)", v)
        if not m: return "color:#9ca3af"
        d = int(m.group(1))
        if d <= 7:  return "color:#A32D2D;font-weight:700"
        if d <= 21: return "color:#BA7517;font-weight:600"
        return "color:#3B6D11"

    def style_score(v):
        if v >= 80: return "color:#3B6D11;font-weight:700"
        if v >= 60: return "color:#BA7517;font-weight:600"
        return "color:#5F5E5A"

    display = df[["Title","Type","Agency","Score","Deadline"]]
    styled  = (display.style
               .applymap(style_type,     subset=["Type"])
               .applymap(style_deadline, subset=["Deadline"])
               .applymap(style_score,    subset=["Score"])
               .format({"Score": "{}%"})
               .set_properties(**{"font-size": "13px"}))

    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=min(420, 45 + len(df)*36))

    st.divider()

    # ── Filters ───────────────────────────────────────────────────────────────
    f1, f2, f3 = st.columns([1, 1, 2])
    type_f   = f1.selectbox("Type",   ["All"]+list(TYPE_COLORS.keys()), label_visibility="collapsed")
    agency_f = f2.selectbox("Agency", ["All"]+sorted({o["agency"] for o in opps if o.get("agency")}),
                            label_visibility="collapsed")
    sort_by  = f3.radio("Sort", ["Relevance","Deadline","Posted"], horizontal=True,
                        label_visibility="collapsed")

    filtered = [o for o in opps
                if (type_f   == "All" or o.get("type")   == type_f)
                and (agency_f == "All" or o.get("agency") == agency_f)]

    def card_sort(o):
        if sort_by == "Relevance": return -(o.get("relevance_score") or 0)
        if sort_by == "Deadline":  return  (o.get("deadline")         or "9999")
        return                            -(o.get("posted_date")       or "0000")
    filtered.sort(key=card_sort)

    st.markdown(f"### Opportunities &nbsp; <span style='font-size:14px;color:#9ca3af'>{len(filtered)} results</span>",
                unsafe_allow_html=True)

    # ── Cards ─────────────────────────────────────────────────────────────────
    for o in filtered:
        tc = TYPE_COLORS.get(o.get("type",""), "#5F5E5A")
        dl, lbl, dlcol = deadline_info(o.get("deadline"))
        wd = working_days(o.get("deadline"), all_holidays) if show_wdays else None
        score = o.get("relevance_score") or 0
        bar_c = "#3B6D11" if score>=80 else "#BA7517" if score>=60 else "#9ca3af"

        with st.container(border=True):
            # Badge row
            badges = badge(o.get("type",""), tc)
            badges += f' &nbsp; {badge(o.get("agency",""), "#374151")}'
            if o.get("estimated_value"):
                badges += f' &nbsp; {badge(o["estimated_value"], "#3B6D11")}'
            score_bar = (f'<span style="float:right;font-size:12px;color:{bar_c};font-weight:700">'
                         f'{score}%</span>')
            st.markdown(badges + score_bar, unsafe_allow_html=True)

            # Title
            title = o.get("title","Untitled")
            if o.get("url"): st.markdown(f"**[{title}]({o['url']})**")
            else:            st.markdown(f"**{title}**")

            if o.get("solicitation_no"):
                st.caption(f"`{o['solicitation_no']}`")

            st.markdown(o.get("description") or "")

            # Footer row
            fc1, fc2, fc3 = st.columns([1, 2, 1])
            if o.get("posted_date"): fc1.caption(f"Posted {o['posted_date']}")
            if dl is not None and o.get("deadline"):
                wday_str = f" / {wd} work days" if wd is not None else ""
                fc2.markdown(
                    f'<span style="color:{dlcol};font-weight:600;font-size:13px">'
                    f'{lbl}{wday_str} · Due {o["deadline"]}</span>',
                    unsafe_allow_html=True)
            if o.get("estimated_value"):
                fc3.markdown(f'<span style="color:#3B6D11;font-weight:700">{o["estimated_value"]}</span>',
                             unsafe_allow_html=True)

    st.divider()

    # ── Calendar ──────────────────────────────────────────────────────────────
    st.markdown("### 📅 Deadline Calendar")
    render_calendar(opps, all_holidays, show_wdays)

    st.divider()

    # ── Scan log ──────────────────────────────────────────────────────────────
    with st.expander("Scan log"):
        for entry in load_logs():
            icon = {"success":"✅","error":"❌"}.get(entry["level"],"ℹ️")
            st.markdown(f'`{entry["created_at"][:16]}` {icon} {entry["message"]}')

if __name__ == "__main__":
    main()
