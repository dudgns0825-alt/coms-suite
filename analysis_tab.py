# -*- coding: utf-8 -*-
"""
비교기업 재무분석 탭
================================================

대상회사와 비교기업을 적어 넣으면 공시에서 재무제표를 받아
매출액·영업이익·EBITDA·EV/EBITDA 등을 나란히 놓고 보는 HTML 리포트를 만든다.
(COMS = comparable companies, 비교기업)

[처리 흐름]
  회사명 --(1)--> 고유번호 --(2)--> 연도별 재무제표 --(3)--> 지표 --(4)--> HTML
        CORPCODE.xml      fnlttSinglAcntAll        metrics       report
        (한글 입력)
        EDGAR_TICKERS     companyfacts
        (영문·티커 입력)

  여기에 두 가지가 더해진다.
   · 시가총액 — 주가는 공시에 없으므로 야후 파이낸스에서 받아
     '종가 × 상장주식수'로 계산한다(market_client).
   · 감가상각비 — 한국 대형사는 재무제표 본문에 없어 사업보고서 원문의
     주석에서 읽어 온다(note_reader).

인증정보와 기업코드 색인은 창(app.py)이 들고 있는 것을 그대로 쓴다.
"""

import os
import sys
import queue
import datetime
import threading
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox

import ui_theme
from ui_theme import INK, INK_SOFT, SURFACE, LINE, ACCENT
import dart_client as dc
import edgar_client as ec
import market_client as mkt
import note_reader
import metrics
import report


# 수기입력 단위 — 통화마다 자릿수가 달라 입력하기 편한 단위를 쓴다
INPUT_UNITS = {"KRW": (1e8, "억원"), "USD": (1e6, "백만달러")}


def default_latest_year(today=None):
    today = today or datetime.date.today()
    return today.year - 1 if today.month >= 4 else today.year - 2


class AnalysisTab(ttk.Frame):

    def __init__(self, parent, shared):
        super().__init__(parent, style="Plane.TFrame", padding=(16, 14))
        self.shared = shared

        self.queue = queue.Queue()      # 작업 스레드 → 화면 전달용
        self.index = None               # DART CorpIndex (창에서 받아 온다)
        self.edgar_index = None         # EDGAR EdgarIndex (미국 회사를 넣을 때만)
        self.client = None              # dart_client.DartClient
        self.edgar = None               # edgar_client.EdgarClient
        self.reader = None              # note_reader.NoteReader
        self.market = mkt.MarketClient()
        self.contact = ""
        self.busy = False
        self.collected = []             # 수집 결과 (수기입력 후 다시 쓴다)
        self.note_values = {}           # 원문 주석에서 자동으로 읽은 값
        self.job_years = []
        self.as_of = datetime.date.today()
        self.avg_days = 1

        self.cache_dir = os.path.join(shared.base_dir, "cache")
        self.output_dir = os.path.join(shared.base_dir, "리포트")

        self._build()
        self.after(100, self._drain)
        self._start_prepare()
        shared.on_credentials_changed(self._on_credentials_changed)

    @staticmethod
    def _card(parent):
        """흰 바탕에 얇은 테두리를 두른 묶음 — 화면을 항목별로 끊어 준다"""
        return ui_theme.card(parent)

    # ── 화면 구성 ────────────────────────────────────────
    def _build(self):
        # 제목줄과 인증정보는 창(app.py)이 두 탭 위에 한 벌만 두고 있다.
        body = ttk.Frame(self, style="Plane.TFrame")
        body.pack(fill="both", expand=True)
        # 왼쪽(회사)은 늘어나고 오른쪽(조건)은 내용이 들어갈 만큼 고정 폭을 지킨다
        body.columnconfigure(0, weight=1, minsize=420)
        body.columnconfigure(1, weight=0, minsize=360)
        # 위(입력)와 아래(진행 상황)가 남는 높이를 2:1로 나눠 갖는다.
        # 위쪽에만 무게를 주지 않으면, 창이 낮을 때 로그창이 화면 밖으로 밀린다.
        body.rowconfigure(0, weight=0)      # 입력 카드는 제 높이를 지키고
        body.rowconfigure(2, weight=1)      # 남는 높이는 진행 상황이 갖는다

        # ── 회사 ──
        card, box = self._card(body)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        ttk.Label(box, text="회사", style="Section.TLabel").pack(anchor="w")
        ttk.Label(box, text="한글은 DART(한국), 영문·티커는 EDGAR(미국)에서 찾습니다",
                  style="Hint.TLabel").pack(anchor="w", pady=(1, 9))

        ttk.Label(box, text="대상회사", style="Field.TLabel").pack(anchor="w")
        self.target_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.target_var,
                  font=("Malgun Gothic", 11)).pack(fill="x", pady=(2, 9), ipady=3)

        ttk.Label(box, text="비교기업 · 한 줄에 한 곳씩 (최대 7곳)",
                  style="Field.TLabel").pack(anchor="w")
        self.peers = tk.Text(box, height=5, font=("Malgun Gothic", 10),
                             relief="solid", bd=1, highlightthickness=0,
                             bg="#ffffff", fg=INK, insertbackground=INK, padx=6, pady=5)
        self.peers.pack(fill="both", expand=True, pady=(2, 0))

        # ── 조건 ──
        card, box = self._card(body)
        card.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        ttk.Label(box, text="조건", style="Section.TLabel").pack(anchor="w")
        ttk.Label(box, text="공시연도가 아니라 결산 사업연도 기준입니다",
                  style="Hint.TLabel").pack(anchor="w", pady=(1, 9))

        # 항목이름과 입력칸을 한 줄에 둔다. 줄마다 이름을 위에 얹으면
        # 카드가 100px 넘게 길어져 아래 진행 상황이 밀려난다.
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="사업연도", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 7))
        latest = default_latest_year()
        self.year_from = tk.StringVar(value=str(latest - 4))
        self.year_to = tk.StringVar(value=str(latest))
        years = ttk.Frame(grid, style="Card.TFrame")
        years.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(0, 7))
        ttk.Spinbox(years, from_=2015, to=2100, width=6,
                    textvariable=self.year_from).pack(side="left")
        ttk.Label(years, text="  ~  ", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(years, from_=2015, to=2100, width=6,
                    textvariable=self.year_to).pack(side="left")

        ttk.Label(grid, text="재무제표", style="Field.TLabel").grid(
            row=1, column=0, sticky="w", pady=(0, 7))
        self.fs_var = tk.StringVar(value=dc.FS_CONSOLIDATED)
        fs_box = ttk.Frame(grid, style="Card.TFrame")
        fs_box.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(0, 7))
        ttk.Radiobutton(fs_box, text="연결 우선", value=dc.FS_CONSOLIDATED,
                        variable=self.fs_var).pack(side="left")
        ttk.Radiobutton(fs_box, text="별도", value=dc.FS_SEPARATE,
                        variable=self.fs_var).pack(side="left", padx=(12, 0))

        ttk.Separator(grid, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(3, 9))

        ttk.Label(grid, text="기준일", style="Field.TLabel").grid(row=3, column=0, sticky="w")
        asof = ttk.Frame(grid, style="Card.TFrame")
        asof.grid(row=3, column=1, sticky="w", padx=(10, 0))
        self.asof_var = tk.StringVar(value=datetime.date.today().isoformat())
        ttk.Entry(asof, textvariable=self.asof_var, width=11,
                  font=("Malgun Gothic", 10)).pack(side="left")
        for label, delta in (("오늘", 0), ("1개월", 30), ("1년", 365)):
            ttk.Button(asof, text=label, style="Quiet.TButton", width=5,
                       command=lambda d=delta: self._set_asof(d)).pack(side="left", padx=(4, 0))

        ttk.Label(grid, text="적용 주가", style="Field.TLabel").grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        self.avg_var = tk.StringVar(value=mkt.AVG_CHOICES[0][0])
        ttk.Combobox(grid, textvariable=self.avg_var, state="readonly", width=15,
                     values=[label for label, _d in mkt.AVG_CHOICES]).grid(
            row=4, column=1, sticky="w", padx=(10, 0), pady=(8, 0))

        ttk.Label(box, text="기준일 하루 종가는 그날 변동에 휘둘립니다. 평균을 권합니다.",
                  style="Hint.TLabel", wraplength=330).pack(anchor="w", pady=(6, 0))

        self.cache_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="받은 자료 저장해 두고 다시 쓰기",
                        variable=self.cache_var).pack(anchor="w", pady=(9, 0))

        # ── 실행 줄 ──
        card, box = self._card(body)
        card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        self.run_btn = ttk.Button(box, text="자료 받고 리포트 만들기",
                                  style="Run.TButton", command=self._run)
        self.run_btn.pack(side="left")
        self.run_btn.state(["disabled"])
        ttk.Button(box, text="리포트 폴더", style="Quiet.TButton",
                   command=self._open_folder).pack(side="left", padx=8)
        self.status = ttk.Label(box, text="준비 중…", style="Hint.TLabel")
        self.status.pack(side="left", padx=14)
        self.progress = ttk.Progressbar(box, mode="determinate", length=200,
                                        style="Thin.Horizontal.TProgressbar")
        self.progress.pack(side="right")

        # ── 진행 상황 ──
        card, box = self._card(body)
        card.grid(row=2, column=0, columnspan=2, sticky="nsew")
        ttk.Label(box, text="진행 상황", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        wrap, self.log_text = ui_theme.log_widget(box, height=5)
        wrap.pack(fill="both", expand=True)

    def _set_asof(self, days_back):
        self.asof_var.set((datetime.date.today() - datetime.timedelta(days=days_back)).isoformat())

    # ── 로그·큐 ──────────────────────────────────────────
    def log(self, message, tag=None):
        """작업 스레드에서도 호출된다. tkinter는 스레드 안전하지 않으므로
        큐에만 넣고 실제 화면 갱신은 메인 스레드(_drain)가 한다."""
        self.queue.put(("log", (message, tag)))

    def _drain(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()

                if kind == "log":
                    message, tag = payload
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", message + "\n", tag or ())
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")

                elif kind == "progress":
                    self.progress["value"] = payload

                elif kind == "status":
                    self.status.configure(text=payload)

                elif kind == "ready":
                    self.run_btn.state(["!disabled"])

                elif kind == "collected":
                    # 수집이 끝났다. 아직 비어 있는 항목이 있으면 여기서 입력을 받는다.
                    self._after_collect()

                elif kind == "done":
                    self._finish("완료")
                    if messagebox.askyesno(
                            "완료", f"리포트를 만들었습니다.\n\n{payload}\n\n지금 열어볼까요?"):
                        webbrowser.open("file:///" + payload.replace("\\", "/"))

                elif kind == "error":
                    self._finish("중단됨", reset=True)
                    messagebox.showerror("오류", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _finish(self, status, reset=False):
        self.busy = False
        self.run_btn.state(["!disabled"])
        self.progress["value"] = 0 if reset else 100
        self.status.configure(text=status)

    # ── 준비: 인증키 + 기업코드 색인 ─────────────────────
    def _start_prepare(self):
        threading.Thread(target=self._prepare, daemon=True).start()

    def _prepare(self):
        """
        인증정보와 기업코드 색인을 창(app.py)에서 받아 온다.
        색인은 다운로드 탭과 같은 것을 쓰므로, 저쪽이 이미 읽어 두었으면
        기다렸다가 그대로 돌려받는다(30MB를 두 번 읽지 않는다).
        """
        try:
            self.client = self.shared.dart()
            self.contact = self.shared.edgar_contact

            if self.client is None:
                self.queue.put(("status", "인증키 필요"))
                self.log("DART 인증키가 없습니다 — 위쪽 인증정보에 40자리 인증키를 "
                         "넣고 [저장]을 눌러 주세요.", "warn")
                return

            self.log("인증키를 읽었습니다.")
            if self.contact:
                self.log(f"EDGAR 연락처: {self.contact}")
            else:
                self.log("EDGAR 연락처가 없습니다 — 한국 회사만 조회할 수 있습니다.", "warn")

            self.queue.put(("status", "색인 만드는 중"))
            self.index = self.shared.corp_index(log=self.log)
            self.log(f"준비 완료 — 국내 법인 {self.index.count:,}곳을 검색할 수 있습니다.\n", "ok")
            self.queue.put(("status", "준비 완료"))
            self.queue.put(("ready", None))

        except Exception as e:
            self.queue.put(("error", f"준비 중 오류가 발생했습니다.\n\n{e}"))

    def _on_credentials_changed(self):
        """위쪽에서 인증정보를 저장했을 때. 아직 준비가 안 됐으면 다시 시도한다."""
        self.contact = self.shared.edgar_contact
        self.edgar = None
        self.edgar_index = None
        if self.index is None:
            self._start_prepare()

    def _ensure_edgar(self):
        """
        미국 회사를 처음 넣었을 때만 EDGAR 목록을 준비한다.
        한국 회사만 볼 사람에게 SEC 접속을 강요하지 않기 위해서다.
        """
        if self.edgar_index is not None:
            return True

        try:
            self.edgar_index = self.shared.edgar_index(log=self.log)
            self.edgar = self.shared.edgar()
            self.log(f"EDGAR 상장사 {self.edgar_index.count:,}곳을 검색할 수 있습니다.")
            return True
        except Exception as e:
            messagebox.showerror("EDGAR 준비", str(e))
            return False

    # ── 회사 확정 (메인 스레드에서 처리) ─────────────────
    def _resolve(self, name):
        """
        회사명으로 후보를 찾는다. 여러 곳이 걸리면 사용자가 고르게 한다.
        한글이 있으면 DART, 없으면 EDGAR로 보낸다.
        """
        if ec.looks_like_edgar(name):
            if not self._ensure_edgar():
                return None
            hits = self.edgar_index.find(name)
            source = "edgar"
        else:
            hits = self.index.find(name)
            source = "dart"

        if not hits:
            where = "EDGAR" if source == "edgar" else "DART"
            messagebox.showerror("검색 실패", f"{where}에서 '{name}' 을(를) 찾지 못했습니다.")
            return None

        corp = hits[0] if len(hits) == 1 else PickDialog(self.winfo_toplevel(), name, hits[:40]).result
        if corp is not None:
            corp.setdefault("source", source)
        return corp

    # ── 실행 ─────────────────────────────────────────────
    def _run(self):
        if self.busy or not self.index:
            return

        target_name = self.target_var.get().strip()
        if not target_name:
            messagebox.showwarning("입력 확인", "대상회사를 입력해 주세요.")
            return

        peer_names = [line.strip() for line in
                      self.peers.get("1.0", "end").splitlines() if line.strip()]
        if not peer_names:
            messagebox.showwarning("입력 확인", "비교기업을 한 곳 이상 입력해 주세요.")
            return

        names = [target_name] + peer_names
        if len(names) > report.MAX_COMPANIES:
            messagebox.showwarning(
                "입력 확인",
                f"대상회사를 포함해 {report.MAX_COMPANIES}곳까지 비교할 수 있습니다.\n"
                f"지금 {len(names)}곳이 입력되어 있습니다.")
            return

        try:
            y_from, y_to = int(self.year_from.get()), int(self.year_to.get())
        except ValueError:
            messagebox.showwarning("입력 확인", "사업연도를 숫자로 입력해 주세요.")
            return
        if y_from > y_to:
            y_from, y_to = y_to, y_from

        try:
            self.as_of = datetime.date.fromisoformat(self.asof_var.get().strip())
        except ValueError:
            messagebox.showwarning("입력 확인",
                                   "시가총액 기준일을 YYYY-MM-DD 형식으로 입력해 주세요.")
            return
        if self.as_of > datetime.date.today():
            messagebox.showwarning("입력 확인", "시가총액 기준일이 오늘보다 뒤입니다.")
            return

        self.avg_days = dict(mkt.AVG_CHOICES).get(self.avg_var.get(), 1)

        corps, seen = [], set()
        for name in names:
            corp = self._resolve(name)
            if corp is None:
                return
            mark = (corp.get("source"), corp.get("corp_code") or corp.get("cik"))
            if mark in seen:
                messagebox.showwarning("입력 확인", f"{corp['corp_name']} 이(가) 중복되었습니다.")
                return
            seen.add(mark)
            corps.append(corp)

        self.busy = True
        self.run_btn.state(["disabled"])
        self.progress["value"] = 0
        self.job_years = list(range(y_from, y_to + 1))
        self.note_values = {}
        threading.Thread(target=self._work_collect, args=(corps,), daemon=True).start()

    # ── 1단계 : 공시 수집 + 주석 보충 ────────────────────
    def _work_collect(self, corps):
        try:
            cache_dir = self.cache_dir if self.cache_var.get() else None
            fs_pref = self.fs_var.get()
            years = self.job_years

            self.queue.put(("status", "공시 수집 중"))
            collected, skipped = [], []
            for i, corp in enumerate(corps):
                where = "EDGAR" if corp.get("source") == "edgar" else "DART"
                self.log(f"[{i + 1}/{len(corps)}] {corp['corp_name']} ({where})", "head")
                try:
                    if corp.get("source") == "edgar":
                        got = ec.collect_company(self.edgar, corp, years,
                                                 log=self.log, cache_dir=cache_dir)
                    else:
                        got = dc.collect_company(self.client, corp, years, fs_pref=fs_pref,
                                                 log=self.log, cache_dir=cache_dir)
                except (dc.CompanyNotReporting, ec.CompanyNotReporting) as e:
                    # 보고서를 제출하지 않는 법인 — 오류가 아니라 대상 밖이다
                    self.log(f"  ! 건너뜀: {e}", "warn")
                    skipped.append(corp["corp_name"])
                    continue

                collected.append(got)
                self.queue.put(("progress", (i + 1) / len(corps) * 50))

            if not collected:
                raise RuntimeError(
                    "재무제표를 받은 회사가 없습니다.\n\n"
                    "한국은 사업보고서 또는 감사보고서를 공시하는 법인(비상장 외감법인 포함),\n"
                    "미국은 10-K 제출법인을 조회할 수 있습니다.")

            if skipped and corps[0]["corp_name"] in skipped:
                raise RuntimeError(f"대상회사({skipped[0]})의 재무제표를 받지 못했습니다.")
            if skipped:
                self.log(f"\n※ 자료가 없어 제외한 회사: {', '.join(skipped)}", "warn")

            self.collected = collected
            self._fill_from_notes(cache_dir)
            self.queue.put(("collected", None))

        except Exception as e:
            self.queue.put(("error", str(e)))

    # 원문에서 찾아 볼 항목 — (키, 이름, NoteReader 메서드 이름)
    NOTE_LOOKUPS = (
        ("dep_amort", "감가상각비", "dep_amort"),
        ("total_debt", "이자부부채", "total_debt"),
    )

    def _fill_from_notes(self, cache_dir):
        """
        재무제표 본문에 감가상각비·차입금이 없는 회사·연도는 사업보고서 원문의
        XBRL 태그에서 읽어 온다. 원문은 용량이 크므로 필요한 것만 받는다.

        ★ 그 해 보고서에 없으면 다음 해 보고서(전기 비교표시분)까지 본다.
          주석 태깅이 FY2025 보고서부터 전면 적용돼, FY2024 감가상각비는
          FY2024 보고서가 아니라 FY2025 보고서에 들어 있다.
        """
        self.reader = note_reader.NoteReader(self.client, cache_dir=cache_dir)
        years = self.job_years
        targets = []

        for got in self.collected:
            if got.get("source") == "edgar":
                continue          # 미국은 본문에 다 들어 있다
            series = metrics.build_series(got, years)
            receipts = {y: (row.get("rcept_no") or "")
                        for y, row in series["rows"].items()}
            for year in sorted(series["rows"]):
                row = series["rows"][year]
                # 주석 표가 맞는지 대조할 본문 금액 (연결/별도·당기/전기·단위 확인용)
                anchors = metrics.note_anchors(got["years"][year], row["items"])
                for key, label, method in self.NOTE_LOOKUPS:
                    if row["items"].get(key) is None:
                        targets.append((got, receipts, year, row["fs_div"],
                                        anchors, key, label, method))

        if not targets:
            return

        self.queue.put(("status", "사업보고서 주석 확인 중"))
        self.log(f"\n재무제표 본문에 없는 {len(targets)}칸을 "
                 "사업보고서 원문에서 찾습니다.", "head")

        for i, (got, receipts, year, fs_div, anchors, key, label, method) in enumerate(targets):
            name = got["corp_name"]
            candidates = [receipts.get(year),
                          self._next_receipt(got, receipts, year, cache_dir)]
            value, source, errors = self.reader.lookup(
                method, candidates, year, fs_div, anchors)

            for message in errors:
                self.log(f"  · {name} {year} {label} 원문 조회 실패: {message}", "warn")

            if value is not None:
                self.note_values.setdefault(name, {}).setdefault(year, {})[key] = value
                origin = "" if source == receipts.get(year) else f" ({year + 1}년 보고서 비교표시분)"
                self.log(f"  · {name} {year} {label} {value / 1e8:,.0f}억원 확보{origin}", "ok")
            self.queue.put(("progress", 50 + (i + 1) / len(targets) * 20))

    def _next_receipt(self, got, receipts, year, cache_dir):
        """
        다음 해 사업보고서의 접수번호. 요청 기간 밖이면 그때 한 번 조회해 둔다
        (아직 공시되지 않았으면 빈 문자열이 되고 다시 묻지 않는다).
        """
        if year + 1 not in receipts:
            try:
                receipts[year + 1] = dc.annual_receipt(
                    self.client, got["corp_code"], year + 1,
                    fs_pref=self.fs_var.get(), cache_dir=cache_dir)
            except Exception:
                receipts[year + 1] = ""
        return receipts[year + 1]

    # ── 2단계 : 남은 항목 직접 입력 (메인 스레드) ────────
    def _after_collect(self):
        """
        EBITDA·EV를 구하려면 감가상각비와 차입금이 있어야 하는데,
        본문에도 주석에도 없으면 사용자가 넣는 수밖에 없다.
        그런 칸만 모아 사업보고서를 보고 채울 수 있게 한다.
        """
        years = self.job_years
        gaps = []
        for got in self.collected:
            series = metrics.build_series(
                got, years, notes=self.note_values.get(got["corp_name"]))
            for year in sorted(series["rows"]):
                items = series["rows"][year]["items"]
                for key in ("dep_amort", "total_debt"):
                    if items.get(key) is None:
                        gaps.append((got["corp_name"], got.get("currency", "KRW"), year, key))

        overrides = {}
        if gaps:
            self.log(f"\n아직 비어 있는 항목이 {len(gaps)}칸 있습니다.")
            overrides = ManualInputDialog(self.winfo_toplevel(), gaps).result or {}
            if overrides:
                filled = sum(len(v) for v in overrides.values())
                self.log(f"직접 입력한 값 {filled}칸을 반영합니다.", "ok")
            else:
                self.log("입력 없이 진행합니다 — 해당 칸의 EBITDA·EV 지표는 비어 있게 됩니다.")

        threading.Thread(target=self._work_report, args=(overrides,), daemon=True).start()

    # ── 3단계 : 시장자료 + 리포트 ────────────────────────
    def _work_report(self, overrides):
        try:
            years = self.job_years
            series_list = []

            for got in self.collected:
                series = metrics.build_series(
                    got, years,
                    overrides=overrides.get(got["corp_name"]),
                    notes=self.note_values.get(got["corp_name"]))
                series_list.append(series)

            base_currency = series_list[0].get("currency", "KRW")
            self.queue.put(("status", "주가 조회 중"))
            self.log(f"\n주가를 받는 중… (기준일 {self.as_of}, {self.avg_var.get()})", "head")
            for i, series in enumerate(series_list):
                metrics.apply_market_data(series, self.market, self.as_of, self.avg_days,
                                          base_currency=base_currency, log=self.log)
                m = series["market"]
                if m.get("market_cap") is not None:
                    self.log(f"  · {series['name']} {m['ticker']} "
                             f"{m['price']:,.0f}{m['currency']} × {m['shares']:,}주")
                self.queue.put(("progress", 70 + (i + 1) / len(series_list) * 25))

            os.makedirs(self.output_dir, exist_ok=True)
            today = datetime.date.today().isoformat()
            safe = "".join(ch for ch in series_list[0]["name"]
                           if ch not in r'\/:*?"<>|').strip()
            out_path = os.path.join(self.output_dir, f"{safe}_비교분석_{today}.html")

            self.queue.put(("status", "리포트 만드는 중"))
            path = report.build_report(series_list, years, series_list[0]["name"],
                                       fs_pref=self.fs_var.get(), out_path=out_path)
            self.log(f"저장: {path}", "ok")
            self.queue.put(("done", path))

        except Exception as e:
            self.queue.put(("error", str(e)))

    def _open_folder(self):
        os.makedirs(self.output_dir, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(self.output_dir)          # noqa: S606  (윈도우 탐색기)
        else:
            webbrowser.open("file://" + self.output_dir)


class PickDialog(tk.Toplevel):
    """같은 이름의 법인이 여러 곳일 때 고르게 하는 창"""

    def __init__(self, parent, keyword, candidates):
        super().__init__(parent)
        self.title("회사 선택")
        self.configure(bg=SURFACE)
        self.result = None
        self.candidates = candidates
        self.transient(parent)
        self.grab_set()
        self.geometry("560x420")

        ttk.Label(self, text=f"'{keyword}' 검색 결과", style="Section.TLabel").pack(
            anchor="w", padx=16, pady=(16, 2))
        ttk.Label(self, text="한 곳을 골라 주세요.", style="Hint.TLabel").pack(
            anchor="w", padx=16, pady=(0, 10))

        wrap = tk.Frame(self, bg=LINE)
        wrap.pack(fill="both", expand=True, padx=16)
        self.listbox = tk.Listbox(wrap, font=("Malgun Gothic", 10), relief="flat", bd=0,
                                  bg="#ffffff", fg=INK, selectbackground=ACCENT,
                                  selectforeground="#ffffff", highlightthickness=0,
                                  activestyle="none")
        self.listbox.pack(fill="both", expand=True, padx=1, pady=1)
        for corp in candidates:
            if corp.get("source") == "edgar" or corp.get("cik"):
                mark = corp.get("ticker") or f"CIK{corp.get('cik')}"
            else:
                mark = corp["stock_code"] if corp["listed"] else "비상장"
            self.listbox.insert("end", f"  {corp['corp_name']}   ·  {mark}")
        self.listbox.selection_set(0)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._ok())

        box = ttk.Frame(self, style="Card.TFrame", padding=(16, 12))
        box.pack(fill="x")
        ttk.Button(box, text="선택", style="Run.TButton", command=self._ok).pack(side="right")
        ttk.Button(box, text="취소", style="Quiet.TButton",
                   command=self.destroy).pack(side="right", padx=8)

        self.wait_window(self)

    def _ok(self):
        sel = self.listbox.curselection()
        if sel:
            self.result = self.candidates[sel[0]]
        self.destroy()


class ManualInputDialog(tk.Toplevel):
    """
    본문에도 주석에도 없어 자동으로 못 채운 감가상각비·차입금을 직접 넣는 창.

    차입금을 '금융부채'로 묶어 표시하는 회사(LG디스플레이)나
    주석 XBRL 태깅이 없는 과거 사업연도가 여기로 온다.
    비워 두면 그 회사의 EBITDA·EV 지표만 비고 나머지는 그대로 나온다.
    """

    LABELS = {"dep_amort": "감가상각비·무형자산상각비",
              "total_debt": "이자부부채(차입금·사채·리스부채)"}

    def __init__(self, parent, gaps):
        super().__init__(parent)
        self.title("직접 입력")
        self.configure(bg=SURFACE)
        self.result = None
        self.entries = {}       # (회사, 연도, 항목) -> (Entry, 나눌값)
        self.transient(parent)
        self.grab_set()
        self.geometry("740x580")

        ttk.Label(self, text="직접 입력", style="Section.TLabel").pack(
            anchor="w", padx=18, pady=(16, 2))
        ttk.Label(
            self,
            text="아래 항목은 재무제표 본문에도, 사업보고서 주석의 XBRL 태그에도 없어 "
                 "자동으로 채우지 못했습니다. 사업보고서에서 찾아 넣으면 EBITDA·EV/EBITDA가 "
                 "계산됩니다. 비워 두어도 나머지 지표는 그대로 나옵니다.",
            style="Hint.TLabel", wraplength=690, justify="left").pack(
            anchor="w", padx=18, pady=(0, 12))

        # 창보다 내용이 길어지므로 스크롤 가능한 영역에 담는다
        outer = tk.Frame(self, bg=SURFACE)
        outer.pack(fill="both", expand=True, padx=18)
        canvas = tk.Canvas(outer, highlightthickness=0, bg=SURFACE, bd=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Card.TFrame")
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        # 자식 위젯 위에서도 휠이 먹으려면 전역 바인딩이 필요하다.
        # 창을 닫을 때 반드시 풀어 준다(안 풀면 사라진 캔버스를 계속 건드린다).
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        current = None
        for name, currency, year, key in gaps:
            if name != current:
                current = name
                unit_name = INPUT_UNITS.get(currency, (1.0, currency))[1]
                header = ttk.Frame(inner, style="Card.TFrame")
                header.pack(fill="x", pady=(14, 5))
                ttk.Label(header, text=name, style="Section.TLabel").pack(side="left")
                ttk.Label(header, text=f"   단위: {unit_name}",
                          style="Hint.TLabel").pack(side="left")
                ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=(0, 5))

            row = ttk.Frame(inner, style="Card.TFrame")
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=f"{year}년", width=8, style="Card.TLabel").pack(side="left")
            ttk.Label(row, text=self.LABELS[key], width=25,
                      style="Card.TLabel").pack(side="left")
            entry = ttk.Entry(row, width=20, font=("Malgun Gothic", 10))
            entry.pack(side="left", ipady=2)
            div = INPUT_UNITS.get(currency, (1.0, currency))[0]
            self.entries[(name, year, key)] = (entry, div)

        box = ttk.Frame(self, style="Card.TFrame", padding=(18, 14))
        box.pack(fill="x")
        ttk.Button(box, text="입력값 적용", style="Run.TButton",
                   command=self._ok).pack(side="right")
        ttk.Button(box, text="건너뛰기", style="Quiet.TButton",
                   command=self._skip).pack(side="right", padx=8)

        self.wait_window(self)

    def _ok(self):
        out, bad = {}, []
        for (name, year, key), (entry, div) in self.entries.items():
            text = entry.get().strip().replace(",", "")
            if not text:
                continue
            try:
                value = float(text) * div
            except ValueError:
                bad.append(f"{name} {year}년 {self.LABELS[key]}")
                continue
            out.setdefault(name, {}).setdefault(year, {})[key] = value

        if bad:
            messagebox.showwarning(
                "입력 확인", "숫자로 읽을 수 없는 칸이 있습니다:\n\n" + "\n".join(bad))
            return

        self.result = out
        self._close()

    def _skip(self):
        self.result = {}
        self._close()

    def _close(self):
        self.unbind_all("<MouseWheel>")
        self.destroy()


if __name__ == "__main__":
    set_dpi_aware()
    App().mainloop()
