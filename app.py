# -*- coding: utf-8 -*-
"""
COMS Suite — 공시원문 다운로드 · 비교기업 재무분석
================================================================

두 프로그램을 창 하나에 탭으로 합친 것이다.

  [공시원문 다운로드]  회사명·사업연도로 DART·EDGAR 공시를 일괄 수집
  [비교기업 재무분석]  같은 공시에서 재무제표를 뽑아 비교 리포트(HTML) 작성

합친 이유는 둘이 같은 것을 쓰기 때문이다 — 같은 인증정보, 같은 기업코드
색인(30MB), 같은 회사 검색. 따로 띄우면 색인을 두 번 읽고 인증키도 두 곳에
넣어야 한다. 여기서는 Shared 하나를 두 탭이 나눠 쓴다.

실행:
  python app.py
"""

import os
import sys
import ctypes
import threading

import tkinter as tk
from tkinter import ttk, messagebox

import settings
import ui_theme
import dart_client as dc
import edgar_client as ec
from download_tab import DownloadTab
from analysis_tab import AnalysisTab


APP_NAME = "COMS Suite"
# 작업 표시줄이 이 프로그램을 파이썬과 구분하는 이름
APP_ID = "COMS.Suite.DartEdgar"
APP_SUBTITLE = "공시원문 다운로드 · 비교기업 재무분석 · DART · EDGAR"

CORPCODE_FILE = "CORPCODE.xml"
ICON_ICO = "icon.ico"      # 윈도우 창·작업표시줄 아이콘
ICON_PNG = "logo.png"      # 머리말 로고 겸, ico 를 못 쓰는 OS의 아이콘


def base_directory():
    """
    설정·기업코드·리포트를 두는 폴더.

    PyInstaller로 exe를 만들면 소스는 임시폴더(_MEIPASS)에 풀리므로
    '실행파일이 놓인 폴더'를 따로 구해야 config.txt 를 제대로 찾는다.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def find_asset(base_dir, filename):
    """아이콘처럼 소스 옆에 두는 파일을 찾는다(exe면 _MEIPASS도 본다)."""
    for folder in (base_dir, getattr(sys, "_MEIPASS", None)):
        if folder:
            path = os.path.join(folder, filename)
            if os.path.exists(path):
                return path
    return None


def set_dpi_aware():
    """
    고DPI 화면에서 글씨가 흐려지는 것을 막는다.
    이걸 하지 않으면 윈도우가 창을 통째로 확대해 비트맵이 뭉갠다.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_app_id():
    """
    작업 표시줄에서 이 프로그램을 파이썬과 따로 세도록 고유 이름을 준다.

    이것을 주지 않으면 python.exe(또는 pythonw.exe)로 실행한 창이 모두 한 덩어리로
    묶여 작업 표시줄 아이콘도 파이썬이 되고, 고정해도 파이썬이 고정된다.
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


class Shared:
    """
    두 탭이 함께 쓰는 것 — 인증정보, DART/EDGAR 클라이언트, 기업코드 색인.

    ★ 색인은 한 번만 읽는다.
      CORPCODE.xml 은 30MB이고 파싱에 몇 초가 걸린다. 두 탭이 각자 읽으면
      그 시간과 메모리가 두 배가 된다. corp_index() 에 자물쇠를 걸어 두어,
      두 탭이 동시에 불러도 실제 읽기는 한 번만 일어나고 나중에 부른 쪽은
      먼저 읽은 결과를 그대로 받는다.
    """

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.api_key = settings.load_api_key(base_dir)
        self.edgar_contact = settings.load_edgar_contact(base_dir)

        self._dart = None
        self._index = None
        self._edgar = None
        self._edgar_index = None
        self._index_lock = threading.Lock()
        self._edgar_lock = threading.Lock()
        self._listeners = []        # 인증정보가 바뀌면 부를 함수들

    # ── 인증정보 ────────────────────────────────────────
    def on_credentials_changed(self, callback):
        self._listeners.append(callback)

    def save_credentials(self, api_key, edgar_contact):
        settings.save_config(self.base_dir, api_key=api_key, edgar_contact=edgar_contact)
        changed_key = api_key != self.api_key
        self.api_key = settings.load_api_key(self.base_dir)
        self.edgar_contact = settings.load_edgar_contact(self.base_dir)

        # 키가 바뀌었으면 그 키로 만들어 둔 클라이언트는 버린다
        if changed_key:
            self._dart = None
        self._edgar = None

        for callback in self._listeners:
            callback()

    # ── 클라이언트 ──────────────────────────────────────
    def dart(self):
        """DART 클라이언트. 인증키가 없으면 None."""
        if not self.api_key:
            return None
        if self._dart is None:
            self._dart = dc.DartClient(self.api_key)
        return self._dart

    def edgar(self):
        """EDGAR 클라이언트. 연락처가 없으면 None(SEC가 403으로 거절한다)."""
        if not self.edgar_contact:
            return None
        if self._edgar is None:
            self._edgar = ec.EdgarClient(self.edgar_contact)
        return self._edgar

    # ── 색인 ────────────────────────────────────────────
    @property
    def corpcode_path(self):
        return os.path.join(self.base_dir, CORPCODE_FILE)

    @property
    def loaded_corp_index(self):
        """이미 읽어 둔 색인. 아직이면 None — 화면에서 '준비됐는지'만 볼 때 쓴다."""
        return self._index

    @property
    def loaded_edgar_index(self):
        return self._edgar_index

    def corp_index(self, log=None):
        """
        기업코드 색인. 없으면 DART에서 받아 파일로 저장한 뒤 읽는다.
        작업 스레드에서 부른다(몇 초 걸린다).
        """
        with self._index_lock:
            if self._index is not None:
                return self._index

            if not os.path.exists(self.corpcode_path):
                client = self.dart()
                if client is None:
                    raise RuntimeError(
                        "DART 인증키가 없어 기업코드를 받을 수 없습니다. "
                        "위쪽 인증정보에 40자리 인증키를 넣고 [저장]을 눌러 주세요.")
                if log:
                    log("기업코드 파일이 없습니다. DART에서 받는 중… (최초 1회, 약 30MB)")
                data = client.download_corpcode()
                with open(self.corpcode_path, "wb") as f:
                    f.write(data)

            if log:
                log("기업코드 파일을 읽는 중…")
            index = dc.CorpIndex(self.corpcode_path)
            index.load()
            self._index = index
            return index

    def edgar_index(self, log=None):
        """EDGAR 기업목록(티커 보유 상장사). 없으면 SEC에서 받는다(약 0.8MB)."""
        with self._edgar_lock:
            if self._edgar_index is not None:
                return self._edgar_index

            client = self.edgar()
            if client is None:
                raise RuntimeError(
                    "EDGAR 연락처가 없습니다. 위쪽 인증정보에 '이름 이메일'을 넣어 주세요.\n"
                    "인증키가 아니라 SEC가 요구하는 신원 표시입니다(등록 절차 없음).")

            index = ec.EdgarIndex(os.path.join(self.base_dir, ec.TICKERS_FILE))
            index.ensure(client, log=log)
            index.load()
            self._edgar_index = index
            return index


class App(tk.Tk):

    def __init__(self, base_dir):
        super().__init__()
        self.base_dir = base_dir
        self.shared = Shared(base_dir)

        self.title(APP_NAME)
        self.geometry("1180x940")
        self.minsize(1040, 780)
        self._apply_icon()

        ui_theme.apply(self)
        self._build()

    def _apply_icon(self):
        """
        창과 작업표시줄 아이콘. 윈도우는 .ico 가 가장 선명하고,
        다른 OS이거나 .ico 가 없으면 PNG로 대신한다.
        아이콘이 없다고 프로그램이 못 뜰 이유는 없으므로 실패해도 넘어간다.
        """
        ico = find_asset(self.base_dir, ICON_ICO)
        if ico:
            try:
                self.iconbitmap(default=ico)
                return
            except tk.TclError:
                pass

        png = find_asset(self.base_dir, ICON_PNG)
        if png:
            try:
                # PhotoImage는 참조가 끊기면 사라지므로 인스턴스에 붙들어 둔다
                self._icon_image = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_image)
            except tk.TclError:
                pass

    # ── 화면 ────────────────────────────────────────────
    def _build(self):
        self._build_header()
        self._build_credentials()

        book = ttk.Notebook(self)
        book.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self.download_tab = DownloadTab(book, self.shared)
        self.analysis_tab = AnalysisTab(book, self.shared)
        book.add(self.download_tab, text="  공시원문 다운로드  ")
        book.add(self.analysis_tab, text="  비교기업 재무분석  ")

    def _build_header(self):
        """진한 띠에 제목 — 두 탭 위에 공통으로 놓는다."""
        head = ttk.Frame(self, style="Head.TFrame", padding=(20, 11))
        head.pack(fill="x")

        png = find_asset(self.base_dir, ICON_PNG)
        if png:
            try:
                self._logo = tk.PhotoImage(file=png).subsample(3, 3)
                tk.Label(head, image=self._logo, bg=ui_theme.INK, bd=0).pack(
                    side="left", padx=(0, 12))
            except tk.TclError:
                pass

        titles = ttk.Frame(head, style="Head.TFrame")
        titles.pack(side="left")
        ttk.Label(titles, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text=APP_SUBTITLE, style="Sub.TLabel").pack(anchor="w")

    def _build_credentials(self):
        """
        인증정보는 두 탭이 같은 것을 쓰므로 탭 밖(창 위)에 한 벌만 둔다.
        여기서 저장하면 두 탭이 곧바로 새 인증정보로 동작한다.
        """
        outer, box = ui_theme.card(self, padding=(16, 9))
        outer.pack(fill="x", padx=12, pady=(12, 6))

        line = ttk.Frame(box, style="Card.TFrame")
        line.pack(fill="x")

        ttk.Label(line, text="DART 인증키", style="Card.TLabel").pack(side="left")
        self.key_var = tk.StringVar(value=self.shared.api_key)
        ttk.Entry(line, textvariable=self.key_var, show="●", width=26).pack(
            side="left", padx=(8, 18))

        ttk.Label(line, text="EDGAR 연락처", style="Card.TLabel").pack(side="left")
        self.contact_var = tk.StringVar(value=self.shared.edgar_contact)
        ttk.Entry(line, textvariable=self.contact_var).pack(
            side="left", fill="x", expand=True, padx=(8, 10))

        ttk.Button(line, text="저장", style="Quiet.TButton",
                   command=self._save_credentials).pack(side="left")

        ttk.Label(box,
                  text="DART 인증키는 40자리입니다(opendart.fss.or.kr, 무료).  ·  "
                       "EDGAR는 인증키 없이 '이름 이메일'만 넣으면 됩니다.",
                  style="Hint.TLabel", wraplength=1000).pack(anchor="w", pady=(7, 0))

    def _save_credentials(self):
        key = self.key_var.get().strip()
        contact = self.contact_var.get().strip()

        if key and len(key) != 40:
            messagebox.showwarning("확인", "DART 인증키는 40자리입니다. 다시 확인해 주세요.")
            return
        if contact and "@" not in contact:
            messagebox.showwarning(
                "확인",
                "EDGAR 연락처는 '이름 이메일' 형식이어야 합니다.\n"
                "예: Hong Gildong hong@example.com")
            return

        self.shared.save_credentials(key, contact)
        messagebox.showinfo("저장", f"인증정보를 {settings.CONFIG_FILE} 에 저장했습니다.")


if __name__ == "__main__":
    set_dpi_aware()
    set_app_id()
    App(base_directory()).mainloop()
