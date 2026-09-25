"""업무일지 & KPI 분석 시스템 (Streamlit)

실행:        streamlit run app.py
수동 백업:   python app.py --backup      (Windows 작업 스케줄러 등록용)
"""
from __future__ import annotations

import html
import json
import shutil
import sys
import threading
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from io import BytesIO
from pathlib import Path

import pandas as pd

# ─────────────────────────────── 설정 ───────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_FILE = DATA_DIR / "worklog.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_PREFIX = "[백업] 업무일지_"

CATEGORIES = ["행정", "인사", "차량", "개인", "디자인"]
STATUSES = ["대기", "진행중", "완료", "보류"]
COLUMNS = ["id", "작성일", "구분", "업무요약", "업무내용", "시작시간", "끝시간", "소요시간(분)", "진행상태", "비고"]
DUR = "소요시간(분)"

DEFAULT_SETTINGS = {
    "auto_backup": True,       # 매일 지정 시각 자동 스냅샷
    "backup_hour": 18,
    "weekdays_only": True,
    "keep_days": 30,           # 0 = 오래된 백업 삭제 안 함
    "standard_min": 480,       # 과중 업무 판단 기준(분)
    "last_auto_backup": "",
}

_file_lock = threading.Lock()


# ─────────────────────────────── 데이터 계층 ───────────────────────────────

def is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (dtime, datetime, date)):
        return False
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip() == ""


def parse_time(v) -> dtime | None:
    if is_blank(v):
        return None
    if isinstance(v, datetime):
        return v.time().replace(second=0, microsecond=0)
    if isinstance(v, dtime):
        return v.replace(second=0, microsecond=0)
    s = str(v).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    return None


def calc_minutes(start, end) -> int | None:
    """(끝 - 시작) 분 단위. 둘 중 하나라도 비어 있으면 빈 값, 형식 오류면 0 (IFERROR 대응).
    끝시간이 시작시간보다 이르면 자정을 넘긴 업무로 계산."""
    if is_blank(start) or is_blank(end):
        return None
    s, e = parse_time(start), parse_time(end)
    if s is None or e is None:
        return 0
    return ((e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)) % 1440


def parse_date(v) -> date | None:
    if is_blank(v):
        return None
    ts = pd.to_datetime(v, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def empty_df() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def normalize(df: pd.DataFrame, default_date: date | None = None) -> pd.DataFrame:
    """타입 정리, 빈 행 제거, id·작성일·진행상태 기본값, 소요시간 재계산."""
    df = df.copy()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    df = df[COLUMNS]

    for c in ["구분", "업무요약", "업무내용", "진행상태", "비고", "id"]:
        df[c] = df[c].map(lambda v: "" if is_blank(v) else str(v).strip())

    has_content = (df["구분"] != "") | (df["업무요약"] != "") | (df["업무내용"] != "")
    df = df[has_content].copy()

    df["작성일"] = df["작성일"].map(parse_date)
    if default_date is not None:
        df["작성일"] = df["작성일"].map(lambda d: d or default_date)
    df["시작시간"] = df["시작시간"].map(parse_time)
    df["끝시간"] = df["끝시간"].map(parse_time)
    df[DUR] = pd.array([calc_minutes(s, e) for s, e in zip(df["시작시간"], df["끝시간"])], dtype="Int64")
    df.loc[df["진행상태"] == "", "진행상태"] = "대기"
    df["id"] = [i or uuid.uuid4().hex[:12] for i in df["id"]]

    return sort_df(df).reset_index(drop=True)


def sort_df(df: pd.DataFrame) -> pd.DataFrame:
    key_date = df["작성일"].map(lambda d: d or date.min)
    key_time = df["시작시간"].map(lambda t: t.hour * 60 + t.minute if t else 9999)
    order = sorted(range(len(df)), key=lambda i: (key_date.iloc[i], key_time.iloc[i]))
    return df.iloc[order]


def load_data() -> pd.DataFrame:
    if not DATA_FILE.exists():
        return empty_df()
    with _file_lock:
        raw = pd.read_csv(DATA_FILE, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    return normalize(raw)


def to_storage(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["작성일"] = out["작성일"].map(lambda d: d.isoformat() if d else "")
    out["시작시간"] = out["시작시간"].map(lambda t: t.strftime("%H:%M") if t else "")
    out["끝시간"] = out["끝시간"].map(lambda t: t.strftime("%H:%M") if t else "")
    out[DUR] = out[DUR].map(lambda v: "" if pd.isna(v) else int(v))
    return out[COLUMNS]


def save_data(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize(df)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with _file_lock:
        to_storage(df).to_csv(tmp, index=False, encoding="utf-8-sig")
        tmp.replace(DATA_FILE)
    return df


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return s


def save_settings(s: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ─────────────────────────────── 보고 텍스트 ───────────────────────────────

def build_report(df: pd.DataFrame, target: date, kind: str) -> tuple[str, int]:
    rows = df[(df["작성일"] == target) & (df["업무요약"] != "")]
    lines = []
    for _, r in rows.iterrows():
        if kind == "summary":
            lines.append(f"- {r['업무요약']}")
        else:
            head = f"[{r['구분']}] " if r["구분"] else ""
            detail = " ".join(r["업무내용"].split("\n")).strip()
            lines.append(f"- {head}{r['업무요약']}" + (f" - {detail}" if detail else ""))
    return "\n".join(lines), len(lines)


# ─────────────────────────────── 백업 ───────────────────────────────

def list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob(f"{BACKUP_PREFIX}*.csv"), reverse=True)


def create_backup() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{BACKUP_PREFIX}{datetime.now():%Y%m%d_%H%M%S}.csv"
    with _file_lock:
        if DATA_FILE.exists():
            shutil.copy2(DATA_FILE, dest)
        else:
            to_storage(empty_df()).to_csv(dest, index=False, encoding="utf-8-sig")
    cleanup_backups(load_settings()["keep_days"])
    return dest


def cleanup_backups(keep_days: int) -> None:
    if not keep_days:
        return
    cutoff = time.time() - keep_days * 86400
    for p in list_backups():
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


def restore_backup(path: Path) -> None:
    create_backup()  # 복원 직전 상태도 보관
    with _file_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, DATA_FILE)


def run_scheduled_backup_if_due(now: datetime | None = None) -> Path | None:
    now = now or datetime.now()
    s = load_settings()
    if not s["auto_backup"] or now.hour < int(s["backup_hour"]):
        return None
    if s["weekdays_only"] and now.weekday() >= 5:
        return None
    today = now.date().isoformat()
    if s["last_auto_backup"] == today:
        return None
    path = create_backup()
    s["last_auto_backup"] = today
    save_settings(s)
    return path


def export_excel(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        to_storage(df).drop(columns="id").to_excel(xw, sheet_name="업무일지", index=False)
    return buf.getvalue()


# ─────────────────────────────── UI ───────────────────────────────

def main() -> None:
    import plotly.express as px
    import plotly.graph_objects as go
    import streamlit as st

    st.set_page_config(page_title="업무일지 & KPI", page_icon="📋", layout="wide")

    @st.cache_resource
    def start_backup_scheduler() -> threading.Thread:
        """앱 서버가 켜져 있는 동안 1분마다 자동 백업 시각을 확인."""
        def loop():
            while True:
                try:
                    run_scheduled_backup_if_due()
                except Exception as exc:  # 스케줄러가 죽지 않도록
                    print(f"[auto-backup] {exc}", file=sys.stderr)
                time.sleep(60)

        t = threading.Thread(target=loop, name="auto-backup", daemon=True)
        t.start()
        return t

    start_backup_scheduler()

    st.markdown(
        """
        <style>
          :root {
            --brand: #8B9A6E; --brand-hover: #7A895F; --brand-deep: #4F5A3B; --on-brand: #141A0E;
            --surface: #EEEEEE; --line: #E2E2E2;
          }
          .block-container { padding-top: 3.75rem; }

          /* 주요 버튼: 브랜드 색 + 진한 굵은 글씨(명암비 약 5.9:1), 호버 시 10% 어둡게 */
          [data-testid^="stBaseButton-primary"] p { color: inherit !important; }
          [data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
            background: var(--brand) !important; border-color: var(--brand) !important;
            color: var(--on-brand) !important; font-weight: 600;
          }
          [data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover,
          [data-testid="stBaseButton-primary"]:active, [data-testid="stBaseButton-primaryFormSubmit"]:active {
            background: var(--brand-hover) !important; border-color: var(--brand-hover) !important;
            color: var(--on-brand) !important;
          }
          /* 보조 버튼: 회색 면 + 진한 글씨 */
          [data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
            background: var(--surface); border-color: var(--line); color: #2F3527;
          }
          [data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover {
            border-color: var(--brand); color: var(--brand-deep); background: #E6E8E0;
          }
          /* 탭: 브랜드 언더라인, 선택 탭 글씨는 진한 톤으로 가독성 확보 */
          .stTabs [data-baseweb="tab-highlight"] { background-color: var(--brand) !important; height: 3px; }
          .stTabs [role="tab"][aria-selected="true"] p { color: var(--brand-deep); font-weight: 600; }
          .stTabs [role="tab"]:hover p { color: var(--brand-deep); }
          /* 폼·펼침 영역을 카드처럼 */
          [data-testid="stForm"], [data-testid="stExpander"] details {
            border: 1px solid var(--line); border-radius: 14px;
          }
          .kpi-grid {
            display: grid; gap: 12px; margin-bottom: 1rem;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          }
          .kpi-card {
            background: var(--surface);
            border: 1px solid var(--line);
            border-top: 3px solid var(--brand);
            border-radius: 16px; padding: 16px 18px; min-width: 0;
          }
          .kpi-label { font-size: .8rem; opacity: .65; margin-bottom: 4px; }
          .kpi-value { font-size: 1.7rem; font-weight: 700; line-height: 1.2; white-space: nowrap; }
          .kpi-sub { font-size: .75rem; opacity: .55; margin-top: 2px; }
          .hero {
            background: var(--brand); color: var(--on-brand);
            border: 1px solid var(--brand-hover);
            border-radius: 18px; padding: 18px 24px; margin-bottom: 1rem;
          }
          .hero h1 { font-size: 1.6rem; margin: 0; padding: 0; color: var(--on-brand); }
          .hero p { margin: 4px 0 0; color: #1F2716; }

          /* 업무 카드 (모바일 목록) */
          .task-card {
            border: 1px solid var(--line); border-left: 4px solid var(--brand); border-radius: 14px;
            padding: 12px 14px; background: var(--surface);
          }
          .task-top { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 6px; }
          .badge { font-size: .72rem; font-weight: 600; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
          .badge-cat { background: rgba(139,154,110,.2); color: var(--brand-deep); }
          .st-완료 { background: #dcfce7; color: #166534; }
          .st-진행중 { background: #dbeafe; color: #1e40af; }
          .st-대기 { background: #fef3c7; color: #92400e; }
          .st-보류 { background: #e5e7eb; color: #374151; }
          .task-time { margin-left: auto; font-size: .78rem; opacity: .65; white-space: nowrap; }
          .task-title { font-weight: 600; font-size: .98rem; word-break: keep-all; overflow-wrap: anywhere; }
          .report-box {
            white-space: pre-wrap; word-break: keep-all; overflow-wrap: anywhere;
            line-height: 1.65; font-size: .95rem; padding: 14px 16px; margin: 8px 0 16px;
            border-radius: 12px; background: var(--surface); border: 1px solid var(--line);
            -webkit-user-select: text; user-select: text;
          }
          .task-detail { font-size: .85rem; opacity: .75; margin-top: 4px; white-space: pre-wrap;
                         word-break: keep-all; overflow-wrap: anywhere; }

          /* ── 휴대폰 (Galaxy S25 ≈ 360~412px) ── */
          @media (max-width: 640px) {
            .block-container { padding: 3.75rem .75rem 4rem !important; }
            .hero { padding: 14px 16px; border-radius: 14px; }
            .hero h1 { font-size: 1.25rem; }
            .hero p { font-size: .82rem; }
            .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
            .kpi-card:last-child:nth-child(odd) { grid-column: span 2; }
            .report-box { font-size: .9rem; padding: 12px 14px; }
            .kpi-card { padding: 12px; border-radius: 12px; }
            .kpi-label { font-size: .72rem; }
            .kpi-value { font-size: 1.3rem; }
            .kpi-sub { font-size: .68rem; }
            .stTabs [role="tablist"] { gap: 2px; overflow-x: auto; scrollbar-width: none; }
            .stTabs [role="tab"] { padding: 6px 8px; font-size: .9rem; white-space: nowrap; }
            h3 { font-size: 1.1rem !important; }
            .stButton button, .stFormSubmitButton button, .stDownloadButton button { min-height: 44px; }
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    def kpi_grid(cards: list[tuple]) -> None:
        """(라벨, 값, 보조문구, 색상) 목록 → 반응형 카드 격자 (PC 한 줄 / 휴대폰 2열)."""
        items = []
        for label, value, sub, color in cards:
            style = f' style="color:{color}"' if color else ""
            items.append(
                f'<div class="kpi-card"><div class="kpi-label">{html.escape(label)}</div>'
                f'<div class="kpi-value"{style}>{html.escape(value)}</div>'
                f'<div class="kpi-sub">{html.escape(sub)}</div></div>'
            )
        st.markdown(f'<div class="kpi-grid">{"".join(items)}</div>', unsafe_allow_html=True)

    def task_cards(view: pd.DataFrame, category_options: list[str]) -> None:
        """휴대폰용 카드 목록. 카드마다 '수정' 영역에서 편집/삭제."""
        if view.empty:
            st.caption("등록된 업무가 없습니다.")
            return
        for _, r in view.iterrows():
            t = ""
            if r["시작시간"] or r["끝시간"]:
                s = r["시작시간"].strftime("%H:%M") if r["시작시간"] else "--:--"
                e = r["끝시간"].strftime("%H:%M") if r["끝시간"] else "--:--"
                dur = "" if pd.isna(r[DUR]) else f" · {int(r[DUR])}분"
                t = f"{s}~{e}{dur}"
            date_txt = f"{r['작성일']:%m/%d} " if r["작성일"] else ""
            cat = f'<span class="badge badge-cat">{html.escape(r["구분"])}</span>' if r["구분"] else ""
            detail = f'<div class="task-detail">{html.escape(r["업무내용"])}</div>' if r["업무내용"] else ""
            st.markdown(
                f'<div class="task-card"><div class="task-top">{cat}'
                f'<span class="badge st-{r["진행상태"]}">{html.escape(r["진행상태"])}</span>'
                f'<span class="task-time">{date_txt}{t}</span></div>'
                f'<div class="task-title">{html.escape(r["업무요약"])}</div>{detail}</div>',
                unsafe_allow_html=True,
            )
            with st.expander("수정"):
                with st.form(f"edit_{r['id']}"):
                    cats = category_options if r["구분"] in category_options else category_options + [r["구분"]]
                    e_cat = st.selectbox("구분", cats, index=cats.index(r["구분"]) if r["구분"] in cats else 0)
                    e_status = st.selectbox("진행상태", STATUSES, index=STATUSES.index(r["진행상태"]))
                    e_summary = st.text_input("업무요약", r["업무요약"])
                    e_detail = st.text_area("업무내용", r["업무내용"], height=80)
                    c1, c2 = st.columns(2)
                    e_start = c1.time_input("시작", r["시작시간"], step=300)
                    e_end = c2.time_input("끝", r["끝시간"], step=300)
                    e_date = st.date_input("작성일", r["작성일"] or date.today(), format="YYYY-MM-DD")
                    e_note = st.text_input("비고", r["비고"])
                    b1, b2 = st.columns(2)
                    saved = b1.form_submit_button("저장", type="primary", width="stretch")
                    deleted = b2.form_submit_button("🗑 삭제", width="stretch")
                if saved or deleted:
                    all_df = load_data()
                    if deleted:
                        all_df = all_df[all_df["id"] != r["id"]]
                        st.toast("삭제했습니다.", icon="🗑")
                    else:
                        mask = all_df["id"] == r["id"]
                        for col, val in [("구분", e_cat), ("진행상태", e_status), ("업무요약", e_summary),
                                         ("업무내용", e_detail), ("시작시간", e_start), ("끝시간", e_end),
                                         ("작성일", e_date), ("비고", e_note)]:
                            all_df.loc[mask, col] = pd.Series([val] * mask.sum(), index=all_df.index[mask], dtype=object)
                        st.toast("저장되었습니다.", icon="✅")
                    save_data(all_df)
                    st.rerun()

    def is_mobile() -> bool:
        ua = (st.context.headers.get("User-Agent") or "").lower()
        return any(k in ua for k in ("android", "iphone", "mobile"))

    def show_chart(fig) -> None:
        fig.update_layout(font=dict(size=12), title_font=dict(size=15))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False, "responsive": True})

    def copy_button(text: str, label: str) -> None:
        """클릭 시 클라이언트 클립보드로 복사 + 토스트 알림."""
        payload = json.dumps(text).replace("</", "<\\/")
        st.iframe(
            f"""
            <style>
              body {{ margin:0; font-family: 'Source Sans Pro', sans-serif; }}
              button {{
                width:100%; padding:10px 14px; border:0; border-radius:10px; cursor:pointer;
                background: #8B9A6E; color:#141A0E;
                font-size:15px; font-weight:600; box-shadow:0 4px 14px rgba(139,154,110,.3);
              }}
              button:hover {{ background: #7A895F; }}
              button:active {{ transform: translateY(1px); }}
              button.ok {{ background: #16a34a; color: #fff; }}
              button.fail {{ background: #dc2626; color: #fff; font-size: 13px; }}
            </style>
            <button id="b">{html.escape(label)}</button>
            <script>
              const text = {payload}, label = {json.dumps(label)};
              const b = document.getElementById('b');
              function fallback() {{
                const ta = document.createElement('textarea');
                ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
                document.body.appendChild(ta); ta.select();
                let ok = false; try {{ ok = document.execCommand('copy'); }} catch (e) {{}}
                ta.remove(); return ok;
              }}
              b.addEventListener('click', async () => {{
                let ok = false;
                try {{ await navigator.clipboard.writeText(text); ok = true; }} catch (e) {{ ok = fallback(); }}
                b.textContent = ok ? '✅ 복사되었습니다' : '⚠️ 실패 — 아래 문구를 길게 눌러 복사하세요';
                b.className = ok ? 'ok' : 'fail';
                setTimeout(() => {{ b.textContent = label; b.className = ''; }}, 2000);
              }});
            </script>
            """,
            height=52,
        )

    settings = load_settings()
    df = load_data()
    today = date.today()

    st.markdown(
        f'<div class="hero"><h1>📋 업무일지</h1>'
        f'<p>{today:%Y-%m-%d} · 오늘 등록 {int((df["작성일"] == today).sum())}건 · 전체 {len(df)}건</p></div>',
        unsafe_allow_html=True,
    )

    tab_log, tab_report, tab_kpi, tab_backup = st.tabs(["📝 일지", "📤 보고", "📊 KPI", "💾 백업"])

    category_options = CATEGORIES + sorted(set(df["구분"]) - set(CATEGORIES) - {""})

    # ───── 업무일지 입력 ─────
    with tab_log:
        with st.expander("➕ 새 업무 추가", expanded=not is_mobile()):
            with st.form("add_form", clear_on_submit=True):
                c1, c2, c3, c4 = st.columns([1.1, 1.1, 1.3, 1])
                f_date = c1.date_input("작성일", value=today, format="YYYY-MM-DD")
                f_cat = c2.selectbox("구분", category_options)
                f_cat_custom = c3.text_input("구분 직접 입력", placeholder="입력 시 우선 적용")
                f_status = c4.selectbox("진행상태", STATUSES)

                f_summary = st.text_input("업무요약 *", placeholder="보고용 한 줄 요약")
                f_detail = st.text_area("업무내용", placeholder="상세 작업 내역", height=90)

                c5, c6, c7 = st.columns([1, 1, 2])
                f_start = c5.time_input("시작시간", value=None, step=300)
                f_end = c6.time_input("끝시간", value=None, step=300)
                f_note = c7.text_input("비고")

                if st.form_submit_button("저장", type="primary", width="stretch"):
                    category = f_cat_custom.strip() or f_cat
                    if not f_summary.strip():
                        st.error("업무요약을 입력하세요.")
                    else:
                        new = pd.DataFrame([{
                            "id": "", "작성일": f_date, "구분": category, "업무요약": f_summary,
                            "업무내용": f_detail, "시작시간": f_start, "끝시간": f_end,
                            "진행상태": f_status, "비고": f_note,
                        }])
                        save_data(pd.concat([df, new], ignore_index=True))
                        if category not in CATEGORIES:
                            st.toast(f"기본 목록에 없는 구분 '{category}'(으)로 저장했습니다.", icon="⚠️")
                        if f_start and f_end and f_end < f_start:
                            st.toast("끝시간이 시작시간보다 이릅니다. 자정을 넘긴 업무로 계산했습니다.", icon="⚠️")
                        st.toast("저장되었습니다.", icon="✅")
                        st.rerun()

        st.subheader("업무 목록")
        v1, v2, v3 = st.columns([1.2, 1.2, 1.2])
        view_mode = v1.radio("보기", ["선택한 날짜", "전체"], horizontal=True, label_visibility="collapsed")
        view_date = v2.date_input("날짜", value=today, format="YYYY-MM-DD", label_visibility="collapsed",
                                  disabled=view_mode == "전체")
        layout = v3.radio("형식", ["카드", "표"], horizontal=True, label_visibility="collapsed",
                          index=0 if is_mobile() else 1)

        view = df if view_mode == "전체" else df[df["작성일"] == view_date]
        if layout == "카드":
            task_cards(view, category_options)
        else:
            edited = st.data_editor(
                view.reset_index(drop=True),
                key=f"editor_{view_mode}_{view_date}",
                num_rows="dynamic",
                hide_index=True,
                width="stretch",
                column_order=[c for c in COLUMNS if c != "id"],
                column_config={
                    "작성일": st.column_config.DateColumn("작성일", format="YYYY-MM-DD", width="small"),
                    "구분": st.column_config.SelectboxColumn(
                        "구분", options=category_options, width="small",
                        help="새 구분은 위 '구분 직접 입력'으로 추가하세요."),
                    "업무요약": st.column_config.TextColumn("업무요약", width="medium"),
                    "업무내용": st.column_config.TextColumn("업무내용", width="large"),
                    "시작시간": st.column_config.TimeColumn("시작", format="HH:mm", step=60, width="small"),
                    "끝시간": st.column_config.TimeColumn("끝", format="HH:mm", step=60, width="small"),
                    DUR: st.column_config.NumberColumn("소요(분)", disabled=True, width="small",
                                                       help="저장 시 자동 계산"),
                    "진행상태": st.column_config.SelectboxColumn("진행상태", options=STATUSES, width="small"),
                    "비고": st.column_config.TextColumn("비고", width="medium"),
                },
            )

            s1, s2 = st.columns([1, 4])
            if s1.button("💾 변경사항 저장", type="primary", width="stretch"):
                rest = df[~df["id"].isin(set(view["id"]))]
                default_date = today if view_mode == "전체" else view_date
                save_data(pd.concat([rest, normalize(edited, default_date=default_date)], ignore_index=True))
                st.toast("변경사항이 저장되었습니다.", icon="✅")
                st.rerun()
            s2.caption("행 추가/삭제/수정 후 반드시 저장을 누르세요. 소요시간은 저장 시 다시 계산되며, "
                       "시간이 비어 있으면 빈 값, 잘못된 값이면 0으로 처리됩니다.")

    # ───── 일일 보고 ─────
    with tab_report:
        r1, _ = st.columns([1, 3])
        report_date = r1.date_input("보고 날짜", value=today, format="YYYY-MM-DD", key="report_date")

        summary_text, n = build_report(df, report_date, "summary")
        detail_text, _ = build_report(df, report_date, "detail")

        if n == 0:
            st.info(f"{report_date:%Y-%m-%d}에 작성된 업무가 없습니다.")
        else:
            st.caption(f"{report_date:%Y-%m-%d} · {n}건")
            left, right = st.columns(2)
            with left:
                copy_button(summary_text, "📋 업무요약 일괄 복사")
                st.markdown(f'<div class="report-box">{html.escape(summary_text)}</div>', unsafe_allow_html=True)
            with right:
                copy_button(detail_text, "📑 업무내용 일괄 복사")
                st.markdown(f'<div class="report-box">{html.escape(detail_text)}</div>', unsafe_allow_html=True)

    # ───── KPI 대시보드 ─────
    with tab_kpi:
        kd = df[df["작성일"].notna()].copy()
        if kd.empty:
            st.info("분석할 데이터가 없습니다. 업무를 먼저 등록하세요.")
        else:
            min_d, max_d = min(kd["작성일"]), max(kd["작성일"])
            f1, f2, f3 = st.columns([2, 1, 1])
            period = f1.date_input("분석 기간", value=(max(min_d, max_d - timedelta(days=29)), max_d),
                                   min_value=min_d, max_value=max_d, format="YYYY-MM-DD")
            exclude_personal = f2.checkbox("'개인' 제외", value=False)
            standard = f3.number_input("과중 기준(분)", min_value=60, max_value=1440, step=30,
                                       value=int(settings["standard_min"]))
            if standard != settings["standard_min"]:
                settings["standard_min"] = int(standard)
                save_settings(settings)

            start_d, end_d = (period if isinstance(period, tuple) and len(period) == 2 else (min_d, max_d))
            kd = kd[(kd["작성일"] >= start_d) & (kd["작성일"] <= end_d)]
            if exclude_personal:
                kd = kd[kd["구분"] != "개인"]
            kd["분"] = kd[DUR].fillna(0).astype(int)

            if kd.empty:
                st.warning("선택한 기간에 데이터가 없습니다.")
            else:
                total_min = int(kd["분"].sum())
                daily = kd.groupby("작성일", as_index=False)["분"].sum().sort_values("작성일")
                work_days = len(daily)
                done_rate = (kd["진행상태"] == "완료").mean()
                overload = int((daily["분"] > standard).sum())

                kpi_grid([
                    ("총 등록 건수", f"{len(kd):,}건", f"{start_d:%m/%d} ~ {end_d:%m/%d}", None),
                    ("총 소요시간", f"{total_min / 60:,.1f}h", f"{total_min:,}분", None),
                    ("업무 완료율", f"{done_rate:.1%}", f"완료 {int((kd['진행상태'] == '완료').sum())}건", "#5F6B48"),
                    ("일평균 업무시간", f"{total_min / 60 / work_days:.1f}h", f"근무일 {work_days}일", None),
                    ("과중 업무일", f"{overload}일", f"{standard}분 초과", "#dc2626" if overload else None),
                ])

                palette = ["#8B9A6E", "#C9A66B", "#6E8B9A", "#B5838D", "#A3B18A", "#9A8B6E", "#7D8471", "#D4C5A9"]
                c_left, c_right = st.columns([1, 1.6])

                cat = (kd.assign(구분=kd["구분"].replace("", "미분류"))
                       .groupby("구분", as_index=False)["분"].sum().sort_values("분", ascending=False))
                cat["시간"] = (cat["분"] / 60).round(1)
                cat["비중"] = cat["분"] / cat["분"].sum() if cat["분"].sum() else 0.0
                with c_left:
                    fig = px.pie(cat, names="구분", values="분", hole=0.55,
                                 color_discrete_sequence=palette, title="카테고리별 시간 배분")
                    fig.update_traces(textinfo="percent+label", hovertemplate="%{label}<br>%{value:,}분<extra></extra>")
                    fig.update_traces(textposition="inside", insidetextorientation="horizontal")
                    fig.update_layout(margin=dict(t=44, b=8, l=8, r=8), height=320, showlegend=False,
                                      uniformtext=dict(minsize=10, mode="hide"))
                    show_chart(fig)

                with c_right:
                    daily["7일 평균"] = daily["분"].rolling(7, min_periods=1).mean()
                    fig = go.Figure()
                    fig.add_bar(x=daily["작성일"], y=daily["분"], name="총 소요(분)",
                                marker_color=["#ef4444" if v > standard else "#8B9A6E" for v in daily["분"]],
                                hovertemplate="%{x|%m/%d}<br>%{y:,}분<extra></extra>")
                    fig.add_scatter(x=daily["작성일"], y=daily["7일 평균"], name="7일 이동평균",
                                    mode="lines", line=dict(color="#4F5A3B", width=2))
                    fig.add_hline(y=standard, line_dash="dash", line_color="#ef4444",
                                  annotation_text=f"기준 {standard}분", annotation_position="top left")
                    fig.update_layout(title="일자별 총 소요시간 추이", height=340,
                                      margin=dict(t=44, b=8, l=8, r=8),
                                      legend=dict(orientation="h", y=-0.12, x=0.5, xanchor="center", yanchor="top"),
                                      xaxis=dict(tickformat="%m/%d", nticks=8, fixedrange=True),
                                      yaxis=dict(title=None, ticksuffix="분", fixedrange=True))
                    show_chart(fig)

                t1, t2, t3 = st.columns([1.2, 1, 1.3])
                with t1:
                    st.markdown("**카테고리별 소요시간**")
                    st.dataframe(cat, hide_index=True, width="stretch", column_config={
                        "분": st.column_config.NumberColumn("소요(분)", format="%d"),
                        "시간": st.column_config.NumberColumn("시간", format="%.1f"),
                        "비중": st.column_config.ProgressColumn("비중", format="percent", min_value=0, max_value=1),
                    })
                with t2:
                    st.markdown("**진행상태별 현황**")
                    status = kd["진행상태"].value_counts().reindex(STATUSES, fill_value=0).rename_axis("상태")
                    status = status.reset_index(name="건수")
                    status["비율"] = status["건수"] / len(kd)
                    st.dataframe(status, hide_index=True, width="stretch", column_config={
                        "비율": st.column_config.ProgressColumn("비율", format="percent", min_value=0, max_value=1),
                    })
                with t3:
                    st.markdown("**주별 총 소요시간**")
                    wk = kd.assign(주=pd.to_datetime(kd["작성일"]).map(lambda d: (d - timedelta(days=d.weekday())).date()))
                    weekly = wk.groupby("주").agg(소요분=("분", "sum"), 근무일=("작성일", "nunique")).reset_index()
                    weekly["시간"] = (weekly["소요분"] / 60).round(1)
                    weekly["일평균(h)"] = (weekly["소요분"] / 60 / weekly["근무일"]).round(1)
                    st.dataframe(weekly.rename(columns={"주": "주 시작(월)", "소요분": "소요(분)"}),
                                 hide_index=True, width="stretch")

    # ───── 백업 ─────
    with tab_backup:
        b1, b2 = st.columns(2)
        with b1:
            st.markdown("#### 수동 백업")
            if st.button("💾 지금 백업", type="primary", width="stretch"):
                path = create_backup()
                st.toast(f"백업 완료: {path.name}", icon="✅")
            d1, d2 = st.columns(2)
            stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
            d1.download_button("⬇️ CSV 다운로드", to_storage(df).drop(columns="id").to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"업무일지_{stamp}.csv", mime="text/csv", width="stretch")
            d2.download_button("⬇️ Excel 다운로드", export_excel(df), file_name=f"업무일지_{stamp}.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                               width="stretch")

        with b2:
            st.markdown("#### 자동 백업")
            with st.form("backup_settings"):
                a_on = st.toggle("매일 자동 스냅샷", value=settings["auto_backup"])
                a1, a2 = st.columns(2)
                a_hour = a1.number_input("백업 시각(시)", 0, 23, int(settings["backup_hour"]))
                a_keep = a2.number_input("보관 기간(일, 0=무제한)", 0, 3650, int(settings["keep_days"]))
                a_wd = st.checkbox("평일에만 실행", value=settings["weekdays_only"])
                if st.form_submit_button("설정 저장", width="stretch"):
                    settings.update(auto_backup=a_on, backup_hour=int(a_hour),
                                    keep_days=int(a_keep), weekdays_only=a_wd)
                    save_settings(settings)
                    st.toast("자동 백업 설정을 저장했습니다.", icon="✅")
            last = settings["last_auto_backup"] or "없음"
            st.caption(f"마지막 자동 백업: {last} · 앱 서버가 실행 중일 때 1분마다 확인합니다. "
                       "PC가 꺼져 있어도 확실히 돌리려면 작업 스케줄러에 `python app.py --backup`을 등록하세요.")

        st.divider()
        st.markdown(f"#### 백업 목록  <small style='opacity:.6'>{BACKUP_DIR}</small>", unsafe_allow_html=True)
        backups = list_backups()
        if not backups:
            st.caption("아직 백업이 없습니다.")
        else:
            st.dataframe(pd.DataFrame([{
                "파일명": p.name,
                "생성시각": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "크기(KB)": round(p.stat().st_size / 1024, 1),
            } for p in backups]), hide_index=True, width="stretch")

            with st.expander("♻️ 백업에서 복원"):
                choice = st.selectbox("복원할 백업", backups, format_func=lambda p: p.name)
                confirm = st.checkbox("현재 데이터를 이 백업으로 덮어씁니다 (현재 상태는 자동으로 먼저 백업됩니다).")
                if st.button("복원", disabled=not confirm):
                    restore_backup(choice)
                    st.toast("복원했습니다.", icon="✅")
                    st.rerun()


if "--backup" in sys.argv:
    print(f"백업 완료: {create_backup()}")
else:
    main()
