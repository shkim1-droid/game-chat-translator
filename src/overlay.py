"""Desktop-only transparent translation overlay; no game-process hooks."""
import ctypes
from ctypes import wintypes
import os
import tkinter as tk
import tkinter.font as tkfont


def overlay_region(source, screen_width, screen_height):
    left = max(0, min(source['left'], screen_width-30))
    top = source['top'] + source['height'] + 8
    available = screen_height - top
    if available < 30:
        raise ValueError('채팅창 아래에 공간이 없습니다. 번역 위치 지정을 사용하세요.')
    return {'left': left, 'top': top,
            'width': min(source['width'], screen_width-left),
            'height': min(max(source['height'], 180), available)}


def regions_overlap(a, b):
    return (a['left'] < b['left']+b['width'] and b['left'] < a['left']+a['width']
            and a['top'] < b['top']+b['height'] and b['top'] < a['top']+a['height'])


def wrap_lines(texts, width, measure, limit):
    lines = []
    for text in texts:
        for paragraph in text.splitlines():
            line = ''
            for character in paragraph:
                if line and measure(line+character) > width:
                    lines.append(line)
                    line = character
                else:
                    line += character
            if line:
                lines.append(line)
    return lines[-limit:] if limit > 0 else []


class TranslationOverlay:
    KEY_COLOR = '#010203'

    def __init__(self, master):
        self.window = tk.Toplevel(master, bg=self.KEY_COLOR)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes('-topmost', True)
        if os.name == 'nt':
            self.window.attributes('-transparentcolor', self.KEY_COLOR)
            self.window.attributes('-toolwindow', True)
        self.canvas = tk.Canvas(self.window, bg=self.KEY_COLOR, highlightthickness=0, borderwidth=0, takefocus=False)
        self.canvas.pack(fill='both', expand=True)
        self.font = tkfont.Font(root=self.window, family='Malgun Gothic', size=-14)
        self.region = None
        self.texts = []
        self.enabled = True

    def set_region(self, region, pixel_size=14):
        self.region = dict(region)
        self.font.configure(size=-pixel_size)
        self.window.geometry(f"{region['width']}x{region['height']}+{region['left']}+{region['top']}")
        self.window.update_idletasks()
        self.redraw()

    def apply_native_styles(self):
        if os.name != 'nt':
            return
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.GetParent.argtypes = [wintypes.HWND]
        api.GetParent.restype = wintypes.HWND
        hwnd = api.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        get_long = api.GetWindowLongPtrW
        set_long = api.SetWindowLongPtrW
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        get_long.restype = ctypes.c_ssize_t
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_long.restype = ctypes.c_ssize_t
        # Layered, pass clicks through, never take activation, hide from Alt+Tab.
        style = get_long(hwnd, -20) | 0x80000 | 0x20 | 0x08000000 | 0x80
        style &= ~0x40000  # WS_EX_APPWINDOW
        set_long(hwnd, -20, style)
        set_long(hwnd, -8, 0)  # independent owner: settings can be minimized
        api.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        api.SetWindowPos.restype = wintypes.BOOL
        r = self.region
        api.SetWindowPos(hwnd, ctypes.c_void_p(-1), r['left'], r['top'], r['width'], r['height'],
                         0x10 | 0x200 | 0x20)  # no activate / no owner z-order / frame changed

    def update(self, texts):
        self.texts = list(texts)[-40:]
        self.redraw()

    def redraw(self):
        self.canvas.delete('all')
        if not self.enabled or not self.region or not self.texts:
            self.window.withdraw()
            return
        r = self.region
        spacing = self.font.metrics('linespace') + 3
        count = max(1, (r['height']-8)//spacing)
        lines = wrap_lines(self.texts, max(10,r['width']-10), self.font.measure, count)
        for index, text in enumerate(lines):
            y = 4+index*spacing
            self.canvas.create_text(5,y+1,text=text,font=self.font,fill='#202020',anchor='nw')
            self.canvas.create_text(4,y,text=text,font=self.font,fill='white',anchor='nw')
        self.apply_native_styles()
        self.window.deiconify()
        self.apply_native_styles()

    def destroy(self):
        self.window.destroy()
