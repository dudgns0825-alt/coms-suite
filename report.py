# -*- coding: utf-8 -*-
"""
비교 리포트(HTML) 생성
================================================

metrics.build_series() 결과를 받아 단일 HTML 파일을 만든다.
차트는 template.html 안의 자바스크립트가 SVG로 직접 그리며,
외부 CDN·라이브러리를 전혀 쓰지 않는다(사내망·오프라인에서도 열린다).

[통화가 섞일 때]
  DART(원화)와 EDGAR(달러) 회사를 한 화면에 놓으려면 금액을 한 통화로 맞춰야 한다.
  기준통화는 대상회사의 통화로 잡고, 나머지 회사 금액에 환산율을 곱한다.
  비율(%)과 배수(倍)는 분자·분모가 같은 통화라 환산 없이 그대로 비교된다.
"""

import os
import json
import html
import datetime


TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")

# 계열색 슬롯은 최대 8개. 그 이상은 색으로 구분되지 않으므로 받지 않는다.
MAX_COMPANIES = 8

EOK = 1e8   # 1억

# 통화별 표시 단위 — (나눌 값, 이름, 소수자리)
CURRENCY_UNITS = {
    "KRW": {"table": (1e8, "억원", 0), "small": (1e8, "억원", 0), "large": (1e12, "조원", 1)},
    "USD": {"table": (1e6, "백만달러", 0), "small": (1e6, "백만달러", 0),
            "large": (1e9, "십억달러", 1)},
}
DEFAULT_UNITS = {"table": (1.0, "", 0), "small": (1.0, "", 0), "large": (1.0, "", 0)}

# 라인 차트로 그릴 지표
LINE_CHARTS = [
    {"key": "revenue", "label": "매출액", "kind": "amount"},
    {"key": "operating_income", "label": "영업이익", "kind": "amount"},
    {"key": "ebitda", "label": "EBITDA", "kind": "amount"},
    {"key": "net_income", "label": "당기순이익", "kind": "amount"},
    {"key": "opm", "label": "영업이익률", "kind": "ratio"},
    {"key": "ebitda_margin", "label": "EBITDA마진", "kind": "ratio"},
    {"key": "roe", "label": "ROE (자기자본이익률)", "kind": "ratio"},
    {"key": "debt_ratio", "label": "부채비율", "kind": "ratio"},
    {"key": "ev_ebitda", "label": "EV/EBITDA", "kind": "multiple"},
    {"key": "ev_ebit", "label": "EV/EBIT", "kind": "multiple"},
]

# 최근 사업연도 순위(가로 막대)
BAR_CHARTS = [
    {"key": "revenue", "label": "매출액 순위", "kind": "amount"},
    {"key": "opm", "label": "영업이익률 순위", "kind": "ratio"},
    {"key": "ev_ebitda", "label": "EV/EBITDA 순위", "kind": "multiple"},
    {"key": "per", "label": "PER 순위", "kind": "multiple"},
]

# 표에 넣을 행 (키, 표시명, 형식)
TABLE_ROWS = [
    ("revenue", "매출액", "amount"),
    ("operating_income", "영업이익", "amount"),
    ("dep_amort", "감가상각비·무형자산상각비", "amount"),
    ("ebitda", "EBITDA", "amount"),
    ("net_income", "당기순이익", "amount"),
    ("assets", "자산총계", "amount"),
    ("liabilities", "부채총계", "amount"),
    ("equity", "자본총계", "amount"),
    ("total_debt", "총차입금(리스부채 포함)", "amount"),
    ("cash", "현금및현금성자산", "amount"),
    ("net_debt", "순차입금", "amount"),
    ("opm", "영업이익률", "ratio"),
    ("npm", "순이익률", "ratio"),
    ("ebitda_margin", "EBITDA마진", "ratio"),
    ("roe", "ROE", "ratio"),
    ("roa", "ROA", "ratio"),
    ("debt_ratio", "부채비율", "ratio"),
    ("current_ratio", "유동비율", "ratio"),
    ("revenue_growth", "매출액증가율", "ratio"),
    ("market_cap", "시가총액", "amount"),
    ("ev", "EV(기업가치)", "amount"),
    ("ev_ebitda", "EV/EBITDA", "multiple"),
    ("ev_ebit", "EV/EBIT", "multiple"),
    ("per", "PER", "multiple"),
    ("pbr", "PBR", "multiple"),
]

RATIO_KEYS = {"opm", "npm", "ebitda_margin", "roe", "roa", "debt_ratio",
              "equity_ratio", "current_ratio", "revenue_growth"}

# 시장자료가 있어야 나오는 항목 — items 가 아니라 multiples 에 들어있다
MARKET_KEYS = {"market_cap", "ev", "ev_ebitda", "ev_ebit", "per", "pbr"}

# 사용자가 직접 넣을 수 있는 항목 (리포트에서 ★로 표시한다)
MANUAL_KEYS = {"dep_amort", "total_debt"}


def _units(currency):
    return CURRENCY_UNITS.get((currency or "").upper(), DEFAULT_UNITS)


def _value(row, key, factor=1.0):
    """
    연도 한 칸에서 지표 값을 꺼낸다.
      비율 → ratios / 시가총액·배수 → multiples / 나머지 금액 → items

    factor 는 통화 환산율이다. 금액에만 곱하고 비율·배수에는 곱하지 않는다.
    """
    if row is None:
        return None

    if key in RATIO_KEYS:
        value = row["ratios"].get(key)
        scale = 1.0
    elif key in MARKET_KEYS:
        value = (row.get("multiples") or {}).get(key)
        scale = 1.0 if key in ("ev_ebitda", "ev_ebit", "per", "pbr") else factor
    else:
        value = row["items"].get(key)
        scale = factor

    return None if value is None else float(value) * scale


def _origin(row, key):
    """
    그 값이 어디서 왔는지.
      None   재무제표 본문
      note   사업보고서 주석의 XBRL 태그에서 자동으로 읽음
      manual 사용자가 직접 넣음
    """
    if row is None or key not in MANUAL_KEYS:
        return None
    if key in (row["items"].get("_manual") or []):
        return "manual"
    if key in (row["items"].get("_from_note") or []):
        return "note"
    return None


MARKS = {
    "manual": '<span class="manual" title="직접 입력한 값">★</span>',
    "note": '<span class="manual" title="사업보고서 주석에서 읽은 값">˚</span>',
}


def _fmt_cell(value, kind, unit_div, origin=None):
    if value is None:
        return '<td class="na">–</td>'
    if kind in ("ratio", "multiple"):
        text = f"{value:,.1f}"
    else:
        text = f"{value / unit_div:,.0f}"
    cls = ' class="neg"' if value < 0 else ""
    return f"<td{cls}>{text}{MARKS.get(origin, '')}</td>"


def _delta_html(now, before, kind):
    """전년 대비 증감. 비율 지표는 %p, 금액·배수는 %."""
    if now is None or before is None:
        return '<div class="k-delta">전년 대비 –</div>'

    if kind == "ratio":
        diff = now - before
        text = f"{diff:+,.1f}%p"
    else:
        if before == 0:
            return '<div class="k-delta">전년 대비 –</div>'
        diff = (now - before) / abs(before) * 100
        text = f"{diff:+,.1f}%"

    cls = "up" if diff > 0 else ("down" if diff < 0 else "")
    return f'<div class="k-delta">전년 대비 <span class="{cls}">{text}</span></div>'


def _kpi_html(target, years, currency, factor):
    """대상회사의 최근 사업연도 헤드라인 숫자."""
    valid = [y for y in years if y in target["rows"]]
    if not valid:
        return ""
    latest = valid[-1]
    prev = valid[-2] if len(valid) >= 2 else None

    units = _units(currency)
    tiles = [
        ("매출액", "revenue", "amount"),
        ("영업이익", "operating_income", "amount"),
        ("EBITDA", "ebitda", "amount"),
        ("영업이익률", "opm", "ratio"),
        ("EV/EBITDA", "ev_ebitda", "multiple"),
    ]

    parts = []
    for label, key, kind in tiles:
        now = _value(target["rows"].get(latest), key, factor)
        before = _value(target["rows"].get(prev), key, factor) if prev else None

        if now is None:
            value_html = '<div class="k-value">–</div>'
        elif kind == "ratio":
            value_html = f'<div class="k-value">{now:,.1f}<span class="k-unit">%</span></div>'
        elif kind == "multiple":
            value_html = f'<div class="k-value">{now:,.1f}<span class="k-unit">배</span></div>'
        else:
            div, name, dp = units["large"] if abs(now) >= units["large"][0] else units["small"]
            value_html = (f'<div class="k-value">{now / div:,.{dp}f}'
                          f'<span class="k-unit">{name}</span></div>')

        parts.append(
            f'<div class="kpi"><div class="k-label">{html.escape(label)} '
            f'· {latest}년</div>{value_html}{_delta_html(now, before, kind)}</div>'
        )

    return "".join(parts)


def _table_html(series_list, years, currency):
    """회사별 블록으로 이어붙인 수치표. 감사조서처럼 연도가 열이다."""
    unit_div = _units(currency)["table"][0]
    head = "".join(f"<th>{y}</th>" for y in years)
    out = [f"<table><thead><tr><th>구분</th>{head}</tr></thead><tbody>"]

    for s in series_list:
        factor = s.get("fx_factor", 1.0)
        tag = f" ({s['stock_code']})" if s["stock_code"] else ""
        origin = "EDGAR" if s.get("source") == "edgar" else "DART"
        out.append(
            f'<tr class="co-head"><td colspan="{len(years) + 1}">'
            f'<span class="cdot" style="background:var(--series-{s["slot"]})"></span>'
            f'{html.escape(s["name"])}{tag}'
            f'<span class="origin">{origin} · {s.get("currency", "KRW")}</span></td></tr>'
        )

        # 연결/별도 표시 — 회사마다, 연도마다 다를 수 있다
        divs = []
        for y in years:
            row = s["rows"].get(y)
            divs.append("<td>–</td>" if row is None
                        else f'<td>{"연결" if row["fs_div"] == "CFS" else "별도"}</td>')
        out.append(f'<tr><td>재무제표 구분</td>{"".join(divs)}</tr>')

        for key, label, kind in TABLE_ROWS:
            cells = "".join(
                _fmt_cell(_value(s["rows"].get(y), key, factor), kind, unit_div,
                          _origin(s["rows"].get(y), key))
                for y in years)
            out.append(f"<tr><td>{html.escape(label)}</td>{cells}</tr>")

    out.append("</tbody></table>")
    return "".join(out)


def _notes_html(series_list, years, generated, fs_pref, currency, market_note):
    """출처·작성기준 각주. 수치를 조서에 옮길 때 근거가 되는 부분이다."""
    has_edgar = any(s.get("source") == "edgar" for s in series_list)
    has_dart = any(s.get("source") != "edgar" for s in series_list)

    notes = []
    if has_dart:
        notes.append(
            "출처(한국): 금융감독원 전자공시시스템(DART) OpenAPI "
            "<span class='src'>단일회사 전체 재무제표(fnlttSinglAcntAll)·주식의 총수 현황"
            "(stockTotqySttus), 사업보고서(11011)</span>")
    if has_edgar:
        notes.append(
            "출처(미국): 미국 증권거래위원회(SEC) EDGAR XBRL "
            "<span class='src'>companyfacts API, Form 10-K 연간 보고치</span>")

    notes.append(f"조회 기준일: {generated}")
    notes.append(
        f"대상 사업연도: {years[0]}~{years[-1]} "
        "<span class='src'>(공시연도가 아니라 결산 사업연도 기준. "
        "미국은 12월 결산이 아닌 회사가 있어 결산일이 회사마다 다를 수 있습니다)</span>")

    pref_label = "연결재무제표 우선" if fs_pref == "CFS" else "별도재무제표"
    notes.append(
        f"재무제표 구분: {pref_label}. "
        "연결재무제표가 없는 회사·연도는 별도재무제표로 대체했으며, "
        "표의 '재무제표 구분' 행에 연도별로 표시했습니다. "
        "<span class='src'>EDGAR 자료는 연결 기준입니다.</span>")

    if market_note:
        notes.append(market_note)

    notes.append(
        "EBITDA = 영업이익 + 감가상각비·무형자산상각비. "
        "EV = 시가총액 + 순차입금이며, 순차입금 = 총차입금(리스부채 포함) − 현금및현금성자산입니다. "
        "<span class='src'>순차입금에서 단기금융상품은 차감하지 않았습니다 — "
        "회사마다 유동성 분류가 달라 한 가지 기준(현금및현금성자산)으로 통일했습니다.</span>")

    notes.append(
        "EBITDA·EBIT·당기순이익·자본총계가 0 이하인 회사·연도의 배수는 "
        "숫자로는 계산되지만 비교에 쓸 수 없어 표시하지 않았습니다(–).")

    # 값의 출처가 본문이 아닌 경우 어떤 회사·연도인지 밝힌다 (조서 추적성)
    def gather(flag):
        out = []
        for s in series_list:
            years_hit = [str(y) for y in sorted(s["rows"])
                         if s["rows"][y]["items"].get(flag)]
            if years_hit:
                out.append(f"{s['name']}({', '.join(years_hit)})")
        return out

    from_note = gather("_from_note")
    if from_note:
        notes.append(
            "<b>˚ 표시는 사업보고서 원문의 주석에서 읽은 값입니다</b> — 감가상각비를 "
            "재무제표 본문에 표시하지 않는 회사가 있어, 공시원문에 붙은 XBRL 태그"
            "(<span class='src'>ifrs-full_AdjustmentsForDepreciationExpense 등</span>)에서 "
            "가져왔습니다. 표의 단위 표기를 읽어 환산했고, 같은 문서의 매출액이 "
            "재무제표 API 값과 일치하는지 대조해 확인했습니다: " + " / ".join(from_note))

    manual_rows = gather("_manual")
    if manual_rows:
        notes.append(
            "<b>★ 표시는 직접 입력한 값입니다</b> — 본문에도 주석 태그에도 없어 "
            "사업보고서를 보고 옮겨 적은 것입니다: " + " / ".join(manual_rows))

    # 자동으로 못 잡은 항목 안내
    missing = []
    for s in series_list:
        gaps = []
        for y in sorted(s["rows"]):
            items = s["rows"][y]["items"]
            if items.get("dep_amort") is None:
                gaps.append(f"{y} 감가상각비")
            if items.get("total_debt") is None:
                gaps.append(f"{y} 차입금")
        if gaps:
            missing.append(f"{s['name']}({', '.join(gaps)})")
    if missing:
        notes.append(
            "다음 항목은 회사가 재무제표 본문에 따로 표시하지 않아 조회되지 않았고, "
            "그 결과 EBITDA·EV 관련 지표가 비어 있습니다: " + " / ".join(missing))

    notes.append(
        "ROE·ROA는 직전 사업연도 잔액을 조회한 연도는 <b>평잔(기초+기말)/2</b>, "
        "직전 연도 자료가 없는 연도는 <b>기말잔액</b> 기준입니다.")
    notes.append(
        "자본총계가 0 이하인 회사·연도의 부채비율·ROE는 수치의 의미가 없어 표시하지 않았습니다(–).")

    unit_name = _units(currency)["table"][1]
    notes.append(
        f"금액 단위: 표는 {unit_name}, 차트는 각 차트 제목 아래에 표시한 단위를 따릅니다. "
        "차트의 '지수' 보기는 회사별 최초 공시연도를 100으로 환산한 값입니다.")

    gaps = []
    for s in series_list:
        absent = [str(y) for y in years if y not in s["rows"]]
        if absent:
            gaps.append(f"{s['name']}({', '.join(absent)})")
    if gaps:
        notes.append("해당 사업연도 재무제표가 조회되지 않은 회사: " + " / ".join(gaps))

    notes.append(
        "<span class='src'>본 자료는 공시 원문을 기계적으로 집계한 것으로, "
        "회계정책·표시방법 차이로 회사 간 계정 범위가 다를 수 있습니다. "
        "인용 전 원문 확인이 필요합니다.</span>")

    return "".join(f"<li>{n}</li>" for n in notes)


def _market_note(series_list, currency):
    """시가총액을 어떻게 잡았는지 — 기준일·평균기간·환율까지 밝힌다."""
    lines = []
    sample = next((s["market"] for s in series_list if s.get("market")), None)
    if not sample:
        return ""

    avg = sample.get("avg_days", 1)
    basis = "기준일 종가" if avg <= 1 else f"기준일까지 {avg}일 단순평균"
    lines.append(
        f"시가총액 = 주가 × 상장주식수. 주가는 야후 파이낸스 {basis}"
        f"(기준일 {sample.get('as_of')}), 상장주식수는 기준일에 가장 가까운 사업연도의 "
        "보통주 발행총수(자기주식 포함)입니다.")

    detail = []
    for s in series_list:
        m = s.get("market") or {}
        if m.get("price") is None:
            continue
        when = m.get("price_to") or ""
        if avg > 1:
            when = f"{m.get('price_from', '')}~{when}, {m.get('price_days', 0)}거래일 평균"
        detail.append(
            f"{s['name']} {m['ticker']} {m['price']:,.0f}{m.get('currency', '')} ({when})")
    if detail:
        lines.append("<span class='src'>적용 주가: " + " / ".join(detail) + "</span>")

    # 환산이 실제로 일어난 회사만 표기
    converted = [s for s in series_list
                 if (s.get("currency") or "KRW") != currency and s.get("fx_factor")]
    if converted:
        rates = " / ".join(
            f"{s.get('currency')} 1 = {s['fx_factor']:,.2f}{currency}" for s in converted[:1])
        lines.append(
            f"통화가 다른 회사는 금액을 {currency} 기준으로 단순환산했습니다({rates}, "
            "시가총액 기준일 환율). <span class='src'>손익 항목도 기간평균환율이 아니라 "
            "기준일 환율로 환산했으므로, 환산 금액은 참고치로만 보시기 바랍니다. "
            "비율과 배수는 환산 없이 각 회사 통화 기준으로 계산되어 영향이 없습니다.</span>")

    return "<br>".join(lines)


def _preferred_note(series_list):
    """우선주가 따로 상장된 회사가 있으면 시가총액이 과소평가된다는 점을 알린다."""
    names = [s["name"] for s in series_list if s.get("has_preferred")]
    if not names:
        return ""
    return ("우선주가 별도로 발행된 회사(" + ", ".join(names) + ")의 시가총액은 "
            "<b>보통주만</b> 반영한 값입니다. 우선주 시가총액이 빠져 EV가 과소계상되므로 "
            "해당 회사의 배수는 낮게 나옵니다.")


def build_report(series_list, years, target_name, fs_pref="CFS", out_path=None):
    """
    series_list : metrics.build_series() 결과 리스트. 첫 번째가 대상회사.
    years       : 오름차순 사업연도 리스트
    반환        : 저장한 HTML 경로
    """
    if not series_list:
        raise ValueError("표시할 회사가 없습니다.")
    if len(series_list) > MAX_COMPANIES:
        raise ValueError(
            f"회사가 {len(series_list)}곳입니다. 색으로 구분 가능한 상한은 "
            f"{MAX_COMPANIES}곳이므로 비교기업을 줄여 주세요."
        )

    years = sorted(years)
    generated = datetime.date.today().isoformat()

    # 계열색 슬롯 배정 — 대상회사가 1번. 한 번 정해지면 범례를 꺼도 바뀌지 않는다.
    for i, s in enumerate(series_list):
        s["slot"] = i + 1
        s["is_target"] = (i == 0)

    # 기준통화는 대상회사 기준. 나머지 회사 금액은 여기에 맞춰 환산한다.
    base_currency = (series_list[0].get("currency") or "KRW").upper()
    base_rate = ((series_list[0].get("market") or {}).get("fx_rate")) or 1.0
    for s in series_list:
        rate = ((s.get("market") or {}).get("fx_rate")) or 1.0
        s["fx_factor"] = rate / base_rate if base_rate else 1.0

    companies = []
    for s in series_list:
        factor = s["fx_factor"]
        series = {}
        for spec in LINE_CHARTS + BAR_CHARTS:
            series[spec["key"]] = [_value(s["rows"].get(y), spec["key"], factor) for y in years]
        companies.append({
            "name": s["name"],
            "slot": s["slot"],
            "is_target": s["is_target"],
            "stock_code": s["stock_code"],
            "series": series,
        })

    # 실제로 자료가 있는 마지막 연도 (막대차트 기준연도)
    filled = [y for y in years if any(y in s["rows"] for s in series_list)]
    latest_year = filled[-1] if filled else years[-1]

    target = series_list[0]
    peers = ", ".join(html.escape(s["name"]) for s in series_list[1:]) or "없음"
    subtitle = (
        f"대상회사 <b>{html.escape(target['name'])}</b> · "
        f"비교기업 {len(series_list) - 1}곳 ({peers})<br>"
        f"{years[0]}~{years[-1]} 사업연도 · "
        f"{'연결재무제표 우선' if fs_pref == 'CFS' else '별도재무제표'} · "
        f"금액 {base_currency} 기준 · 조회일 {generated}"
    )

    market_note = _market_note(series_list, base_currency)
    preferred = _preferred_note(series_list)
    if preferred:
        market_note = (market_note + "<br>" + preferred) if market_note else preferred

    units = _units(base_currency)
    payload = {
        "title": f"{target['name']} 비교기업 재무분석",
        "subtitle": subtitle,
        "years": years,
        "latest_year": latest_year,
        "companies": companies,
        "line_charts": LINE_CHARTS,
        "bar_charts": BAR_CHARTS,
        "units": {
            "small": {"div": units["small"][0], "name": units["small"][1],
                      "dp": units["small"][2]},
            "large": {"div": units["large"][0], "name": units["large"][1],
                      "dp": units["large"][2]},
        },
        "kpi_html": _kpi_html(target, years, base_currency, target["fx_factor"]),
        "table_html": _table_html(series_list, years, base_currency),
        "notes_html": _notes_html(series_list, years, generated, fs_pref,
                                  base_currency, market_note),
    }

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()

    # </script> 가 JSON 문자열 안에 들어가면 스크립트 블록이 거기서 끊긴다.
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    doc = (template
           .replace("__TITLE__", html.escape(payload["title"]))
           .replace("__DATA__", data_json))

    if out_path is None:
        safe = "".join(ch for ch in target["name"] if ch not in r'\/:*?"<>|').strip()
        out_path = f"{safe}_비교분석_{generated}.html"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)

    return os.path.abspath(out_path)
