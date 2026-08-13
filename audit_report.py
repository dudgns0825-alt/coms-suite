# -*- coding: utf-8 -*-
"""
감사보고서 단독공시에서 재무제표 읽기 (비상장 외감법인)
================================================

사업보고서를 내지 않는 비상장 외부감사대상 법인은 재무제표 API가 [013]
(데이터 없음)을 준다. 이 회사들의 재무제표는 '감사보고서 단독공시'
원문 안에만 있다 — 삼성디스플레이가 대표적이다.

  상장사        : 사업보고서 O · 감사보고서 단독공시 X → 재무제표 API 사용
  비상장 외감법인 : 사업보고서 X · 감사보고서 단독공시 O → 이 파일이 담당

[왜 표를 글자로 읽어야 하는가]
  감사보고서 단독공시에는 XBRL 태그가 붙지 않는다(삼성전자 FY2025 사업보고서가
  태그 6,000개인데 삼성디스플레이 FY2025 연결감사보고서는 44개뿐이고 사업연도
  컨텍스트도 없다). 그래서 재무제표 표를 글자로 읽는다.

[★ 어느 열이 어느 해인가 — 검산식으로 가린다]
  표에는 당기·전기 두 열이 있고, 그 앞에 주석번호 열이 섞여 들어오기도 한다.
  값을 곧이곧대로 집지 않고, 재무제표가 스스로 만족해야 하는 항등식이
  성립하는 열만 쓴다.

    재무상태표   자산총계 = 부채총계 + 자본총계
    손익계산서   매출총이익 = 매출액 - 매출원가
                 영업이익 = 매출총이익 - 판매비와관리비
                 당기순이익 = 법인세비용차감전순이익 - 법인세비용
    현금흐름표   기말현금 = 기초현금 + 현금의 증감

  두 열 모두 항등식을 만족하므로(당기도 전기도 재무제표니까) 통과한 열 중
  가장 왼쪽을 당기로 본다 — 우리말 재무제표는 당기를 먼저 적는다.
  열은 오른쪽 끝에서부터 센다(줄마다 주석칸이 있다 없다 해서 왼쪽 기준으로는
  줄끼리 어긋난다).

[돌려주는 모양]
  재무제표 API(fnlttSinglAcntAll)와 같은 '계정 행' 리스트로 만들어 돌려준다.
  그래야 metrics.extract_items 이하가 상장사와 똑같은 길로 처리한다.
"""

import re

import note_reader


# 감사보고서 종류 — 연결/별도에 따라 다른 문서를 본다
REPORT_NAMES = {
    "CFS": re.compile(r"연결\s*감사보고서"),
    "OFS": re.compile(r"^(?!.*연결).*감사보고서"),
}

# '연결감사보고서 (2025.12)' 에서 사업연도를 뽑는다
PERIOD = re.compile(r"\((\d{4})[.\-/](\d{2})\)")

# 표 머리글에서 사업연도를 찾을 때 쓰는 표기
YEAR_IN_HEADER = re.compile(r"(20\d{2})")
CURRENT_MARK = re.compile(r"당\s*기|당\s*반?기|제?\s*\d+\s*\(?\s*당\s*\)?\s*기")

# 재무제표 한 장에 있어야 하는 최소 줄 수 (요약표·목차를 걸러 낸다)
MIN_ROWS = 8
# 내역 표는 '과목 | 주석 | 당기 | 전기' 꼴이다. 열이 더 많으면 다른 표다.
MAX_COLUMNS = 5


def _clean(label):
    """
    'Ⅰ.매출액' '1.현금및현금성자산' '가.당기순이익' → 계정명만 남긴다.

    ★ 로마숫자를 ASCII로 적는 회사가 있다('VIII.기말현금및현금성자산').
      이걸 떼지 않으면 '기말현금'을 못 찾아 현금흐름표 검산이 실패하고,
      그러면 전기 열을 당기로 잘못 고르게 된다(삼성디스플레이 FY2023).
    """
    text = re.sub(r"\s+", "", label)
    text = re.sub(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+[.)]?", "", text)
    text = re.sub(r"^[IVXivx]{1,5}[.)]", "", text)
    text = re.sub(r"^\(?\d{1,2}\)?[.)]", "", text)
    text = re.sub(r"^[가-힣][.)]", "", text)
    text = re.sub(r"^[①-⑳]", "", text)
    return text.strip()


def _column_values(table, offset):
    """
    오른쪽에서 offset 번째 열의 {계정명: 금액}. offset 은 1부터 센다.

    ★ 왼쪽이 아니라 오른쪽에서 센다.
      줄마다 주석칸이 있기도 없기도 해서 왼쪽 기준 열 번호가 줄마다 어긋난다
      (애플코리아 손익계산서: 어떤 줄은 ['과목','14','','7,372,015','','7,837,637'],
      다른 줄은 주석칸이 아예 없다). 금액 열은 항상 표의 오른쪽 끝에 모여 있으므로
      오른쪽에서 세면 줄이 달라도 같은 해끼리 모인다.

    같은 이름이 두 번 나오면 처음 것을 쓴다(총계가 먼저 온다).
    """
    values = {}
    for name, numbers in table["rows"]:
        index = len(numbers) - offset
        if index < 0 or numbers[index] is None:
            continue
        values.setdefault(_clean(name), numbers[index] * table["scale"])
    return values


def _close(left, right):
    """재무제표 항등식은 딱 떨어진다. 표기 반올림만 허용한다."""
    if left is None or right is None:
        return False
    return abs(left - right) <= max(abs(left), abs(right)) * 1e-6 + 1.0


def _minus_ok(total, amount, deduction):
    """
    total = amount - deduction 인지 본다.

    ★ 비용을 음수로 적는 회사가 있다 — 매출원가를 '(6,809,446)' 으로 적으면
      이미 부호가 붙어 있어 빼면 두 번 빼는 셈이 된다(애플코리아).
      그래서 빼는 쪽·더하는 쪽 어느 하나만 맞으면 통과로 본다.
    """
    return _close(total, amount - deduction) or _close(total, amount + deduction)


def _first(values, *names):
    for name in names:
        if name in values:
            return values[name]
    return None


def _like(values, pattern):
    """'영업활동으로인한현금흐름' 처럼 회사마다 조금씩 다른 이름을 찾는다."""
    for name, value in values.items():
        if re.search(pattern, name):
            return value
    return None


def check_balance_sheet(values):
    """자산총계 = 부채총계 + 자본총계"""
    assets = _first(values, "자산총계")
    debts = _first(values, "부채총계")
    equity = _first(values, "자본총계")
    if assets is None or debts is None or equity is None:
        return False
    return _close(assets, debts + equity)


def check_income_statement(values):
    """
    손익계산서가 스스로 맞는지 — 아래 중 하나만 확인되면 된다.

    제조업은 매출액·매출원가·판매비와관리비로 적고, 서비스업은 영업수익·영업비용
    두 줄로 끝내기도 한다(우아한형제들). 어느 쪽이든 걸리도록 네 가지를 본다.
    """
    checks = []

    revenue = _first(values, "매출액", "영업수익", "수익(매출액)", "매출및지분법손익")
    cost = _first(values, "매출원가")
    gross = _first(values, "매출총이익", "매출총이익(손실)")
    operating = _first(values, "영업이익", "영업이익(손실)", "영업손실", "영업손익")

    if None not in (revenue, cost, gross):
        checks.append(_minus_ok(gross, revenue, cost))

    sga = _first(values, "판매비와관리비", "판매비및관리비", "판매관리비")
    if None not in (gross, sga, operating):
        checks.append(_minus_ok(operating, gross, sga))

    expense = _first(values, "영업비용")
    if None not in (revenue, expense, operating):
        checks.append(_minus_ok(operating, revenue, expense))

    pretax = _like(values, r"^법인세.*(차감전).*(순이익|이익|손익)")
    tax = _first(values, "법인세비용", "법인세비용(수익)", "법인세수익")
    net = _like(values, r"^(당기|연결|당기연결|반기|분기)?(연결)?순(이익|손실|손익)(\(손실\))?$")
    if None not in (pretax, tax, net):
        checks.append(_minus_ok(net, pretax, tax))

    return any(checks)


def check_cash_flow(values):
    """
    기말현금 = 기초현금 + 현금의 증감

    이 식을 먼저 본다. 어느 회사 현금흐름표든 마지막이 기초·기말이라 가장 확실하다.
    활동별 합계로 확인하는 방법은 사업결합 유출액처럼 활동 밖의 구분이 따로
    잡히는 회사가 있어(삼성디스플레이 FY2023) 보조로만 쓴다.
    """
    opening = _like(values, r"^기초.*현금|^현금.*기초")
    closing = _like(values, r"^기말.*현금|^현금.*기말")
    change = _like(values, r"현금(및현금성자산)?의(순)?(증가|증감)")

    if None not in (opening, closing, change):
        return _close(closing, opening + change)

    operating = _like(values, r"영업활동.*현금흐름")
    investing = _like(values, r"투자활동.*현금흐름")
    financing = _like(values, r"재무활동.*현금흐름")
    if None not in (operating, investing, financing, change):
        effect = _like(values, r"환율변동|외화환산") or 0.0
        combination = _like(values, r"사업결합") or 0.0
        return _close(change, operating + investing + financing + effect + combination)

    return False


CHECKS = {"BS": check_balance_sheet, "IS": check_income_statement, "CF": check_cash_flow}


def _pick_column(table, year):
    """
    검산식을 통과하는 열 중에서 당기 열을 고른다.

    ★ 통과한 열 중 '가장 왼쪽'을 쓴다.
      당기도 전기도 똑같이 재무제표이므로 검산식은 두 열 모두 통과한다.
      우리말 재무제표는 당기를 왼쪽에 먼저 적으므로 왼쪽이 당기다.
      (오른쪽에서 세고 있으므로 offset 이 가장 큰 것이 가장 왼쪽이다)

      머리글의 '2023.12.31' 같은 글자로 가려 보려 했으나, 병합된 칸 때문에
      머리글의 칸 위치와 숫자 열 위치가 어긋나 전기를 당기로 잡는 일이 있었다
      (삼성디스플레이 FY2023 현금흐름표에서 2022년 열을 골라, 그 값을 앵커로
      쓴 주석 감가상각비까지 한 해 어긋났다).
    """
    width = max(len(numbers) for _name, numbers in table["rows"])
    if width > MAX_COLUMNS:
        return None

    best = None
    for offset in range(1, width + 1):
        values = _column_values(table, offset)
        for kind, check in CHECKS.items():
            if check(values):
                best = (kind, values)      # 더 왼쪽 열로 계속 덮어쓴다
                break

    return best


def read_statements(reader, rcept_no, year):
    """
    감사보고서 원문에서 재무상태표·손익계산서·현금흐름표를 읽는다.
    반환: {"BS": {계정명: 금액}, "IS": {...}, "CF": {...}}  (못 읽은 것은 빠진다)
    """
    found = {}
    for table in reader.tables(rcept_no):
        if len(table["rows"]) < MIN_ROWS:
            continue
        picked = _pick_column(table, year)
        if picked is None:
            continue
        kind, values = picked
        # 같은 재무제표가 여러 번 나오면(요약·주석 재게시) 항목이 많은 쪽을 쓴다
        if kind not in found or len(values) > len(found[kind]):
            found[kind] = values

    derive_net_income(found)
    return found


def derive_net_income(statements):
    """
    당기순이익 줄이 없으면 '법인세비용차감전순이익 - 법인세비용' 으로 채운다.

    포괄손익계산서 하나로 끝내면서 당기순이익 줄을 생략하고 총포괄이익만
    적는 회사가 있다(애플코리아). 정의상 계산되는 값이라 그대로 쓸 수 있지만,
    중단영업손익이 있으면 두 값이 갈라지므로 그때는 만들지 않는다.
    """
    income = statements.get("IS")
    if not income:
        return

    if _like(income, r"^(당기|연결|당기연결)?(연결)?순(이익|손실|손익)"):
        return
    if _like(income, r"중단영업"):
        return

    pretax = _like(income, r"^법인세.*(차감전).*(순이익|이익|손익)")
    tax = _first(income, "법인세비용", "법인세비용(수익)", "법인세수익")
    if None in (pretax, tax):
        return

    # 법인세를 음수로 적었으면 더해야 한다
    net = pretax + tax if tax < 0 else pretax - tax
    income["당기순이익"] = net


def to_rows(statements, year, rcept_no, term_name=""):
    """
    읽어 낸 재무제표를 재무제표 API와 같은 '계정 행' 리스트로 바꾼다.

    표준계정코드는 붙일 수 없으므로 '-표준계정코드 미사용-' 으로 두고
    계정명으로만 찾게 한다(metrics 가 그렇게 처리한다).
    """
    rows = []
    for sj_div, values in statements.items():
        for name, amount in values.items():
            rows.append({
                "sj_div": sj_div,
                "account_id": "-표준계정코드 미사용-",
                "account_nm": name,
                "thstrm_amount": f"{amount:.0f}",
                "thstrm_nm": term_name or f"제 {year} 기",
                "currency": "KRW",
                "rcept_no": rcept_no,
                "source_note": "감사보고서 단독공시",
            })
    return rows


def find_reports(client, corp_code, years, fs_pref="CFS"):
    """
    사업연도별 감사보고서 접수번호를 찾는다. {연도: (접수번호, 실제구분)}

    연결감사보고서가 있으면 그쪽(CFS), 없으면 감사보고서(OFS)를 쓴다 —
    재무제표 API에서 연결이 없으면 별도로 넘어가는 것과 같은 규칙이다.
    """
    if not years:
        return {}

    # 사업연도 다음 해에 공시되므로 조회 종료일을 한 해 늘린다
    rows = client.search(corp_code, f"{min(years)}0101", f"{max(years) + 1}1231",
                         "F")

    found = {}
    for row in rows:
        name = (row.get("report_nm") or "").strip()
        m = PERIOD.search(name)
        if not m:
            continue
        year = int(m.group(1))
        if year not in years:
            continue

        for div in (("CFS", "OFS") if fs_pref == "CFS" else ("OFS",)):
            if REPORT_NAMES[div].search(name):
                # 같은 연도에 여러 건이면 연결 우선, 그다음 나중 접수분
                current = found.get(year)
                if current is None or (current[1] == "OFS" and div == "CFS"):
                    found[year] = (row["rcept_no"], div)
                break

    return found
