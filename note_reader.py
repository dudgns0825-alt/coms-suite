# -*- coding: utf-8 -*-
"""
사업보고서 원문(주석)에서 수치 읽기
================================================

재무제표 API(fnlttSinglAcntAll)는 재무제표 '본문'만 준다.
그런데 한국 대형사는 현금흐름표의 조정 항목을 '조정' 한 줄로 묶어 제출해
감가상각비가 본문에 없다(삼성전자·SK하이닉스·LG디스플레이 모두 그렇다).
차입금을 재무상태표에 '금융부채'로 묶어 표시해 식별되지 않는 회사도 있다
(LG디스플레이).

다행히 공시원문 XML에는 표의 숫자마다 XBRL 태그가 붙어 있다.

    <TE ACODE="ifrs-full_AdjustmentsForDepreciationExpense"
        ACONTEXT="CFY2025dFY_..._ConsolidatedMember_...">43,605,740</TE>

  ACODE    : 표준계정코드
  ACONTEXT : 기간(CFY=당기, PFY=전기)과 연결/별도(ConsolidatedMember/SeparateMember)

이 태그를 읽으면 주석에 있는 감가상각비를 그대로 가져올 수 있다.

[단위를 어떻게 아는가]
  ★ 표마다 단위가 다르다. 재무제표 표는 백만원인데 주석 표는 원 단위인 회사가 있어
    (한미반도체가 그렇다) 문서 전체에 한 가지 배수를 적용하면 100배씩 틀린다.
    그래서 각 숫자의 위치보다 앞에 있는 가장 가까운 '(단위 : 백만원)' 표기를 찾아
    그 표의 단위로 삼는다. 공시 서식상 단위는 표 바로 위에 적히므로 이 방법이 맞다.

  구한 단위가 맞는지는 매출액으로 교차확인한다. 매출액은 API로 이미 알고 있으므로
  원문에서 읽은 매출액에 배수를 곱해 API 값과 어긋나면 그 보고서는 신뢰하지 않는다.
"""

import io
import os
import re
import zipfile


TE_TAG = re.compile(r"<TE\b([^>]*)>(.*?)</TE>", re.DOTALL | re.IGNORECASE)
ATTR = re.compile(r"(\w+)\s*=\s*\"([^\"]*)\"")

# 감가상각비 — 유형자산과 무형자산을 나눠 태깅하는 회사와 합쳐 태깅하는 회사가 있다.
#
# ★ 현금흐름표의 조정 항목(AdjustmentsFor...)을 먼저 본다.
#   일반 계정인 DepreciationAndAmortisationExpense 는 주석의 어느 표에나 붙을 수 있어
#   엉뚱한 값이 잡힌다. DB하이텍은 이 계정에 159,679원이 태깅돼 있는데
#   실제 감가상각비는 1,678억이다. 조정 항목은 현금흐름표 한 줄이라 그럴 일이 없다.
DEP_CODES = ["ifrs-full_AdjustmentsForDepreciationExpense"]
AMORT_CODES = ["ifrs-full_AdjustmentsForAmortisationExpense"]
DEP_AMORT_CODES = [
    "ifrs-full_AdjustmentsForDepreciationAndAmortisationExpense",
    "ifrs-full_DepreciationAndAmortisationExpense",
]

# 단위 검증에 쓰는 계정 (값을 이미 알고 있는 것)
REVENUE_CODES = [
    "ifrs-full_Revenue",
    "ifrs-full_RevenueFromContractsWithCustomers",
    "dart_OperatingRevenue",
]

# 표 위에 적히는 단위 표기 — '(단위 : 백만원)' 형태
UNIT_MARK = re.compile(r"단위\s*[:：]?\s*[^)\]<]{0,6}?(억원|백만원|천원|원)")
UNIT_SCALE = {"원": 1.0, "천원": 1e3, "백만원": 1e6, "억원": 1e8}


# ── 차입금 ──────────────────────────────────────────────
#
# ★ 주석의 차입금 태그를 그냥 더하면 안 된다.
#   회사가 재무상태표에 '차입금'으로 표시한 줄에 사채가 이미 들어 있는 경우가 있어
#   (SK하이닉스가 그렇다) 주석의 사채 내역을 더하면 이중계상된다.
#   그래서 구성요소를 더한 값이 회사가 주석에 스스로 밝힌 차입금 총계
#   (ifrs-full_Borrowings)와 맞을 때만 채택한다. 어긋나면 직접 입력으로 넘긴다.
#
# 같은 성격의 계정을 여러 코드로 태깅하는 회사가 있어 묶음마다 하나씩만 쓴다.
DEBT_GROUPS = [
    ["ifrs-full_ShorttermBorrowings"],
    ["ifrs-full_CurrentPortionOfLongtermBorrowings",
     "ifrs-full_OtherCurrentBorrowingsAndCurrentPortionOfOtherNoncurrentBorrowings",
     "ifrs-full_CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
    ["ifrs-full_LongtermBorrowings",
     "ifrs-full_NoncurrentPortionOfNoncurrentLoansReceived",
     "ifrs-full_NoncurrentPortionOfOtherNoncurrentBorrowings"],
    ["ifrs-full_CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued",
     "ifrs-full_CurrentPortionOfNoncurrentBondsIssued"],
    ["ifrs-full_NoncurrentPortionOfNoncurrentBondsIssued"],
]
LEASE_SPLIT = ["ifrs-full_CurrentLeaseLiabilities", "ifrs-full_NoncurrentLeaseLiabilities"]
LEASE_TOTAL = "ifrs-full_LeaseLiabilities"
DEBT_CHECK = "ifrs-full_Borrowings"      # 회사가 밝힌 차입금 총계 (검산용)

# 컨텍스트에서 연결/별도 축은 내역 구분이 아니다
FS_AXIS = re.compile(r"_ifrs-full_ConsolidatedAndSeparateFinancialStatementsAxis"
                     r"_ifrs-full_(?:Consolidated|Separate)Member")
# '보고금액(장부금액)' 축도 내역이 아니라 표기 방식이라 총계로 본다
REPORTED_AXIS = re.compile(r"_ifrs-full_CarryingAmountAccumulatedDepreciationAmortisation"
                           r"AndImpairmentAndGrossCarryingAmountAxis_dart_ReportedAmountMember")


# ── 태그가 없는 옛 보고서 : 표를 글자로 읽는다 ────────────
#
# FY2023 이전 보고서에는 주석 XBRL 태깅이 아예 없다(태그 200~340개, 사업연도
# 컨텍스트도 없음). 그래도 주석 표 자체는 서식이 일정해서 글자로 읽을 수 있다.
#
# ★ 문제는 '어느 표의 어느 열'인지 알 수 없다는 것이다.
#   한 문서에 연결 주석과 별도 주석이 같이 들어 있고, 표마다 당기·전기 열이 있고,
#   단위도 표마다 다르다. 그래서 값을 곧이곧대로 집어 오지 않고,
#   열을 전부 더한 값이 재무제표 본문의 대조금액과 맞는 열만 골라 쓴다.
#   이 한 번의 대조로 연결/별도·당기/전기·단위가 동시에 확인된다.
#   맞는 열이 없으면 아무것도 돌려주지 않는다(직접 입력으로 넘어간다).
TABLE_TAG = re.compile(r"<TABLE\b[^>]*>(.*?)</TABLE>", re.DOTALL | re.IGNORECASE)
ROW_TAG = re.compile(r"<TR\b[^>]*>(.*?)</TR>", re.DOTALL | re.IGNORECASE)
# 칸은 TD·TH 뿐 아니라 TE 로도 적힌다 — 사업보고서 본문의 표(XBRL 태그가 붙는 그 표)가
# TE 를 쓴다. TD 만 보면 그런 보고서는 표가 통째로 안 읽힌다(DB하이텍 FY2023).
CELL_TAG = re.compile(r"<T[DHE]\b[^>]*>(.*?)</T[DHE]>", re.DOTALL | re.IGNORECASE)

# 표에서 더할 줄 — 감가상각비·무형자산상각비 (사용권자산은 나눠 적는 회사가 있다)
# 성격별 분류 표에는 '감가상각비 등'으로 묶어 적는 회사가 있어(SK하이닉스) 끝의 '등'도 받는다
DEP_ROW = re.compile(
    r"^(?:"
    r"(?:유형자산|투자부동산|사용권자산|리스자산|기타)?감가상각비"
    r"|(?:사용권자산|리스자산)상각비"
    r"|(?:기타)?무형자산(?:감가)?상각비"
    r"|감가상각비?(?:및|와)무형자산상각비"
    r")등?$"
)
# 합계 줄은 더하면 안 된다(이미 다른 줄에 들어 있다)
TOTAL_ROW = re.compile(r"^(합\s*계|소\s*계|계|총\s*계)$")
# 내역 표에서 볼 수 있는 최대 열 수 (당기·전기, 많아야 전전기까지)
MAX_DETAIL_COLUMNS = 4


class NoteReader:
    """
    공시원문을 받아 태그된 수치를 꺼낸다.
    같은 보고서를 여러 번 읽지 않도록 파일과 메모리에 담아 둔다.
    """

    def __init__(self, client, cache_dir=None):
        self.client = client
        self.cache_dir = cache_dir
        self._parsed = {}      # rcept_no -> {(acode, context): [값, ...]}
        self._tables = {}      # rcept_no -> [{"scale": 배수, "rows": [(이름, [값,...]), ...]}]

    # ── 원문 받기 ────────────────────────────────────────
    def _raw(self, rcept_no):
        path = os.path.join(self.cache_dir, f"doc_{rcept_no}.zip") if self.cache_dir else None
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()

        raw = self.client.document(rcept_no)
        if path:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "wb") as f:
                f.write(raw)
        return raw

    @staticmethod
    def _decode(data):
        """
        2021년 이전 공시는 선언이 utf-8인데 실제 바이트는 CP949인 경우가 있다.
        선언을 믿지 말고 utf-8 → cp949 순으로 실제 디코드를 시도한다.
        (순서가 중요하다. cp949는 아무 바이트나 받아들여 먼저 넣으면
         utf-8 파일도 깨진 채 '성공'한다.)
        """
        for encoding in ("utf-8", "cp949"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "ignore")

    def parse(self, rcept_no):
        """
        원문의 모든 <TE ACODE=...> 를 읽는다.
        값마다 그 위치에 적용되는 단위 배수를 함께 붙여 둔다.
          {(계정코드, 컨텍스트): [(값, 배수), ...]}
        """
        if rcept_no in self._parsed:
            return self._parsed[rcept_no]

        values = {}
        with zipfile.ZipFile(io.BytesIO(self._raw(rcept_no))) as z:
            for name in z.namelist():
                text = self._decode(z.read(name))

                # 문서에 나오는 단위 표기의 위치를 미리 모아 둔다
                marks = [(m.start(), UNIT_SCALE[m.group(1)])
                         for m in UNIT_MARK.finditer(text)]

                for match in TE_TAG.finditer(text):
                    attrs = dict(ATTR.findall(match.group(1)))
                    acode = attrs.get("ACODE")
                    if not acode:
                        continue

                    number = self._to_number(match.group(2))
                    if number is None:
                        continue

                    key = (acode, attrs.get("ACONTEXT", ""))
                    values.setdefault(key, []).append(
                        (number, self._scale_at(marks, match.start())))

        self._parsed[rcept_no] = values
        return values

    @staticmethod
    def _scale_at(marks, position):
        """그 위치보다 앞에 있는 가장 가까운 단위 표기를 쓴다. 없으면 원 단위."""
        scale = 1.0
        for start, value in marks:
            if start > position:
                break
            scale = value
        return scale

    @staticmethod
    def _to_number(chunk):
        text = re.sub(r"<[^>]+>", "", chunk).strip()
        text = text.replace(",", "").replace(" ", "").replace("\xa0", "")
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        text = text.replace("△", "-").replace("▲", "-")
        try:
            number = float(text)
        except ValueError:
            return None
        return -number if negative else number

    # ── 값 고르기 ────────────────────────────────────────
    @staticmethod
    def _all(values, codes, year, fs_div):
        """조건에 맞는 값을 모두(원 단위로) 돌려준다."""
        want = "ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"
        other = "SeparateMember" if fs_div == "CFS" else "ConsolidatedMember"

        matched, neutral = [], []
        for code in codes:
            for (acode, context), pairs in values.items():
                if acode != code or f"FY{year}" not in context:
                    continue
                amounts = [number * scale for number, scale in pairs]
                if want in context:
                    matched.extend(amounts)
                elif other not in context:
                    neutral.extend(amounts)
        return matched or neutral

    @staticmethod
    def _pick(values, codes, year, fs_div):
        """
        해당 계정의 값 중 '그 사업연도, 그 재무제표 구분'인 것을 원 단위로 고른다.

        ACONTEXT 예:
          CFY2025dFY_ifrs-full_ConsolidatedAndSeparateFinancialStatementsAxis
          _ifrs-full_ConsolidatedMember_...

        연결/별도가 컨텍스트에 표시되지 않은 회사도 있어, 일치하는 것이 없으면
        구분 표기 자체가 없는 값을 쓴다.
        """
        want = "ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"
        other = "SeparateMember" if fs_div == "CFS" else "ConsolidatedMember"

        for code in codes:
            matched, neutral = [], []
            for (acode, context), pairs in values.items():
                if acode != code or f"FY{year}" not in context:
                    continue
                amounts = [number * scale for number, scale in pairs]
                if want in context:
                    matched.extend(amounts)
                elif other not in context:
                    neutral.extend(amounts)

            found = matched or neutral
            if found:
                # 같은 계정이 여러 표에 반복되면 값이 같다.
                # 다르면 가장 큰 값(총계)을 쓴다 — 부문별 내역이 함께 잡히는 경우가 있다.
                return max(found)

        return None

    def verify_scale(self, rcept_no, year, fs_div, anchors):
        """
        읽어 낸 단위가 맞는지 매출액으로 확인한다.

        매출액은 요약표·부문별표 등 여러 표에 나오므로 후보가 여럿이다.
        그중 하나라도 API 값과 1% 이내로 맞으면 단위 해석이 통한 것으로 본다.
        (총계만 골라 비교하면 부문 합계가 더 큰 표에 걸려 어긋난다)
        """
        revenue = anchors.get("revenue")
        if not revenue:
            return False

        for value in self._all(self.parse(rcept_no), REVENUE_CODES, year, fs_div):
            if abs(value - revenue) <= abs(revenue) * 0.01:
                return True
        return False

    # ── 표를 글자로 읽기 ─────────────────────────────────
    def tables(self, rcept_no):
        """
        원문의 모든 표를 [{"scale": 단위배수, "rows": [(줄이름, [숫자, ...]), ...]}]로.

        줄이름은 첫 칸, 숫자는 나머지 칸이며 숫자가 아닌 칸은 None으로 남겨
        열 위치가 밀리지 않게 한다.
        """
        if rcept_no in self._tables:
            return self._tables[rcept_no]

        tables = []
        with zipfile.ZipFile(io.BytesIO(self._raw(rcept_no))) as z:
            for name in z.namelist():
                text = self._decode(z.read(name))
                marks = [(m.start(), UNIT_SCALE[m.group(1)])
                         for m in UNIT_MARK.finditer(text)]

                for table in TABLE_TAG.finditer(text):
                    rows = []
                    for row in ROW_TAG.finditer(table.group(1)):
                        cells = [self._plain(c) for c in CELL_TAG.findall(row.group(1))]
                        if len(cells) < 2 or not cells[0]:
                            continue
                        rows.append((cells[0],
                                     [self._to_number(c) for c in cells[1:]]))
                    if rows:
                        tables.append({"scale": self._scale_at(marks, table.start()),
                                       "rows": rows})

        self._tables[rcept_no] = tables
        return tables

    @staticmethod
    def _plain(chunk):
        """칸 안의 태그·주석표시(*1)·공백을 걷어 낸 글자."""
        text = re.sub(r"<[^>]+>", "", chunk)
        text = text.replace("\xa0", " ").replace("&nbsp;", " ")
        text = re.sub(r"\((?:주|\*)[^)]*\)|\*+\d*|※", "", text)
        return re.sub(r"\s+", "", text).strip()

    def dep_amort_from_tables(self, rcept_no, anchors):
        """
        태그가 없는 보고서에서 감가상각비를 표로 읽는다.

        열 합계가 본문의 '조정'(현금흐름표) 또는 '매출원가+판매비와관리비'와
        맞는 열만 쓴다 — 그 대조가 통과해야 연결/별도·당기/전기·단위가 맞는
        표의 맞는 열이라는 뜻이다. 맞는 열이 없으면 None.

        ★ 대조는 빠듯하게 본다(0.1%). 내역 표의 열 합계는 본문 금액과 원래
          딱 떨어지므로 느슨하게 잡을 이유가 없고, 느슨하면 엉뚱한 표가 걸린다
          — 유형자산 증감표의 어느 한 열 합계가 0.8% 차이로 통과해
          그 표의 감가상각비(자산 한 종류분)를 물어 온 적이 있다.
        """
        # 현금흐름표의 조정 내역을 먼저 본다 — 태그로 읽을 때와 같은 기준이라
        # 연도끼리 기준이 어긋나지 않는다. 없으면 비용의 성격별 분류 표를 쓴다.
        for check in (anchors.get("adjustments"), anchors.get("opex")):
            if not check:
                continue
            value = self._dep_from_tables(rcept_no, check)
            if value is not None:
                return value

        return None

    def _dep_from_tables(self, rcept_no, check):
        """열 합계가 check 와 맞는 표에서 감가상각비 줄을 더한다."""
        for table in self.tables(rcept_no):
            scale = table["scale"]
            width = max(len(numbers) for _name, numbers in table["rows"])

            # 내역 표는 '구분 | 당기 | 전기' 꼴이다. 열이 더 많으면 자산 종류별·
            # 부문별로 펼친 표이므로 열 합계에 아무 의미가 없다.
            if width > MAX_DETAIL_COLUMNS:
                continue

            for column in range(width):
                total, depreciation = 0.0, []
                for name, numbers in table["rows"]:
                    value = numbers[column] if column < len(numbers) else None
                    if value is None or TOTAL_ROW.match(name):
                        continue
                    total += value
                    # 상각비는 더해 주는 항목이라 양수로 실린다.
                    # 음수면 증감표의 차감란이지 내역이 아니다.
                    if value > 0 and DEP_ROW.match(name):
                        depreciation.append(value)

                if depreciation and abs(total * scale - check) <= abs(check) * 0.001:
                    return sum(depreciation) * scale

        return None

    def lookup(self, method, receipts, year, fs_div, anchors):
        """
        같은 값을 여러 보고서에서 차례로 찾아 (값, 찾은 보고서, 실패기록)을 돌려준다.

        ★ 그 해 보고서에 없어도 다음 해 보고서에는 있는 경우가 많다.
          주석 XBRL 태깅은 FY2025 보고서부터 전면 적용됐는데(그 전에는 재무제표
          본문 위주로 200~1,500개, FY2025부터 5,000~8,000개), 보고서 하나에
          당기(CFY)뿐 아니라 전기(PFY) 비교표시분도 함께 태깅된다.
          그래서 FY2024 감가상각비는 FY2024 보고서에는 없고 FY2025 보고서에 있다.
          (전전기 BPFY는 재무제표 본문 3개년 표에만 붙어 주석 수치가 없다)
        """
        errors = []
        for rcept_no in receipts:
            if not rcept_no:
                continue
            try:
                value = getattr(self, method)(rcept_no, year, fs_div, anchors)
            except Exception as e:
                errors.append(f"{rcept_no}: {e}")
                continue
            if value is not None:
                return value, rcept_no, errors
        return None, None, errors

    def dep_amort(self, rcept_no, year, fs_div, anchors):
        """
        감가상각비 + 무형자산상각비를 원 단위로 돌려준다.
        구하지 못하면 None (그러면 사용자가 직접 넣는다).

        태그를 먼저 보고, 태그가 없는 보고서(FY2023 이전)는 표를 글자로 읽는다.
        """
        if self.verify_scale(rcept_no, year, fs_div, anchors):
            values = self.parse(rcept_no)

            # 나눠 태깅한 회사(삼성전자·DB하이텍)를 먼저 본다
            depreciation = self._pick(values, DEP_CODES, year, fs_div)
            if depreciation is not None:
                amortisation = self._pick(values, AMORT_CODES, year, fs_div) or 0.0
                return depreciation + amortisation

            # 합쳐 태깅한 회사(LG디스플레이)는 통합 계정에서 가져온다
            combined = self._pick(values, DEP_AMORT_CODES, year, fs_div)
            if combined is not None:
                return combined

        return self.dep_amort_from_tables(rcept_no, anchors)

    # ── 차입금 ───────────────────────────────────────────
    @staticmethod
    def _is_total_context(context):
        """
        내역(통화별·차입처별·만기별)이 아니라 총계인 컨텍스트인지 본다.

        차입금 주석에는 같은 계정이 통화별·차입처별로 여러 번 나온다.
        그런 값에는 컨텍스트에 축(...Axis)이 하나 더 붙으므로,
        연결/별도 축과 보고금액 축을 걷어 낸 뒤에도 축이 남으면 내역으로 본다.
        """
        rest = REPORTED_AXIS.sub("", FS_AXIS.sub("", context))
        return "Axis" not in rest

    def _totals(self, values, year, fs_div):
        """총계 컨텍스트의 값만 {계정코드: 금액} 으로 추린다."""
        want = "ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"
        other = "SeparateMember" if fs_div == "CFS" else "ConsolidatedMember"

        matched, neutral = {}, {}
        for (acode, context), pairs in values.items():
            if f"FY{year}" not in context or not self._is_total_context(context):
                continue
            amounts = [number * scale for number, scale in pairs]
            if not amounts:
                continue
            if want in context:
                matched.setdefault(acode, []).append(max(amounts))
            elif other not in context:
                neutral.setdefault(acode, []).append(max(amounts))

        return {code: max(found) for code, found in (matched or neutral).items()}

    def total_debt(self, rcept_no, year, fs_div, anchors):
        """
        총차입금(리스부채 포함)을 원 단위로 돌려준다.

        구성요소를 더한 값이 회사가 주석에 밝힌 차입금 총계와 1% 이내로
        맞을 때만 채택한다. 확인되지 않으면 None — 사용자가 직접 넣는다.
        (SK하이닉스처럼 '차입금' 줄에 사채가 포함된 회사는 여기서 걸러진다)
        """
        if not self.verify_scale(rcept_no, year, fs_div, anchors):
            return None

        totals = self._totals(self.parse(rcept_no), year, fs_div)

        borrowings = 0.0
        for group in DEBT_GROUPS:
            for code in group:
                if totals.get(code, 0) > 0:
                    borrowings += totals[code]
                    break
        if borrowings <= 0:
            return None

        # 회사가 밝힌 차입금 총계와 대조 — 맞지 않으면 쓰지 않는다
        declared = self._all(self.parse(rcept_no), [DEBT_CHECK], year, fs_div)
        if not any(abs(value - borrowings) <= borrowings * 0.01 for value in declared):
            return None

        lease = sum(totals[code] for code in LEASE_SPLIT if totals.get(code, 0) > 0)
        if not lease:
            lease = max(totals.get(LEASE_TOTAL, 0.0), 0.0)

        return borrowings + lease
