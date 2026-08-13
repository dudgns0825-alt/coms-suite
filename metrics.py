# -*- coding: utf-8 -*-
"""
재무지표 추출·계산
================================================

fnlttSinglAcntAll 이 돌려주는 계정 행에서 비교에 쓸 항목을 뽑아
연도별 지표를 만든다.

[왜 account_id 로 찾는가]
  account_nm(계정명)은 같은 회사도 연도마다 조금씩 달라진다.
    영업이익 ↔ 영업이익(손실),  매출액 ↔ 수익(매출액) ↔ 영업수익
  반면 account_id(IFRS 표준계정코드)는 안정적이다.
  그래서 표준계정은 account_id 로 찾고, 표준계정코드를 쓰지 않는
  기업고유계정(-표준계정코드 미사용-)만 계정명 정규식으로 보완한다.
"""

import re


# ─────────────────────────────────────────────────────────────
# 계정 매핑
# ─────────────────────────────────────────────────────────────
# sj  : 찾을 재무제표 구분. 앞에 있는 것을 먼저 본다.
#         BS=재무상태표  IS=손익계산서  CIS=포괄손익계산서  CF=현금흐름표
#       (단일 포괄손익계산서만 작성하는 회사는 IS 가 없고 CIS 에 손익이 들어있다)
# ids : account_id 후보. 앞에 있는 것이 우선.
# nm  : ids 로 못 찾았을 때만 쓰는 계정명 정규식(기업고유계정 대응).

ITEM_SPECS = {
    "revenue": {
        "label": "매출액",
        "sj": ["IS", "CIS"],
        "ids": [
            "ifrs-full_Revenue",
            "ifrs-full_RevenueFromContractsWithCustomers",
            "dart_OperatingRevenue",           # 금융·서비스업의 '영업수익'
            "ifrs-full_RevenueFromRenderingOfServices",
        ],
        "nm": r"^(매출액|영업수익|수익\(매출액\)|매출및지분법손익|영업총수익)$",
    },
    "operating_income": {
        "label": "영업이익",
        "sj": ["IS", "CIS"],
        "ids": [
            "dart_OperatingIncomeLoss",
            "ifrs-full_ProfitLossFromOperatingActivities",
        ],
        "nm": r"^영업이익(\(손실\))?$",
    },
    "net_income": {
        "label": "당기순이익",
        "sj": ["IS", "CIS"],
        "ids": ["ifrs-full_ProfitLoss"],
        # '당기연결순이익'으로 적는 회사가 있다(우아한형제들 감사보고서)
        "nm": r"^(당기|연결|당기연결)?(연결)?순이익(\(손실\))?$|^당기순손실$",
    },
    "net_income_owners": {
        "label": "지배주주순이익",
        "sj": ["IS", "CIS"],
        "ids": ["ifrs-full_ProfitLossAttributableToOwnersOfParent"],
        "nm": r"지배기업.*소유주",
    },
    "gross_profit": {
        "label": "매출총이익",
        "sj": ["IS", "CIS"],
        "ids": ["ifrs-full_GrossProfit"],
        "nm": r"^매출총이익(\(손실\))?$",
    },
    "assets": {
        "label": "자산총계",
        "sj": ["BS"],
        "ids": ["ifrs-full_Assets"],
        "nm": r"^자산총계$",
    },
    "liabilities": {
        "label": "부채총계",
        "sj": ["BS"],
        "ids": ["ifrs-full_Liabilities"],
        "nm": r"^부채총계$",
    },
    "equity": {
        "label": "자본총계",
        "sj": ["BS"],
        "ids": ["ifrs-full_Equity"],
        "nm": r"^자본총계$",
    },
    "current_assets": {
        "label": "유동자산",
        "sj": ["BS"],
        "ids": ["ifrs-full_CurrentAssets"],
        "nm": r"^유동자산$",
    },
    "current_liabilities": {
        "label": "유동부채",
        "sj": ["BS"],
        "ids": ["ifrs-full_CurrentLiabilities"],
        "nm": r"^유동부채$",
    },
    "cfo": {
        "label": "영업활동현금흐름",
        "sj": ["CF"],
        "ids": ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
        "nm": r"영업활동.*현금흐름",
    },
    "cash": {
        "label": "현금및현금성자산",
        "sj": ["BS"],
        "ids": ["ifrs-full_CashAndCashEquivalents"],
        "nm": r"^현금및현금성자산$",
    },
}


# ─────────────────────────────────────────────────────────────
# 합산 항목 — 해당하는 계정을 모두 더해야 하는 것들
# ─────────────────────────────────────────────────────────────
# 위의 ITEM_SPECS는 '한 계정을 찾아 그 값을 쓰는' 항목이지만,
# 감가상각비와 차입금은 여러 계정에 흩어져 있어 더해야 한다.
#   감가상각비 = 감가상각비 + 무형자산상각비 (+ 사용권자산상각비)
#   총차입금   = 단기차입금 + 유동성장기부채 + 사채 + 장기차입금 + 리스부채
#
# ★ 회사마다 표시가 제각각이라 자동으로 못 잡는 경우가 있다.
#   - 삼성전자·SK하이닉스는 현금흐름표의 조정 항목을 '조정' 한 줄로 묶어 제출해
#     감가상각비가 아예 없다(주석에만 있다).
#   - LG디스플레이는 재무상태표에 차입금을 '금융부채'로 묶어 표시해 식별되지 않는다.
#   본문에 없으면 사업보고서 원문의 XBRL 태그에서 찾아 보고(note_reader),
#   거기서도 못 구하면 사용자가 직접 넣을 수 있게 build_series(overrides=...) 로
#   값을 덮어쓸 수 있게 했다.

SUM_SPECS = {
    "dep_amort": {
        "label": "감가상각비·무형자산상각비",
        "sj": ["CF"],
        "ids": [
            "ifrs-full_AdjustmentsForDepreciationExpense",
            "ifrs-full_AdjustmentsForAmortisationExpense",
            "ifrs-full_AdjustmentsForDepreciationAndAmortisationExpense",
            # dart_ 접두어를 쓰는 회사가 있다 — LG디스플레이 2021·2022년
            # 현금흐름표의 '감가상각 및 무형자산상각'이 이 코드다
            "dart_AdjustmentsForDepreciationExpense",
            "dart_AdjustmentsForAmortisationExpense",
            "dart_AdjustmentsForDepreciationAndAmortisationExpense",
            "ifrs-full_DepreciationAndAmortisationExpense",
            "ifrs-full_DepreciationExpense",
            "ifrs-full_AmortisationExpense",
            "dart_DepreciationExpense",
            "dart_AmortisationExpense",
        ],
        "nm": r"^(감가상각비|무형자산상각비|감가상각비및무형자산상각비|"
              r"사용권자산상각비|감가상각비와무형자산상각비)(에대한조정)?$",
    },
    "total_debt": {
        "label": "총차입금",
        "sj": ["BS"],
        "ids": [
            "ifrs-full_ShorttermBorrowings",
            "ifrs-full_CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
            "ifrs-full_ShorttermBorrowingsAndCurrentPortionOfLongtermBorrowings",
            "ifrs-full_CurrentPortionOfLongtermBorrowings",
            "ifrs-full_LongtermBorrowings",
            "ifrs-full_NoncurrentPortionOfNoncurrentLoansReceived",
            "ifrs-full_BondsIssued",
            "ifrs-full_NoncurrentPortionOfNoncurrentBondsIssued",
            "ifrs-full_CurrentPortionOfNoncurrentBondsIssued",
            "ifrs-full_CurrentLeaseLiabilities",
            "ifrs-full_NoncurrentLeaseLiabilities",
        ],
        # 표준계정코드를 쓰지 않는 회사(삼성전자 단기차입금 등) 대비.
        # '차입금의차입' 같은 현금흐름표 항목이 섞이지 않도록 완전일치로 본다.
        "nm": r"^(단기차입금|장기차입금|차입금|유동성장기차입금|유동성장기부채|"
              r"사채|전환사채|신주인수권부사채|유동성사채|리스부채|유동리스부채|비유동리스부채)$",
    },
}

# 파생지표 — 절대금액이 아니라 비율(%)
RATIO_SPECS = {
    "opm": "영업이익률",
    "npm": "순이익률",
    "ebitda_margin": "EBITDA마진",
    "roe": "ROE",
    "roa": "ROA",
    "debt_ratio": "부채비율",
    "equity_ratio": "자기자본비율",
    "current_ratio": "유동비율",
    "revenue_growth": "매출액증가율",
}

# 배수(倍) — 비율(%)도 금액도 아니라 '몇 배'로 읽는 지표
MULTIPLE_SPECS = {
    "ev_ebitda": "EV/EBITDA",
    "ev_ebit": "EV/EBIT",
    "per": "PER",
    "pbr": "PBR",
}

# 차트에 올릴 금액 지표(단위: 원)
AMOUNT_KEYS = ["revenue", "operating_income", "net_income", "ebitda", "assets", "equity"]

# 사용자가 직접 넣을 수 있는 항목 — 회사가 재무제표 본문에 표시하지 않아
# 자동으로 못 잡는 경우가 있어 사업보고서 주석을 보고 채워 넣는다.
MANUAL_KEYS = {
    "dep_amort": "감가상각비·무형자산상각비",
    "total_debt": "총차입금(리스부채 포함)",
}

# EDGAR(SEC)는 차입금을 태그별로 따로 주므로 더해서 총차입금을 만든다.
EDGAR_DEBT_LABELS = {
    "short_debt": "단기차입금",
    "current_portion_debt": "유동성장기부채",
    "long_debt": "장기차입금",
    "lease_current": "유동리스부채",
    "lease_noncurrent": "비유동리스부채",
}
EDGAR_DEBT_KEYS = list(EDGAR_DEBT_LABELS)


def _to_number(text):
    """'333,605,938,000,000' → 333605938000000. 빈칸·'-'는 None."""
    if text is None:
        return None
    text = str(text).strip().replace(",", "").replace(" ", "")
    if text in ("", "-", "--"):
        return None
    # 회계표기 음수 (1,234) → -1234
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if neg else value


def extract_items(rows):
    """
    계정 행 리스트에서 ITEM_SPECS 항목의 당기 금액을 뽑는다.
    반환: {"revenue": 333605938000000.0, ...}  (못 찾은 항목은 None)
    """
    # sj_div 별로 account_id → 금액, account_nm → 금액 색인을 만든다.
    by_sj = {}
    for row in rows:
        sj = row.get("sj_div")
        bucket = by_sj.setdefault(sj, {"ids": {}, "nms": {}})

        amount = _to_number(row.get("thstrm_amount"))
        if amount is None:
            continue

        acc_id = (row.get("account_id") or "").strip()
        acc_nm = re.sub(r"\s+", "", row.get("account_nm") or "")

        # 같은 id가 여러 번 나오면(세부내역 등) 처음 것을 쓴다. 총계가 먼저 온다.
        if acc_id and acc_id != "-표준계정코드 미사용-":
            bucket["ids"].setdefault(acc_id, amount)
        if acc_nm:
            bucket["nms"].setdefault(acc_nm, amount)

    items = {}
    # 합산 항목 — 해당 계정을 모두 더한다. 무엇을 더했는지 내역도 남겨
    # 리포트에서 근거를 확인할 수 있게 한다(계정 표시가 회사마다 달라 검증이 필요하다).
    detail = {}
    for key, spec in SUM_SPECS.items():
        pattern = re.compile(spec["nm"])
        total, seen, lines = 0.0, set(), []

        for row in rows:
            if row.get("sj_div") not in spec["sj"]:
                continue
            amount = _to_number(row.get("thstrm_amount"))
            if amount is None:
                continue

            acc_id = (row.get("account_id") or "").strip()
            acc_nm = re.sub(r"\s+", "", row.get("account_nm") or "")
            standard = acc_id and acc_id != "-표준계정코드 미사용-"

            if standard:
                hit = acc_id in spec["ids"]
            else:
                hit = bool(pattern.match(acc_nm))
            if not hit:
                continue

            # 같은 계정이 두 번 실리는 경우(세부내역 반복)를 막는다
            mark = (acc_id, acc_nm)
            if mark in seen:
                continue
            seen.add(mark)

            total += amount
            lines.append({"name": acc_nm, "amount": amount, "id": acc_id if standard else ""})

        items[key] = total if lines else None
        detail[key] = lines

    items["_detail"] = detail

    for key, spec in ITEM_SPECS.items():
        value = None
        for sj in spec["sj"]:
            bucket = by_sj.get(sj)
            if not bucket:
                continue

            for acc_id in spec["ids"]:
                if acc_id in bucket["ids"]:
                    value = bucket["ids"][acc_id]
                    break
            if value is not None:
                break

            # 표준계정으로 못 찾으면 계정명으로 한 번 더
            pattern = re.compile(spec["nm"])
            for nm, amount in bucket["nms"].items():
                if pattern.search(nm):
                    value = amount
                    break
            if value is not None:
                break

        items[key] = value

    return items


# 현금흐름표의 '조정' 한 줄 — 주석의 조정 내역 표가 풀어 쓴 그 금액이다
ADJUSTMENTS_ID = "ifrs-full_AdjustmentsForReconcileProfitLoss"
ADJUSTMENTS_NM = re.compile(r"^조정(\(주.*\))?$")


def note_anchors(rows, items):
    """
    주석 표를 믿어도 되는지 대조하는 데 쓸 값들.

    태그가 없는 옛 보고서에서는 표를 글자로 읽어야 하는데, 문서 안에는
    연결·별도 주석이 같이 들어 있고 표마다 당기·전기 열이 있어 어느 표의
    어느 열인지 알 수 없다. 그래서 '열을 다 더한 값이 본문의 이 금액과
    맞는 열'만 골라 쓴다(note_reader.dep_amort_from_tables).

      adjustments : 현금흐름표의 '조정' — 조정 내역 표의 열 합계와 같아야 한다
      opex        : 매출액 - 영업이익 = 매출원가 + 판매비와관리비
                    — 비용의 성격별 분류 표의 열 합계와 같아야 한다
    """
    adjustments = None
    for row in rows:
        if row.get("sj_div") != "CF":
            continue
        acc_id = (row.get("account_id") or "").strip()
        acc_nm = re.sub(r"\s+", "", row.get("account_nm") or "")
        if acc_id == ADJUSTMENTS_ID or (acc_id == "-표준계정코드 미사용-"
                                        and ADJUSTMENTS_NM.match(acc_nm)):
            adjustments = _to_number(row.get("thstrm_amount"))
            break

    opex = None
    if items.get("revenue") is not None and items.get("operating_income") is not None:
        opex = items["revenue"] - items["operating_income"]

    return {"revenue": items.get("revenue"), "adjustments": adjustments, "opex": opex}


def _ratio(numerator, denominator, positive_denominator=True):
    """
    비율(%) 계산. 분모가 없거나 0이면 None.

    positive_denominator=True 이면 분모가 음수일 때도 None을 준다.
    자본잠식(자본총계 < 0) 회사의 부채비율·ROE는 수치로는 계산되지만
    의미가 없고 차트를 왜곡하므로 아예 표시하지 않는다.
    """
    if numerator is None or denominator is None:
        return None
    if denominator == 0:
        return None
    if positive_denominator and denominator < 0:
        return None
    return numerator / denominator * 100.0


def compute_ratios(items, prev_items=None):
    """
    한 연도의 파생지표를 계산한다.

    ROE·ROA는 전기 잔액을 알 수 있으면 평잔(기초+기말)/2 기준으로,
    전기가 없으면 기말 잔액 기준으로 계산한다. 어느 쪽을 썼는지는
    'roe_basis' 로 남겨 리포트 각주에 표기한다.
    """
    rev = items.get("revenue")
    ni = items.get("net_income")

    ratios = {
        "opm": _ratio(items.get("operating_income"), rev, positive_denominator=True),
        "npm": _ratio(ni, rev, positive_denominator=True),
        "ebitda_margin": _ratio(items.get("ebitda"), rev, positive_denominator=True),
        "debt_ratio": _ratio(items.get("liabilities"), items.get("equity")),
        "equity_ratio": _ratio(items.get("equity"), items.get("assets")),
        "current_ratio": _ratio(items.get("current_assets"), items.get("current_liabilities")),
    }

    def average(key):
        now = items.get(key)
        before = (prev_items or {}).get(key)
        if now is None:
            return None, "기말"
        if before is None:
            return now, "기말"
        return (now + before) / 2.0, "평잔"

    equity_base, basis = average("equity")
    ratios["roe"] = _ratio(ni, equity_base)
    ratios["roe_basis"] = basis

    asset_base, basis = average("assets")
    ratios["roa"] = _ratio(ni, asset_base)
    ratios["roa_basis"] = basis

    if prev_items and prev_items.get("revenue") and rev is not None:
        base = prev_items["revenue"]
        ratios["revenue_growth"] = (rev - base) / abs(base) * 100.0 if base else None
    else:
        ratios["revenue_growth"] = None

    return ratios


def derive_ebitda_and_debt(items, override=None, note=None):
    """
    EBITDA와 순차입금을 만든다. 사용자가 넣은 값(override)이 있으면 그쪽을 우선한다.

      EBITDA = 영업이익 + 감가상각비·무형자산상각비
      순차입금 = 총차입금(리스부채 포함) − 현금및현금성자산

    ★ 순차입금에서 단기금융상품(정기예금 등)은 빼지 않는다.
      회사마다 유동성 분류가 달라 자동으로 가려내면 오히려 들쭉날쭉해지므로,
      '현금및현금성자산'이라는 한 가지 기준으로 통일했다. 이 점은 리포트 각주에 밝힌다.
    """
    # ① 공시원문 주석에서 자동으로 읽어 온 값 (note_reader)
    from_note = set()
    for key in MANUAL_KEYS:
        value = (note or {}).get(key)
        if value is not None and items.get(key) is None:
            items[key] = float(value)
            from_note.add(key)

    # ② 사용자가 직접 넣은 값 — 자동으로 읽은 값보다 우선한다
    manual = set()
    for key in MANUAL_KEYS:
        value = (override or {}).get(key)
        if value is not None:
            items[key] = float(value)
            manual.add(key)
            from_note.discard(key)

    op = items.get("operating_income")
    da = items.get("dep_amort")
    items["ebitda"] = None if (op is None or da is None) else op + da

    # EDGAR 자료는 차입금이 태그별로 쪼개져 오므로 여기서 합친다.
    # (DART는 재무상태표 계정을 훑어 extract_items 단계에서 이미 합쳐 둔다)
    if items.get("total_debt") is None:
        parts = [(EDGAR_DEBT_LABELS[k], items.get(k)) for k in EDGAR_DEBT_KEYS]
        found = [(label, value) for label, value in parts if value is not None]
        if found:
            items["total_debt"] = sum(value for _label, value in found)
            detail = items.setdefault("_detail", {})
            detail["total_debt"] = [{"name": label, "amount": value, "id": ""}
                                    for label, value in found]

    debt = items.get("total_debt")
    cash = items.get("cash")
    items["net_debt"] = None if debt is None else debt - (cash or 0.0)

    items["_manual"] = sorted(manual)
    items["_from_note"] = sorted(from_note)
    return items


def compute_multiples(items, market_cap):
    """
    시가총액을 받아 배수를 만든다. 주가는 DART·EDGAR에 없으므로
    이 단계에서만 외부(야후) 자료가 들어온다.

      EV = 시가총액 + 순차입금
      EV/EBITDA, EV/EBIT, PER, PBR

    EBITDA·EBIT가 음수면 배수를 계산하지 않는다.
    적자 회사의 배수는 숫자로는 나오지만 비교에 쓸 수 없기 때문이다.
    """
    out = {"market_cap": market_cap, "ev": None,
           "ev_ebitda": None, "ev_ebit": None, "per": None, "pbr": None}
    if market_cap is None:
        return out

    net_debt = items.get("net_debt")
    ev = None if net_debt is None else market_cap + net_debt
    out["ev"] = ev

    def divide(numerator, denominator):
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    out["ev_ebitda"] = divide(ev, items.get("ebitda"))
    out["ev_ebit"] = divide(ev, items.get("operating_income"))
    out["per"] = divide(market_cap, items.get("net_income"))
    out["pbr"] = divide(market_cap, items.get("equity"))
    return out


def apply_market_data(series, market, as_of, avg_days, base_currency="KRW", log=None):
    """
    시가총액을 구해 각 연도의 배수를 채운다.

    ★ 시가총액은 '기준일 하나', 순차입금·EBITDA는 '각 사업연도 말' 값이다.
      주가는 지금 시점의 것이고 실적은 그 해의 것이므로 성격이 다르지만,
      비교기업 배수를 볼 때 쓰는 통상적인 방식이다(현재 EV ÷ 각 연도 실적).
      기준일을 과거로 잡으면 그 시점 주가로 계산되므로 과거 시점 배수도 볼 수 있다.

    ★ 주식수는 기준일에 가장 가까운 사업연도의 상장주식수를 쓴다.
      기준일 시점의 주식수를 알 수 있는 가장 가까운 공시 값이기 때문이다.

    통화가 다른 회사(EDGAR)는 환산율을 함께 담아 둔다. 배수 자체는
    분자·분모가 같은 통화라 환산 없이도 그대로 비교된다.
    """
    currency = series.get("currency", "KRW")
    info = {"ticker": "", "price": None, "currency": currency,
            "shares": None, "market_cap": None, "fx_rate": 1.0,
            "as_of": as_of.isoformat(), "avg_days": avg_days,
            "price_days": 0, "price_from": "", "price_to": "", "error": ""}
    series["market"] = info

    # 환율은 주가와 별개로 먼저 구한다. 주가 조회에 실패해도 금액을 한 통화로
    # 맞춰 보여줄 수 있어야 하기 때문이다.
    if currency != "KRW":
        try:
            info["fx_rate"] = market.fx_to_krw(currency, as_of, avg_days)["rate"]
        except Exception as e:
            if log:
                log(f"  · {series['name']}: 환율 조회 실패 — {e}")

    if not series.get("listed"):
        info["error"] = "비상장 법인이라 시가총액을 구할 수 없습니다"
        return series

    # 기준일에 가장 가까운(=가장 최근) 사업연도의 주식수
    shares = None
    for year in sorted(series["rows"], reverse=True):
        value = series["rows"][year].get("shares")
        if value:
            shares = value
            break
    info["shares"] = shares

    try:
        if series.get("source") == "edgar":
            ticker = series["stock_code"]
        else:
            ticker = market.korean_ticker(series["stock_code"])
        quote = market.quote(ticker, as_of, avg_days)
    except Exception as e:
        info["error"] = str(e)
        if log:
            log(f"  · {series['name']}: 주가 조회 실패 — {e}")
        return series

    info.update({
        "ticker": ticker, "price": quote["price"], "currency": quote["currency"],
        "price_days": quote["days"], "price_from": quote["first_date"],
        "price_to": quote["last_date"],
    })

    if not shares:
        info["error"] = "상장주식수를 구하지 못했습니다"
        return series

    info["market_cap"] = quote["price"] * shares

    # 주가 통화가 재무제표 통화와 다르면(해외 상장 등) 그쪽 기준으로 환율을 다시 잡는다
    if quote["currency"] and quote["currency"] != currency:
        try:
            info["fx_rate"] = market.fx_to_krw(quote["currency"], as_of, avg_days)["rate"]
        except Exception:
            pass

    for year, row in series["rows"].items():
        row["multiples"] = compute_multiples(row["items"], info["market_cap"])

    return series


def build_series(collected, years, overrides=None, notes=None):
    """
    collect_company() 결과를 연도별 지표표로 바꾼다.

    overrides 는 {연도: {"dep_amort": 값, "total_debt": 값}} 형태로,
    자동으로 잡히지 않은 항목을 사용자가 직접 넣은 것이다.

    ★ DART와 EDGAR는 담겨 오는 모양이 다르다.
      DART  : collected["years"][연도] = 계정 행 리스트 (여기서 항목을 뽑아야 한다)
      EDGAR : collected["years"][연도] = 이미 항목별로 정리된 dict
      SEC는 XBRL 태그로 바로 조회되므로 계정 매핑 단계가 필요 없다.

    반환:
      {
        "name": "삼성전자(주)",
        "rows": {연도: {"items": {...}, "ratios": {...}, "fs_div": "CFS", ...}},
      }
    """
    rows = {}
    prev_items = None
    is_edgar = collected.get("source") == "edgar"

    for year in sorted(years):
        raw = collected["years"].get(year)
        if raw is None:
            prev_items = None      # 연도가 끊기면 증가율·평잔을 잇지 않는다
            continue

        if is_edgar:
            items = dict(raw)
            items.setdefault("_detail", {})
            currency = collected.get("currency") or "USD"
            term_name = (collected.get("period_end") or {}).get(year, "")
            receipt = ""
        else:
            items = extract_items(raw)
            currency = (raw[0].get("currency") or "KRW").strip() if raw else "KRW"
            term_name = (raw[0].get("thstrm_nm") or "").strip() if raw else ""
            receipt = (raw[0].get("rcept_no") or "").strip() if raw else ""

        items = derive_ebitda_and_debt(items, (overrides or {}).get(year),
                                       (notes or {}).get(year))
        ratios = compute_ratios(items, prev_items)

        rows[year] = {
            "items": items,
            "ratios": ratios,
            "multiples": {},          # 시장자료를 받은 뒤 채운다
            "fs_div": collected["fs_div_used"].get(year),
            "currency": currency,
            "term_name": term_name,
            "rcept_no": receipt,
            "shares": (collected.get("shares") or {}).get(year),
        }
        prev_items = items

    profile = collected.get("profile") or {}
    return {
        "name": collected["corp_name"],
        "source": collected.get("source", "dart"),
        "corp_code": collected.get("corp_code", ""),
        "cik": collected.get("cik"),
        "stock_code": collected["stock_code"],
        "listed": collected["listed"],
        "currency": collected.get("currency", "KRW"),
        # 우선주가 따로 상장된 회사는 보통주 시가총액만으로는 EV가 과소계상된다
        "has_preferred": bool(collected.get("preferred")),
        # 사업보고서를 내지 않는 비상장 외감법인은 감사보고서 원문에서 읽어 온다
        "from_audit_report": any(
            raw and isinstance(raw, list) and raw[0].get("source_note")
            for raw in collected["years"].values()),
        "industry": (profile.get("induty_code") or "").strip(),
        "ceo": (profile.get("ceo_nm") or "").strip(),
        "est_dt": (profile.get("est_dt") or "").strip(),
        "acc_mt": (profile.get("acc_mt") or "").strip(),
        "rows": rows,
    }
