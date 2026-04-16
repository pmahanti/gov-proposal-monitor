"""
Gov Proposal Monitor  -  app.py
Single file: Streamlit UI + Anthropic scanner + SQLite + APScheduler
+ Historical funding via USASpending.gov and SBIR.gov (no extra API key needed)
+ News intelligence scan (space industry award announcements)
+ SAM.gov PDF solicitation parser
Tweak this file, push to GitHub, Railway auto-deploys.
"""

import os, re, json, calendar as cal_mod, sqlite3, requests
from datetime import date, datetime, timedelta

import streamlit as st
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler

# -----------------------------------------------------------------------------
# CONSTANTS  (edit freely)
# -----------------------------------------------------------------------------
MODEL         = "claude-sonnet-4-6"
DB_PATH       = "monitor.db"
MAX_LOG_ROWS  = 50

DEFAULT_KEYWORDS = [
    "lunar lander", "CLPS", "cislunar", "commercial lunar payload",
    "space domain awareness", "launch services", "spacecraft bus",
    "lunar surface", "moon landing", "deep space navigation",
]
DEFAULT_AGENCIES = ["NASA", "Space Force", "DARPA"]
ALL_AGENCIES     = ["NASA", "Space Force", "DARPA", "NRO", "DoD", "AFRL", "MDA", "NOAA"]
REFRESH_OPTIONS  = {"6 hours": 6, "12 hours": 12, "1 day": 24}

TYPE_COLORS = {
    "RFP":            "#185FA5",
    "RFI":            "#BA7517",
    "BAA":            "#3B6D11",
    "SBIR/STTR":      "#534AB7",
    "Contract Award": "#A32D2D",
    "Sources Sought": "#5F5E5A",
}

SPACE_COMPANIES = [
    "Intuitive Machines", "Astrobotic", "Firefly Aerospace",
    "SpaceX", "Blue Origin", "Rocket Lab", "Sierra Space",
    "Maxar Technologies", "Planet Labs", "Redwire",
]

USASPENDING_URL = "https://api.usaspending.gov/api/v2"
SBIR_URL        = "https://api.sbir.gov/awards"

# -----------------------------------------------------------------------------
# DATABASE
# -----------------------------------------------------------------------------
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
            CREATE TABLE IF NOT EXISTS funding_cache (
                cache_key  TEXT PRIMARY KEY,
                data       TEXT,
                fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS news_intel (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                headline     TEXT,
                summary      TEXT,
                company      TEXT,
                agency       TEXT,
                award_value  TEXT,
                source_url   TEXT,
                published    TEXT,
                scanned_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS pdf_analysis (
                solicitation_no  TEXT PRIMARY KEY,
                title            TEXT,
                url              TEXT,
                requirements     TEXT,
                eval_criteria    TEXT,
                page_limits      TEXT,
                key_dates        TEXT,
                summary          TEXT,
                raw_text         TEXT,
                analyzed_at      TEXT DEFAULT CURRENT_TIMESTAMP
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
        c.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                  (key, json.dumps(value)))

def log(msg, level="info"):
    with db() as c:
        c.execute("INSERT INTO scan_log(message,level) VALUES(?,?)", (msg, level))
        c.execute(
            "DELETE FROM scan_log WHERE id NOT IN "
            "(SELECT id FROM scan_log ORDER BY id DESC LIMIT ?)",
            (MAX_LOG_ROWS,)
        )

def save_opportunities(opps, sources):
    with db() as c:
        for o in opps:
            c.execute("""
                INSERT OR REPLACE INTO opportunities
                    (id,title,agency,sub_agency,type,solicitation_no,posted_date,deadline,
                     description,url,relevance_score,matched_keywords,estimated_value,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            """, (
                o.get("id", ""), o.get("title", ""), o.get("agency", ""),
                o.get("subAgency"), o.get("type", ""), o.get("solicitationNumber"),
                o.get("postedDate"), o.get("deadline"), o.get("description", ""),
                o.get("url"), o.get("relevanceScore", 0),
                json.dumps(o.get("matchedKeywords", [])), o.get("estimatedValue"),
            ))

def load_opportunities():
    rows = db().execute("SELECT * FROM opportunities").fetchall()
    return [dict(r) for r in rows]

def load_logs(n=30):
    rows = db().execute(
        "SELECT message,level,created_at FROM scan_log ORDER BY id DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in rows]

# -----------------------------------------------------------------------------
# FUNDING CACHE
# -----------------------------------------------------------------------------
def cache_get(key):
    row = db().execute(
        "SELECT data, fetched_at FROM funding_cache WHERE cache_key=?", (key,)
    ).fetchone()
    if not row:
        return None
    age = datetime.now() - datetime.fromisoformat(row["fetched_at"])
    if age.total_seconds() > 86400:
        return None
    try:    return json.loads(row["data"])
    except: return None

def cache_set(key, data):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO funding_cache(cache_key,data) VALUES(?,?)",
            (key, json.dumps(data))
        )

# -----------------------------------------------------------------------------
# JSON REPAIR
# -----------------------------------------------------------------------------
def robust_parse(text: str):
    text = re.sub(r"```json|```", "", text).strip()
    try: return json.loads(text)
    except: pass

    start = text.find("{")
    if start == -1: return None
    cleaned = re.sub(r",(\s*[}\]])", r"\1", text[start:])
    try: return json.loads(cleaned)
    except: pass

    idx = cleaned.find('"opportunities"')
    if idx == -1: return None
    arr = cleaned.find("[", idx)
    if arr == -1: return None

    opps, depth, in_str, esc, obj_start = [], 0, False, False, -1
    for i, ch in enumerate(cleaned[arr + 1:], arr + 1):
        if esc:               esc = False;  continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"':         in_str = not in_str; continue
        if in_str:            continue
        if ch == "{":
            if depth == 0:    obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                seg = re.sub(r",(\s*[}\]])", r"\1", cleaned[obj_start:i + 1])
                try:    opps.append(json.loads(seg))
                except: pass
                obj_start = -1
        elif ch == "]" and depth == 0:
            break

    return {"opportunities": opps, "sources": []} if opps else None

# -----------------------------------------------------------------------------
# SCANNER
# -----------------------------------------------------------------------------
def run_scan(keywords=None, agencies=None) -> int:
    keywords = keywords or cfg_get("keywords", DEFAULT_KEYWORDS)
    agencies = agencies or cfg_get("agencies",  DEFAULT_AGENCIES)
    log(f"Scanning {', '.join(agencies)} - {len(keywords)} keywords...")

    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        log("ANTHROPIC_API_KEY not set", "error")
        return 0

    client = anthropic.Anthropic(api_key=api_key)

    prompt = (
        "Find up to 10 current government contract opportunities for space companies "
        "(e.g. Intuitive Machines, Astrobotic, Firefly Aerospace).\n"
        f"Agencies: {', '.join(agencies)}\n"
        f"Keywords: {', '.join(keywords)}\n"
        "Search SAM.gov, SBIR.gov, NASA SEWP, SpaceWERX, DARPA BAAs.\n"
        "Focus on opportunities posted in the last 60 days or with upcoming deadlines.\n"
        "IMPORTANT: Return ONLY valid JSON, no markdown. Keep descriptions under 40 words.\n\n"
        '{"opportunities":[{"id":"uid","title":"full title","agency":"NASA",'
        '"subAgency":"GSFC or null","type":"RFP or RFI or BAA or SBIR/STTR or Contract Award or Sources Sought",'
        '"solicitationNumber":"number or null","postedDate":"YYYY-MM-DD or null",'
        '"deadline":"YYYY-MM-DD or null","description":"short description under 40 words",'
        '"url":"https://... or null","relevanceScore":85,'
        '"matchedKeywords":["kw"],"estimatedValue":"$Xm or null"}],'
        '"sources":["sam.gov"]}'
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=(
                "You are a government contract intelligence agent for the space industry. "
                "Return ONLY valid JSON -- no markdown, no code fences, no preamble. "
                "Keep descriptions under 40 words. Return at most 10 opportunities."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text   = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = robust_parse(text)
        if not parsed:
            log("Could not parse API response -- try again", "error")
            return 0
        opps    = parsed.get("opportunities", [])
        sources = parsed.get("sources", [])
        save_opportunities(opps, sources)
        cfg_set("last_scanned", datetime.now().isoformat())
        log(f"Found {len(opps)} opportunities - {', '.join(sources) or 'various'}", "success")
        return len(opps)
    except Exception as e:
        log(f"Scan failed: {e}", "error")
        return 0

# -----------------------------------------------------------------------------
# SCHEDULER
# -----------------------------------------------------------------------------
@st.cache_resource
def get_scheduler():
    sched = BackgroundScheduler(daemon=True)
    sched.start()
    return sched

def apply_schedule(hours: int):
    sched = get_scheduler()
    sched.remove_all_jobs()
    if hours > 0:
        sched.add_job(
            run_scan, "interval", hours=hours, id="scan_job",
            next_run_time=datetime.now() + timedelta(hours=hours)
        )
        # News scan runs every 12 hours regardless of main scan frequency
        news_interval = min(hours, 12)
        sched.add_job(
            run_news_scan, "interval", hours=news_interval, id="news_job",
            next_run_time=datetime.now() + timedelta(hours=news_interval)
        )
    cfg_set("refresh_hours", hours)

# -----------------------------------------------------------------------------
# HOLIDAYS
# -----------------------------------------------------------------------------
def _nth(year, month, weekday, n):
    last = cal_mod.monthrange(year, month)[1]
    if n == -1:
        d = date(year, month, last)
        while d.weekday() != weekday:
            d -= timedelta(1)
        return d.day
    count = 0
    for day in range(1, last + 1):
        if date(year, month, day).weekday() == weekday:
            count += 1
            if count == n:
                return day

def federal_holidays(year: int) -> dict:
    h = {}
    def fixed(name, month, day):
        d = date(year, month, day)
        if d.weekday() == 5: d -= timedelta(1)
        if d.weekday() == 6: d += timedelta(1)
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
    h[date(year, 11, _nth(year, 11, 3, 4)).isoformat()] = "Thanksgiving"
    return h

def working_days(deadline_str: str, holidays: dict):
    if not deadline_str: return None
    try:    target = date.fromisoformat(deadline_str)
    except: return None
    today = date.today()
    if target <= today: return 0
    n, d = 0, today + timedelta(1)
    while d <= target:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            n += 1
        d += timedelta(1)
    return n

# -----------------------------------------------------------------------------
# USASPENDING + SBIR API HELPERS
# -----------------------------------------------------------------------------
def _usa_payload(fiscal_years, extra_filters):
    base = {
        "fields": [
            "Award ID", "Recipient Name", "Award Amount",
            "Awarding Agency", "Awarding Sub Agency",
            "Award Type", "Start Date", "End Date", "Description",
        ],
        "page": 1, "limit": 100,
        "sort": "Award Amount", "order": "desc",
    }
    filters = {
        "award_type_codes": ["A","B","C","D","02","03","04","05"],
        "time_period": [
            {"start_date": f"{y}-01-01", "end_date": f"{y}-12-31"}
            for y in fiscal_years
        ],
    }
    filters.update(extra_filters)
    base["filters"] = filters
    return base

def fetch_usaspending_recipient(company: str, fiscal_years: list) -> list:
    key = f"recip_{company}_{'_'.join(map(str, fiscal_years))}"
    cached = cache_get(key)
    if cached is not None: return cached
    try:
        r = requests.post(
            f"{USASPENDING_URL}/search/spending_by_award/",
            json=_usa_payload(fiscal_years, {"recipient_search_text": [company]}),
            timeout=25,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        cache_set(key, results)
        return results
    except Exception:
        return []

def fetch_agency_spending_by_year(keyword: str, years: list) -> dict:
    key = f"trend_{keyword}_{'_'.join(map(str, years))}"
    cached = cache_get(key)
    if cached is not None: return cached
    totals = {}
    for y in years:
        try:
            r = requests.post(
                f"{USASPENDING_URL}/search/spending_by_award/",
                json=_usa_payload([y], {"keywords": [keyword]}),
                timeout=20,
            )
            r.raise_for_status()
            rows = r.json().get("results", [])
            totals[y] = sum(float(x.get("Award Amount") or 0) for x in rows)
        except Exception:
            totals[y] = 0.0
    cache_set(key, totals)
    return totals

def fetch_sbir_awards(company: str) -> list:
    key = f"sbir_{company}"
    cached = cache_get(key)
    if cached is not None: return cached
    try:
        r = requests.get(SBIR_URL, params={"firm": company, "rows": 50}, timeout=20)
        r.raise_for_status()
        data = r.json()
        results = data if isinstance(data, list) else data.get("data", [])
        cache_set(key, results)
        return results
    except Exception:
        return []

def fmt_dollars(v: float) -> str:
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

# -----------------------------------------------------------------------------
# UI HELPERS
# -----------------------------------------------------------------------------
def deadline_info(deadline_str: str):
    if not deadline_str:
        return None, "--", "#9ca3af"
    try:    days = (date.fromisoformat(deadline_str) - date.today()).days
    except: return None, "--", "#9ca3af"
    if days < 0:   return days, "Expired",             "#9ca3af"
    if days == 0:  return 0,    "Due today",            "#A32D2D"
    if days <= 7:  return days, f"{days}d left",        "#A32D2D"
    if days <= 21: return days, f"{days}d",             "#BA7517"
    return days,   f"{days}d",                          "#3B6D11"

def badge(text: str, color: str, size=12) -> str:
    bg = color + "22"
    return (
        f'<span style="background:{bg};color:{color};border:1px solid {color}55;'
        f'padding:3px 10px;border-radius:20px;font-size:{size}px;font-weight:700">'
        f'{text}</span>'
    )

def render_calendar(opps, all_holidays, show_wdays):
    if "cy" not in st.session_state:
        st.session_state.cy = date.today().year
        st.session_state.cm = date.today().month

    dl_map = {}
    for o in opps:
        if o.get("deadline"):
            dl_map.setdefault(o["deadline"], []).append(o)

    c1, c2, c3 = st.columns([1, 3, 1])
    with c1:
        if st.button("<", key="prev_mo"):
            if st.session_state.cm == 1:
                st.session_state.cy -= 1; st.session_state.cm = 12
            else:
                st.session_state.cm -= 1
    with c2:
        mo_label = date(st.session_state.cy, st.session_state.cm, 1).strftime("%B %Y")
        st.markdown(f"<h4 style='text-align:center;margin:4px 0'>{mo_label}</h4>",
                    unsafe_allow_html=True)
    with c3:
        if st.button(">", key="next_mo"):
            if st.session_state.cm == 12:
                st.session_state.cy += 1; st.session_state.cm = 1
            else:
                st.session_state.cm += 1

    y, m     = st.session_state.cy, st.session_state.cm
    today_s  = date.today().isoformat()
    first_d  = (date(y, m, 1).weekday() + 1) % 7
    dim      = cal_mod.monthrange(y, m)[1]
    cells    = [None]*first_d + list(range(1, dim+1))
    while len(cells) % 7:
        cells.append(None)

    dot_css = "display:inline-block;width:7px;height:7px;border-radius:50%;margin:1px;"
    html = """
<style>
.gcal{width:100%;border-collapse:separate;border-spacing:3px}
.gcal th{font-size:11px;font-weight:700;color:#9ca3af;text-align:center;padding:4px}
.gcal td{text-align:center;vertical-align:top;border-radius:8px;
         min-height:46px;padding:4px 2px;font-size:13px;width:14.28%}
.g-past{color:#d1d5db;background:#fafafa}
.g-today{background:#E6F1FB;border:2px solid #185FA5;font-weight:700;color:#185FA5}
.g-hol{background:#FAEEDA;color:#BA7517}
.g-wknd{color:#ef4444;background:#fafafa}
.g-norm{background:#fff;border:1px solid #f3f4f6}
</style>
<table class='gcal'>
<tr><th>Su</th><th>Mo</th><th>Tu</th><th>We</th><th>Th</th><th>Fr</th><th>Sa</th></tr>
"""
    for week in [cells[i:i+7] for i in range(0, len(cells), 7)]:
        html += "<tr>"
        for col, day in enumerate(week):
            if day is None:
                html += "<td></td>"
                continue
            ds       = date(y, m, day).isoformat()
            is_today = ds == today_s
            is_wknd  = col in (0, 6)
            is_hol   = ds in all_holidays
            is_past  = ds < today_s
            day_opps = dl_map.get(ds, [])

            if   is_today:                   css = "g-today"
            elif is_hol:                     css = "g-hol"
            elif is_past and not day_opps:   css = "g-past"
            elif is_wknd:                    css = "g-wknd"
            else:                            css = "g-norm"

            dots = "".join(
                f'<span style="{dot_css}background:{TYPE_COLORS.get(o["type"],"#888")}"></span>'
                for o in day_opps[:4]
            ) + (f'<span style="font-size:9px">+{len(day_opps)-4}</span>' if len(day_opps) > 4 else "")

            hol_title = f'title="{all_holidays[ds]}"' if is_hol else ""
            wday_sfx  = ""
            if show_wdays and ds > today_s and day_opps:
                wd = working_days(ds, all_holidays)
                wday_sfx = f"<br><span style='font-size:9px;color:#9ca3af'>{wd}wd</span>" if wd else ""

            html += f'<td class="{css}" {hol_title}><div>{day}{wday_sfx}</div><div>{dots}</div></td>'
        html += "</tr>"
    html += "</table>"

    html += "<div style='margin-top:10px;display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#4b5563'>"
    for t, c in TYPE_COLORS.items():
        html += f'<span><span style="{dot_css}background:{c}"></span>{t}</span>'
    html += (
        '<span><span style="display:inline-block;width:9px;height:9px;border-radius:3px;'
        'background:#FAEEDA;border:2px solid #BA7517;margin-right:3px"></span>US Holiday</span>'
    )
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# HISTORICAL FUNDING TAB
# -----------------------------------------------------------------------------
def render_funding_tab():
    import pandas as pd

    st.markdown("## Historical Funding Statistics")
    st.caption(
        "Live data from USASpending.gov and SBIR.gov -- no extra API key needed. "
        "Results are cached for 24 hours."
    )

    t1, t2, t3 = st.tabs(["Company lookup", "Keyword trends", "SBIR awards"])

    # -- Company Lookup -------------------------------------------------------
    with t1:
        st.markdown("#### Awards by company")
        col1, col2 = st.columns([2, 1])
        company     = col1.selectbox("Company", SPACE_COMPANIES, key="fund_co")
        custom_co   = col1.text_input("Or enter any recipient name", key="fund_custom",
                                      placeholder="e.g. Boeing, Northrop Grumman")
        lookup_name = custom_co.strip() if custom_co.strip() else company

        cur_yr  = date.today().year
        yr_opts = list(range(cur_yr, cur_yr - 6, -1))
        sel_yrs = col2.multiselect("Fiscal years", yr_opts,
                                   default=yr_opts[:3], key="fund_yrs")

        if st.button("Fetch awards", key="btn_co", type="primary"):
            if not sel_yrs:
                st.warning("Select at least one fiscal year.")
            else:
                with st.spinner(f"Querying USASpending.gov for {lookup_name}..."):
                    awards = fetch_usaspending_recipient(lookup_name, sorted(sel_yrs))

                if not awards:
                    st.info(
                        f"No awards found for **{lookup_name}** in the selected years. "
                        "Try a partial name or different years."
                    )
                else:
                    total   = sum(float(a.get("Award Amount") or 0) for a in awards)
                    largest = max(float(a.get("Award Amount") or 0) for a in awards)
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total awarded",   fmt_dollars(total))
                    m2.metric("Number of awards", len(awards))
                    m3.metric("Largest award",   fmt_dollars(largest))

                    # By-agency breakdown bar chart
                    agency_totals: dict = {}
                    for a in awards:
                        ag = a.get("Awarding Agency") or "Unknown"
                        agency_totals[ag] = agency_totals.get(ag, 0.0) + float(a.get("Award Amount") or 0)

                    df_ag = pd.DataFrame([
                        {"Agency": k, "Total ($)": v}
                        for k, v in sorted(agency_totals.items(), key=lambda x: -x[1])
                    ]).set_index("Agency")
                    st.markdown(f"##### {lookup_name} -- funding by agency")
                    st.bar_chart(df_ag)

                    # Full awards table
                    st.markdown("##### All awards")
                    rows = []
                    for a in awards:
                        rows.append({
                            "Award ID":   a.get("Award ID", ""),
                            "Recipient":  (a.get("Recipient Name", "") or "")[:50],
                            "Agency":     (a.get("Awarding Agency", "") or "")[:35],
                            "Sub-Agency": (a.get("Awarding Sub Agency", "") or "")[:35],
                            "Type":       a.get("Award Type", ""),
                            "Amount":     float(a.get("Award Amount") or 0),
                            "Start":      a.get("Start Date", ""),
                        })
                    df_aw = pd.DataFrame(rows)

                    def style_amount(v):
                        if v >= 1e7: return "color:#3B6D11;font-weight:700"
                        if v >= 1e6: return "color:#BA7517;font-weight:600"
                        return "color:#374151"

                    styled = (
                        df_aw.style
                        .map(style_amount, subset=["Amount"])
                        .format({"Amount": fmt_dollars})
                        .set_properties(**{"font-size": "12px"})
                    )
                    st.dataframe(styled, use_container_width=True, hide_index=True,
                                 height=min(500, 45 + len(df_aw) * 35))

    # -- Keyword Trends -------------------------------------------------------
    with t2:
        st.markdown("#### Spending trends by keyword")
        st.caption("Total contract/grant dollars awarded per fiscal year for a search term.")

        kw_opts  = ["lunar lander", "CLPS", "cislunar", "commercial lunar payload",
                    "space domain awareness", "launch vehicle", "spacecraft"]
        tc1, tc2 = st.columns([2, 1])
        trend_kw     = tc1.selectbox("Keyword", kw_opts, key="trend_kw")
        custom_kw    = tc1.text_input("Or enter custom keyword", key="trend_custom",
                                      placeholder="e.g. satellite navigation")
        search_kw    = custom_kw.strip() if custom_kw.strip() else trend_kw
        cur_yr2      = date.today().year
        trend_yrs    = tc2.multiselect(
            "Years", list(range(cur_yr2, cur_yr2 - 6, -1)),
            default=list(range(cur_yr2 - 1, cur_yr2 - 5, -1)),
            key="trend_yrs"
        )

        if st.button("Fetch trend", key="btn_trend", type="primary"):
            if not trend_yrs:
                st.warning("Select at least one year.")
            else:
                with st.spinner(f"Fetching year-by-year data for '{search_kw}'..."):
                    totals = fetch_agency_spending_by_year(search_kw, sorted(trend_yrs))

                if not any(totals.values()):
                    st.info("No spending data found for that keyword and year range.")
                else:
                    grand = sum(totals.values())
                    yr_range = f"{min(trend_yrs)}-{max(trend_yrs)}"
                    st.metric(
                        f"Total federal spending on '{search_kw}' ({yr_range})",
                        fmt_dollars(grand)
                    )
                    df_tr = pd.DataFrame([
                        {"Year": str(y), "Total ($)": totals.get(y, 0)}
                        for y in sorted(trend_yrs)
                    ]).set_index("Year")
                    st.bar_chart(df_tr)

                    # YoY table
                    raw_vals = [totals.get(y, 0) for y in sorted(trend_yrs)]
                    yoy = []
                    for i, y in enumerate(sorted(trend_yrs)):
                        prev = raw_vals[i-1] if i > 0 else None
                        if prev and prev > 0:
                            pct = (raw_vals[i] - prev) / prev * 100
                            delta = f"+{pct:.1f}%" if pct > 0 else f"{pct:.1f}%"
                        else:
                            delta = "--"
                        yoy.append({"Year": str(y),
                                    "Total": fmt_dollars(raw_vals[i]),
                                    "YoY change": delta})
                    st.dataframe(pd.DataFrame(yoy), use_container_width=True, hide_index=True)

    # -- SBIR Awards ----------------------------------------------------------
    with t3:
        st.markdown("#### SBIR / STTR awards")
        st.caption("Small Business Innovation Research awards from SBIR.gov.")

        sc1, sc2 = st.columns([2, 1])
        sbir_co     = sc1.selectbox("Company", SPACE_COMPANIES, key="sbir_co")
        sbir_custom = sc1.text_input("Or enter company name", key="sbir_custom")
        sbir_name   = sbir_custom.strip() if sbir_custom.strip() else sbir_co

        if st.button("Fetch SBIR awards", key="btn_sbir", type="primary"):
            with st.spinner(f"Querying SBIR.gov for {sbir_name}..."):
                sbir = fetch_sbir_awards(sbir_name)

            if not sbir:
                st.info(f"No SBIR/STTR awards found for **{sbir_name}**.")
            else:
                total_sbir = sum(float(a.get("award_amount") or 0) for a in sbir)
                phases: dict = {}
                for a in sbir:
                    p = str(a.get("phase", "Unknown"))
                    phases[p] = phases.get(p, 0) + 1

                m1, m2, m3 = st.columns(3)
                m1.metric("Total SBIR funding",  fmt_dollars(total_sbir))
                m2.metric("Awards found",        len(sbir))
                m3.metric("Phase I / II",
                          f"{phases.get('I',0)} / {phases.get('II',0)}")

                # Phase breakdown chart
                if phases:
                    df_ph = pd.DataFrame([
                        {"Phase": k, "Count": v}
                        for k, v in sorted(phases.items())
                    ]).set_index("Phase")
                    st.bar_chart(df_ph)

                rows = []
                for a in sbir:
                    rows.append({
                        "Title":    ((a.get("award_title") or a.get("title","")) or "")[:70],
                        "Agency":   a.get("agency", ""),
                        "Phase":    str(a.get("phase", "")),
                        "Amount":   float(a.get("award_amount") or 0),
                        "Year":     str(a.get("award_year") or a.get("fiscal_year", "")),
                        "Abstract": ((a.get("abstract","") or "")[:120]) + "...",
                    })
                df_sb = pd.DataFrame(rows)

                def style_phase(v):
                    if str(v) == "II": return "color:#534AB7;font-weight:700"
                    return "color:#185FA5"

                styled_sb = (
                    df_sb.style
                    .map(style_phase, subset=["Phase"])
                    .format({"Amount": fmt_dollars})
                    .set_properties(**{"font-size": "12px"})
                )
                st.dataframe(styled_sb, use_container_width=True, hide_index=True,
                             height=min(500, 45 + len(df_sb) * 35))


# -----------------------------------------------------------------------------
# NEWS INTELLIGENCE SCAN
# -----------------------------------------------------------------------------
NEWS_SOURCES = [
    "SpaceNews", "NASASpaceflight", "Space.com", "DefenseNews",
    "C4ISRNET", "Breaking Defense", "Aviation Week", "SpacePolicyInstitute",
]

def run_news_scan(keywords=None, companies=None):
    """Scan space industry news for contract award announcements."""
    keywords  = keywords  or cfg_get("keywords",  DEFAULT_KEYWORDS)
    companies = companies or SPACE_COMPANIES[:6]

    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("ANTHROPIC_API_KEY not set", "error")
        return 0

    client = anthropic.Anthropic(api_key=api_key)
    log("Running news intelligence scan...")

    prompt = (
        "Search space industry news from the last 30 days for government contract award "
        "announcements, funding news, and procurement intelligence.\n"
        f"Companies of interest: {', '.join(companies)}\n"
        f"Keywords: {', '.join(keywords[:8])}\n"
        "Search SpaceNews, Breaking Defense, NASASpaceflight, DefenseNews, C4ISRNET, "
        "Aviation Week, and official agency press release pages.\n\n"
        "Return ONLY valid JSON, no markdown:\n"
        '{"articles":[{"headline":"title","summary":"2-3 sentence summary of the award/funding news",'
        '"company":"primary company mentioned or null","agency":"NASA or DoD etc or null",'
        '"award_value":"dollar amount or null","source_url":"https://... or null",'
        '"published":"YYYY-MM-DD or null"}]}'
    )

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            system=(
                "You are a space industry intelligence analyst. Search for real, recent news "
                "about government contract awards to space companies. Return ONLY valid JSON."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text   = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = robust_parse(text)
        if not parsed:
            log("News scan: could not parse response", "error")
            return 0

        articles = parsed.get("articles", [])
        with db() as c:
            for a in articles:
                c.execute("""
                    INSERT OR IGNORE INTO news_intel
                        (headline,summary,company,agency,award_value,source_url,published)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    a.get("headline",""), a.get("summary",""),
                    a.get("company"),     a.get("agency"),
                    a.get("award_value"), a.get("source_url"),
                    a.get("published"),
                ))
        cfg_set("last_news_scan", datetime.now().isoformat())
        log(f"News scan: found {len(articles)} articles", "success")
        return len(articles)
    except Exception as e:
        log(f"News scan failed: {e}", "error")
        return 0

def load_news(n=50):
    rows = db().execute(
        "SELECT * FROM news_intel ORDER BY scanned_at DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in rows]

# -----------------------------------------------------------------------------
# SAM.GOV PDF SOLICITATION PARSER
# -----------------------------------------------------------------------------
SAM_SEARCH_URL = "https://api.sam.gov/opportunities/v2/search"
SAM_DOC_URL    = "https://api.sam.gov/opportunities/v1/noticedesc"

def fetch_sam_notice_ids(keywords: list, limit: int = 5) -> list:
    """Search SAM.gov for recent solicitations matching keywords (public API)."""
    results = []
    for kw in keywords[:3]:
        try:
            r = requests.get(
                SAM_SEARCH_URL,
                params={
                    "q": kw, "limit": limit, "offset": 0,
                    "postedFrom": (date.today() - timedelta(days=90)).strftime("%m/%d/%Y"),
                    "postedTo":   date.today().strftime("%m/%d/%Y"),
                    "ptype": "o,p,k,r",
                },
                timeout=15,
            )
            if r.ok:
                data = r.json()
                for opp in data.get("opportunitiesData", []):
                    results.append({
                        "notice_id":       opp.get("noticeId",""),
                        "title":           opp.get("title",""),
                        "solicitation_no": opp.get("solicitationNumber",""),
                        "agency":          opp.get("fullParentPathName",""),
                        "posted_date":     opp.get("postedDate",""),
                        "deadline":        opp.get("responseDeadLine",""),
                        "url":             f"https://sam.gov/opp/{opp.get('noticeId','')}/view",
                    })
        except Exception:
            pass
    # deduplicate by notice_id
    seen = set()
    unique = []
    for r in results:
        if r["notice_id"] not in seen:
            seen.add(r["notice_id"])
            unique.append(r)
    return unique

def fetch_sam_pdf_text(notice_id: str, title: str) -> str:
    """Fetch document listing for a SAM notice and download first available PDF."""
    try:
        r = requests.get(
            SAM_DOC_URL,
            params={"noticeid": notice_id, "deleteIndicator": "N"},
            timeout=15,
        )
        if not r.ok:
            return ""
        docs = r.json()
        # Find first PDF attachment
        for doc in docs if isinstance(docs, list) else docs.get("opportunityAttachments", []):
            fname = doc.get("name","") or doc.get("filename","")
            furl  = doc.get("accessibilitySolutionUrl","") or doc.get("fileAccessUrl","")
            if fname.lower().endswith(".pdf") and furl:
                pr = requests.get(furl, timeout=30)
                if pr.ok:
                    return _extract_pdf_text(pr.content)
        return ""
    except Exception:
        return ""

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages  = []
        for page in reader.pages[:30]:   # cap at 30 pages
            t = page.extract_text()
            if t: pages.append(t)
        return "\n".join(pages)[:40000]  # cap chars sent to Claude
    except Exception:
        return ""

def analyze_solicitation_pdf(notice: dict, pdf_text: str, api_key: str) -> dict:
    """Send PDF text to Claude and extract structured requirements."""
    client = anthropic.Anthropic(api_key=api_key)
    sol_no = notice.get("solicitation_no","") or notice.get("notice_id","")

    if not pdf_text:
        # Fall back to web search if no PDF available
        prompt = (
            f"Search SAM.gov for the solicitation '{notice['title']}' "
            f"(solicitation number: {sol_no}).\n"
            "Extract: key technical requirements, evaluation criteria, page limits, "
            "important dates, and a plain-language summary of what the government wants.\n"
            "Return ONLY valid JSON:\n"
            '{"requirements":"bullet list","eval_criteria":"bullet list",'
            '"page_limits":"e.g. 20 pages technical volume","key_dates":"list of dates",'
            '"summary":"3-4 sentence plain English summary"}'
        )
        tools = [{"type": "web_search_20250305", "name": "web_search"}]
    else:
        prompt = (
            f"Analyze this government solicitation document.\n"
            f"Title: {notice['title']}\nSolicitation: {sol_no}\n\n"
            f"--- DOCUMENT TEXT (first 40,000 chars) ---\n{pdf_text}\n---\n\n"
            "Extract and return ONLY valid JSON:\n"
            '{"requirements":"bullet list of key technical requirements",'
            '"eval_criteria":"evaluation factors and their weights",'
            '"page_limits":"page limits for each volume",'
            '"key_dates":"all important dates and deadlines",'
            '"summary":"3-4 sentence plain English summary of what is being procured"}'
        )
        tools = []

    try:
        kwargs = dict(
            model=MODEL, max_tokens=2000,
            system=(
                "You are a government proposal analyst. Extract structured information "
                "from solicitation documents. Return ONLY valid JSON, no markdown."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        if tools:
            kwargs["tools"] = tools
        resp   = client.messages.create(**kwargs)
        text   = "".join(b.text for b in resp.content if hasattr(b, "text"))
        parsed = robust_parse(text)
        if not parsed:
            return {}
        return parsed
    except Exception as e:
        log(f"PDF analysis failed for {sol_no}: {e}", "error")
        return {}

def save_pdf_analysis(notice: dict, analysis: dict, pdf_text: str):
    sol_no = notice.get("solicitation_no","") or notice.get("notice_id","unknown")
    with db() as c:
        c.execute("""
            INSERT OR REPLACE INTO pdf_analysis
                (solicitation_no,title,url,requirements,eval_criteria,
                 page_limits,key_dates,summary,raw_text,analyzed_at)
            VALUES (?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
        """, (
            sol_no, notice.get("title",""), notice.get("url",""),
            analysis.get("requirements",""), analysis.get("eval_criteria",""),
            analysis.get("page_limits",""),  analysis.get("key_dates",""),
            analysis.get("summary",""),      pdf_text[:5000],
        ))

def load_pdf_analyses():
    rows = db().execute(
        "SELECT * FROM pdf_analysis ORDER BY analyzed_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]

# -----------------------------------------------------------------------------
# NEWS INTELLIGENCE UI
# -----------------------------------------------------------------------------
def render_news_tab():
    import pandas as pd

    st.markdown("## News Intelligence")
    st.caption(
        "Scans SpaceNews, Breaking Defense, NASASpaceflight, DefenseNews, "
        "Aviation Week and agency press release pages for contract award announcements."
    )

    last_ns = cfg_get("last_news_scan")
    if last_ns:
        st.caption(f"Last news scan: {datetime.fromisoformat(last_ns).strftime('%b %d at %H:%M')}")

    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("Run news scan", type="primary", use_container_width=True, key="btn_news"):
            with st.spinner("Searching space industry news sources..."):
                n = run_news_scan()
            st.success(f"Found {n} articles")
            st.rerun()

    articles = load_news()
    if not articles:
        st.info("No news yet. Click 'Run news scan' to search for recent award announcements.")
        return

    # Summary metrics
    companies_seen = len({a["company"] for a in articles if a.get("company")})
    with_value     = [a for a in articles if a.get("award_value")]
    m1, m2, m3 = st.columns(3)
    m1.metric("Articles found",    len(articles))
    m2.metric("Companies covered", companies_seen)
    m3.metric("With dollar value", len(with_value))

    st.divider()

    # Filters
    f1, f2 = st.columns(2)
    all_cos  = sorted({a["company"] for a in articles if a.get("company")})
    all_ags  = sorted({a["agency"]  for a in articles if a.get("agency")})
    co_filt  = f1.selectbox("Filter by company", ["All"] + all_cos, key="news_co_filt")
    ag_filt  = f2.selectbox("Filter by agency",  ["All"] + all_ags, key="news_ag_filt")

    shown = [
        a for a in articles
        if (co_filt == "All" or a.get("company") == co_filt)
        and (ag_filt == "All" or a.get("agency")  == ag_filt)
    ]

    for a in shown:
        with st.container(border=True):
            header_cols = st.columns([4, 1])
            with header_cols[0]:
                if a.get("source_url"):
                    st.markdown(f"**[{a['headline']}]({a['source_url']})**")
                else:
                    st.markdown(f"**{a['headline']}**")
            with header_cols[1]:
                if a.get("award_value"):
                    st.markdown(
                        f'<span style="color:#3B6D11;font-weight:700;font-size:13px">'
                        f'{a["award_value"]}</span>',
                        unsafe_allow_html=True
                    )

            st.markdown(a.get("summary",""))

            tag_cols = st.columns([2, 2, 2, 2])
            if a.get("company"):
                tag_cols[0].caption(f"Company: {a['company']}")
            if a.get("agency"):
                tag_cols[1].caption(f"Agency: {a['agency']}")
            if a.get("published"):
                tag_cols[2].caption(f"Published: {a['published']}")
            scanned = a.get("scanned_at","")[:16]
            if scanned:
                tag_cols[3].caption(f"Found: {scanned}")

# -----------------------------------------------------------------------------
# PDF SOLICITATION PARSER UI
# -----------------------------------------------------------------------------
def render_pdf_tab():
    st.markdown("## Solicitation PDF Parser")
    st.caption(
        "Fetches solicitation documents from SAM.gov and uses Claude to extract "
        "requirements, evaluation criteria, page limits, and key dates -- "
        "saving hours of manual document review."
    )

    try:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    # ── Auto-analyze if navigated here from an Opportunity card ──────────────
    queued = st.session_state.pop("queued_opp", None)
    if queued:
        title = queued.get("title","")
        sol   = queued.get("solicitation_no","")
        st.info(
            f"Navigated from Opportunities: **{title}**"
            + (f"  |  Sol: `{sol}`" if sol else "")
        )
        with st.spinner(f"Fetching and analyzing '{title[:50]}...'"):
            notice_id = queued.get("notice_id","")
            pdf_text  = fetch_sam_pdf_text(notice_id, title) if notice_id else ""
            if pdf_text:
                st.toast(f"PDF fetched ({len(pdf_text):,} chars) -- analyzing...")
            else:
                st.toast("No PDF found -- using web search fallback...")
            analysis = analyze_solicitation_pdf(queued, pdf_text, api_key)
            if analysis:
                save_pdf_analysis(queued, analysis, pdf_text)
                st.success("Analysis complete and saved!")
            else:
                st.error("Analysis failed -- try again or paste the URL below.")

    st.markdown("### Search SAM.gov solicitations")
    p1, p2 = st.columns([3, 1])
    pdf_kw  = p1.text_input(
        "Search keyword", value="lunar lander",
        placeholder="e.g. lunar lander, cislunar, launch services",
        key="pdf_kw"
    )
    pdf_limit = p2.number_input("Max results", min_value=1, max_value=10, value=5, key="pdf_limit")

    if st.button("Search SAM.gov", key="btn_sam_search", type="primary"):
        with st.spinner("Searching SAM.gov for solicitations..."):
            notices = fetch_sam_notice_ids([pdf_kw], limit=int(pdf_limit))

        if not notices:
            st.warning(
                "No solicitations found on SAM.gov for that keyword. "
                "SAM.gov public API may require an API key for full access -- "
                "results may be limited. Try a broader keyword."
            )
        else:
            st.session_state["sam_notices"] = notices
            st.success(f"Found {len(notices)} solicitations")

    notices = st.session_state.get("sam_notices", [])
    if notices:
        st.markdown("#### Select a solicitation to analyze")
        for i, n in enumerate(notices):
            with st.container(border=True):
                nc1, nc2 = st.columns([4, 1])
                with nc1:
                    st.markdown(f"**{n['title']}**")
                    st.caption(
                        f"Sol: `{n.get('solicitation_no','N/A')}` "
                        f" |  Agency: {n.get('agency','')[:60]}"
                        f" |  Posted: {n.get('posted_date','')[:10]}"
                        f" |  Deadline: {n.get('deadline','')[:10]}"
                    )
                with nc2:
                    if st.button("Analyze", key=f"analyze_{i}"):
                        with st.spinner(
                            f"Fetching and parsing '{n['title'][:40]}...' -- "
                            "this can take 30-60 seconds..."
                        ):
                            pdf_text = fetch_sam_pdf_text(n["notice_id"], n["title"])
                            if pdf_text:
                                st.toast(f"PDF fetched ({len(pdf_text):,} chars) -- analyzing...")
                            else:
                                st.toast("No PDF found -- using web search fallback...")
                            analysis = analyze_solicitation_pdf(n, pdf_text, api_key)
                            if analysis:
                                save_pdf_analysis(n, analysis, pdf_text)
                                st.success("Analysis saved!")
                                st.rerun()
                            else:
                                st.error("Analysis failed -- see scan log for details.")

    st.divider()

    st.markdown("### Analyzed solicitations")
    st.caption(
        "Paste any SAM.gov URL or solicitation number below to analyze directly."
    )
    manual_url = st.text_input(
        "SAM.gov URL or solicitation number",
        placeholder="https://sam.gov/opp/.../view  or  80NSSC24R0001",
        key="manual_url"
    )
    if st.button("Analyze from URL / number", key="btn_manual"):
        if manual_url.strip():
            manual_notice = {
                "notice_id":       manual_url.strip(),
                "solicitation_no": manual_url.strip(),
                "title":           manual_url.strip(),
                "url":             manual_url.strip() if manual_url.startswith("http") else "",
            }
            with st.spinner("Analyzing solicitation (web search fallback)..."):
                analysis = analyze_solicitation_pdf(manual_notice, "", api_key)
                if analysis:
                    save_pdf_analysis(manual_notice, analysis, "")
                    st.success("Analysis complete!")
                    st.rerun()
                else:
                    st.error("Could not analyze -- check the URL/number and try again.")

    analyses = load_pdf_analyses()
    if not analyses:
        st.info("No solicitations analyzed yet.")
        return

    for a in analyses:
        with st.expander(f"{a['title'][:80]}  |  {a['solicitation_no']}", expanded=False):
            st.caption(f"Analyzed: {a['analyzed_at'][:16]}")
            if a.get("url"):
                st.markdown(f"[View on SAM.gov]({a['url']})")

            tabs = st.tabs(["Summary", "Requirements", "Eval Criteria", "Dates & Limits"])

            with tabs[0]:
                st.markdown(a.get("summary") or "_No summary extracted._")

            with tabs[1]:
                raw = a.get("requirements","")
                if raw:
                    for line in raw.replace("- ","\n- ").split("\n"):
                        if line.strip():
                            st.markdown(f"- {line.strip().lstrip('-').strip()}")
                else:
                    st.info("No requirements extracted.")

            with tabs[2]:
                raw = a.get("eval_criteria","")
                if raw:
                    for line in raw.replace("- ","\n- ").split("\n"):
                        if line.strip():
                            st.markdown(f"- {line.strip().lstrip('-').strip()}")
                else:
                    st.info("No evaluation criteria extracted.")

            with tabs[3]:
                c1, c2 = st.columns(2)
                c1.markdown("**Key dates**")
                c1.markdown(a.get("key_dates") or "_None extracted._")
                c2.markdown("**Page limits**")
                c2.markdown(a.get("page_limits") or "_None extracted._")

            if a.get("raw_text"):
                with st.expander("Raw PDF text (first 5000 chars)"):
                    st.text(a["raw_text"][:5000])

# -----------------------------------------------------------------------------
# MAIN APP
# -----------------------------------------------------------------------------
def main():
    st.set_page_config(
        page_title="Gov Proposal Monitor", page_icon="🛰", layout="wide"
    )
    st.markdown("""
        <style>
        .block-container{padding-top:1.2rem;max-width:1400px}
        [data-testid="metric-container"]{background:#f9fafb;border-radius:10px;
            padding:14px;border:1px solid #e5e7eb}
        </style>
    """, unsafe_allow_html=True)

    init_db()

    # -- Sidebar --------------------------------------------------------------
    with st.sidebar:
        st.markdown("## Configure")

        st.markdown("**Keywords** (one per line)")
        saved_kw  = cfg_get("keywords", DEFAULT_KEYWORDS)
        new_kw_raw = st.text_area("keywords", "\n".join(saved_kw), height=220,
                                  label_visibility="collapsed")
        new_kw = [k.strip() for k in new_kw_raw.splitlines() if k.strip()]
        if new_kw != saved_kw:
            cfg_set("keywords", new_kw)

        st.markdown("**Agencies**")
        saved_ag = cfg_get("agencies", DEFAULT_AGENCIES)
        new_ag   = []
        cols     = st.columns(2)
        for i, a in enumerate(ALL_AGENCIES):
            if cols[i % 2].checkbox(a, value=a in saved_ag, key=f"ag_{a}"):
                new_ag.append(a)
        if new_ag != saved_ag:
            cfg_set("agencies", new_ag)

        st.divider()

        st.markdown("**Auto-scan**")
        saved_hrs  = cfg_get("refresh_hours", 24)
        freq_label = st.selectbox(
            "Frequency", list(REFRESH_OPTIONS.keys()),
            index=(list(REFRESH_OPTIONS.values()).index(saved_hrs)
                   if saved_hrs in REFRESH_OPTIONS.values() else 2),
            label_visibility="collapsed"
        )
        auto_on    = st.toggle("Enable auto-scan", value=bool(saved_hrs))
        chosen_hrs = REFRESH_OPTIONS[freq_label] if auto_on else 0
        if chosen_hrs != saved_hrs:
            apply_schedule(chosen_hrs)

        st.divider()
        show_wdays = st.toggle("Show working days", value=False)

        if st.button("Scan Opportunities", use_container_width=True, type="primary"):
            with st.spinner("Searching federal procurement databases..."):
                n = run_scan(new_kw, new_ag)
            st.success(f"Found {n} opportunities")

        if st.button("Scan News", use_container_width=True, key="sidebar_news"):
            with st.spinner("Searching space industry news..."):
                n = run_news_scan()
            st.success(f"Found {n} articles")
            st.rerun()

        st.markdown("**Sources**")
        for s in ["SAM.gov (PDF)","SBIR.gov","NASA SEWP","SpaceWERX","DARPA.mil","USASpending","SpaceNews","Breaking Defense","NASASpaceflight","DefenseNews"]:
            st.caption(f"* {s}")

    # -- Header ---------------------------------------------------------------
    last_ts  = cfg_get("last_scanned")
    status   = "Auto-scan on" if chosen_hrs else "Auto-scan off"
    last_lbl = (
        datetime.fromisoformat(last_ts).strftime("Last scan %b %d at %H:%M")
        + f"  -  {status}"
        if last_ts else "Not yet scanned -- click Scan Now"
    )

    st.markdown("## Gov Proposal Monitor")
    st.caption(last_lbl)

    # -- Top-level tabs -------------------------------------------------------
    # ── Page navigation via sidebar ──────────────────────────────────────────
    PAGES = ["Opportunities", "Historical Funding", "News Intel", "PDF Parser"]
    if "page" not in st.session_state:
        st.session_state.page = "Opportunities"
    # Allow other buttons to navigate here
    if st.session_state.page not in PAGES:
        st.session_state.page = "Opportunities"

    with st.sidebar:
        st.divider()
        st.markdown("**Navigation**")
        for p in PAGES:
            active = st.session_state.page == p
            if st.button(
                p,
                key=f"nav_{p}",
                use_container_width=True,
                type="primary" if active else "secondary",
            ):
                st.session_state.page = p
                st.rerun()

    page = st.session_state.page

    if page == "Historical Funding":
        render_funding_tab()
        return
    if page == "News Intel":
        render_news_tab()
        return
    if page == "PDF Parser":
        render_pdf_tab()
        return

    # ── Opportunities page ────────────────────────────────────────────────────
    if True:
        opps  = load_opportunities()
        today = date.today()
        all_holidays: dict = {}
        for yr in [today.year - 1, today.year, today.year + 1]:
            all_holidays.update(federal_holidays(yr))

        if not opps:
            st.info("No opportunities yet.  Configure keywords and click Scan Now.")
            return

        # Stat cards
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
        c3.metric("Due < 14d",  urgent,
                  delta=f"{urgent} urgent" if urgent else None,
                  delta_color="inverse")
        c4.metric("High match", high)

        st.divider()

        # Quick-lookup table
        st.markdown("### Quick Lookup")
        import pandas as pd

        def sort_key(o):
            return (o.get("deadline") or "9999-12-31", -(o.get("relevance_score") or 0))

        rows = []
        for o in sorted(opps, key=sort_key):
            dl, lbl, col = deadline_info(o.get("deadline", ""))
            wd = (working_days(o.get("deadline"), all_holidays)
                  if show_wdays and o.get("deadline") else None)
            dl_cell = (
                f"{o['deadline']}  ({dl}d)" if dl and dl > 0
                else (o.get("deadline", "--") or "--")
            )
            if wd is not None:
                dl_cell += f"  /  {wd}wd"
            rows.append({
                "Title":    (o.get("title","") or "")[:75] + ("..." if len(o.get("title","")) > 75 else ""),
                "Type":     o.get("type", ""),
                "Agency":   o.get("agency", ""),
                "Score":    o.get("relevance_score") or 0,
                "Deadline": dl_cell,
            })

        df = pd.DataFrame(rows)

        def style_type(v):
            c = TYPE_COLORS.get(v, "#5F5E5A")
            return f"color:{c};font-weight:700"

        def style_deadline(v):
            if "--" in str(v) or not v: return "color:#9ca3af"
            m = re.search(r"\((\d+)d\)", str(v))
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
        styled  = (
            display.style
            .map(style_type,     subset=["Type"])
            .map(style_deadline, subset=["Deadline"])
            .map(style_score,    subset=["Score"])
            .format({"Score": "{}%"})
            .set_properties(**{"font-size": "13px"})
        )
        st.dataframe(styled, use_container_width=True, hide_index=True,
                     height=min(420, 45 + len(df) * 36))

        st.divider()

        # Filters
        f1, f2, f3 = st.columns([1, 1, 2])
        type_f   = f1.selectbox("Type",   ["All"] + list(TYPE_COLORS.keys()),
                                label_visibility="collapsed")
        agency_f = f2.selectbox("Agency",
                                ["All"] + sorted({o["agency"] for o in opps if o.get("agency")}),
                                label_visibility="collapsed")
        sort_by  = f3.radio("Sort", ["Relevance","Deadline","Posted"],
                            horizontal=True, label_visibility="collapsed")

        filtered = [
            o for o in opps
            if (type_f   == "All" or o.get("type")   == type_f)
            and (agency_f == "All" or o.get("agency") == agency_f)
        ]

        def card_sort(o):
            if sort_by == "Relevance": return -(o.get("relevance_score") or 0)
            if sort_by == "Deadline":  return  (o.get("deadline") or "9999")
            return                            -(o.get("posted_date") or "0000")
        filtered.sort(key=card_sort)

        st.markdown(
            f"### Opportunities &nbsp;"
            f"<span style='font-size:14px;color:#9ca3af'>{len(filtered)} results</span>",
            unsafe_allow_html=True
        )

        # Cards
        for o in filtered:
            tc   = TYPE_COLORS.get(o.get("type", ""), "#5F5E5A")
            dl, lbl, dlcol = deadline_info(o.get("deadline"))
            wd   = working_days(o.get("deadline"), all_holidays) if show_wdays else None
            score = o.get("relevance_score") or 0
            bar_c = "#3B6D11" if score >= 80 else "#BA7517" if score >= 60 else "#9ca3af"

            with st.container(border=True):
                badges  = badge(o.get("type", ""), tc)
                badges += f" &nbsp; {badge(o.get('agency',''), '#374151')}"
                if o.get("estimated_value"):
                    badges += f" &nbsp; {badge(o['estimated_value'], '#3B6D11')}"
                score_html = (
                    f'<span style="float:right;font-size:12px;color:{bar_c};font-weight:700">'
                    f'{score}%</span>'
                )
                st.markdown(badges + score_html, unsafe_allow_html=True)

                title = o.get("title", "Untitled")
                if o.get("url"): st.markdown(f"**[{title}]({o['url']})**")
                else:            st.markdown(f"**{title}**")

                if o.get("solicitation_no"):
                    st.caption(f"`{o['solicitation_no']}`")

                st.markdown(o.get("description") or "")

                fc1, fc2, fc3 = st.columns([1, 2, 1])
                if o.get("posted_date"):
                    fc1.caption(f"Posted {o['posted_date']}")
                if dl is not None and o.get("deadline"):
                    wday_str = f" / {wd} work days" if wd is not None else ""
                    fc2.markdown(
                        f'<span style="color:{dlcol};font-weight:600;font-size:13px">'
                        f'{lbl}{wday_str} - Due {o["deadline"]}</span>',
                        unsafe_allow_html=True
                    )
                if o.get("estimated_value"):
                    fc3.markdown(
                        f'<span style="color:#3B6D11;font-weight:700">{o["estimated_value"]}</span>',
                        unsafe_allow_html=True
                    )

            # Analyze button → jump to PDF Parser tab
            opp_id = o.get("id","")
            if st.button(
                "Analyze solicitation docs",
                key=f"analyze_opp_{opp_id}",
                help="Fetch and parse this solicitation on the PDF Parser page",
            ):
                st.session_state["page"] = "PDF Parser"
                st.session_state["queued_opp"] = {
                    "notice_id":       o.get("solicitation_no") or o.get("id",""),
                    "solicitation_no": o.get("solicitation_no",""),
                    "title":           o.get("title",""),
                    "url":             o.get("url",""),
                    "agency":          o.get("agency",""),
                }
                st.rerun()

        st.divider()

        # Calendar
        st.markdown("### Deadline Calendar")
        render_calendar(opps, all_holidays, show_wdays)

        st.divider()

        # Scan log
        with st.expander("Scan log"):
            for entry in load_logs():
                icon = {"success": "OK", "error": "ERR"}.get(entry["level"], "...")
                st.markdown(f'`{entry["created_at"][:16]}` [{icon}] {entry["message"]}')

if __name__ == "__main__":
    main()
