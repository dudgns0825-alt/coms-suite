# -*- coding: utf-8 -*-
"""
EDGAR (미국 SEC) 호출 담당 (두 탭이 함께 쓴다)
================================================

dart_client.py 와 짝을 이루는 모듈이다. 미국 상장사를 한국 회사와 같은
자리에 끼워 쓸 수 있게 만들었다.

[앞부분 — looks_like_edgar/parse_cik/EdgarIndex/EdgarClient]
  회사명 찾기(CIK 조회)와 SEC 호출(User-Agent·gzip·재시도 처리).
  두 탭이 똑같이 쓴다.

[가운데 — list_filings/download/form_matches/fiscal_year]
  공시목록과 원문. 다운로드 탭이 쓴다.

[뒷부분 — companyfacts 이하]
  DART의 fnlttSinglAcntAll(계정 행 리스트)에 대응하는 것은 SEC의
  companyfacts API다. 다만 DART처럼 '계정명 문자열'로 오는 게 아니라
  us-gaap 표준 태그(XBRL)로 온다. 같은 항목도 회사·연도마다 태그가 달라서
  (예: 매출액이 Revenues 였다가 RevenueFromContractWithCustomer...로 바뀜)
  태그 후보를 우선순위대로 찾는 과정이 필요하다.

★★ DART 쪽과 결정적으로 다른 점 (뒤에서 쓰는 코드가 헷갈리는 지점) ★★
  dart_client.collect_company() 의 result["years"] 는
      {연도: [계정행(raw dict), ...]}   ← DART 원문 그대로, 가공 전
  인 반면, 이 모듈의 collect_company() 의 result["years"] 는
      {연도: {"revenue": 값, "operating_income": 값, ...}}  ← 이미 항목별로 정리된 dict
  이다. EDGAR는 계정 행이 아니라 태그별 시계열로 오기 때문에, 이 모듈
  안에서 미리 표준 항목명으로 뽑아 놓았다. metrics.py 등 뒤에서 쓰는 쪽은
  source가 "dart"인지 "edgar"인지에 따라 데이터 형태가 다르다는 것을
  전제해야 한다.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date


TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_FILE = "EDGAR_TICKERS.json"

# 공시목록·원문 (다운로드 탭)
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

# 재무수치 (재무분석 탭)
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class EdgarError(Exception):
    pass


def looks_like_edgar(name):
    """
    입력된 회사명이 EDGAR 대상인지 판별한다.

    한글이 한 글자라도 있으면 DART, 없으면 EDGAR로 본다.
    '삼성전자'는 DART, 'Apple'·'AAPL'·'NVIDIA CORP'는 EDGAR가 된다.
    숫자만 있는 경우는 CIK 직접 입력으로 보고 EDGAR로 보낸다.
    """
    return not re.search(r"[가-힣]", name)


def parse_cik(name):
    """'320193', 'CIK0000320193' 처럼 CIK를 직접 입력한 경우 숫자만 뽑는다."""
    m = re.fullmatch(r"(?:CIK)?\s*0*(\d{1,10})", name.strip(), re.IGNORECASE)
    return int(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────
# 1단계 : 회사명 → CIK
# ─────────────────────────────────────────────────────────────

class EdgarIndex:
    """
    SEC가 배포하는 company_tickers.json(약 0.8MB, 1만여개)을 읽어
    회사명 또는 티커로 CIK를 찾는다.

    주의 — 이 목록에는 '티커가 있는 상장사'만 들어있다.
    비상장 제출인(사모펀드·SPV 등)은 이름으로 찾을 수 없고 CIK를 직접 넣어야 한다.
    DART의 CORPCODE.xml이 비상장 11.8만개를 모두 담는 것과 반대다.
    """

    def __init__(self, json_path):
        self.json_path = json_path
        self.by_name = {}     # 정규화된 회사명 -> [기업정보, ...]
        self.by_ticker = {}   # 티커 -> 기업정보
        self.count = 0

    @staticmethod
    def normalize(name):
        """'Apple Inc.' 와 'apple' 을 같은 것으로 취급하기 위한 정규화"""
        name = name.strip().lower()
        # 법인격 표기는 회사마다 제각각이라(Inc. / Corp / Co., Ltd.) 떼어낸다.
        name = re.sub(r"[.,]", "", name)
        name = re.sub(
            r"\b(inc|corp|corporation|co|company|ltd|limited|plc|lp|llc|holdings?|group)\b",
            "", name)
        name = re.sub(r"\s+", "", name)
        return name

    def ensure(self, client, log=None):
        """목록 파일이 없으면 SEC에서 받는다. (첫 실행 시 1회)"""
        if os.path.exists(self.json_path):
            return
        if log:
            log("EDGAR 기업목록이 없습니다. SEC에서 받는 중... (약 0.8MB)")
        raw = client.request(TICKERS_URL)
        with open(self.json_path, "wb") as f:
            f.write(raw)
        if log:
            log(f"내려받음: {len(raw):,}바이트")

    def load(self):
        if not os.path.exists(self.json_path):
            raise FileNotFoundError(f"EDGAR 기업목록이 없습니다: {self.json_path}")

        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        for row in data.values():
            corp = {
                "source": "edgar",
                "cik": int(row["cik_str"]),
                "corp_name": (row.get("title") or "").strip(),
                "ticker": (row.get("ticker") or "").strip(),
                "listed": True,          # 이 목록은 전부 상장사다
                "corp_code": "",         # DART 전용 필드. 구조를 맞추기 위해 비워둔다.
            }
            if corp["corp_name"]:
                self.by_name.setdefault(self.normalize(corp["corp_name"]), []).append(corp)
            if corp["ticker"]:
                self.by_ticker.setdefault(corp["ticker"].upper(), corp)
            self.count += 1

        return self.count

    def find(self, name):
        """티커 → 정확한 회사명 → 부분일치 순으로 찾는다."""
        raw = name.strip()

        # CIK를 직접 입력한 경우 (비상장 제출인을 받을 수 있는 유일한 경로)
        cik = parse_cik(raw)
        if cik is not None:
            return [{
                "source": "edgar", "cik": cik, "ticker": "",
                "corp_name": f"CIK{cik:010d}", "listed": True, "corp_code": "",
            }]

        if raw.upper() in self.by_ticker:
            return [self.by_ticker[raw.upper()]]

        key = self.normalize(raw)
        if key in self.by_name:
            return list(self.by_name[key])

        hits = []
        for k, corps in self.by_name.items():
            if key and key in k:
                hits.extend(corps)
        # 이름이 짧은 쪽이 대개 찾던 회사다 (Apple Inc. vs Apple Hospitality REIT)
        return sorted(hits, key=lambda c: len(c["corp_name"]))


# ─────────────────────────────────────────────────────────────
# 2단계 : SEC 호출
# ─────────────────────────────────────────────────────────────

class EdgarClient:
    """
    SEC 호출 담당.

    인증키는 없지만 User-Agent에 연락처를 넣는 것이 의무다.
    SEC 공지: "Declare your traffic by updating your user agent" — 없으면 403.
    """

    def __init__(self, contact, delay=0.15, max_retry=3):
        contact = (contact or "").strip()
        if not contact:
            raise EdgarError(
                "EDGAR는 인증키 대신 연락처가 필요합니다.\n"
                "'이름 이메일' 형식으로 입력해 주세요 (예: Hong Gildong hong@example.com).\n"
                "SEC 규정상 이 값이 없으면 403으로 거절당합니다."
            )
        self.contact = contact
        self.delay = delay          # 초당 10건 제한 → 0.15초면 안전하다
        self.max_retry = max_retry
        self._last_call = 0.0

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.time()

    def request(self, url):
        # Host 헤더는 urllib이 알아서 넣는다. User-Agent만 우리가 책임진다.
        req = urllib.request.Request(url, headers={
            "User-Agent": self.contact,
            "Accept-Encoding": "gzip",
        })

        last_err = None
        for attempt in range(self.max_retry):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        import gzip
                        raw = gzip.decompress(raw)
                    return raw
            except urllib.error.HTTPError as e:
                # 404는 재시도해도 소용없다 (그 공시가 없는 것)
                if e.code == 404:
                    raise EdgarError(f"파일이 존재하지 않습니다 (404): {url}")
                if e.code == 403:
                    raise EdgarError(
                        "SEC가 요청을 거절했습니다 (403). "
                        "연락처(User-Agent)를 '이름 이메일' 형식으로 정확히 넣었는지 확인하세요."
                    )
                last_err = e
                time.sleep(2 ** attempt)
            except Exception as e:
                last_err = e
                time.sleep(2 ** attempt)

        raise EdgarError(f"네트워크 오류: {last_err}")

    # ── 공시목록·원문 (다운로드 탭) ──────────────────────
    def list_filings(self, cik, since_year=None):
        """
        회사의 공시목록을 가져온다.

        submissions JSON은 최근 1,000건만 'recent'에 담고
        그보다 오래된 것은 files[]에 별도 파일로 쪼개 둔다(Apple 기준 2015년이 경계).

        오래된 연도를 요청하면 분할분까지 합쳐야 하지만, 최근 몇 년만 필요한데
        1994년치까지 받으면 회사당 수 초가 그냥 버려진다.
        각 분할 파일에 붙어있는 filingTo(그 파일의 마지막 접수일)를 보고 건너뛴다.
        어떤 사업연도의 보고서도 그 해보다 먼저 접수될 수는 없으므로 안전하다.
        """
        data = json.loads(self.request(SUBMISSIONS_URL.format(cik=cik)).decode("utf-8"))

        company = (data.get("name") or "").strip()
        rows = list(self._rows(data.get("filings", {}).get("recent", {})))

        for extra in data.get("filings", {}).get("files", []):
            name = extra.get("name")
            if not name:
                continue
            to = str(extra.get("filingTo") or "")
            if since_year and to[:4].isdigit() and int(to[:4]) < since_year:
                continue
            raw = self.request(SUBMISSIONS_PAGE_URL.format(name=name))
            rows.extend(self._rows(json.loads(raw.decode("utf-8"))))

        return company, rows

    @staticmethod
    def _rows(block):
        """
        submissions의 '열 단위(column-oriented)' 구조를 한 건씩의 dict로 바꾼다.
        {"form": [...], "filingDate": [...]} → [{"form":..., "filingDate":...}, ...]
        """
        forms = block.get("form") or []
        for i in range(len(forms)):
            yield {
                "form": forms[i],
                "accession": block["accessionNumber"][i],
                "filing_date": block["filingDate"][i],
                "report_date": (block.get("reportDate") or [""] * len(forms))[i],
                "primary_doc": (block.get("primaryDocument") or [""] * len(forms))[i],
                "description": (block.get("primaryDocDescription") or [""] * len(forms))[i],
            }

    def download(self, cik, accession, doc):
        """공시원문을 받는다. EDGAR는 zip이 아니라 파일 하나씩 준다."""
        acc = accession.replace("-", "")
        return self.request(ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc))


# ─────────────────────────────────────────────────────────────
# 보고서 선별 (다운로드 탭)
# ─────────────────────────────────────────────────────────────

def form_matches(form, wanted):
    """
    '10-K'를 원하면 정정공시 '10-K/A'도 함께 받는다.
    DART에서 '[기재정정]사업보고서'를 함께 받는 것과 같은 취급이다.
    """
    form = (form or "").upper()
    for w in wanted:
        w = w.upper()
        if form == w or form.startswith(w + "/"):
            return True
    return False


def fiscal_year(row):
    """
    사업연도를 정한다.

    DART와 달리 추정이 필요 없다. reportDate가 결산일 그 자체다.
      Apple 10-K reportDate=2025-09-27 → FY2025
    비어 있는 드문 경우에만 접수일에서 역산한다.
    """
    rd = row.get("report_date") or ""
    if len(rd) >= 4 and rd[:4].isdigit():
        return int(rd[:4])

    fd = row.get("filing_date") or ""
    if len(fd) >= 7:
        year, month = int(fd[:4]), int(fd[5:7])
        return year - 1 if month <= 6 else year
    return 0


# ─────────────────────────────────────────────────────────────
# 회사 한 곳의 다년도 재무수치 수집 (companyfacts, 재무분석 탭)
# ─────────────────────────────────────────────────────────────

class CompanyNotReporting(Exception):
    """companyfacts에 10-K 연간 자료가 하나도 없는 경우.

    10-K 를 제출하지 않는 법인일 수 있습니다(외국민간발행인의 20-F,
    소규모 신규상장사의 자료 미비 등). dart_client.CompanyNotReporting과
    같은 뜻으로, 호출하는 쪽(peer_compare.py)에서 두 예외를 같은 방식으로
    처리할 수 있게 이름과 쓰임을 맞췄다.
    """


# 뽑을 항목 -> (us-gaap 태그 후보 목록, 기간값 여부)
#   기간값(True)  : 손익계산서·현금흐름표 항목. start~end 기간이 있고,
#                    분기값이 섞이지 않도록 연간(300~400일)인 것만 골라야 한다.
#   시점값(False) : 재무상태표 항목. end(결산일) 하나만 있으면 된다.
# 앞에 적은 태그부터 우선 사용하고, 그 태그에 해당 연도 값이 없을 때만
# 다음 태그로 넘어간다(연도별로 개별 판단 — 회사가 태그를 도중에 바꾸는
# 경우가 흔해서 태그 단위로 전부 버리면 값이 뭉텅 빠진다).
ITEM_SPECS = {
    "revenue": ([
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ], True),
    "operating_income": (["OperatingIncomeLoss"], True),
    "net_income": (["NetIncomeLoss"], True),
    "assets": (["Assets"], False),
    "liabilities": (["Liabilities"], False),
    "equity": ([
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ], False),
    "current_assets": (["AssetsCurrent"], False),
    "current_liabilities": (["LiabilitiesCurrent"], False),
    "cfo": (["NetCashProvidedByUsedInOperatingActivities"], True),
    "dep_amort": ([
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "DepreciationNonproduction",
    ], True),
    "cash": ([
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ], False),
    "short_debt": ([
        "ShortTermBorrowings",
        "CommercialPaper",
        "OtherShortTermBorrowings",
    ], False),
    "current_portion_debt": (["LongTermDebtCurrent"], False),
    "long_debt": (["LongTermDebtNoncurrent", "LongTermDebt"], False),
    "lease_current": ([
        "OperatingLeaseLiabilityCurrent",
        "FinanceLeaseLiabilityCurrent",
    ], False),
    "lease_noncurrent": ([
        "OperatingLeaseLiabilityNoncurrent",
        "FinanceLeaseLiabilityNoncurrent",
    ], False),
}


def _pick_unit(units, preferred="USD"):
    """
    units는 {"USD": [관측치, ...], "USD/shares": [...]} 같은 dict다.
    선호 단위가 있으면 그것을, 없으면 첫 번째 단위를 쓰고 어느 단위였는지도 돌려준다.
    """
    if preferred and preferred in units:
        return preferred, units[preferred]
    if units:
        first_key = next(iter(units))
        return first_key, units[first_key]
    return None, []


def _days_between(start, end):
    """ISO 날짜 문자열 두 개의 일수 차이. 형식이 이상하면 None."""
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return None


def _collect_tag_observations(node, is_duration, preferred_unit="USD"):
    """
    단일 us-gaap(또는 dei) 태그의 units 아래에서 10-K/연간(FY) 관측치만 골라
    {회계연도: {"val":, "end":, "unit":}} 로 정리한다.

    - form이 '10-K'로 시작(10-K/A 정정공시 포함)하고 fp가 'FY'인 것만 쓴다.
    - 기간값은 start~end가 300~400일인 것만 남긴다(분기값 혼입 방지).
    - 같은 fy에 값이 여럿이면 end가 가장 늦은(가장 최근에 정정된) 것을 쓴다.
    """
    units = node.get("units") or {}
    unit_name, observations = _pick_unit(units, preferred_unit)

    by_fy = {}
    for obs in observations:
        if not str(obs.get("form") or "").startswith("10-K"):
            continue
        if obs.get("fp") != "FY":
            continue

        fy = obs.get("fy")
        val = obs.get("val")
        end = obs.get("end") or ""
        if fy is None or val is None or not end:
            continue

        if is_duration:
            start = obs.get("start") or ""
            days = _days_between(start, end)
            if days is None or not (300 <= days <= 400):
                continue

        existing = by_fy.get(fy)
        if existing is None or end > existing["end"]:
            by_fy[fy] = {"val": val, "end": end, "unit": unit_name}

    return by_fy


def _pick_value_by_year(sources, is_duration, preferred_unit="USD"):
    """
    (facts dict, 태그) 쌍의 우선순위 목록을 순서대로 보되, 연도 단위로 병합한다.
    앞선 태그에 어떤 연도 값이 없을 때만 그 연도를 다음 태그에서 채운다.
    """
    merged = {}
    for facts_dict, tag in sources:
        node = (facts_dict or {}).get(tag)
        if not node:
            continue
        by_fy = _collect_tag_observations(node, is_duration, preferred_unit)
        for fy, obs in by_fy.items():
            merged.setdefault(fy, obs)   # 이미 앞선 태그로 채워진 연도는 건드리지 않는다
    return merged


def _companyfacts_cache_path(cache_dir, cik):
    # 회사당 파일 하나(응답이 커서 연도별로 쪼개지 않는다). CORPCODE.xml처럼
    # 자료가 자주 바뀌지 않는 편이지만, 최신 사업연도가 추가될 수 있으니
    # 오래된 캐시를 지우고 다시 받으면 최신화된다.
    return os.path.join(cache_dir, f"edgar_{cik}.json")


def _load_companyfacts(client, cik, cache_dir=None, log=None):
    """companyfacts 원본(JSON 전체)을 받는다. 있으면 캐시를 먼저 본다."""
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        path = _companyfacts_cache_path(cache_dir, cik)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if log:
                log("  · 저장된 companyfacts 자료를 사용합니다")
            return data

    url = COMPANYFACTS_URL.format(cik=cik)
    try:
        raw = client.request(url)
    except EdgarError as e:
        # companyfacts 자체가 없다는 것은(404) 대개 10-K를 내지 않는 법인이라는 뜻이다.
        raise CompanyNotReporting(
            f"CIK{cik:010d}: companyfacts 조회 실패({e}). "
            "10-K 를 제출하지 않는 법인일 수 있습니다."
        )

    data = json.loads(raw.decode("utf-8"))

    if cache_dir:
        path = _companyfacts_cache_path(cache_dir, cik)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    return data


def collect_company(client, corp, years, log=None, cache_dir=None):
    """
    회사 한 곳의 연도별 재무수치를 모은다. dart_client.collect_company()와
    같은 자리에 끼워 쓸 수 있는 형태로 반환한다(반환 dict의 shape은 맞추되,
    "years" 안의 내용물은 다르다 — 모듈 상단 docstring 참고).

    corp 는 EdgarIndex.find()가 돌려주는 dict
    ({"source":"edgar","cik":...,"corp_name":...,"ticker":...}).

    cache_dir 를 주면 companyfacts 응답을 회사당 파일 하나로 저장해 두었다가
    다음 실행에서 재사용한다(파일이 커서 DART처럼 연도·구분별로 쪼개지 않는다).
    """
    cik = corp["cik"]
    facts = _load_companyfacts(client, cik, cache_dir=cache_dir, log=log)

    usgaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})
    years_wanted = set(years)

    year_data = {}     # {연도: {"revenue": 값, ...}}
    period_end = {}     # {연도: "YYYY-MM-DD"}
    currency = None

    for key, (tags, is_duration) in ITEM_SPECS.items():
        sources = [(usgaap, tag) for tag in tags]
        merged = _pick_value_by_year(sources, is_duration)
        for fy, obs in merged.items():
            if fy not in years_wanted:
                continue
            year_data.setdefault(fy, {})[key] = obs["val"]
            # 기간값·시점값 모두 end가 그 사업연도의 결산일이다(같은 fy면 값이 같아야 정상).
            period_end[fy] = max(period_end.get(fy, ""), obs["end"])
            if currency is None and obs.get("unit"):
                currency = obs["unit"]

    # 상장주식수(dei) — 시점값이며, 없으면 us-gaap의 발행/유통주식수로 대신한다.
    share_sources = [
        (dei, "EntityCommonStockSharesOutstanding"),
        (usgaap, "CommonStockSharesOutstanding"),
        (usgaap, "CommonStockSharesIssued"),
    ]
    share_merged = _pick_value_by_year(share_sources, is_duration=False, preferred_unit="shares")
    shares = {fy: obs["val"] for fy, obs in share_merged.items() if fy in years_wanted}

    if not year_data:
        raise CompanyNotReporting(
            f"{corp.get('corp_name', f'CIK{cik:010d}')}: companyfacts에 10-K 연간 자료가 없습니다. "
            "10-K 를 제출하지 않는 법인일 수 있습니다(20-F 제출 외국민간발행인 등)."
        )

    if log:
        for fy in sorted(year_data):
            log(f"  · {fy}년 {len(year_data[fy])}개 항목 (결산일 {period_end.get(fy, '?')})")

    fs_div_used = {fy: "CFS" for fy in year_data}   # EDGAR는 연결기준만 제공한다

    return {
        "source": "edgar",
        "corp_code": "",                       # DART 전용 필드. 자리만 맞춘다.
        "cik": cik,
        "corp_name": corp.get("corp_name") or facts.get("entityName", ""),
        "stock_code": corp.get("ticker", ""),  # 야후 조회 등에 쓸 티커
        "listed": corp.get("listed", True),
        "currency": currency or "USD",
        "years": year_data,
        "fs_div_used": fs_div_used,
        "shares": shares,
        "period_end": period_end,
        "profile": {
            "cik": cik,
            "ticker": corp.get("ticker", ""),
            "name": facts.get("entityName") or corp.get("corp_name", ""),
        },
    }
