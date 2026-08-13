"""Tk 창을 화면에 안 띄우고 그대로 캡처한다 (개발용 도구).

  from _ui_screenshot import capture
  capture(app, "화면.png", settle=1500, tab=1)

주의 — PrintWindow는 제목표시줄까지 포함한 '창 전체'를 그린다.
크기를 winfo_width/height(=클라이언트 영역)로 잡으면 제목표시줄 높이만큼
아래가 잘려서 맨 아래 줄이 없는 것처럼 보인다. GetWindowRect로 잡아야 한다.
"""
import ctypes
import ctypes.wintypes as wt

from PIL import Image


def capture(app, out, settle=800, tab=None):
    if tab is not None:
        app.update_idletasks()
        book = app.nametowidget(app.download_tab.winfo_parent())
        book.select(tab)

    app.update_idletasks()
    app.update()
    app.after(settle, app.quit)
    app.mainloop()          # 백그라운드 로딩이 로그를 채울 시간
    app.update_idletasks()
    app.update()

    hwnd = ctypes.windll.user32.GetAncestor(app.winfo_id(), 2)
    rect = wt.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(hwnd, memdc, 2)

    class Header(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    head = Header()
    head.biSize = ctypes.sizeof(head)
    head.biWidth, head.biHeight = w, -h
    head.biPlanes, head.biBitCount, head.biCompression = 1, 32, 0

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(head), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(out)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)
    print("저장:", out, f"({w}x{h})")
