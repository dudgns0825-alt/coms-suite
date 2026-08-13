# -*- coding: utf-8 -*-
"""
화면 색과 위젯 모양 (두 탭이 같은 옷을 입는다)
================================================

탭마다 스타일을 따로 만들면 ttk 스타일 이름이 겹쳐 나중에 만든 쪽이 먼저
만든 쪽의 색을 덮어쓴다. 그래서 팔레트와 스타일 정의를 여기 한 곳에 모으고
창이 뜰 때 한 번만 적용한다(apply).

색은 리포트(HTML)와 같은 계열로 맞췄다 — 프로그램과 결과물이 한 벌로 보이게.
"""

import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont


# ── 팔레트 ──────────────────────────────────────────────
INK = "#0b0b0b"          # 본문 글자
INK_SOFT = "#52514e"     # 보조 글자
INK_MUTED = "#898781"    # 설명 글자
SURFACE = "#ffffff"      # 카드 안쪽
PLANE = "#f4f4f1"        # 창 바탕
LINE = "#e1e0d9"         # 카드 테두리
FIELD_LINE = "#d5d4cc"   # 입력칸 테두리
ACCENT = "#2a78d6"       # 강조 (기본 동작 버튼, 진행률)
ACCENT_DARK = "#1c5cab"  # 강조 눌렸을 때
ACCENT_DIM = "#b9c9de"   # 강조 버튼이 잠겼을 때
SOFT = "#f7f7f5"         # 보조 버튼 바탕
SOFT_ACTIVE = "#eceae4"  # 보조 버튼 눌렸을 때
GOOD = "#006300"         # 성공 로그
BAD = "#c0392b"          # 경고 로그
LOG_BG = "#fbfbfa"       # 로그창 바탕

# ── 글꼴 ────────────────────────────────────────────────
# 설치돼 있지 않은 글꼴을 지정하면 tkinter가 조용히 엉뚱한 것으로 바꾸므로
# 후보를 순서대로 확인해서 고른다.
BODY_FAMILIES = ["맑은 고딕", "Malgun Gothic", "Segoe UI"]
MONO_FAMILIES = ["Consolas", "D2Coding", "Courier New"]

fonts = {}      # apply() 가 채운다: body/head/title/sub/small/mono


def pick_font(root, prefer, size, weight="normal"):
    families = set(tkfont.families(root))
    for name in prefer:
        if name in families:
            return (name, size, weight)
    return (tkfont.nametofont("TkDefaultFont").actual("family"), size, weight)


def apply(root):
    """창 하나에 대해 테마를 적용한다. 창이 만들어진 직후 한 번만 부른다."""
    style = ttk.Style(root)
    # 'clam'은 색을 마음대로 바꿀 수 있는 유일한 기본 테마다.
    # 윈도우 기본 테마('vista')는 배경색 지정이 먹지 않아 카드 위에 회색 네모가 남는다.
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    fonts.update({
        "body": pick_font(root, BODY_FAMILIES, 10),
        "head": pick_font(root, BODY_FAMILIES, 11, "bold"),
        "title": pick_font(root, BODY_FAMILIES, 15, "bold"),
        "sub": pick_font(root, BODY_FAMILIES, 9),
        "small": pick_font(root, BODY_FAMILIES, 9),
        "mono": pick_font(root, MONO_FAMILIES, 9),
    })

    root.configure(bg=PLANE)
    root.option_add("*Font", fonts["body"])

    style.configure(".", background=SURFACE, foreground=INK, font=fonts["body"])

    # 틀
    style.configure("Plane.TFrame", background=PLANE)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Head.TFrame", background=INK)

    # 글자
    style.configure("TLabel", background=SURFACE, foreground=INK)
    style.configure("Card.TLabel", background=SURFACE, foreground=INK)
    style.configure("Plane.TLabel", background=PLANE, foreground=INK_SOFT,
                    font=fonts["small"])
    style.configure("Field.TLabel", background=SURFACE, foreground=INK_SOFT,
                    font=fonts["small"])
    style.configure("Hint.TLabel", background=SURFACE, foreground=INK_MUTED,
                    font=fonts["small"])
    style.configure("Section.TLabel", background=SURFACE, foreground=INK,
                    font=fonts["head"])
    style.configure("Title.TLabel", background=INK, foreground="#ffffff",
                    font=fonts["title"])
    style.configure("Sub.TLabel", background=INK, foreground="#c3c2b7",
                    font=fonts["sub"])

    # 입력칸
    style.configure("TEntry", fieldbackground="#ffffff", bordercolor=FIELD_LINE,
                    lightcolor=FIELD_LINE, darkcolor=FIELD_LINE, insertcolor=INK,
                    padding=4)
    # 입력 중인 칸을 강조색 테두리로 알려준다
    for option in ("bordercolor", "lightcolor", "darkcolor"):
        style.map("TEntry", **{option: [("focus", ACCENT)]})
    style.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=FIELD_LINE,
                    arrowcolor=INK_SOFT)
    style.configure("TCombobox", fieldbackground="#ffffff", bordercolor=FIELD_LINE,
                    arrowcolor=INK_SOFT)

    # 선택칸
    for name in ("TRadiobutton", "TCheckbutton"):
        style.configure(name, background=SURFACE, foreground=INK, focuscolor=SURFACE,
                        indicatorbackground="#ffffff", indicatorforeground=ACCENT,
                        bordercolor=FIELD_LINE)
        style.map(name,
                  background=[("active", SURFACE)],
                  indicatorbackground=[("active", "#ffffff"), ("selected", "#ffffff")],
                  bordercolor=[("selected", ACCENT), ("active", ACCENT)])

    # 단추
    style.configure("Run.TButton", background=ACCENT, foreground="#ffffff",
                    font=fonts["head"], borderwidth=0, padding=(18, 9))
    style.map("Run.TButton",
              background=[("active", ACCENT_DARK), ("disabled", ACCENT_DIM)],
              foreground=[("disabled", "#f0f4fa")])
    style.configure("Quiet.TButton", background=SOFT, foreground=INK_SOFT,
                    bordercolor=FIELD_LINE, borderwidth=1, padding=(12, 7))
    style.map("Quiet.TButton",
              background=[("active", SOFT_ACTIVE), ("disabled", SOFT)],
              foreground=[("disabled", INK_MUTED)])

    # 진행률·스크롤
    style.configure("Thin.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=PLANE, bordercolor=PLANE, lightcolor=ACCENT,
                    darkcolor=ACCENT, borderwidth=0, thickness=6)
    style.configure("Card.Vertical.TScrollbar", background="#d7d5cd",
                    troughcolor=SURFACE, bordercolor=SURFACE,
                    arrowcolor=INK_MUTED, relief="flat")
    style.map("Card.Vertical.TScrollbar", background=[("active", "#bfbdb4")])

    # 탭
    style.configure("TNotebook", background=PLANE, borderwidth=0, tabmargins=(14, 8, 14, 0))
    style.configure("TNotebook.Tab", background=PLANE, foreground=INK_MUTED,
                    font=fonts["head"], padding=(18, 9), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", SURFACE)],
              foreground=[("selected", INK)],
              expand=[("selected", (0, 0, 0, 0))])

    style.configure("TSeparator", background=LINE)
    return style


def card(parent, padding=(16, 13)):
    """
    흰 바탕에 얇은 테두리를 두른 묶음. (테두리 프레임, 내용 프레임)을 돌려준다.

    ttk.Frame은 1px 테두리를 그릴 방법이 마땅치 않아 tk.Frame의 배경색을
    테두리 삼아 1px만 남기고 안쪽 프레임을 덮는다.
    """
    outer = tk.Frame(parent, bg=LINE)
    inner = ttk.Frame(outer, style="Card.TFrame", padding=padding)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return outer, inner


def titled_card(parent, title, hint=None, expand=False, pady=(0, 10)):
    """제목이 달린 카드. 다운로드 탭처럼 카드마다 제목이 필요한 화면에서 쓴다."""
    outer, inner = card(parent, padding=(18, 14))
    outer.pack(fill="both" if expand else "x", expand=expand, pady=pady)

    head = ttk.Frame(inner, style="Card.TFrame")
    head.pack(fill="x")
    ttk.Label(head, text=title, style="Section.TLabel").pack(side="left")
    if hint:
        ttk.Label(head, text=hint, style="Hint.TLabel").pack(side="left", padx=(10, 0))

    body = ttk.Frame(inner, style="Card.TFrame")
    body.pack(fill="both", expand=True, pady=(9, 0))
    return body


def log_widget(parent, height=10, wrap="word"):
    """진행 상황을 찍는 글상자. 두 탭이 같은 모양을 쓴다."""
    wrapper = tk.Frame(parent, bg=LINE)
    text = tk.Text(wrapper, height=height, state="disabled", relief="flat", bd=0,
                   font=fonts.get("mono", ("Consolas", 9)), wrap=wrap,
                   bg=LOG_BG, fg=INK_SOFT, padx=10, pady=8)
    scroll = ttk.Scrollbar(wrapper, command=text.yview, style="Card.Vertical.TScrollbar")
    text.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
    text.pack(fill="both", expand=True, padx=1, pady=1)

    mono = fonts.get("mono", ("Consolas", 9))
    text.tag_configure("ok", foreground=GOOD)
    text.tag_configure("warn", foreground=BAD)
    text.tag_configure("head", foreground=INK, font=(mono[0], mono[1], "bold"))
    return wrapper, text
