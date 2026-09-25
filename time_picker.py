"""시계 다이얼로 고르거나 숫자로 직접 입력하는 시간 선택 위젯 (st.components.v2).

- 입력칸/🕐 클릭 → 시계 다이얼 팝업 (시 선택 → 분 선택 → 확인)
- 직접 입력: 9:30 · 0930 · 930 · 15 (=15:00) 모두 허용, Enter 또는 칸을 벗어나면 적용
- 값은 st.session_state[key] 에 datetime.time | None 으로 저장된다.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import streamlit as st

_CSS = """
.wtp-root { font-family: inherit; }
.wtp-label { font-size: 14px; line-height: 1.6; margin-bottom: 4px; color: var(--st-text-color); }
.wtp-box {
  display: flex; align-items: center; height: 40px; box-sizing: border-box;
  border: 1px solid transparent; border-radius: var(--st-base-radius, 8px);
  background: var(--st-secondary-background-color); color: var(--st-text-color);
}
.wtp-box:focus-within { border-color: var(--st-primary-color); }
.wtp-box.wtp-bad { border-color: #dc2626; }
.wtp-inp {
  flex: 1; min-width: 0; height: 100%; padding: 0 10px; border: 0; outline: 0;
  background: transparent; color: inherit; font: inherit; font-size: 15px; letter-spacing: .03em;
}
.wtp-clk {
  display: grid; place-items: center; width: 38px; height: 100%; padding: 0; border: 0;
  background: transparent; color: inherit; opacity: .7; cursor: pointer;
}
.wtp-clk:hover { opacity: 1; color: var(--st-primary-color); }

.wtp-pop {
  position: fixed; z-index: 100000; width: 264px; box-sizing: border-box; padding: 14px;
  border-radius: 14px; background: var(--wtp-bg); color: var(--wtp-fg);
  border: 1px solid var(--wtp-line); box-shadow: 0 12px 32px rgba(0,0,0,.18);
  font-family: var(--wtp-font); user-select: none; -webkit-user-select: none;
}
.wtp-head { display: flex; justify-content: center; align-items: baseline; gap: 4px; margin-bottom: 10px; }
.wtp-seg {
  font-size: 34px; font-weight: 600; line-height: 1.1; padding: 2px 8px; border-radius: 8px;
  cursor: pointer; background: var(--wtp-soft); opacity: .6;
}
.wtp-seg.on { opacity: 1; color: var(--wtp-primary); }
.wtp-colon { font-size: 30px; font-weight: 600; }
.wtp-dial { display: block; margin: 0 auto; touch-action: none; cursor: pointer; }
.wtp-dial text { font-size: 13px; fill: var(--wtp-fg); pointer-events: none; }
.wtp-dial text.in { font-size: 11px; opacity: .75; }
.wtp-dial text.sel { fill: #fff; opacity: 1; font-weight: 600; }
.wtp-foot { display: flex; gap: 6px; margin-top: 12px; }
.wtp-btn {
  flex: 1; height: 34px; border-radius: 8px; border: 1px solid var(--wtp-line);
  background: transparent; color: var(--wtp-fg); font: inherit; font-size: 13px; cursor: pointer;
}
.wtp-btn:hover { border-color: var(--wtp-primary); color: var(--wtp-primary); }
.wtp-btn.ok { background: var(--wtp-primary); border-color: var(--wtp-primary); color: #fff; }
.wtp-hint { font-size: 11px; opacity: .6; text-align: center; margin-top: 8px; }
"""

_JS = r"""
const SVG = "http://www.w3.org/2000/svg";
const pad = (n) => String(n).padStart(2, "0");
const fmt = (v) => (v ? `${pad(v.h)}:${pad(v.m)}` : "");

function parse(s) {
  s = (s || "").replace(/\s/g, "").replace("：", ":");
  if (!s) return null;
  let h, m;
  let mt = s.match(/^(\d{1,2}):(\d{1,2})$/);
  if (mt) { h = +mt[1]; m = +mt[2]; }
  else if (/^\d{3,4}$/.test(s)) { h = +s.slice(0, -2); m = +s.slice(-2); }
  else if (/^\d{1,2}$/.test(s)) { h = +s; m = 0; }
  else return undefined;
  return h < 24 && m < 60 ? { h, m } : undefined;
}

function build(host) {
  const S = { value: null, draft: null, mode: "h", open: false, sent: null, lastData: null, send: null };

  const root = document.createElement("div");
  root.className = "wtp-root";
  root.innerHTML = `
    <div class="wtp-label"></div>
    <div class="wtp-box">
      <input class="wtp-inp" type="text" inputmode="numeric" autocomplete="off" maxlength="5" placeholder="HH:MM">
      <button class="wtp-clk" type="button" title="시계에서 선택" tabindex="-1">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
      </button>
    </div>`;
  host.appendChild(root);
  const label = root.querySelector(".wtp-label");
  const box = root.querySelector(".wtp-box");
  const inp = root.querySelector(".wtp-inp");
  const clk = root.querySelector(".wtp-clk");

  // 팝업은 스크롤 영역에 잘리지 않도록 body에 붙인다
  const pop = document.createElement("div");
  pop.className = "wtp-pop";
  pop.hidden = true;
  pop.innerHTML = `
    <div class="wtp-head"><span class="wtp-seg" data-m="h">--</span><span class="wtp-colon">:</span>
      <span class="wtp-seg" data-m="m">--</span></div>
    <svg class="wtp-dial" width="232" height="232" viewBox="0 0 232 232"></svg>
    <div class="wtp-foot">
      <button class="wtp-btn" data-a="now" type="button">지금</button>
      <button class="wtp-btn" data-a="clear" type="button">지우기</button>
      <button class="wtp-btn ok" data-a="ok" type="button">확인</button>
    </div>
    <div class="wtp-hint">시 → 분 순서로 선택 · 직접 입력도 가능</div>`;
  document.body.appendChild(pop);
  const dial = pop.querySelector(".wtp-dial");
  const segH = pop.querySelector('[data-m="h"]');
  const segM = pop.querySelector('[data-m="m"]');

  const C = 116, R_OUT = 92, R_IN = 60;
  const pos = (deg, r) => [C + r * Math.sin(deg * Math.PI / 180), C - r * Math.cos(deg * Math.PI / 180)];

  function el(tag, attrs, text) {
    const e = document.createElementNS(SVG, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }

  function render() {
    const d = S.draft;
    segH.textContent = d ? pad(d.h) : "--";
    segM.textContent = d ? pad(d.m) : "--";
    segH.classList.toggle("on", S.mode === "h");
    segM.classList.toggle("on", S.mode === "m");

    dial.replaceChildren(el("circle", { cx: C, cy: C, r: 112, fill: "var(--wtp-soft)" }));
    let selDeg = null, selR = R_OUT, selTxt = null;
    if (d) {
      if (S.mode === "h") {
        selDeg = (d.h % 12) * 30;
        selR = d.h === 0 || d.h > 12 ? R_IN : R_OUT;
        selTxt = d.h;
      } else {
        selDeg = d.m * 6;
        selTxt = d.m % 5 === 0 ? d.m : null;
      }
      const [x, y] = pos(selDeg, selR);
      dial.appendChild(el("line", { x1: C, y1: C, x2: x, y2: y, stroke: "var(--wtp-primary)", "stroke-width": 2 }));
      dial.appendChild(el("circle", { cx: C, cy: C, r: 3, fill: "var(--wtp-primary)" }));
      dial.appendChild(el("circle", { cx: x, cy: y, r: 16, fill: "var(--wtp-primary)" }));
    }
    const label = (val, deg, r, cls) => {
      const [x, y] = pos(deg, r);
      const c = [cls, val === selTxt && r === selR ? "sel" : ""].join(" ").trim();
      dial.appendChild(el("text", { x, y: y + 4.5, "text-anchor": "middle", class: c }, pad(val)));
    };
    for (let i = 0; i < 12; i++) {
      if (S.mode === "h") {
        label(i === 0 ? 12 : i, i * 30, R_OUT, "");
        label(i === 0 ? 0 : i + 12, i * 30, R_IN, "in");
      } else {
        label(i * 5, i * 30, R_OUT, "");
      }
    }
  }

  function pick(ev) {
    const r = dial.getBoundingClientRect();
    const dx = ev.clientX - r.left - C * r.width / 232, dy = ev.clientY - r.top - C * r.height / 232;
    const deg = (Math.atan2(dx, -dy) * 180 / Math.PI + 360) % 360;
    const dist = Math.hypot(dx, dy) * 232 / r.width;
    const d = S.draft || { h: 9, m: 0 };
    if (S.mode === "h") {
      const i = Math.round(deg / 30) % 12;
      d.h = dist < (R_IN + R_OUT) / 2 ? (i === 0 ? 0 : i + 12) : (i === 0 ? 12 : i);
    } else {
      d.m = Math.round(deg / 6) % 60;
    }
    S.draft = d;
    inp.value = fmt(d);
    box.classList.remove("wtp-bad");
    render();
  }

  let dragging = false;
  dial.addEventListener("pointerdown", (e) => { dragging = true; dial.setPointerCapture(e.pointerId); pick(e); });
  dial.addEventListener("pointermove", (e) => { if (dragging) pick(e); });
  dial.addEventListener("pointerup", () => {
    if (!dragging) return;
    dragging = false;
    if (S.mode === "h") { S.mode = "m"; render(); }
  });
  segH.onclick = () => { S.mode = "h"; render(); };
  segM.onclick = () => { S.mode = "m"; render(); };

  function place() {
    const r = box.getBoundingClientRect(), w = 264, h = pop.offsetHeight || 380;
    const left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    const below = r.bottom + 6 + h <= window.innerHeight || r.top - 6 - h < 0;
    pop.style.left = left + "px";
    pop.style.top = (below ? r.bottom + 6 : r.top - 6 - h) + "px";
  }

  function theme() {
    const cs = getComputedStyle(root);
    const v = (n, f) => cs.getPropertyValue(n).trim() || f;
    pop.style.setProperty("--wtp-primary", v("--st-primary-color", "#ff4b4b"));
    pop.style.setProperty("--wtp-bg", v("--st-background-color", "#fff"));
    pop.style.setProperty("--wtp-fg", v("--st-text-color", "#31333f"));
    pop.style.setProperty("--wtp-soft", v("--st-secondary-background-color", "#f0f2f6"));
    pop.style.setProperty("--wtp-line", v("--st-border-color", "#d6d6d9"));
    pop.style.setProperty("--wtp-font", cs.fontFamily);
  }

  function openPop() {
    if (S.open) return;
    S.open = true;
    S.draft = S.value ? { ...S.value } : null;
    S.mode = "h";
    theme();
    pop.hidden = false;
    render();
    place();
  }

  function closePop(apply) {
    if (!S.open) return;
    S.open = false;
    pop.hidden = true;
    if (apply) commit(S.draft);
    else inp.value = fmt(S.value);
  }

  function commit(v) {
    S.value = v ? { h: v.h, m: v.m } : null;
    inp.value = fmt(S.value);
    box.classList.remove("wtp-bad");
    const t = fmt(S.value);
    if (t !== S.sent) {
      S.sent = t;
      // 같은 값을 다시 보내도 Python 쪽 콜백이 확실히 불리도록 n(시각)을 함께 보낸다
      S.send && S.send("value", { t, n: Date.now() });
    }
  }

  function commitTyped() {
    const v = parse(inp.value);
    if (v === undefined) {        // 잘못된 형식 → 이전 값으로 되돌림
      box.classList.add("wtp-bad");
      setTimeout(() => { inp.value = fmt(S.value); box.classList.remove("wtp-bad"); }, 900);
      return false;
    }
    commit(v);
    return true;
  }

  inp.addEventListener("click", openPop);
  clk.addEventListener("click", () => (S.open ? closePop(true) : (inp.focus(), openPop())));
  inp.addEventListener("input", () => {
    const v = parse(inp.value);
    box.classList.toggle("wtp-bad", v === undefined && inp.value.length >= 4);
    if (v && S.open) { S.draft = v; render(); }
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (commitTyped()) closePop(false); inp.blur(); }
    else if (e.key === "Escape") { closePop(false); inp.blur(); }
    else if (e.key === "ArrowDown" && !S.open) openPop();
  });
  inp.addEventListener("blur", () => { if (!S.open) commitTyped(); });

  pop.addEventListener("pointerdown", (e) => { if (e.target !== inp) e.preventDefault(); });
  pop.addEventListener("click", (e) => {
    const a = e.target.closest("[data-a]")?.dataset.a;
    if (a === "now") {
      const d = new Date(); S.draft = { h: d.getHours(), m: d.getMinutes() }; closePop(true);
    } else if (a === "clear") {
      S.draft = null; closePop(true);
    } else if (a === "ok") {
      const typed = parse(inp.value);
      if (typed) S.draft = typed;
      closePop(true);
    }
  });

  const onDocDown = (e) => {
    if (!S.open) return;
    const path = e.composedPath();
    if (!path.includes(pop) && !path.includes(root)) {
      const typed = parse(inp.value);
      if (typed) S.draft = typed;
      closePop(true);
    }
  };
  const onScroll = () => { if (S.open) place(); };
  document.addEventListener("pointerdown", onDocDown, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", onScroll);

  S.setValue = (t) => {
    const v = parse(t);
    S.value = v || null;
    S.sent = fmt(S.value);
    if (document.activeElement !== inp || !S.open) inp.value = fmt(S.value);
    if (S.open) { S.draft = S.value ? { ...S.value } : null; render(); }
  };
  S.label = label;
  S.destroy = () => {
    document.removeEventListener("pointerdown", onDocDown, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", onScroll);
    pop.remove();
    root.remove();
  };
  return S;
}

export default function (component) {
  const { data, parentElement, setStateValue } = component;
  let S = parentElement.__wtp;
  if (!S) {
    S = build(parentElement);
    parentElement.__wtp = S;
  }
  S.send = setStateValue;
  S.label.textContent = data.label || "";
  S.label.hidden = !data.label;
  if (data.value !== S.lastData) {
    S.lastData = data.value;
    S.setValue(data.value);
  }
  return () => {
    S.destroy();
    parentElement.__wtp = null;
  };
}
"""

_component = st.components.v2.component("worklog_time_picker", css=_CSS, js=_JS, isolate_styles=False)


def time_picker(label: str, key: str) -> dtime | None:
    """시계 다이얼 + 직접 입력 시간 위젯. 값은 st.session_state[key] (time | None)."""
    comp_key, seen_key = f"{key}_picker", f"{key}_picker_seen"
    ss = st.session_state

    # 브라우저에서 새로 보낸 값(n이 바뀐 경우)만 반영. '현재 시간' 버튼 등 Python 쪽 변경은 그대로 둔다.
    sent = (ss.get(comp_key) or {}).get("value") or {}
    if sent.get("n") and sent["n"] != ss.get(seen_key):
        ss[seen_key] = sent["n"]
        t = str(sent.get("t") or "").strip()
        ss[key] = datetime.strptime(t, "%H:%M").time() if t else None

    cur = ss.get(key)
    _component(
        key=comp_key,
        data={"label": label, "value": cur.strftime("%H:%M") if cur else ""},
        default={"value": None},
        on_value_change=lambda: None,
    )
    return st.session_state.get(key)
