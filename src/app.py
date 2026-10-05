import ctypes
import os
import sys
import json
from ctypes import wintypes
from difflib import SequenceMatcher
import re
import queue
import threading
import time
import logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog


def lines_from_ocr(result):
    if not result:
        return []
    ordered = sorted(result, key=lambda r: (min(p[1] for p in r[0]), min(p[0] for p in r[0])))
    return [str(r[1]).strip() for r in ordered if float(r[2]) >= 0.55 and str(r[1]).strip()]


def region_from_points(a, b):
    x, y = min(a[0], b[0]), min(a[1], b[1])
    w, h = abs(a[0] - b[0]), abs(a[1] - b[1])
    if w < 30 or h < 20:
        raise ValueError('영역이 너무 작습니다.')
    return {'left': x, 'top': y, 'width': w, 'height': h}


def hotkey_loop(events, quit_event):
    user32 = ctypes.windll.user32
    msg = wintypes.MSG()
    registered = []
    try:
        # Ctrl+Alt+F8: first corner, F9: second corner, F10: stop.
        for ident, key in [(1, 0x77), (2, 0x78), (3, 0x79)]:
            if user32.RegisterHotKey(None, ident, 0x4003, key):
                registered.append(ident)
            else:
                events.put(('status', '단축키 등록 실패: 다른 프로그램의 Ctrl+Alt+F8/F9/F10 사용을 확인하세요.'))
        while not quit_event.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == 0x0312:
                    point = wintypes.POINT()
                    if user32.GetCursorPos(ctypes.byref(point)):
                        events.put(('hotkey', int(msg.wParam), (point.x, point.y)))
            quit_event.wait(.03)
    finally:
        for ident in registered:
            user32.UnregisterHotKey(None, ident)


def parse_translation_json(data):
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        raise ValueError('번역 JSON 형식이 올바르지 않습니다.')
    translated = ''.join(row[0] for row in data[0]
                         if isinstance(row, list) and row and isinstance(row[0], str))
    if not translated.strip():
        raise ValueError('번역 응답이 비어 있습니다.')
    return translated


def translate_korean(text):
    import requests
    from bs4 import BeautifulSoup
    errors = []
    paths = [
        ('JSON 연결', 'https://translate.googleapis.com/translate_a/single',
         {'client': 'gtx', 'sl': 'auto', 'tl': 'ko', 'dt': 't', 'q': text[:4500]}),
        ('웹 연결', 'https://translate.google.com/m',
         {'sl': 'auto', 'tl': 'ko', 'q': text[:4500]})
    ]
    for name, url, params in paths:
        try:
            response = requests.get(url, params=params, timeout=(4, 8),
                                    headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code != 200:
                raise RuntimeError('HTTP ' + str(response.status_code))
            if name == 'JSON 연결':
                return parse_translation_json(response.json())
            soup = BeautifulSoup(response.text, 'html.parser')
            element = soup.select_one('.result-container') or soup.select_one('.t0')
            if element is None or not element.get_text(strip=True):
                raise RuntimeError('응답 형식 변경 또는 서비스 제한')
            return element.get_text(strip=True)
        except requests.exceptions.SSLError:
            errors.append(name + ': 보안 인증서 오류')
        except requests.exceptions.Timeout:
            errors.append(name + ': 연결 시간 초과')
        except requests.exceptions.ConnectionError:
            errors.append(name + ': 서버 연결 실패')
        except Exception as exc:
            errors.append(name + ': ' + str(exc)[:100])
    raise RuntimeError(' / '.join(errors))


def same_chat_line(a, b):
    a = re.sub(r'\s+', '', a).casefold()
    b = re.sub(r'\s+', '', b).casefold()
    if a == b:
        return True
    # Tolerate a small OCR change, but keep short repeated messages exact.
    return min(len(a), len(b)) >= 20 and SequenceMatcher(None, a, b, autojunk=False).ratio() >= .96


class ChatTracker:
    def __init__(self):
        self.previous = []

    def new_lines(self, current):
        current = [x.strip() for x in current if x.strip()]
        if not current:
            return []  # A disappearing chat window does not erase the baseline.
        previous = self.previous
        if previous:
            # Brief partial OCR/fading must not resurrect existing lines.
            for start in range(max(0, len(previous)-len(current))+1):
                part = previous[start:start+len(current)]
                if len(part) == len(current) and all(same_chat_line(a,b) for a,b in zip(part,current)):
                    return []
            for overlap in range(min(len(previous),len(current)),0,-1):
                if all(same_chat_line(a,b) for a,b in zip(previous[-overlap:],current[:overlap])):
                    self.previous = current
                    return current[overlap:]
        self.previous = current
        return current


class FrameGate:
    def __init__(self):
        self.previous = None
        self.last_ocr = -1e9

    def should_read(self, frame, now, minimum_interval):
        import numpy as np
        # Tiny subsample: compare appearance without invoking the OCR model.
        sample = frame[::max(1,frame.shape[0]//100), ::max(1,frame.shape[1]//300), :3].astype(np.int16)
        if now - self.last_ocr < minimum_interval:
            return False
        changed = self.previous is None or self.previous.shape != sample.shape
        if not changed:
            changed = np.mean(np.abs(sample-self.previous)) > 2.0
        if changed or now-self.last_ocr >= 15:
            self.previous = sample
            self.last_ocr = now
            return True
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.region = None
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.worker = None
        self.cache = {}
        self.tracker = ChatTracker()
        self.translation_tasks = queue.Queue()
        self.history = {}
        self.next_entry_id = 0
        self.translation_thread = None
        self.corner = None
        self.pending_region = None
        self.quit_event = threading.Event()
        self.app_version = json.loads(Path(__file__).with_name('version.json').read_text(encoding='utf-8'))['version']
        root.title('게임 채팅 → 한국어 '+self.app_version+' · 투명 자막')
        from updater import UpdateManager
        self.install_root = Path(os.environ.get('GCT_INSTALL_ROOT', Path(__file__).parent))
        self.data_dir = Path(os.environ.get('GCT_DATA_DIR', self.install_root/'data'))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.update_manager = UpdateManager(self.install_root, self.app_version)
        self.update_lock = threading.Lock()
        self.save_job = None
        self.restoring = True
        from overlay import TranslationOverlay
        self.subtitle_overlay = TranslationOverlay(root)
        self.output_region = None
        self.overlay_enabled = tk.BooleanVar(value=True)
        self.overlay_font_size = tk.StringVar(value='14')
        self.last_frame = None
        self.preview_window = None
        self.capture_mode = tk.StringVar(value='자동')
        root.geometry('740x620')
        root.attributes('-topmost', True)
        root.configure(bg='#151923')
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='영역 지정 즉시 자동 번역 · 전체화면은 단축키로 지정하세요.').pack(anchor='w')
        ttk.Label(frame, text='선택 영역의 글자가 인터넷 번역 서비스로 전송됩니다.').pack(anchor='w')
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=10)
        self.select_button = ttk.Button(bar, text='채팅 영역 드래그', command=self.select)
        self.select_button.pack(side='left')
        self.start_button = ttk.Button(bar, text='번역 재시작', command=self.start)
        self.start_button.pack(side='left', padx=6)
        ttk.Button(bar, text='중지', command=self.stop.set).pack(side='left')
        self.interval = tk.StringVar(value='2')
        ttk.Label(bar, text='최소 인식 간격(초)').pack(side='left', padx=6)
        ttk.Combobox(bar, textvariable=self.interval, values=['1', '2', '3', '5', '10'], width=4, state='readonly').pack(side='left')
        diagbar = ttk.Frame(frame)
        diagbar.pack(fill='x', pady=4)
        ttk.Label(diagbar, text='캡처 방식').pack(side='left')
        ttk.Combobox(diagbar, textvariable=self.capture_mode, values=['자동', '일반', '전체화면'], width=10, state='readonly').pack(side='left', padx=6)
        ttk.Button(diagbar, text='캡처 미리보기', command=self.preview).pack(side='left')
        ttk.Button(diagbar, text='번역 연결 검사', command=self.check_translation).pack(side='left', padx=6)
        ttk.Button(diagbar, text='번역 기록 지우기', command=self.clear_history).pack(side='left')
        overlaybar = ttk.Frame(frame)
        overlaybar.pack(fill='x', pady=4)
        ttk.Checkbutton(overlaybar, text='게임 위 투명 자막', variable=self.overlay_enabled, command=self.refresh_overlay).pack(side='left')
        ttk.Button(overlaybar, text='번역 위치 지정', command=lambda: self.select('translation')).pack(side='left', padx=4)
        ttk.Button(overlaybar, text='채팅 아래 자동 배치', command=self.reset_output_region).pack(side='left')
        ttk.Label(overlaybar, text='글자(px)').pack(side='left', padx=4)
        sizes = ttk.Combobox(overlaybar, textvariable=self.overlay_font_size, values=['12','14','16','18','20'], width=3, state='readonly')
        sizes.pack(side='left')
        sizes.bind('<<ComboboxSelected>>', lambda e: self.refresh_overlay())
        ttk.Button(overlaybar, text='설정창 최소화', command=self.root.iconify).pack(side='left', padx=4)
        updatebar = ttk.Frame(frame)
        updatebar.pack(fill='x', pady=4)
        ttk.Button(updatebar, text='업데이트 확인', command=lambda: self.check_update(True)).pack(side='left')
        ttk.Button(updatebar, text='새 버전으로 재시작', command=self.restart_for_update).pack(side='left', padx=4)
        ttk.Button(updatebar, text='업데이트 파일 적용', command=self.apply_update_file).pack(side='left')
        ttk.Button(updatebar, text='배포 주소 설정', command=self.configure_updates).pack(side='left', padx=4)
        self.update_status = tk.StringVar(value='자동 확인: 실행 시 / 60초마다')
        ttk.Label(frame, textvariable=self.update_status, wraplength=700).pack(anchor='w')
        ttk.Label(frame, text='Ctrl+Alt+F8: 첫 모서리 / F9: 반대 모서리·자동 시작 / F10: 중지').pack(anchor='w')
        self.status = tk.StringVar(value='채팅 영역을 먼저 선택하세요. 주 모니터를 지원합니다.')
        ttk.Label(frame, textvariable=self.status, wraplength=610).pack(anchor='w')
        self.error_status = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self.error_status, foreground='#b00020', wraplength=650).pack(anchor='w')
        history_frame = ttk.Frame(frame)
        history_frame.pack(fill='both', expand=True, pady=10)
        self.text = tk.Text(history_frame, bg='#151923', fg='#f3f4f8', font=('Malgun Gothic', 12), wrap='word', state='disabled')
        self.text.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(history_frame, command=self.text.yview)
        scroll.pack(side='right', fill='y')
        self.text.configure(yscrollcommand=scroll.set)
        self.root.after(100, self.poll)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.translation_thread = threading.Thread(target=self.translate_pending, daemon=True)
        self.translation_thread.start()
        if os.name == 'nt':
            threading.Thread(target=hotkey_loop, args=(self.events, self.quit_event), daemon=True).start()
        self.restore_settings()
        self.restoring = False
        self.refresh_overlay()
        self.root.after(1500, self.automatic_update_tick)
        if self.region:
            self.root.after(800, self.start)

    def schedule_save(self):
        if getattr(self, 'restoring', True):
            return
        if self.save_job is not None:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(1200, self.save_settings)

    def save_settings(self):
        from updater import atomic_json
        self.save_job = None
        try:
            atomic_json(self.data_dir/'settings.json', {
                'region': self.region, 'output_region': self.output_region,
                'interval': self.interval.get(), 'capture_mode': self.capture_mode.get(),
                'overlay_enabled': self.overlay_enabled.get(), 'overlay_font_size': self.overlay_font_size.get(),
                'history': [{'id': ident, **entry} for ident, entry in self.history.items()],
                'chat_baseline': list(self.tracker.previous)})
        except Exception:
            logging.exception('Could not save settings/history')

    def restore_settings(self):
        from updater import read_json
        config = read_json(self.data_dir/'settings.json', {})
        def valid_region(r):
            return isinstance(r,dict) and all(isinstance(r.get(k),int) for k in ('left','top','width','height')) and r['width']>=30 and r['height']>=20
        if valid_region(config.get('region')):
            self.region = config['region']
        if valid_region(config.get('output_region')):
            self.output_region = config['output_region']
        for name, allowed in [('interval',['1','2','3','5','10']),('capture_mode',['자동','일반','전체화면']),('overlay_font_size',['12','14','16','18','20'])]:
            if config.get(name) in allowed:
                getattr(self,name).set(config[name])
        self.overlay_enabled.set(bool(config.get('overlay_enabled',True)))
        for entry in config.get('history',[]):
            if not isinstance(entry,dict) or not isinstance(entry.get('id'),int) or not all(isinstance(entry.get(k),str) for k in ('source','stamp','translation')):
                continue
            ident = entry.pop('id')
            self.history[ident] = entry
            self.next_entry_id = max(self.next_entry_id,ident)
            self.render_entry(ident)
            if not entry.get('ready'):
                self.translation_tasks.put((ident,entry['source']))
        # Persist the visible-message baseline across updates and restarts.
        baseline = config.get('chat_baseline',[])
        if isinstance(baseline,list) and all(isinstance(x,str) for x in baseline):
            self.tracker.previous = baseline
        self.refresh_overlay()

    def automatic_update_tick(self):
        if self.quit_event.is_set():
            return
        self.check_update(False)
        self.root.after(60000,self.automatic_update_tick)

    def check_update(self, manual=False):
        if not self.update_lock.acquire(blocking=False):
            return
        def check():
            try:
                result = self.update_manager.check_and_stage()
                self.events.put(('update_status',result['message']))
            except Exception as exc:
                logging.exception('Update check failed')
                self.events.put(('update_status','업데이트 확인 실패 · 현재 버전은 유지됩니다: '+str(exc)[:160]))
            finally:
                self.update_lock.release()
        threading.Thread(target=check,daemon=True).start()

    def configure_updates(self):
        value = simpledialog.askstring('업데이트 배포 주소', '배포한 latest.json의 HTTPS 주소를 입력하세요.\n빈 값으로 저장하면 원격 확인을 끕니다.',initialvalue=self.update_manager.channel_url(),parent=self.root)
        if value is None:
            return
        try:
            self.update_manager.configure_channel(value.strip())
            self.check_update(True)
        except Exception as exc:
            messagebox.showerror('배포 주소 설정',str(exc))

    def apply_update_file(self):
        path = filedialog.askopenfilename(parent=self.root,title='전용 업데이트 ZIP 선택',filetypes=[('업데이트 ZIP','*.zip')])
        if not path or not self.update_lock.acquire(blocking=False):
            return
        def stage():
            try:
                result = self.update_manager.stage_file(path)
                self.events.put(('update_status',result['message']))
            except Exception as exc:
                self.events.put(('update_status','업데이트 파일 적용 실패: '+str(exc)))
            finally:
                self.update_lock.release()
        threading.Thread(target=stage,daemon=True).start()

    def restart_for_update(self):
        from updater import read_json
        if not read_json(self.data_dir/'pending-update.json',{}).get('version'):
            messagebox.showinfo('업데이트','아직 준비된 새 버전이 없습니다.')
            return
        self.save_settings()
        self.close()
        sys.exit(75)

    def reset_output_region(self):
        self.output_region = None
        self.refresh_overlay()

    def refresh_overlay(self):
        if getattr(self, 'restoring', False):
            return
        self.schedule_save()
        from overlay import overlay_region, regions_overlap
        self.subtitle_overlay.enabled = self.overlay_enabled.get()
        if not self.subtitle_overlay.enabled or not self.region:
            self.subtitle_overlay.update([])
            return
        try:
            region = self.output_region or overlay_region(self.region, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
            if regions_overlap(region, self.region):
                raise ValueError('번역 위치와 읽을 채팅 영역이 겹칩니다. 번역 위치를 채팅창 아래에 지정하세요.')
            self.subtitle_overlay.set_region(region, int(self.overlay_font_size.get()))
            texts = [e['translation'] for e in self.history.values() if e.get('ready')]
            self.subtitle_overlay.update(texts)
        except Exception as exc:
            self.subtitle_overlay.update([])
            self.error_status.set('투명 자막 표시 오류: ' + str(exc))
            logging.exception('Overlay update failed')

    def clear_history(self):
        self.history.clear()
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        marks = [x for x in self.text.mark_names() if x.startswith('entry')]
        if marks:
            self.text.mark_unset(*marks)
        self.text.configure(state='disabled')
        self.refresh_overlay()

    def render_entry(self, ident, translated=None):
        if ident not in self.history:
            return
        entry = self.history[ident]
        if translated is not None:
            entry['translation'] = translated
            entry['ready'] = not translated.startswith('번역 실패:')
        content = entry['stamp'] + '\n' + entry['translation'] + '\n原文: ' + entry['source'] + '\n\n'
        start, end = f'entry{ident}_start', f'entry{ident}_end'
        follow = self.text.yview()[1] >= .98
        self.text.configure(state='normal')
        if start not in self.text.mark_names():
            self.text.mark_set(start, 'end-1c')
            self.text.mark_gravity(start, 'left')
            self.text.insert(start, content)
        else:
            boundary_marks = [name for name in self.text.mark_names() if name != start and self.text.compare(name, '==', end)]
            self.text.delete(start, end)
            self.text.insert(start, content)
            # Keep the next entry anchored after this updated block.
            for name in boundary_marks:
                self.text.mark_set(name, f'{start}+{len(content)}c')
        self.text.mark_set(end, f'{start}+{len(content)}c')
        self.text.mark_gravity(end, 'left')
        self.text.configure(state='disabled')
        if follow:
            self.text.see('end')
        if hasattr(self, 'subtitle_overlay'):
            self.refresh_overlay()

    def translate_pending(self):
        while not self.quit_event.is_set():
            try:
                ident, source = self.translation_tasks.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                for attempt in range(3):
                    if self.quit_event.is_set():
                        return
                    try:
                        translated = self.cache.get(source)
                        if translated is None:
                            translated = translate_korean(source)
                            if len(self.cache) >= 500:
                                self.cache.clear()
                            self.cache[source] = translated
                        self.events.put(('entry_update', ident, translated))
                        self.events.put(('error', ''))
                        break
                    except Exception as exc:
                        logging.exception('Pending chat translation failed')
                        self.events.put(('entry_update', ident, '번역 실패: ' + str(exc)))
                        self.events.put(('error', '번역 연결 실패: ' + str(exc)))
                        if attempt < 2 and self.quit_event.wait(5):
                            return
            finally:
                self.translation_tasks.task_done()

    def preview(self):
        if self.last_frame is None:
            messagebox.showinfo('미리보기', '영역을 지정하고 번역을 시작하면 캡처 화면을 확인할 수 있습니다.')
            return
        from PIL import Image, ImageTk
        if self.preview_window is None or not self.preview_window.winfo_exists():
            self.preview_window = tk.Toplevel(self.root)
            self.preview_window.title('실제로 읽는 화면 · 채팅 영역 밖에 놓으세요')
            self.preview_label = tk.Label(self.preview_window)
            self.preview_label.pack()
        im = Image.fromarray(self.last_frame[:, :, ::-1])
        im.thumbnail((800, 450))
        self.preview_image = ImageTk.PhotoImage(im)
        self.preview_label.configure(image=self.preview_image)

    def check_translation(self):
        def check():
            self.events.put(('status', '번역 서비스 연결 검사 중…'))
            try:
                answer = translate_korean('Please defend the base.')
                self.events.put(('test_result', answer))
                self.events.put(('error', ''))
                self.events.put(('status', '번역 연결 검사 성공. 화면 번역은 영역을 지정하세요.'))
            except Exception as exc:
                logging.exception('Translation connectivity test failed')
                self.events.put(('error', '번역 연결 검사 실패: ' + str(exc)))
        threading.Thread(target=check, daemon=True).start()

    def select(self, target="source"):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('영역 선택', '먼저 중지한 뒤 작업이 끝나면 영역을 바꾸세요.')
            return
        self.subtitle_overlay.window.withdraw()
        self.root.withdraw()
        overlay = tk.Toplevel(self.root)
        overlay.attributes('-fullscreen', True)
        overlay.attributes('-topmost', True)
        overlay.attributes('-alpha', .35)
        overlay.configure(bg='black')
        canvas = tk.Canvas(overlay, bg='black', highlightthickness=0, cursor='crosshair')
        canvas.pack(fill='both', expand=True)
        canvas.create_text(30, 30, text=('번역을 표시할 위치를 드래그하세요. ESC: 취소' if target == 'translation' else '읽을 채팅 영역을 드래그하세요. ESC: 취소'), fill='white', anchor='nw', font=('Malgun Gothic', 20))
        point = []
        rectangle = [None]
        def press(e):
            point[:] = [e.x, e.y]
            if rectangle[0]:
                canvas.delete(rectangle[0])
            rectangle[0] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline='cyan', width=3)
        def drag(e):
            if point:
                canvas.coords(rectangle[0], point[0], point[1], e.x, e.y)
        def finish(e):
            if not point:
                return
            x, y = min(point[0], e.x), min(point[1], e.y)
            w, h = abs(e.x-point[0]), abs(e.y-point[1])
            if w < 30 or h < 20:
                return
            region = {'left': x, 'top': y, 'width': w, 'height': h}
            if target == 'translation':
                from overlay import regions_overlap
                if self.region and regions_overlap(region, self.region):
                    return
                self.output_region = region
            else:
                self.region = region
            overlay.destroy()
            self.root.deiconify()
            self.refresh_overlay()
            if self.region:
                self.root.after(800, self.start)
        def cancel(e):
            overlay.destroy()
            self.root.deiconify()
            self.refresh_overlay()
        canvas.bind('<ButtonPress-1>', press)
        canvas.bind('<B1-Motion>', drag)
        canvas.bind('<ButtonRelease-1>', finish)
        overlay.bind('<Escape>', cancel)
        overlay.focus_force()

    def handle_hotkey(self, ident, point):
        import winsound
        if ident == 3:
            self.pending_region = None
            self.stop.set()
            winsound.Beep(500, 80)
            return
        if ident == 1:
            self.stop.set()
            self.pending_region = None
            self.corner = point
            self.status.set('첫 모서리 지정됨. 반대 모서리에 마우스를 놓고 Ctrl+Alt+F9를 누르세요.')
            winsound.Beep(800, 80)
        elif ident == 2:
            if self.corner is None:
                self.status.set('Ctrl+Alt+F8로 첫 모서리를 먼저 지정하세요.')
                winsound.Beep(300, 80)
                return
            try:
                region = region_from_points(self.corner, point)
                width = ctypes.windll.user32.GetSystemMetrics(0)
                height = ctypes.windll.user32.GetSystemMetrics(1)
                if region['left'] < 0 or region['top'] < 0 or region['left'] + region['width'] > width or region['top'] + region['height'] > height:
                    raise ValueError('현재 버전은 주 모니터의 영역만 지원합니다.')
                self.stop.set()
                self.pending_region = region
                self.corner = None
                winsound.Beep(1100, 100)
                self.status.set('영역 지정됨. 자동 번역을 시작합니다.')
            except ValueError as exc:
                self.status.set(str(exc))
                winsound.Beep(300, 80)

    def start(self):
        if not self.region:
            messagebox.showinfo('영역 선택', '먼저 채팅 영역을 선택하세요.')
            return
        if self.worker and self.worker.is_alive():
            return
        self.stop.clear()
        self.refresh_overlay()
        region = dict(self.region)
        interval = float(self.interval.get())
        self.status.set('OCR 준비 중… 첫 실행은 시간이 걸릴 수 있습니다.')
        self.worker = threading.Thread(target=self.run, args=(region, interval, self.capture_mode.get()), daemon=True)
        self.worker.start()

    def run(self, region, interval, mode="자동"):
        camera = None
        try:
            import mss
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR
            import cv2
            engine = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1)
            gate = FrameGate()
            self.events.put(('status', 'OCR 준비 완료. 화면 캡처 시작…'))
            with mss.mss() as capture:
                while not self.stop.is_set():
                    shot = None
                    bounds = (region['left'], region['top'], region['left']+region['width'], region['top']+region['height'])
                    if mode != '전체화면':
                        shot = np.array(capture.grab(region))[:, :, :3].copy()
                    if mode == '전체화면' or (mode == '자동' and np.max(shot) < 8):
                        if os.name == 'nt':
                            try:
                                if camera is None:
                                    import dxcam
                                    camera = dxcam.create(output_color='BGR')
                                try:
                                    alternative = camera.grab(region=bounds, new_frame_only=False)
                                except TypeError:
                                    alternative = camera.grab(region=bounds)
                                if alternative is not None:
                                    shot = alternative
                            except Exception as exc:
                                logging.exception('DXGI capture failed')
                                self.events.put(('status', '전체화면 캡처 실패: ' + str(exc)[:130]))
                    if shot is None:
                        self.events.put(('status', '캡처 프레임 없음. 캡처 방식을 일반으로 바꾸고 테두리 없는 창모드로 재시작하세요.'))
                        self.stop.wait(interval)
                        continue
                    self.events.put(('frame', shot))
                    if np.max(shot) < 8:
                        self.events.put(('status', '검은 화면이 캡처되었습니다. 테두리 없는 창모드로 바꾸세요.'))
                        self.stop.wait(interval)
                        continue
                    if not gate.should_read(shot, time.monotonic(), interval):
                        self.stop.wait(min(interval, .75))
                        continue
                    self.events.put(('status', '채팅 변화 확인 · 글자 인식 중…'))
                    if shot.shape[0] < 400 and shot.shape[1] < 900:
                        shot = cv2.resize(shot, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
                    result, _ = engine(shot, use_cls=False)
                    new_lines = self.tracker.new_lines(lines_from_ocr(result))
                    if new_lines:
                        source = '\n'.join(new_lines)
                        # Bound each translation request without dropping new messages.
                        chunks = []
                        chunk = ''
                        for line in new_lines:
                            for start in range(0, len(line), 4000):
                                part = line[start:start+4000]
                                if len(chunk) + len(part) + 1 > 4000:
                                    chunks.append(chunk)
                                    chunk = ''
                                chunk += ('\n' if chunk else '') + part
                        if chunk:
                            chunks.append(chunk)
                        for source in chunks:
                            self.next_entry_id += 1
                            ident = self.next_entry_id
                            self.events.put(('entry_add', ident, source, time.strftime('%H:%M:%S')))
                            self.translation_tasks.put((ident, source))
                        self.events.put(('status', f'새 채팅 {len(new_lines)}줄 추가 · 기존 번역 유지'))
                    else:
                        self.events.put(('status', '새 채팅 대기 중 · 기존 번역 유지'))
                    self.stop.wait(min(interval, .75))
        except Exception as exc:
            logging.exception('Capture/OCR worker failed')
            self.events.put(('status', '실행 오류: ' + str(exc)[:250]))
        finally:
            if camera is not None:
                try:
                    camera.release()
                except Exception:
                    pass
            self.events.put(('stopped',))

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'hotkey':
                    self.handle_hotkey(event[1], event[2])
                elif event[0] == 'update_status':
                    self.update_status.set(event[1])
                elif event[0] == 'error':
                    self.error_status.set(event[1])
                elif event[0] == 'frame':
                    self.last_frame = event[1]
                elif event[0] == 'entry_add':
                    ident, source, stamp = event[1:]
                    self.history[ident] = {'source': source, 'stamp': stamp, 'translation': '번역 대기 중…', 'ready': False}
                    self.render_entry(ident)
                elif event[0] == 'entry_update':
                    self.render_entry(event[1], event[2])
                elif event[0] == 'test_result':
                    messagebox.showinfo('번역 연결 검사 성공', event[1])
                elif event[0] == 'status':
                    self.status.set(event[1])
                elif event[0] == 'stopped':
                    self.status.set(self.status.get() + ' · 중지됨')
        except queue.Empty:
            pass
        if self.pending_region and not (self.worker and self.worker.is_alive()):
            self.region = self.pending_region
            self.pending_region = None
            self.start()
        self.root.after(100, self.poll)

    def close(self):
        self.save_settings()
        self.quit_event.set()
        self.stop.set()
        self.subtitle_overlay.destroy()
        self.root.destroy()


if __name__ == '__main__':
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    if os.name == 'nt':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    log_dir = Path(os.environ.get('GCT_DATA_DIR', Path(__file__).parent/'data'))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir/'runtime-log.txt', level=logging.WARNING, encoding='utf-8', format='%(asctime)s %(levelname)s %(message)s')
    app = App(tk.Tk())
    app.root.update_idletasks()
    if os.environ.get('GCT_HEALTH_FILE'):
        Path(os.environ['GCT_HEALTH_FILE']).write_text('ready',encoding='utf-8')
    app.root.mainloop()
