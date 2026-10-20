#!/usr/bin/env python3
"""Windows GUI for NTRIP bridge/logger; bundled worker runs inside the same exe."""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from serial.tools import list_ports

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

from ntrip_core import main as core_main, fetch_mountpoints

FIELDS = [
    ('host', 'Caster host', ''), ('port', 'Caster port', '2101'),
    ('mount', 'Mountpoint', ''), ('user', 'Username', ''), ('password', 'Password', ''),
    ('serial', 'Receiver GGA COM port', 'COM5'), ('baud', 'Baud rate', '115200'),
    ('gga-interval', 'GGA interval (s)', '10'),
    ('serial-retry-seconds', 'COM reconnect interval (s)', '5'),
    ('serial-hwid', 'USB identity (optional)', ''), ('log-dir', 'Log directory', 'NTRIP_Logs'),
    ('retry-seconds', 'Initial retry (s)', '5'),
    ('max-retry-seconds', 'Maximum retry (s)', '60'),
    ('connect-timeout', 'Connect timeout (s)', '15'),
    ('idle-timeout', 'Caster idle timeout (s)', '30'),
    ('gga-timeout', 'GGA warning timeout (s)', '30'),
]
CONFIG_PATH = Path(os.environ.get('APPDATA', str(Path.home()))) / 'NTRIP_RTCM_Bridge' / 'settings.json'

class App:
    def __init__(self, root):
        self.root = root
        root.title('OMG NTRIP RTCM Logger & Bridge')
        self.apply_icon()
        root.geometry('1080x850')
        root.minsize(740, 650)
        self.vars = {k: tk.StringVar(value=default) for k, _, default in FIELDS}
        self.mode = tk.StringVar(value='logger')
        self.tls = tk.BooleanVar(value=False)
        self.static_position = tk.BooleanVar(value=False)
        self.latitude = tk.StringVar(value='')
        self.longitude = tk.StringVar(value='')
        self.altitude = tk.StringVar(value='0.0')
        self.save_file = tk.BooleanVar(value=True)
        self.forward_input = tk.BooleanVar(value=False)
        self.output_serial_enabled = tk.BooleanVar(value=False)
        self.output_serial = tk.StringVar(value='')
        self.output_baud = tk.StringVar(value='115200')
        self.tcp_enabled = tk.BooleanVar(value=False)
        self.tcp_bind = tk.StringVar(value='0.0.0.0')
        self.tcp_port = tk.StringVar(value='2102')
        self.udp_enabled = tk.BooleanVar(value=False)
        self.udp_host = tk.StringVar(value='255.255.255.255')
        self.udp_port = tk.StringVar(value='2103')
        self.status = tk.StringVar(value='Stopped')
        self.rotation = tk.StringVar(value='connection')
        self.rotation_mb = tk.StringVar(value='100')
        self.dashboard = {key: tk.StringVar(value='—') for key in
            ('rate','valid','crc','types','gga','gga_age','file','input','serial','tcp','udp')}
        self.theme_choice = tk.StringVar(value='OMG Light')
        self.profile_name = tk.StringVar(value='Field Survey')
        self.profile_dir = CONFIG_PATH.parent / 'profiles'

        self.validation_received = tk.StringVar(value='0 B')
        self.validation_logged = tk.StringVar(value='0 B')
        self.validation_size = tk.StringVar(value='0 B')
        self.validation_last = tk.StringVar(value='No data yet')
        self.validation_health = tk.StringVar(value='Waiting for corrections')
        self.last_telemetry_at = None
        self.proc = None
        self.lines = queue.Queue()
        self.build()
        self.load_settings()
        self.apply_theme()
        self.refresh_com_ports()
        self.root.after(100, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def apply_icon(self):
        """Apply both Tk and native Win32 title-bar icons after window creation."""
        icon = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'omg_icon.ico'
        if not icon.is_file():
            message = f'GUI icon missing: {icon}'
            print(message, file=sys.stderr)
            return
        self._icon_path = icon
        if sys.platform == 'win32':
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    'OMG.NTRIP.RTCM.Logger.Bridge')
            except Exception as exc:
                print(f'App ID warning: {exc}', file=sys.stderr)
        # Tk needs a persistent PhotoImage, and ICO files must first be converted
        # to PNG/RGBA for iconphoto. The native Win32 icon is set separately.
        try:
            if Image is not None and ImageTk is not None:
                with Image.open(icon) as im:
                    im.seek(0)
                    self._icon_image = ImageTk.PhotoImage(im.convert('RGBA'))
                self.root.iconphoto(True, self._icon_image)
        except Exception as exc:
            print(f'Tk icon warning: {exc}', file=sys.stderr)
        if sys.platform == 'win32':
            try:
                self.root.iconbitmap(str(icon))
            except Exception as exc:
                print(f'Windows iconbitmap warning: {exc}', file=sys.stderr)
            # Native title-bar icon needs the actual mapped HWND, not just the
            # Tk root widget identifier. Reapply after Windows creates its frame.
            self.root.after(200, self._apply_native_icon)
            self.root.bind('<Map>', self._on_window_map, add='+')

    def _on_window_map(self, event):
        if event.widget is self.root:
            self.root.after_idle(self._apply_native_icon)

    def _apply_native_icon(self):
        if sys.platform != 'win32' or not getattr(self, '_icon_path', None):
            return
        if getattr(self, '_native_icon_applied', False):
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                           wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                             wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = wintypes.LPARAM
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x0010, 0x0080
            hwnd = self.root.winfo_id()
            frame = user32.GetParent(hwnd) or hwnd
            # Request the two Windows-standard icon sizes from the ICO resource.
            for size, kind in ((16, 0), (32, 1)):
                handle = user32.LoadImageW(None, str(self._icon_path), IMAGE_ICON,
                                           size, size, LR_LOADFROMFILE)
                if not handle:
                    print(f'Unable to load {size}px native icon: {ctypes.get_last_error()}',
                          file=sys.stderr)
                    continue
                # Keep handles alive while the window exists.
                if not hasattr(self, '_native_icon_handles'):
                    self._native_icon_handles = []
                self._native_icon_handles.append(handle)
                user32.SendMessageW(frame, WM_SETICON, kind, handle)
                if frame != hwnd:
                    user32.SendMessageW(hwnd, WM_SETICON, kind, handle)
            self._native_icon_applied = True
        except Exception as exc:
            print(f'Native GUI icon warning: {exc}', file=sys.stderr)

    def refresh_com_ports(self):
        """Refresh available ports, preserving manual or disconnected selections."""
        selected = self.vars['serial'].get().strip()
        try:
            ports = sorted(list_ports.comports(), key=lambda p: p.device.lower())
        except Exception as exc:
            messagebox.showerror('COM port scan failed', str(exc))
            return
        values = [p.device for p in ports]
        if selected and selected not in values:
            values.insert(0, selected)
        self.com_dropdown.configure(values=values)
        if hasattr(self, 'output_com_dropdown'):
            extra = self.output_serial.get().strip()
            output_values = list(values)
            if extra and extra not in output_values: output_values.insert(0, extra)
            self.output_com_dropdown.configure(values=output_values)
        if not selected and ports:
            self.vars['serial'].set(ports[0].device)
        if hasattr(self, 'console'):
            details = ', '.join(f'{p.device} ({p.description})' for p in ports)
            self.append('COM ports refreshed: ' + (details or 'none detected'))

    def browse_mountpoints(self):
        if self.proc is not None:
            messagebox.showinfo('Stop logging first', 'Stop the active logger before querying the caster.')
            return
        host = self.vars['host'].get().strip()
        try:
            port = int(self.vars['port'].get())
            timeout = float(self.vars['connect-timeout'].get())
        except ValueError:
            messagebox.showerror('Invalid settings', 'Enter a valid caster port and connection timeout.')
            return
        self.mount_button.configure(state='disabled')
        self.status.set('Fetching mountpoints...')
        self.append(f'Fetching sourcetable from {host}:{port} ...')
        def worker():
            try:
                result = fetch_mountpoints(host, port, self.vars['user'].get(),
                                           self.vars['password'].get(), self.tls.get(), timeout)
                self.lines.put(('__MOUNTS__', result))
            except Exception as exc:
                self.lines.put(('__MOUNTS_ERROR__', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def show_mountpoints(self, records):
        self.mount_button.configure(state='normal')
        self.status.set('Stopped')
        if not records:
            messagebox.showwarning('No advertised mountpoints',
                'The caster did not return any STR mountpoints. It may restrict sourcetable access or use a different NTRIP protocol.')
            return
        popup = tk.Toplevel(self.root)
        popup.title(f'Caster mountpoints ({len(records)} advertised)')
        popup.geometry('900x480')
        popup.transient(self.root)
        frame = ttk.Frame(popup, padding=12); frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Select an advertised mountpoint. Listing does not guarantee access or VRS compatibility.',
                  wraplength=840).pack(anchor='w', pady=(0,8))
        columns = ('mount','identifier','format','nav','network','country','nmea','auth','fee')
        tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        widths = (160,190,140,90,100,70,55,55,55)
        for name, width in zip(columns,widths):
            tree.heading(name,text=name.upper()); tree.column(name,width=width,stretch=name in ('identifier','format'))
        for index, item in enumerate(records):
            tree.insert('', 'end', iid=str(index), values=[item.get(k,'') for k in columns])
        sb = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side='left',fill='both',expand=True); sb.pack(side='right',fill='y')
        def select():
            chosen = tree.selection()
            if not chosen: return
            mount = records[int(chosen[0])]['mount']
            self.vars['mount'].set(mount)
            self.append(f'Selected mountpoint: {mount}')
            popup.destroy()
        tree.bind('<Double-1>', lambda _e: select())
        footer = ttk.Frame(popup, padding=(12,0,12,12)); footer.pack(fill='x')
        ttk.Button(footer,text='Use selected mountpoint',command=select).pack(side='right')
        ttk.Button(footer,text='Cancel',command=popup.destroy).pack(side='right',padx=8)

    def make_section(self, parent, title, expanded=True):
        frame = ttk.LabelFrame(parent, text=title, padding=12)
        frame.pack(fill='x', padx=12, pady=8)
        return frame

    def build(self):
        # Notebook pages keep configuration separate from live monitoring.
        header = ttk.Frame(self.root, padding=(12, 8))
        header.pack(fill='x')
        ttk.Label(header, text='OMG NTRIP RTCM Logger & Bridge', font=('Segoe UI', 16, 'bold')).pack(side='left')
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill='both', expand=True, padx=10, pady=4)
        pages = {}
        for name in ('Dashboard', 'Caster', 'Position', 'Outputs', 'Settings'):
            frame = ttk.Frame(self.tabs, padding=8)
            self.tabs.add(frame, text=name)
            pages[name] = frame
        dashboard_canvas = tk.Canvas(pages['Dashboard'], highlightthickness=0)
        self.dashboard_canvas = dashboard_canvas
        dashboard_scroll = ttk.Scrollbar(pages['Dashboard'], orient='vertical', command=dashboard_canvas.yview)
        dashboard_canvas.configure(yscrollcommand=dashboard_scroll.set)
        dashboard_scroll.pack(side='right', fill='y')
        dashboard_canvas.pack(side='left', fill='both', expand=True)
        container = ttk.Frame(dashboard_canvas)
        dashboard_window = dashboard_canvas.create_window((0,0), window=container, anchor='nw')
        container.bind('<Configure>', lambda e: dashboard_canvas.configure(scrollregion=dashboard_canvas.bbox('all')))
        dashboard_canvas.bind('<Configure>', lambda e: dashboard_canvas.itemconfigure(dashboard_window, width=e.width))
        dashboard_canvas.bind('<Enter>', lambda e: dashboard_canvas.bind_all('<MouseWheel>', lambda ev: dashboard_canvas.yview_scroll(-int(ev.delta/120), 'units')))
        dashboard_canvas.bind('<Leave>', lambda e: dashboard_canvas.unbind_all('<MouseWheel>'))
        self.dashboard_page = container
        caster_page = pages['Caster']
        position_page = pages['Position']
        output_page = pages['Outputs']
        settings_page = pages['Settings']
        self.tabs.select(pages["Dashboard"])
        ttk.Label(container, text='Live status and RTCM validation. Configure the caster, position and outputs in their tabs.', wraplength=850).pack(anchor='w', pady=(3, 10))
        modes = ttk.Frame(container)
        modes.pack(fill='x', pady=(0, 10))
        ttk.Label(modes, text='Operating mode:').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(modes, text='Option B — Logger only', variable=self.mode, value='logger').pack(side='left', padx=(0, 15))
        ttk.Radiobutton(modes, text='Option A — Bridge + logger', variable=self.mode, value='bridge').pack(side='left')
        # Distinct sections for caster, position source, and independent RTCM destinations.
        caster = self.make_section(caster_page, 'NTRIP caster connection')
        def field(parent, key, label, row, col, password=False):
            group = ttk.Frame(parent)
            group.grid(row=row, column=col, sticky='ew', padx=5, pady=3)
            ttk.Label(group, text=label).pack(anchor='w')
            ttk.Entry(group, textvariable=self.vars[key], show='*' if password else '').pack(fill='x')
            return group
        for i,key in enumerate(('host','port','mount','user','password','gga-interval','retry-seconds','max-retry-seconds','connect-timeout','idle-timeout')):
            label = next(label for k,label,_ in FIELDS if k==key)
            group=field(caster,key,label,i//2,i%2,key=='password')
            if key=='mount':
                self.mount_button=ttk.Button(group,text='Search caster mountpoints...',command=self.browse_mountpoints)
                self.mount_button.pack(anchor='e')
        caster.columnconfigure(0,weight=1);caster.columnconfigure(1,weight=1)
        ttk.Checkbutton(caster,text='TLS / SSL',variable=self.tls).grid(row=5,column=0,sticky='w',padx=5)
        position_frame = self.make_section(position_page, 'GGA position source')
        ttk.Checkbutton(position_frame,text='Use static position (no input COM port required)',variable=self.static_position,command=self.update_position_source).pack(anchor='w')
        position_fields=ttk.Frame(position_frame);position_fields.pack(fill='x')
        self.position_entries=[]
        for label,var in [('Latitude (degrees)',self.latitude),('Longitude (degrees)',self.longitude),('Altitude (m)',self.altitude)]:
            group=ttk.Frame(position_fields);group.pack(side='left',fill='x',expand=True,padx=4)
            ttk.Label(group,text=label).pack(anchor='w')
            entry=ttk.Entry(group,textvariable=var);entry.pack(fill='x');self.position_entries.append(entry)
        serialrow=ttk.Frame(position_frame);serialrow.pack(fill='x',pady=3)
        ttk.Label(serialrow,text='Receiver input COM:').pack(side='left')
        self.com_dropdown=ttk.Combobox(serialrow,textvariable=self.vars['serial'],state='normal',width=12)
        self.com_dropdown.pack(side='left',padx=5)
        ttk.Button(serialrow,text='Refresh COM ports',command=self.refresh_com_ports).pack(side='left')
        ttk.Label(serialrow,text='Baud:').pack(side='left',padx=(12,3))
        ttk.Entry(serialrow,textvariable=self.vars['baud'],width=10).pack(side='left')
        for key in ('serial-retry-seconds','serial-hwid','gga-timeout'):
            label=next(label for k,label,_ in FIELDS if k==key)
            group=ttk.Frame(position_frame);group.pack(side='left',fill='x',expand=True,padx=4)
            ttk.Label(group,text=label).pack(anchor='w')
            ttk.Entry(group,textvariable=self.vars[key]).pack(fill='x')
        self.update_position_source()
        output = self.make_section(output_page, 'RTCM outputs (select one or more)')
        ttk.Checkbutton(output,text='Save RTCM to files',variable=self.save_file).grid(row=0,column=0,sticky='w')
        logrow=ttk.Frame(output);logrow.grid(row=0,column=1,sticky='ew')
        ttk.Entry(logrow,textvariable=self.vars['log-dir']).pack(side='left',fill='x',expand=True)
        ttk.Button(logrow,text='Browse...',command=self.browse).pack(side='left',padx=3)
        ttk.Checkbutton(output,text='Forward to receiver input COM',variable=self.forward_input).grid(row=1,column=0,sticky='w')
        ttk.Checkbutton(output,text='Forward to additional COM port',variable=self.output_serial_enabled).grid(row=2,column=0,sticky='w')
        comrow=ttk.Frame(output);comrow.grid(row=2,column=1,sticky='ew')
        self.output_com_dropdown=ttk.Combobox(comrow,textvariable=self.output_serial,width=14)
        self.output_com_dropdown.pack(side='left')
        ttk.Label(comrow,text='Baud').pack(side='left',padx=5)
        ttk.Entry(comrow,textvariable=self.output_baud,width=10).pack(side='left')
        ttk.Button(comrow,text='Refresh',command=self.refresh_com_ports).pack(side='left',padx=5)
        ttk.Checkbutton(output,text='Ethernet TCP server (raw RTCM)',variable=self.tcp_enabled).grid(row=3,column=0,sticky='w')
        tcprow=ttk.Frame(output);tcprow.grid(row=3,column=1,sticky='ew')
        ttk.Label(tcprow,text='Bind IP').pack(side='left')
        ttk.Entry(tcprow,textvariable=self.tcp_bind,width=17).pack(side='left',padx=4)
        ttk.Label(tcprow,text='Port').pack(side='left')
        ttk.Entry(tcprow,textvariable=self.tcp_port,width=8).pack(side='left',padx=4)
        ttk.Checkbutton(output,text='Ethernet UDP output (unicast/broadcast)',variable=self.udp_enabled).grid(row=4,column=0,sticky='w')
        udprow=ttk.Frame(output);udprow.grid(row=4,column=1,sticky='ew')
        ttk.Label(udprow,text='Destination IP').pack(side='left')
        ttk.Entry(udprow,textvariable=self.udp_host,width=17).pack(side='left',padx=4)
        ttk.Label(udprow,text='Port').pack(side='left')
        ttk.Entry(udprow,textvariable=self.udp_port,width=8).pack(side='left',padx=4)
        output.columnconfigure(0,weight=1);output.columnconfigure(1,weight=2)
        appearance = self.make_section(settings_page, 'Appearance')
        ttk.Label(appearance, text='Color theme (applies immediately):').pack(anchor='w')
        ttk.Combobox(appearance, textvariable=self.theme_choice, values=('OMG Light', 'OMG Dark'), state='readonly', width=24).pack(anchor='w', pady=5)
        self.theme_choice.trace_add('write', lambda *_: self.apply_theme())
        ttk.Label(appearance, text='OMG Light uses the blue and white colors from the OMG icon.').pack(anchor='w')
        rotation = self.make_section(settings_page, 'File rotation')
        ttk.Label(rotation, text='Start a new RTCM file:').pack(anchor='w')
        for value, label in [('connection','Every caster connection'),('hourly','Every UTC hour'),('size','After selected file size')]:
            ttk.Radiobutton(rotation, text=label, value=value, variable=self.rotation).pack(anchor='w')
        rr = ttk.Frame(rotation); rr.pack(anchor='w', pady=5)
        ttk.Label(rr, text='Size threshold (MB):').pack(side='left')
        ttk.Entry(rr, textvariable=self.rotation_mb, width=10).pack(side='left', padx=5)
        profiles = self.make_section(settings_page, 'Named configuration profiles')
        ttk.Label(profiles, text='Profile name:').pack(anchor='w')
        ttk.Entry(profiles, textvariable=self.profile_name).pack(fill='x')
        buttons = ttk.Frame(profiles); buttons.pack(fill='x', pady=6)
        ttk.Button(buttons, text='Save profile', command=self.save_profile).pack(side='left', padx=3)
        ttk.Button(buttons, text='Load profile', command=self.load_profile).pack(side='left', padx=3)
        ttk.Button(buttons, text='Delete profile', command=self.delete_profile).pack(side='left', padx=3)
        ttk.Label(profiles, text='Profiles exclude caster passwords. Enter password each time you open the application.').pack(anchor='w')
        metrics = self.make_section(container, 'RTCM quality / GGA / output monitoring')
        descriptions = [('rate','Receive rate'),('valid','Valid RTCM frames'),('crc','CRC failures'),
                        ('types','Message types'),('gga','GGA position / fix / satellites'),('gga_age','GGA age'),
                        ('file','Current RTCM file'),('input','Input COM forwarded'),('serial','Extra COM output'),
                        ('tcp','TCP output'),('udp','UDP output')]
        for i, (key, label) in enumerate(descriptions):
            ttk.Label(metrics, text=label + ':').grid(row=i, column=0, sticky='w', padx=4, pady=2)
            ttk.Label(metrics, textvariable=self.dashboard[key], wraplength=650).grid(row=i, column=1, sticky='w', padx=4, pady=2)
        metrics.columnconfigure(1, weight=1)
        actions = ttk.Frame(self.root, padding=(12, 4))
        actions.pack(fill='x', pady=5)
        self.start_btn = ttk.Button(actions, text='Start', command=self.start)
        self.start_btn.pack(side='left', padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text='Stop', command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='Save settings', command=self.save_settings).pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='Open log folder', command=self.open_logs).pack(side='left')
        ttk.Label(actions, textvariable=self.status).pack(side='right')
        validation = self.make_section(container, 'RTCM collection and logging validation')
        for col, (heading, variable) in enumerate((('Received', self.validation_received), ('Written to file', self.validation_logged), ('Current file size', self.validation_size), ('Last correction', self.validation_last))):
            cell = ttk.Frame(validation)
            cell.grid(row=0, column=col, sticky='w', padx=(0, 20))
            ttk.Label(cell, text=heading).pack(anchor='w')
            ttk.Label(cell, textvariable=variable, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
            validation.columnconfigure(col, weight=1)
        ttk.Label(validation, textvariable=self.validation_health).grid(row=1, column=0, columnspan=4, sticky='w', pady=(6, 0))
        events_panel = ttk.LabelFrame(self.root, text='Live connection events (always visible)', padding=6)
        events_panel.pack(fill='both', expand=False, padx=12, pady=(0, 8))
        self.console = scrolledtext.ScrolledText(events_panel, height=11, state='disabled', font=('Consolas', 9))
        self.console.pack(fill='both', expand=True)
        self.apply_theme()
        ttk.Label(container, text='Password is used for this session only and is never stored in settings. Close Lefebure before starting: only one program can open the COM port.', wraplength=850).pack(anchor='w', pady=(8, 0))

    def apply_theme(self):
        """Apply OMG-inspired light/dark colors to ttk and classic Tk widgets."""
        dark = self.theme_choice.get() == 'OMG Dark'
        c = ({'bg':'#152331','panel':'#1d3041','entry':'#233a4c','fg':'#ecf6fa',
              'muted':'#b9d2e0','accent':'#55b5e5','selected':'#315c77','border':'#365367'} if dark else
             {'bg':'#f2f8fc','panel':'#ffffff','entry':'#ffffff','fg':'#193b52',
              'muted':'#48697c','accent':'#2187bb','selected':'#d7eef9','border':'#bddbe9'})
        style = ttk.Style(self.root)
        style.theme_use('clam')
        self.root.configure(background=c['bg'])
        style.configure('.', background=c['bg'], foreground=c['fg'], font=('Segoe UI', 9))
        style.configure('TFrame', background=c['bg'])
        style.configure('TLabelframe', background=c['panel'], bordercolor=c['border'], relief='solid')
        style.configure('TLabelframe.Label', background=c['bg'], foreground=c['accent'], font=('Segoe UI', 10, 'bold'))
        style.configure('TLabel', background=c['bg'], foreground=c['fg'])
        style.configure('TButton', background=c['selected'], foreground=c['fg'], bordercolor=c['border'], padding=5)
        style.map('TButton', background=[('active', c['accent']), ('pressed', c['accent'])], foreground=[('active', '#ffffff')])
        style.configure('TEntry', fieldbackground=c['entry'], foreground=c['fg'], bordercolor=c['border'], insertcolor=c['fg'])
        style.configure('TCombobox', fieldbackground=c['entry'], background=c['panel'], foreground=c['fg'], arrowcolor=c['accent'])
        style.map('TCombobox', fieldbackground=[('readonly', c['entry'])], foreground=[('readonly', c['fg'])])
        style.configure('TCheckbutton', background=c['bg'], foreground=c['fg'])
        style.configure('TRadiobutton', background=c['bg'], foreground=c['fg'])
        style.map('TCheckbutton', background=[('active', c['bg'])])
        style.map('TRadiobutton', background=[('active', c['bg'])])
        style.configure('TNotebook', background=c['bg'], bordercolor=c['border'])
        style.configure('TNotebook.Tab', background=c['selected'] if not dark else c['panel'], foreground=c['fg'], padding=(15, 8))
        style.map('TNotebook.Tab', background=[('selected', c['accent'])], foreground=[('selected', '#ffffff')])
        style.configure('Vertical.TScrollbar', background=c['selected'], troughcolor=c['bg'], arrowcolor=c['fg'])
        try:
            self.dashboard_canvas.configure(background=c['bg'])
            self.console.configure(background=c['entry'], foreground=c['fg'], insertbackground=c['fg'],
                                   selectbackground=c['accent'], selectforeground='#ffffff')
            self.root.option_add('*TCombobox*Listbox.background', c['entry'])
            self.root.option_add('*TCombobox*Listbox.foreground', c['fg'])
            self.root.option_add('*TCombobox*Listbox.selectBackground', c['accent'])
        except AttributeError:
            pass

    def update_position_source(self):
        enabled = self.static_position.get()
        for entry in self.position_entries:
            entry.configure(state='normal' if enabled else 'disabled')
        self.com_dropdown.configure(state='disabled' if enabled else 'normal')

    def browse(self):
        path = filedialog.askdirectory()
        if path:
            self.vars['log-dir'].set(path)

    def save_settings(self):
        data = {k: v.get() for k, v in self.vars.items() if k != 'password'}
        data.update(mode=self.mode.get(), tls=self.tls.get(), static_position=self.static_position.get(), latitude=self.latitude.get(), longitude=self.longitude.get(), altitude=self.altitude.get(), theme=self.theme_choice.get(), rotation=self.rotation.get(), rotation_mb=self.rotation_mb.get())
        for key in ('save_file','forward_input','output_serial_enabled','output_serial','output_baud','tcp_enabled','tcp_bind','tcp_port','udp_enabled','udp_host','udp_port'):
            data[key] = getattr(self,key).get()
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
            self.append('Settings saved (password excluded).')
        except OSError as exc:
            messagebox.showerror('Save error', str(exc))

    def load_settings(self):
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            for k, var in self.vars.items():
                if k != 'password' and k in data:
                    var.set(str(data[k]))
            if data.get('mode') in ('logger', 'bridge'):
                self.mode.set(data['mode'])
            self.tls.set(bool(data.get('tls', False)))
            self.static_position.set(bool(data.get('static_position', False)))
            for key in ('latitude', 'longitude', 'altitude'):
                if key in data: getattr(self, key).set(str(data[key]))
            for key in ('save_file','forward_input','output_serial_enabled','output_serial','output_baud','tcp_enabled','tcp_bind','tcp_port','udp_enabled','udp_host','udp_port'):
                if key in data: getattr(self,key).set(data[key])
            self.theme_choice.set(data.get('theme', 'OMG Light'))
            self.rotation.set(data.get('rotation','connection'))
            self.rotation_mb.set(str(data.get('rotation_mb','100')))
            self.update_position_source()
        except (OSError, ValueError):
            pass

    def command_args(self):
        for k in (('host', 'mount') if self.static_position.get() else ('host', 'mount', 'serial')):
            if not self.vars[k].get().strip():
                raise ValueError(f'{k} is required')
        for k in ('port', 'baud'):
            if int(self.vars[k].get()) <= 0:
                raise ValueError(f'{k} must be positive')
        for k in ('gga-interval', 'serial-retry-seconds', 'retry-seconds', 'max-retry-seconds', 'connect-timeout'):
            if float(self.vars[k].get()) <= 0:
                raise ValueError(f'{k} must be positive')
        for k in ('idle-timeout', 'gga-timeout'):
            if float(self.vars[k].get()) < 0:
                raise ValueError(f'{k} cannot be negative')
        if float(self.rotation_mb.get()) <= 0: raise ValueError('Rotation size must be positive')
        args = ['--mode', self.mode.get(), '--rotate', self.rotation.get(), '--rotate-mb', self.rotation_mb.get()]
        if self.static_position.get():
            if self.mode.get() == 'bridge' or self.forward_input.get():
                raise ValueError('Forwarding to input COM requires serial position source')
            lat, lon, alt = float(self.latitude.get()), float(self.longitude.get()), float(self.altitude.get())
            import math
            if not all(map(math.isfinite, (lat, lon, alt))) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('Latitude must be -90..90 and longitude -180..180; all values must be finite')
            args += ['--static-position', '--latitude', str(lat), '--longitude', str(lon), '--altitude', str(alt)]
        for k, _, _ in FIELDS:
            args += ['--' + k, self.vars[k].get().strip()]
        if self.tls.get(): args.append('--tls')
        if not self.save_file.get(): args.append('--no-file')
        if self.forward_input.get(): args.append('--forward-input')
        if self.output_serial_enabled.get():
            if not self.output_serial.get().strip(): raise ValueError('Select an additional output COM port')
            args += ['--output-serial',self.output_serial.get().strip(),'--output-baud',self.output_baud.get().strip()]
        if self.tcp_enabled.get():
            args += ['--tcp-listen','--tcp-bind',self.tcp_bind.get().strip(),'--tcp-port',self.tcp_port.get().strip()]
        if self.udp_enabled.get():
            args += ['--udp-send','--udp-host',self.udp_host.get().strip(),'--udp-port',self.udp_port.get().strip()]
        if not (self.save_file.get() or self.forward_input.get() or self.mode.get()=='bridge' or self.output_serial_enabled.get() or self.tcp_enabled.get() or self.udp_enabled.get()):
            raise ValueError('Select at least one RTCM output')
        return args

    def start(self):
        if self.proc is not None:
            return
        try:
            args = self.command_args()
            # In frozen builds sys.executable is this GUI exe, not python.exe.
            if getattr(sys, 'frozen', False):
                command = [sys.executable, '--worker'] + args
            else:
                command = [sys.executable, '-u', str(Path(__file__).resolve()), '--worker'] + args
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            self.proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding='utf-8', errors='replace', bufsize=1,
                                         startupinfo=startupinfo)
            threading.Thread(target=self.read_output, args=(self.proc,), daemon=True).start()
            self.start_btn.configure(state='disabled')
            self.stop_btn.configure(state='normal')
            self.status.set('Running / connecting')
            self.set_config_locked(True)
            self.append('Starting ' + self.mode.get() + ' mode...')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Cannot start', str(exc))

    def read_output(self, proc):
        try:
            for line in proc.stdout:
                self.lines.put(line.rstrip('\n'))
        except (OSError, ValueError) as exc:
            self.lines.put(f'Output error: {exc}')
        finally:
            proc.wait()
            self.lines.put(('__EXIT__', proc.returncode))

    @staticmethod
    def pretty_bytes(value):
        value = float(value)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if abs(value) < 1024 or unit == 'GB':
                return f'{value:,.0f} {unit}' if unit == 'B' else f'{value:,.2f} {unit}'
            value /= 1024

    def update_telemetry(self, line):
        try:
            detail = line.split('[telemetry]', 1)[1].strip()
            values = dict(item.split('=', 1) for item in detail.split())
            received = int(values['received'])
            logged = int(values['logged'])
            file_size = int(values['file_size'])
            age = float(values['age'])
            enabled = values['file_enabled'] == '1'
        except (IndexError, KeyError, ValueError):
            return
        self.validation_received.set(self.pretty_bytes(received))
        self.validation_logged.set(self.pretty_bytes(logged) if enabled else 'Disabled')
        self.validation_size.set(self.pretty_bytes(file_size) if enabled else 'Disabled')
        self.validation_last.set(f'{age:.1f} s ago')
        self.validation_health.set('Receiving RTCM / writing to file' if enabled and received > 0 and logged > 0 and age < 5 else
                                   'Receiving RTCM / file logging disabled' if not enabled and received > 0 and age < 5 else
                                   'No recent correction data' if age >= 5 else 'Connected / awaiting corrections')
        import time
        self.last_telemetry_at = time.monotonic()

    def update_metrics(self, line):
        try:
            data = json.loads(line.partition('] ')[2])
            self.dashboard['rate'].set(f"{self.pretty_bytes(data['rate'])}/s")
            self.dashboard['valid'].set(str(data['valid']))
            self.dashboard['crc'].set(str(data['bad_crc']))
            self.dashboard['types'].set(', '.join(f'{k}: {v}' for k,v in data['types'].items()) or 'Waiting')
            pos = data.get('gga') or {}
            if pos.get('latitude') is not None and pos.get('longitude') is not None:
                self.dashboard['gga'].set(f"{pos['latitude']:.7f}, {pos['longitude']:.7f} | alt {pos.get('altitude')} m | fix {pos.get('fix')} | sats {pos.get('satellites')}")
            else: self.dashboard['gga'].set('Waiting for GGA')
            self.dashboard['gga_age'].set(str(data.get('gga_age')) + ' s')
            self.dashboard['file'].set(data.get('file') or 'File logging disabled')
            counts = data.get('outputs', {}); errors = data.get('errors', {})
            for key in ('input','serial','tcp','udp'):
                extra = f" | {data['tcp_clients']} client(s)" if key == 'tcp' else ''
                if key == 'serial': extra = (' | connected' if data['serial_output_connected'] else ' | not connected')
                self.dashboard[key].set(f"{self.pretty_bytes(counts.get(key, 0))} | errors: {errors.get(key, 0)}{extra}")
        except (ValueError, KeyError, TypeError):
            pass

    def set_config_locked(self, locked):
        # Disable editable configuration widgets while a recording is active.
        for i in (1,2,3,4):
            self.tabs.tab(i, state='disabled' if locked else 'normal')
        if locked:
            self.tabs.select(0)

    def _profile_path(self):
        import re
        name = self.profile_name.get().strip()
        if not name or not re.fullmatch(r'[A-Za-z0-9 _-]{1,60}', name):
            raise ValueError('Profile name must contain only letters, numbers, spaces, hyphens and underscores')
        return self.profile_dir / (name + '.json')

    def save_profile(self):
        try:
            path = self._profile_path()
            self.save_settings()
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2), encoding='utf-8')
            self.append(f'Profile saved: {path.stem}')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Profile save failed', str(exc))

    def load_profile(self):
        try:
            path = self._profile_path()
            data = json.loads(path.read_text(encoding='utf-8'))
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding='utf-8')
            self.load_settings()
            self.vars['password'].set('')
            self.append(f'Profile loaded: {path.stem} (enter caster password)')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Profile load failed', str(exc))

    def delete_profile(self):
        try:
            path = self._profile_path()
            if messagebox.askyesno('Delete profile?', f'Delete {path.stem}?'):
                path.unlink()
                self.append(f'Profile deleted: {path.stem}')
        except (ValueError, OSError) as exc:
            messagebox.showerror('Profile delete failed', str(exc))

    def poll(self):
        for _ in range(100):
            try:
                item = self.lines.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and item[0] == '__MOUNTS__':
                self.append(f'Sourcetable returned {len(item[1])} advertised mountpoints.')
                self.show_mountpoints(item[1])
            elif isinstance(item, tuple) and item[0] == '__MOUNTS_ERROR__':
                self.mount_button.configure(state='normal')
                self.status.set('Stopped')
                self.append('Sourcetable query failed: ' + item[1])
                messagebox.showerror('Mountpoint lookup failed', item[1])
            elif isinstance(item, tuple) and item[0] == '__EXIT__':
                self.append(f'Worker exited (code {item[1]}).')
                self.proc = None
                self.start_btn.configure(state='normal')
                self.stop_btn.configure(state='disabled')
                self.status.set('Stopped' if item[1] == 0 else 'Stopped / error')
                self.set_config_locked(False)
                self.validation_health.set('Stopped')
                self.last_telemetry_at = None
            elif isinstance(item, str) and item.startswith('[metrics_json]'):
                self.update_metrics(item)
            elif isinstance(item, str) and '[telemetry]' in item:
                self.update_telemetry(item)
            else:
                self.append(item)
                if '[connected]' in item:
                    self.status.set('Connected')
                elif '[serial_wait]' in item or '[serial_disconnected]' in item or '[serial_retry]' in item:
                    self.status.set('Waiting for COM port')
                elif '[serial_connected]' in item:
                    self.status.set('COM connected / connecting caster')
                elif '[disconnected]' in item or '[connect_failed]' in item:
                    self.status.set('Reconnecting')
        import time
        if self.last_telemetry_at is not None and time.monotonic() - self.last_telemetry_at > 4:
            self.validation_health.set('No live telemetry — disconnected or reconnecting')
            self.validation_last.set('No recent status')
        self.root.after(100, self.poll)

    def append(self, line):
        self.console.configure(state='normal')
        self.console.insert('end', line + '\n')
        self.console.see('end')
        self.console.configure(state='disabled')

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.append('Stopping worker...')
            self.proc.terminate()
            self.status.set('Stopping')

    def open_logs(self):
        path = Path(self.vars['log-dir'].get()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(str(path))
        else:
            messagebox.showinfo('Log directory', str(path))

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno('Stop logging?', 'Stop the active logger and exit?'):
                return
            self.stop()
        self.root.destroy()

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        core_main(sys.argv[2:])
    else:
        root = tk.Tk()
        ttk.Style().theme_use('clam')
        App(root)
        root.mainloop()
