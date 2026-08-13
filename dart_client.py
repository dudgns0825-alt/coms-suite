# -*- coding: utf-8 -*-
"""
OpenDART 호출 담당 (두 탭이 함께 쓴다)
================================================

회사명 -> 고유번호(corp_code) -> 공시목록 / 기업개황 / 재무제표 순으로 조회한다.

  공시원문 다운로드 탭 : search() 로 접수번호를 찾아 document() 로 원문 zip
  비교기업 재무분석 탭 : financials() / shares() / company()

CorpIndex(30MB 기업코드 색인)와 DartClient는 창 하나에 하나만 두고 두 탭이
나눠 쓴다 — 색인을 두 번 읽으면 그만큼 기다려야 하고 메모리도 두 배가 된다.

[재무제표 대상 범위]
  재무제표 API(fnlttSinglAcntAll)는 사업보고서·분반기보고서를 제출한 법인만
  값을 돌려주며, 사업보고서를 내지 않는 비상장 외감법인은 [013](데이터 없음)이
  된다. 이 경우는 오류가 아니라 '대상이 아님'으로 안내한다.
  (그런 회사도 감사보고서 단독공시는 있으므로 다운로드 탭에서는 받을 수 있다)
"""

import io
import os
import re
import json
import time
import zipfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


API_BASE = "https://opendart.fss.or.kr/api"

# 공시유형 코드 (OpenDART 명세)
#   A = 정기공시    : 사업보고서 / 반기보고서 / 분기보고서
#   F = 외부감사관련 : 감사보고서 / 연결감사보고서 (비상장 외감법인은 여기에만 존재)
PBLNTF_JEONGGI = "A"
PBLNTF_GAMSA = "F"

# 보고서 코드 (OpenDART 명세)
REPRT_ANNUAL = "11011"   # 사업보고서
REPRT_Q3 = "11014"       # 3분기보고서
REPRT_HALF = "11012"     # 반기보고서
REPRT_Q1 = "11013"       # 1분기보고서

# 재무제표 구분
FS_CONSOLIDATED = "CFS"  # 연결
FS_SEPARATE = "OFS"      # 별도

STATUS_MESSAGES = {
    "000": "정상",
    "010": "등록되지 않은 인증키입니다",
    "011": "사용할 수 없는 인증키입니다 (일시적 사용중지)",
    "012": "접근할 수 없는 IP입니다",
    "013": "조회된 데이터가 없습니다",
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한을 초과했습니다 (일 20,000건)",
    "021": "조회 가능한 회사 개수가 초과했습니다",
    "100": "부적절한 필드값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검 중입니다",
    "900": "정의되지 않은 오류입니다",
    "901": "사용자 계정의 개인정보 보호 정책에 따라 이용이 정지되었습니다",
}

# 조회 결과 없음(013)은 오류가 아니라 '그 연도에 해당 공시가 없음'을 뜻한다.
STATUS_NOT_FATAL = {"013", "014"}


class DartError(Exception):
    """API가 정상(000) 이외의 상태를 반환했을 때"""

    def __init__(self, status, message=""):
        self.status = status
        self.message = message or STATUS_MESSAGES.get(status, "알 수 없는 오류")
        super().__init__(f"[{status}] {self.message}")


# ─────────────────────────────────────────────────────────────
# 1단계 : 회사명 → 고유번호(corp_code)
# ─────────────────────────────────────────────────────────────

class CorpIndex:
    """
    DART가 배포하는 CORPCODE.xml(약 30MB, 11만개 이상 법인)로 회사명 색인을 만든다.

    30MB를 통째로 파싱(ET.parse)하면 트리 전체가 메모리에 올라가므로
    iterparse로 한 건씩 읽고 즉시 el.clear()로 비운다.
    """

    def __init__(self, xml_path):
        self.xml_path = xml_path
        self.by_name = {}   # 정규화된 회사명 -> [기업정보, ...]
        self.count = 0

    @staticmethod
    def normalize(name):
        """'(주)삼성전자 ' 와 '삼성전자' 를 같은 것으로 취급하기 위한 정규화"""
        name = name.strip()
        name = re.sub(r"\(주\)|㈜|주식회사", "", name)
        name = re.sub(r"\s+", "", name)
        return name.lower()

    def load(self, progress=None):
        for _event, el in ET.iterparse(self.xml_path):
            if el.tag != "list":
                continue

            corp = {
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "stock_code": (el.findtext("stock_code") or "").strip(),
            }
            # stock_code가 비어 있으면 비상장 법인이다.
            corp["listed"] = bool(corp["stock_code"])

            if corp["corp_name"]:
                self.by_name.setdefault(self.normalize(corp["corp_name"]), []).append(corp)
                self.count += 1

            el.clear()   # 메모리 해제 (이게 없으면 30MB 파일이 수백MB로 불어난다)

            if progress and self.count % 20000 == 0:
                progress(self.count)

        return self.count

    def find(self, name):
        """
        회사명으로 검색. 정확히 일치하는 것을 우선 반환하고,
        없으면 부분일치(포함)로 후보를 찾는다.
        """
        key = self.normalize(name)
        if key in self.by_name:
            return self._prefer_listed(self.by_name[key])

        hits = []
        for k, corps in self.by_name.items():
            if key and key in k:
                hits.extend(corps)
        return self._prefer_listed(hits)

    @staticmethod
    def _prefer_listed(corps):
        """동명 회사가 여러 개면 상장사를 먼저 보여준다 (대개 그쪽을 찾는다)"""
        return sorted(corps, key=lambda c: (not c["listed"], c["corp_name"]))


# ─────────────────────────────────────────────────────────────
# 2단계 : OpenDART API 호출
# ─────────────────────────────────────────────────────────────

class DartClient:
    """OpenDART API 호출 담당. 재시도와 호출간격 제어를 포함한다."""

    def __init__(self, api_key, delay=0.3, max_retry=3):
        self.api_key = api_key
        self.delay = delay          # 연속 호출 사이 간격(초)
        self.max_retry = max_retry
        self._last_call = 0.0

    def _throttle(self):
        gap = time.time() - self._last_call
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last_call = time.time()

    def _request(self, endpoint, params):
        params = dict(params, crtfc_key=self.api_key)
        url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"

        last_err = None
        for attempt in range(self.max_retry):
            self._throttle()
            try:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    return resp.read()
            except Exception as e:
                last_err = e
                # 일시적 네트워크 오류는 점점 더 오래 기다렸다가 재시도(지수 백오프)
                time.sleep(2 ** attempt)

        raise DartError("900", f"네트워크 오류: {last_err}")

    def _json(self, endpoint, params):
        data = json.loads(self._request(endpoint, params).decode("utf-8"))
        status = data.get("status")
        if status != "000":
            if status in STATUS_NOT_FATAL:
                return None          # 데이터 없음 → 호출한 쪽에서 건너뛴다
            raise DartError(status, data.get("message"))
        return data

    def search(self, corp_code, bgn_de, end_de, pblntf_ty):
        """
        공시목록 조회(list.json) — 다운로드 탭이 접수번호를 찾는 데 쓴다.
        한 번에 최대 100건이므로 total_page만큼 반복해서 전부 가져온다.
        """
        results = []
        page = 1
        while True:
            data = json.loads(self._request("list.json", {
                "corp_code": corp_code,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": pblntf_ty,
                "page_no": page,
                "page_count": 100,
            }).decode("utf-8"))

            status = data.get("status")
            if status != "000":
                if status in STATUS_NOT_FATAL:
                    return results          # 공시 없음 → 빈 목록
                raise DartError(status, data.get("message"))

            results.extend(data.get("list", []))

            if page >= int(data.get("total_page", 1)):
                break
            page += 1

        return results

    def company(self, corp_code):
        """기업개황(company.json) — 업종·대표자·결산월·설립일"""
        return self._json("company.json", {"corp_code": corp_code})

    def financials(self, corp_code, year, reprt_code=REPRT_ANNUAL, fs_div=FS_CONSOLIDATED):
        """
        단일회사 전체 재무제표(fnlttSinglAcntAll).
        반환은 계정 행 리스트이며, 데이터가 없으면 None.
        """
        data = self._json("fnlttSinglAcntAll.json", {
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        })
        return data.get("list") if data else None

    def shares(self, corp_code, year, reprt_code=REPRT_ANNUAL):
        """
        주식의 총수 현황(stockTotqySttus). 시가총액을 구하는 데 쓴다.

        보통주·우선주·합계가 각각 한 줄로 오며, 여기서는 '보통주 발행총수'를 쓴다.
        자기주식은 빼지 않는다 — 거래소가 발표하는 시가총액도 상장주식수(자기주식 포함)
        기준이라 그쪽에 맞췄다.

        반환: {"common": 발행총수, "preferred": 우선주 발행총수 or None}
        우선주가 따로 상장된 회사(삼성전자 등)는 보통주 시가총액만으로는
        기업가치가 과소평가되므로, 우선주가 있으면 리포트 각주에서 알린다.
        """
        data = self._json("stockTotqySttus.json", {
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": reprt_code,
        })
        if not data:
            return None

        out = {"common": None, "preferred": None}
        for row in data.get("list", []):
            kind = (row.get("se") or "").strip()
            raw = (row.get("istc_totqy") or "").replace(",", "").strip()
            if not raw.isdigit():
                continue
            count = int(raw)
            if kind.startswith("보통주"):
                out["common"] = count
            elif kind.startswith("우선주") and count > 0:
                out["preferred"] = count

        return out if out["common"] else None

    def document(self, rcept_no):
        """
        공시원문(document.xml)을 받는다. zip 안에 본문과 첨부가 들어 있다.

        재무제표 API가 주지 않는 주석 수치를 여기서 얻는다.
        실패하면 zip이 아니라 XML 오류 응답이 오므로 앞부분을 보고 구분한다.
        """
        raw = self._request("document.xml", {"rcept_no": rcept_no})
        if raw[:2] != b"PK":          # zip 파일은 항상 'PK'로 시작한다
            m = re.search(rb"<status>(\d+)</status>", raw[:500])
            raise DartError(m.group(1).decode() if m else "900")
        return raw

    def download_corpcode(self):
        """
        전체 법인의 고유번호 목록(CORPCODE.xml)을 받는다.
        zip으로 오며 압축을 풀면 약 30MB다.
        """
        raw = self._request("corpCode.xml", {})
        # zip 파일은 항상 'PK'로 시작한다. 아니면 오류 XML이다.
        if raw[:2] != b"PK":
            m = re.search(rb"<status>(\d+)</status>", raw[:500])
            raise DartError(m.group(1).decode() if m else "900")

        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return z.read("CORPCODE.xml")


# ─────────────────────────────────────────────────────────────
# 3단계 : 회사 한 곳의 다년도 재무제표 수집
# ─────────────────────────────────────────────────────────────

class CompanyNotReporting(Exception):
    """사업보고서 공시대상법인이 아니어서 재무제표 API에 자료가 없는 경우"""


def _cache_path(cache_dir, corp_code, year, fs_div):
    return os.path.join(cache_dir, f"{corp_code}_{year}_{fs_div}.json")


def annual_receipt(client, corp_code, year, fs_pref=FS_CONSOLIDATED, cache_dir=None):
    """
    해당 사업연도 사업보고서의 접수번호만 얻는다. 없으면 빈 문자열.

    요청 기간 밖의 보고서를 주석 조회용으로 열어 볼 때 쓴다 — 보고서에는 전기
    비교표시분도 함께 태깅돼 있어, 마지막 연도의 감가상각비는 그 다음 해
    보고서에서 찾아야 한다(note_reader.lookup 참고).
    """
    if cache_dir:
        for div in (FS_CONSOLIDATED, FS_SEPARATE):
            path = _cache_path(cache_dir, corp_code, year, div)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    rows = json.load(f)
                return (rows[0].get("rcept_no") or "").strip() if rows else ""

    rows = client.financials(corp_code, year, fs_div=fs_pref)
    used = fs_pref
    if rows is None and fs_pref == FS_CONSOLIDATED:
        rows = client.financials(corp_code, year, fs_div=FS_SEPARATE)
        used = FS_SEPARATE
    if not rows:
        return ""

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        with open(_cache_path(cache_dir, corp_code, year, used), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)

    return (rows[0].get("rcept_no") or "").strip()


def collect_company(client, corp, years, fs_pref=FS_CONSOLIDATED, log=None, cache_dir=None):
    """
    회사 한 곳의 연도별 재무제표 원자료를 모은다.

    ★ 연도별로 따로 호출한다.
      한 번 호출하면 당기·전기·전전기 3개년이 함께 오므로 호출 수를 줄일 수는 있으나,
      전기 열의 값은 그 후 재분류·수정된 금액이라 당시 공시치와 다를 수 있다.
      조서 정합을 위해 '각 연도 보고서의 당기 열'만 사용한다.

    ★ 연결(CFS)이 없는 회사는 별도(OFS)로 자동 폴백한다.
      연결대상 종속회사가 없으면 연결재무제표 자체가 존재하지 않는다.

    cache_dir 를 주면 받은 응답을 파일로 남겨 두었다가 다음 실행에서 재사용한다.
    과거 사업연도 재무제표는 바뀌지 않으므로 같은 회사를 다시 조회할 때
    API 호출(일 20,000건 한도)을 아낄 수 있다.
    """
    result = {
        "source": "dart",
        "corp_code": corp["corp_code"],
        "corp_name": corp["corp_name"],
        "stock_code": corp["stock_code"],
        "listed": corp["listed"],
        "currency": "KRW",
        "years": {},          # {연도: [계정행, ...]}
        "fs_div_used": {},    # {연도: 'CFS' | 'OFS'}  연도마다 다를 수 있다
        "shares": {},         # {연도: 보통주 발행총수}
        "preferred": {},      # {연도: 우선주 발행총수}  있으면 각주로 알린다
        "profile": None,
    }

    try:
        result["profile"] = client.company(corp["corp_code"])
    except DartError as e:
        if log:
            log(f"  · 기업개황 조회 실패: {e}")

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)

    for year in years:
        rows, used, from_cache = None, fs_pref, False

        # 캐시 확인 — 연결/별도 어느 쪽으로 받아 두었든 찾는다
        if cache_dir:
            for div in ([fs_pref, FS_SEPARATE] if fs_pref == FS_CONSOLIDATED else [fs_pref]):
                path = _cache_path(cache_dir, corp["corp_code"], year, div)
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        rows = json.load(f)
                    used = div
                    from_cache = True
                    break

        if rows is None:
            rows = client.financials(corp["corp_code"], year, fs_div=fs_pref)
            used = fs_pref

            if rows is None and fs_pref == FS_CONSOLIDATED:
                rows = client.financials(corp["corp_code"], year, fs_div=FS_SEPARATE)
                used = FS_SEPARATE

            if rows is not None and cache_dir:
                path = _cache_path(cache_dir, corp["corp_code"], year, used)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rows, f, ensure_ascii=False)

        if rows is None:
            if log:
                log(f"  · {year}년 자료 없음")
            continue

        result["years"][year] = rows
        result["fs_div_used"][year] = used

        # 상장사만 시가총액이 의미가 있다. 비상장 사업보고서 제출사는 건너뛴다.
        if corp["listed"]:
            share_path = os.path.join(cache_dir, f"{corp['corp_code']}_{year}_shares.json") \
                if cache_dir else None
            counts = None
            if share_path and os.path.exists(share_path):
                with open(share_path, encoding="utf-8") as f:
                    counts = json.load(f)
            else:
                try:
                    counts = client.shares(corp["corp_code"], year)
                except DartError as e:
                    if log:
                        log(f"  · {year}년 주식총수 조회 실패: {e}")
                if share_path and counts:
                    with open(share_path, "w", encoding="utf-8") as f:
                        json.dump(counts, f)

            if counts:
                result["shares"][year] = counts.get("common")
                if counts.get("preferred"):
                    result["preferred"][year] = counts["preferred"]

        if log:
            label = "연결" if used == FS_CONSOLIDATED else "별도"
            note = " (저장된 자료)" if from_cache else ""
            log(f"  · {year}년 {label} {len(rows)}개 계정{note}")

    # 사업보고서를 내지 않는 비상장 외감법인은 API에 자료가 없다.
    # 그런 회사도 감사보고서 단독공시는 있으므로 그 원문에서 읽어 온다.
    missing = [year for year in years if year not in result["years"]]
    if missing:
        _collect_from_audit_reports(client, corp, missing, result,
                                    fs_pref=fs_pref, log=log, cache_dir=cache_dir)

    if not result["years"]:
        raise CompanyNotReporting(
            f"{corp['corp_name']}: 재무제표를 찾지 못했습니다. "
            "사업보고서도 감사보고서 단독공시도 없는 법인일 수 있습니다."
        )

    return result


def _collect_from_audit_reports(client, corp, years, result,
                                fs_pref=FS_CONSOLIDATED, log=None, cache_dir=None):
    """
    감사보고서 단독공시 원문에서 재무제표를 읽어 result 에 채운다.

    읽어 낸 계정 행은 재무제표 API와 같은 모양이라, 이 뒤의 처리
    (metrics·note_reader·report)는 상장사와 똑같은 길을 탄다.
    자세한 것은 audit_report.py 참고.
    """
    import audit_report
    import note_reader

    try:
        reports = audit_report.find_reports(client, corp["corp_code"], years, fs_pref)
    except DartError as e:
        if log:
            log(f"  · 감사보고서 조회 실패: {e}")
        return

    if not reports:
        return

    if log:
        log(f"  · 재무제표 API에 자료가 없어 감사보고서 원문에서 읽습니다 "
            f"({len(reports)}개년)")

    reader = note_reader.NoteReader(client, cache_dir=cache_dir)
    for year in sorted(reports):
        rcept_no, used = reports[year]
        try:
            statements = audit_report.read_statements(reader, rcept_no, year)
        except Exception as e:
            if log:
                log(f"  · {year}년 감사보고서 원문을 읽지 못했습니다: {e}")
            continue

        # 재무상태표와 손익계산서가 모두 확인돼야 비교에 쓸 수 있다
        if "BS" not in statements or "IS" not in statements:
            if log:
                log(f"  · {year}년 감사보고서에서 재무제표를 가려내지 못했습니다"
                    f"(읽은 것: {', '.join(sorted(statements)) or '없음'})")
            continue

        rows = audit_report.to_rows(statements, year, rcept_no)
        result["years"][year] = rows
        result["fs_div_used"][year] = used

        if cache_dir:
            with open(_cache_path(cache_dir, corp["corp_code"], year, used),
                      "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)

        if log:
            label = "연결" if used == FS_CONSOLIDATED else "별도"
            log(f"  · {year}년 {label} {len(rows)}개 계정 (감사보고서 원문)")
