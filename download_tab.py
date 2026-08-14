# -*- coding: utf-8 -*-
"""
공시원문 다운로드 탭
================================================

회사명과 사업연도만 넣으면 한국(DART)·미국(EDGAR) 공시를 한 번에 받아
`회사명/보고서종류_연도/` 로 정리한다.

만든 이유:
  감사 업무에서 피감회사와 비교대상 회사의 과거 공시를 받는 일이 반복되는데,
  DART 웹에서 연도별로 하나씩 내려받으면 회사 5곳 × 5개년만 해도 클릭이
  100번을 넘는다. 비상장 외부감사대상 법인은 사업보고서를 제출하지 않아
  '감사보고서 단독공시'를 따로 찾아야 하는데, 이 경로를 모르면 웹에서는
  아예 검색이 되지 않는다.

인증정보와 기업코드 색인은 창(app.py)이 들고 있는 것을 그대로 쓴다.
"""

import os
import re
import time
import queue
import threading

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import ui_theme
import edgar_client as ec
from downloader import Downloader, REPORT_TYPES


DEFAULT_OUT_DIR = "다운로드"


class DownloadTab(ttk.Frame):

    def __init__(self, parent, shared):
        super().__init__(parent, style="Plane.TFrame", padding=(14, 12))
        self.shared = shared
        self.queue = queue.Queue()      # 작업 스레드 → 화면 전달용
        self.downloader = None
        self.worker = None

        self._build()
        self._poll()

        # 창이 뜨자마자 기업코드 색인을 백그라운드에서 읽어 둔다.
        # (분석 탭도 같은 색인을 쓰므로 어느 쪽이 먼저 부르든 읽기는 한 번뿐이다)
        threading.Thread(target=self._prepare_index, daemon=True).start()
        shared.on_credentials_changed(self._on_credentials_changed)

    # ── 화면 ────────────────────────────────────────────
    def _build(self):
        # 세로로만 쌓으면 창 높이가 1000px을 넘어 로그가 화면 밖으로 밀린다.
        # 왼쪽은 설정(폭 고정), 오른쪽은 진행 로그로 나눈다.
        left = ttk.Frame(self, style="Plane.TFrame", width=620)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = ttk.Frame(self, style="Plane.TFrame")
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        # ★ 실행 줄과 상태를 먼저 아래쪽에 붙여 둔다.
        #   카드부터 쌓으면 글꼴이 큰 PC에서 카드가 길어져 [다운로드 시작]이
        #   화면 밖으로 밀려난다. 아래를 먼저 잡아 두면 그럴 일이 없다.
        self.status_var = tk.StringVar(value="기업목록을 읽는 중입니다…")
        ttk.Label(left, textvariable=self.status_var, style="Plane.TLabel").pack(
            side="bottom", anchor="w", pady=(6, 0))

        run = ttk.Frame(left, style="Plane.TFrame")
        run.pack(side="bottom", fill="x", pady=(4, 0))
        self.start_btn = ttk.Button(run, text="다운로드 시작", style="Run.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="중지", style="Quiet.TButton",
                                   command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        self.progress = ttk.Progressbar(run, mode="determinate",
                                        style="Thin.Horizontal.TProgressbar")
        self.progress.pack(side="left", fill="x", expand=True, padx=(14, 0))

        # 회사
        corp = ui_theme.titled_card(left, "회사", "한글은 DART, 영문·티커는 EDGAR")
        self.corp_text = tk.Text(corp, height=3, font=ui_theme.fonts["body"],
                                 bg="#ffffff", fg=ui_theme.INK, relief="flat",
                                 highlightthickness=1,
                                 highlightbackground=ui_theme.FIELD_LINE,
                                 highlightcolor=ui_theme.ACCENT,
                                 padx=10, pady=8, insertbackground=ui_theme.INK,
                                 wrap="word")
        self.corp_text.pack(fill="x")
        self.corp_text.insert("1.0", "삼성전자, Apple")
        ttk.Label(corp,
                  text="여러 개면 줄바꿈 또는 쉼표로 구분  ·  비상장 미국 법인은 CIK 직접 입력",
                  style="Hint.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(8, 0))

        # 조건
        opt = ui_theme.titled_card(left, "조건")

        years = ttk.Frame(opt, style="Card.TFrame")
        years.pack(fill="x")
        ttk.Label(years, text="사업연도", style="Card.TLabel").pack(side="left")
        self.year_from = tk.StringVar(value="2021")
        self.year_to = tk.StringVar(value="2025")
        ttk.Entry(years, textvariable=self.year_from, width=7).pack(side="left", padx=(12, 6))
        ttk.Label(years, text="~", style="Hint.TLabel").pack(side="left")
        ttk.Entry(years, textvariable=self.year_to, width=7).pack(side="left", padx=(6, 12))
        ttk.Label(years, text="공시연도가 아니라 결산 사업연도 기준",
                  style="Hint.TLabel").pack(side="left")

        kinds = ttk.Frame(opt, style="Card.TFrame")
        kinds.pack(fill="x", pady=(14, 0))
        self.type_vars = {}
        for key, spec in REPORT_TYPES.items():
            var = tk.BooleanVar(value=key in ("annual", "audit"))
            self.type_vars[key] = var
            ttk.Checkbutton(kinds, text=spec["label"], variable=var).pack(
                side="left", padx=(0, 12))
        ttk.Label(opt,
                  text="감사보고서 = 비상장 단독공시 포함  ·  미국은 10-K / 20-F / 10-Q",
                  style="Hint.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(8, 0))

        after = ttk.Frame(opt, style="Card.TFrame")
        after.pack(fill="x", pady=(14, 0))
        self.unzip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(after, text="zip 자동 압축해제", variable=self.unzip_var).pack(
            side="left", padx=(0, 18))
        self.html_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(after, text="읽을 수 있는 HTML로 변환", variable=self.html_var).pack(
            side="left", padx=(0, 18))
        # 세 개를 한 줄에 둔다 — 줄을 늘리면 아래의 [다운로드 시작]이 밀려난다
        self.excel_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(after, text="재무제표 엑셀", variable=self.excel_var).pack(
            side="left")
        ttk.Label(opt,
                  text="재무제표 엑셀 = 재무상태표·손익계산서·자본변동표·현금흐름표를 "
                       "원문 표 그대로 시트별로 (DART 공시만)",
                  style="Hint.TLabel", wraplength=560, justify="left").pack(
            anchor="w", pady=(8, 0))

        # 저장 위치
        out = ui_theme.titled_card(left, "저장 위치")
        self.out_var = tk.StringVar(
            value=os.path.join(self.shared.base_dir, DEFAULT_OUT_DIR))
        ttk.Entry(out, textvariable=self.out_var).pack(side="left", fill="x", expand=True)
        ttk.Button(out, text="찾아보기", style="Quiet.TButton",
                   command=self._choose_dir).pack(side="left", padx=(10, 0))

        # 진행 상황
        log_card = ui_theme.titled_card(right, "진행 상황", expand=True, pady=(0, 0))
        wrapper, self.log_text = ui_theme.log_widget(log_card, height=8, wrap="none")
        wrapper.pack(fill="both", expand=True)

    # ── 로그·큐 (스레드 안전) ───────────────────────────
    def log(self, message, tag=None):
        """작업 스레드에서 호출된다. tkinter는 스레드 안전하지 않으므로
        직접 위젯을 건드리지 않고 큐에 넣기만 한다."""
        self.queue.put(("log", (message, tag)))

    def status(self, message):
        self.queue.put(("status", message))

    def _poll(self):
        """화면 스레드에서 100ms마다 큐를 비운다."""
        try:
            while True:
                kind, payload = self.queue.get_nowait()

                if kind == "log":
                    message, tag = payload
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", message + "\n", tag or ())
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")

                elif kind == "status":
                    self.status_var.set(payload)

                elif kind == "progress":
                    done, total = payload
                    self.progress["maximum"] = total
                    self.progress["value"] = done
                    self.status_var.set(f"내려받는 중… {done}/{total}")

                elif kind == "finish":
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ── 준비 ────────────────────────────────────────────
    def _prepare_index(self):
        """기업코드 색인을 읽는다(창이 들고 있는 것을 함께 쓴다)."""
        try:
            index = self.shared.corp_index(log=self.log)
            self.log(f"준비 완료: 국내 법인 {index.count:,}곳\n", "ok")
            self.status(f"DART 기업목록 {index.count:,}곳 준비 완료")
        except Exception as e:
            self.log(f"[오류] {e}", "warn")
            self.status("기업목록을 읽지 못했습니다 — 인증키를 확인하세요")

    def _on_credentials_changed(self):
        """위쪽에서 인증정보를 저장했을 때. 키가 없어 못 읽었으면 다시 시도한다."""
        if self.shared.loaded_corp_index is None and self.shared.api_key:
            threading.Thread(target=self._prepare_index, daemon=True).start()

    def _choose_dir(self):
        folder = filedialog.askdirectory(initialdir=self.out_var.get())
        if folder:
            self.out_var.set(folder)

    # ── 실행 ────────────────────────────────────────────
    def _start(self):
        raw = self.corp_text.get("1.0", "end")
        names = [n.strip() for n in re.split(r"[,\n]", raw) if n.strip()]
        if not names:
            messagebox.showwarning("확인", "회사명을 입력해 주세요.")
            return

        type_keys = [k for k, v in self.type_vars.items() if v.get()]
        if not type_keys:
            messagebox.showwarning("확인", "받을 보고서 종류를 하나 이상 선택해 주세요.")
            return

        try:
            year_from, year_to = int(self.year_from.get()), int(self.year_to.get())
            if year_from > year_to:
                raise ValueError
        except ValueError:
            messagebox.showwarning("확인", "연도를 확인해 주세요.")
            return

        # 입력을 공시원별로 가른다. 한글이 있으면 DART, 없으면 EDGAR로 본다.
        has_dart = any(not ec.looks_like_edgar(n) for n in names)
        has_edgar = any(ec.looks_like_edgar(n) for n in names)

        # 필요한 쪽의 준비물만 확인한다.
        # (미국 회사만 받을 때 DART 인증키를 요구하면 쓸 수 없다)
        if has_dart:
            if not self.shared.api_key:
                messagebox.showwarning(
                    "확인", "DART 인증키를 위쪽 인증정보에 넣어 주세요 (40자리).")
                return
            if self.shared.loaded_corp_index is None:
                messagebox.showinfo("잠시만", "기업코드를 아직 읽는 중입니다.")
                return
        if has_edgar and not self._ensure_edgar():
            return

        corps = self._resolve(names)
        if not corps:
            self.log("받을 회사가 없습니다.\n")
            return

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        self.downloader = Downloader(
            self.shared.dart() if has_dart else None,
            self.out_var.get(), self.log,
            unzip=self.unzip_var.get(), make_html=self.html_var.get(),
            make_excel=self.excel_var.get(),
            edgar=self.shared.edgar() if has_edgar else None,
        )

        self.worker = threading.Thread(
            target=self._work, args=(corps, year_from, year_to, type_keys), daemon=True)
        self.worker.start()

    def _ensure_edgar(self):
        """미국 회사가 섞여 있을 때만 EDGAR 목록을 준비한다."""
        try:
            index = self.shared.edgar_index(log=self.log)
        except Exception as e:
            messagebox.showwarning("확인", str(e))
            return False
        self.log(f"EDGAR 기업목록 준비 완료: {index.count:,}개 (티커 보유 상장사)")
        return True

    def _resolve(self, names):
        """회사명 → 고유번호. 입력한 순서를 유지한다."""
        corps = []
        for name in names:
            if ec.looks_like_edgar(name):
                hits = self.shared.loaded_edgar_index.find(name)
                if not hits:
                    self.log(f"[찾을 수 없음] {name}"
                             "  — EDGAR 목록에는 티커 있는 상장사만 있습니다. "
                             "CIK를 직접 넣어보세요.", "warn")
                    continue
                corp = hits[0]
                ticker = f", {corp['ticker']}" if corp.get("ticker") else ""
                self.log(f"[확인] {name} -> {corp['corp_name']} "
                         f"(EDGAR, CIK {corp['cik']:010d}{ticker})")
            else:
                hits = self.shared.loaded_corp_index.find(name)
                if not hits:
                    self.log(f"[찾을 수 없음] {name}", "warn")
                    continue
                corp = hits[0]
                kind = "상장" if corp["listed"] else "비상장"
                self.log(f"[확인] {name} -> {corp['corp_name']} ({kind}, {corp['corp_code']})")

            if len(hits) > 1:
                others = ", ".join(c["corp_name"] for c in hits[1:4])
                self.log(f"        유사한 회사도 있습니다: {others}")
            corps.append(corp)
        return corps

    def _work(self, corps, year_from, year_to, type_keys):
        self.log(f"\n=== 다운로드 시작 (사업연도 {year_from}~{year_to}) ===", "head")
        self.status("내려받는 중…")
        started = time.time()
        try:
            stats = self.downloader.run(
                corps, year_from, year_to, type_keys,
                progress=lambda done, total: self.queue.put(("progress", (done, total))),
            )
            elapsed = time.time() - started
            self.log(
                f"\n=== 완료 ===\n"
                f"  받음 {stats['ok']}건 / 건너뜀 {stats['skip']}건 / 실패 {stats['fail']}건\n"
                f"  소요 {elapsed:.1f}초\n"
                f"  저장 위치: {self.out_var.get()}\n", "ok")
            self.status(f"완료 — 받음 {stats['ok']}건 · 건너뜀 {stats['skip']}건 · "
                        f"실패 {stats['fail']}건 ({elapsed:.0f}초)")
        except Exception as e:
            self.log(f"[중단] {e}", "warn")
            self.status(f"중단됨 — {e}")
        finally:
            self.queue.put(("finish", None))

    def _stop(self):
        if self.downloader:
            self.downloader.stop_flag.set()
            self.log("중지 요청... 진행 중인 파일까지 마치고 멈춥니다.")
