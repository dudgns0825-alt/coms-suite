# -*- coding: utf-8 -*-
"""
공시원문 일괄 수집 엔진
================================================

선택한 회사·사업연도·보고서 종류를 돌면서 원문을 받아
`회사명/보고서종류_연도/회사명_사업연도_보고서명` 으로 저장한다.

화면(download_tab.py)과 떼어 두었다 — 여기에는 tkinter가 없고,
로그는 넘겨받은 log 함수로만 내보낸다.

[처리 흐름 — DART]
  회사명 --(1)--> corp_code --(2)--> 접수번호 목록 --(3)--> 공시원문 zip
        CORPCODE.xml       list.json              document.xml

미국 공시도 3단계 구조가 같으며 세부는 edgar_client.py 에 있다.
"""

import io
import os
import re
import zipfile
import threading

import dart_client
import dart_viewer     # 공시원문 XML -> 읽을 수 있는 HTML 변환
import edgar_client    # 미국 SEC(EDGAR)

PBLNTF_JEONGGI = dart_client.PBLNTF_JEONGGI
PBLNTF_GAMSA = dart_client.PBLNTF_GAMSA


# 받을 보고서 종류 정의
#   key   : GUI 체크박스 식별자
#   label : 폴더명에 쓸 이름
#   ty    : 공시유형 코드
#   match : report_nm 에서 이 보고서를 골라내는 정규식
#   edgar : 같은 성격의 미국 서식. 비어 있으면 EDGAR 회사에는 해당 없음.
#           미국은 반기보고서가 없고(10-Q가 1·2·3분기를 모두 담당),
#           감사보고서도 따로 제출하지 않는다(10-K 안에 감사의견이 포함된다).
REPORT_TYPES = {
    "annual":  {"label": "사업보고서", "ty": PBLNTF_JEONGGI, "match": r"사업보고서",
                "edgar": ["10-K", "20-F"]},
    "half":    {"label": "반기보고서", "ty": PBLNTF_JEONGGI, "match": r"반기보고서",
                "edgar": []},
    "quarter": {"label": "분기보고서", "ty": PBLNTF_JEONGGI, "match": r"분기보고서",
                "edgar": ["10-Q"]},
    "audit":   {"label": "감사보고서", "ty": PBLNTF_GAMSA,   "match": r"감사보고서",
                "edgar": []},
}

# EDGAR에서 해당 서식이 없을 때 한 번만 안내하고 넘어갈 문구
EDGAR_NO_FORM = {
    "half":  "미국은 반기보고서가 없습니다 (10-Q가 분기별로 대신합니다)",
    "audit": "미국은 감사보고서를 따로 제출하지 않습니다 (10-K 안에 감사의견이 포함됩니다)",
}

# 파일명에 쓸 수 없는 문자 (Windows)
INVALID_CHARS = r'[\\/:*?"<>|]'


def safe_name(name):
    return re.sub(INVALID_CHARS, "_", name).strip()


def build_stem(corp_name, fiscal_year, label, month=None):
    """
    저장 파일명을 '회사명_사업연도_보고서명' 으로 만든다.

    month를 주면 뒤에 '_MM월'을 덧붙인다. 한 사업연도에 같은 이름의 보고서가
    여러 건인 경우(분기보고서 3건, 10-Q 3건)에 서로 덮어쓰지 않게 하기 위함이다.
    """
    stem = f"{corp_name}_{fiscal_year}_{label}"
    if month:
        stem += f"_{month}월"
    return safe_name(stem)


def dart_report_label(report_nm):
    """
    DART 보고서명에서 파일명에 쓸 이름과 결산월을 뽑는다.

    보고서명 끝의 '(2023.12)'는 사업연도와 겹치므로 이름에서는 뗀다.
    다만 반기·분기보고서는 한 해에 여러 건이라 결산월이 있어야 구분되므로
    월을 따로 돌려준다.  예) '분기보고서 (2023.03)' -> ('분기보고서', '03')
    """
    m = re.search(r"\((\d{4})[.\-/](\d{2})\)", report_nm)
    label = re.sub(r"\s*\(\d{4}[.\-/]\d{2}\)\s*", "", report_nm).strip()
    label = re.sub(r"\s+", " ", label)
    month = m.group(2) if (m and re.search(r"반기|분기", label)) else None
    return label, month


def unique_stem(folder, stem, ext):
    """
    이미 같은 이름이 있으면 _2, _3 을 붙인다.

    같은 해에 정정공시가 두 번 나오는 등 이름이 겹칠 수 있다.
    이미 받은 공시는 .done 표식에서 걸러지므로, 여기까지 온 것은
    실제로 다른 공시다.
    """
    if not os.path.exists(os.path.join(folder, stem + ext)):
        return stem
    n = 2
    while os.path.exists(os.path.join(folder, f"{stem}_{n}{ext}")):
        n += 1
    return f"{stem}_{n}"


def extract_fiscal_year(report_nm, rcept_dt):
    """
    보고서명에서 '사업연도'를 뽑는다.

    주의 — 공시연도와 사업연도는 다르다.
      '사업보고서 (2023.12)' 가 2024년 3월에 공시된다.
    폴더를 공시연도로 만들면 2023년 자료가 2024 폴더에 들어가 헷갈리므로
    보고서명 괄호 안의 사업연도를 우선 사용한다.
    """
    m = re.search(r"\((\d{4})[.\-/](\d{2})\)", report_nm)
    if m:
        return int(m.group(1))

    # 감사보고서 단독공시는 '감사보고서제출' 처럼 기간 표기가 없는 경우가 있다.
    # 이때는 접수일 기준 직전 사업연도로 본다 (12월 결산은 익년 3월 제출).
    year = int(str(rcept_dt)[:4])
    month = int(str(rcept_dt)[4:6])
    return year - 1 if month <= 6 else year


class Downloader:
    """선택된 회사/연도/보고서종류를 순회하며 실제로 내려받는다."""

    def __init__(self, client, out_dir, log, unzip=True, make_html=True, edgar=None):
        self.client = client
        self.edgar = edgar          # EdgarClient. 미국 회사가 없으면 None이어도 된다.
        self.out_dir = out_dir
        self.log = log
        self.unzip = unzip
        self.make_html = make_html
        self.stop_flag = threading.Event()
        self.stats = {"ok": 0, "skip": 0, "fail": 0}
        self._warned = set()        # EDGAR에 없는 서식 안내를 회사마다 반복하지 않기 위함

    def run(self, corps, year_from, year_to, type_keys, progress=None):
        # 조회 기간은 사업연도보다 넉넉히 잡는다.
        # 2023년 사업보고서가 2024년에 공시되므로 종료일을 +1년 해야 빠지지 않는다.
        bgn_de = f"{year_from}0101"
        end_de = f"{year_to + 1}1231"

        jobs = [(c, k) for c in corps for k in type_keys]
        done = 0

        for corp, key in jobs:
            if self.stop_flag.is_set():
                self.log("\n[중지] 사용자가 중단했습니다.")
                break

            spec = REPORT_TYPES[key]
            self._process(corp, spec, bgn_de, end_de, year_from, year_to)

            done += 1
            if progress:
                progress(done, len(jobs))

        return self.stats

    def _process(self, corp, spec, bgn_de, end_de, year_from, year_to):
        corp_name = corp["corp_name"]
        label = spec["label"]

        # 미국 회사는 조회 경로가 완전히 다르므로 여기서 갈라진다.
        if corp.get("source") == "edgar":
            return self._process_edgar(corp, spec, year_from, year_to)

        try:
            items = self.client.search(corp["corp_code"], bgn_de, end_de, spec["ty"])
        except dart_client.DartError as e:
            self.log(f"  x {corp_name} / {label} 조회 실패: {e}")
            self.stats["fail"] += 1
            return

        # 보고서명으로 원하는 종류만 거른다.
        # '[기재정정]사업보고서' 같은 정정공시도 정규식에 걸리므로 함께 받는다.
        pattern = re.compile(spec["match"])
        targets = []
        for it in items:
            if not pattern.search(it["report_nm"]):
                continue
            fy = extract_fiscal_year(it["report_nm"], it["rcept_dt"])
            if year_from <= fy <= year_to:
                targets.append((fy, it))

        if not targets:
            self.log(f"  - {corp_name} / {label}: 해당 기간 공시 없음")
            return

        self.log(f"  > {corp_name} / {label}: {len(targets)}건")

        # 사업연도 오름차순. 같은 연도에 '감사보고서'와 '연결감사보고서'가 함께 있으므로
        # 보고서명까지 정렬키에 넣는다(dict끼리는 비교할 수 없다).
        for fy, it in sorted(targets, key=lambda x: (x[0], x[1]["report_nm"])):
            if self.stop_flag.is_set():
                return
            self._download_one(corp_name, label, fy, it)

    def _download_one(self, corp_name, label, fiscal_year, item):
        rcept_no = item["rcept_no"]
        folder = os.path.join(
            self.out_dir, safe_name(corp_name), f"{safe_name(label)}_{fiscal_year}"
        )
        os.makedirs(folder, exist_ok=True)

        # 이미 받은 것은 건너뛴다. 중단 후 재실행해도 이어받기가 된다.
        done_marker = os.path.join(folder, f".done_{rcept_no}")
        if os.path.exists(done_marker):
            self.log(f"      · {fiscal_year} {item['report_nm']} (이미 받음)")
            self.stats["skip"] += 1
            return

        try:
            blob = self.client.document(rcept_no)
        except dart_client.DartError as e:
            self.log(f"      x {fiscal_year} {item['report_nm']} 실패: {e}")
            self.stats["fail"] += 1
            return

        # 파일명은 접수번호 대신 '회사명_사업연도_보고서명' 으로 붙인다.
        # 조서에 그대로 옮겨도 무엇인지 알아볼 수 있어야 한다.
        label, month = dart_report_label(item["report_nm"])
        stem = unique_stem(folder, build_stem(corp_name, fiscal_year, label, month), ".zip")

        with open(os.path.join(folder, stem + ".zip"), "wb") as f:
            f.write(blob)

        if self.unzip:
            try:
                # extractall은 zip 안의 원래 이름(접수번호_문서번호.xml)을 그대로 쓰므로
                # 한 건씩 꺼내 우리 이름으로 저장한다.
                with zipfile.ZipFile(io.BytesIO(blob)) as z:
                    members = [n for n in z.namelist() if not n.endswith("/")]
                    for i, name in enumerate(members):
                        ext = os.path.splitext(name)[1] or ".xml"
                        suffix = "" if len(members) == 1 else f"_{i + 1}"
                        with open(os.path.join(folder, stem + suffix + ext), "wb") as f:
                            f.write(z.read(name))
            except zipfile.BadZipFile:
                self.log(f"      ! {rcept_no} 압축 해제 실패 (zip은 보관됨)")

        # 원문 XML은 DART 자체 스키마라 그냥 열면 태그만 보인다.
        # 바로 읽을 수 있도록 HTML로 변환해 함께 저장한다.
        if self.make_html:
            try:
                dart_viewer.convert_zip(blob, folder, stem=stem)
            except Exception as e:
                self.log(f"      ! {rcept_no} HTML 변환 실패: {e}")

        # 정상 완료된 뒤에만 표식을 남긴다.
        # 다운로드 도중 죽으면 표식이 없으므로 다음 실행 때 다시 받는다.
        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(f"{item['report_nm']}\t{item['rcept_dt']}\n")

        self.log(f"      + {fiscal_year} {item['report_nm']}")
        self.stats["ok"] += 1

    # ── EDGAR(미국) ────────────────────────────────────────
    def _process_edgar(self, corp, spec, year_from, year_to):
        """
        DART용 _process 와 같은 역할.

        다른 점은 세 가지뿐이다.
          · 공시목록을 기간이 아니라 회사 단위로 통째 받고 우리가 걸러낸다
          · 사업연도를 추정하지 않는다 (reportDate가 결산일 그 자체)
          · 원문이 zip이 아니라 HTML 파일 하나다
        """
        corp_name = corp["corp_name"]
        label = spec["label"]
        forms = spec.get("edgar") or []

        # 미국에 대응 서식이 없는 종류(반기·감사보고서)는 안내만 하고 넘어간다.
        if not forms:
            key = next((k for k, v in REPORT_TYPES.items() if v is spec), None)
            if key and key not in self._warned:
                self._warned.add(key)
                self.log(f"  - EDGAR: {label} 건너뜀 — {EDGAR_NO_FORM.get(key, '해당 서식 없음')}")
            return

        if self.edgar is None:
            self.log(f"  x {corp_name}: EDGAR 연락처가 설정되지 않았습니다")
            self.stats["fail"] += 1
            return

        try:
            real_name, rows = self.edgar.list_filings(corp["cik"], since_year=year_from)
        except edgar_client.EdgarError as e:
            self.log(f"  x {corp_name} / {label} 조회 실패: {e}")
            self.stats["fail"] += 1
            return

        if real_name:
            corp_name = real_name        # CIK만 입력한 경우 여기서 실제 회사명이 채워진다

        targets = []
        for row in rows:
            if not edgar_client.form_matches(row["form"], forms):
                continue
            fy = edgar_client.fiscal_year(row)
            if year_from <= fy <= year_to:
                targets.append((fy, row))

        if not targets:
            self.log(f"  - {corp_name} / {label}: 해당 기간 공시 없음")
            return

        self.log(f"  > {corp_name} / {label}: {len(targets)}건")

        for fy, row in sorted(targets, key=lambda x: (x[0], x[1]["form"], x[1]["accession"])):
            if self.stop_flag.is_set():
                return
            self._download_one_edgar(corp, corp_name, fy, row)

    def _download_one_edgar(self, corp, corp_name, fiscal_year, row):
        acc = row["accession"]
        doc = row["primary_doc"]

        # 폴더는 미국 서식명 그대로 쓴다 (10-K_2025). '사업보고서_2025'로 적으면
        # 나중에 조서에서 원문을 대조할 때 실제 서식과 이름이 달라 혼선이 생긴다.
        folder = os.path.join(
            self.out_dir, safe_name(corp_name), f"{safe_name(row['form'])}_{fiscal_year}"
        )
        os.makedirs(folder, exist_ok=True)

        done_marker = os.path.join(folder, f".done_{acc}")
        if os.path.exists(done_marker):
            self.log(f"      · {fiscal_year} {row['form']} (이미 받음)")
            self.stats["skip"] += 1
            return

        if not doc:
            self.log(f"      x {fiscal_year} {row['form']}: 원문 파일명이 없습니다 ({acc})")
            self.stats["fail"] += 1
            return

        try:
            blob = self.edgar.download(corp["cik"], acc, doc)
        except edgar_client.EdgarError as e:
            self.log(f"      x {fiscal_year} {row['form']} 실패: {e}")
            self.stats["fail"] += 1
            return

        # EDGAR 원문은 이미 HTML이라 변환할 필요가 없다. 그대로 저장하면 브라우저로 열린다.
        # 10-Q는 한 사업연도에 3건이라 결산월을 붙여 구분한다.
        rd = row.get("report_date") or ""
        month = rd[5:7] if (len(rd) >= 7 and row["form"].upper().startswith("10-Q")) else None
        ext = os.path.splitext(doc)[1] or ".htm"
        stem = unique_stem(folder, build_stem(corp_name, fiscal_year, row["form"], month), ext)

        with open(os.path.join(folder, stem + ext), "wb") as f:
            f.write(blob)

        with open(done_marker, "w", encoding="utf-8") as f:
            f.write(f"{row['form']}\t{row['filing_date']}\t{row['report_date']}\n")

        self.log(f"      + {fiscal_year} {row['form']} ({row['filing_date']} 접수)")
        self.stats["ok"] += 1
