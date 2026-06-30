from __future__ import annotations

import asyncio, json, logging, os, shutil, threading, time
from pathlib import Path
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Gio
from PIL import Image
from .models import FrameConfig, FrameType, PanelState, PANEL_WIDTH, PANEL_HEIGHT
from .rendering import render_frame, FONT_ALIASES
from .ipixel import IPixelClient, discover_panels

LOG_LEVEL = os.environ.get("LEDPANEL_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logging.getLogger("bleak").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ledpanel-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
ASSET_DIR = CONFIG_DIR / "assets"
PANEL_PLACEHOLDER_ITEMS = ("Panel MAC Address", "Discovering panels...", "No panels found")
IMAGE_FILTERS = ("*.png", "*.jpg", "*.jpeg", "*.gif", "*.bmp")


class PanelConnection:
    def __init__(self, address: str):
        self.address = address; self.client = IPixelClient(address); self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name=f"panel-{address}", daemon=True); self.thread.start()
    def _run_loop(self): asyncio.set_event_loop(self.loop); self.loop.run_forever()
    def submit(self, coro): return asyncio.run_coroutine_threadsafe(coro, self.loop)
    def connect(self): return self.submit(self.client.connect())
    def send_image(self, image): return self.submit(self.client.send_image(image))
    def clear(self): return self.submit(self.client.clear_display())
    def set_brightness(self, value): return self.submit(self.client.set_brightness(value))
    def disconnect(self):
        fut = self.submit(self.client.disconnect())
        try: return fut.result(timeout=10)
        finally: self.loop.call_soon_threadsafe(self.loop.stop)


class PixelPreview(Gtk.DrawingArea):
    def __init__(self, width=720, height=120):
        super().__init__(); self.image = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT)); self.set_content_width(width); self.set_content_height(height); self.set_draw_func(self.draw)
    def set_image(self, image): self.image = image.copy(); self.queue_draw()
    def draw(self, area, cr, w, h):
        cell = min(w / PANEL_WIDTH, h / PANEL_HEIGHT); ox=(w-cell*PANEL_WIDTH)/2; oy=(h-cell*PANEL_HEIGHT)/2
        cr.set_source_rgb(0,0,0); cr.rectangle(ox,oy,cell*PANEL_WIDTH,cell*PANEL_HEIGHT); cr.fill()
        px=self.image.load()
        for y in range(PANEL_HEIGHT):
            for x in range(PANEL_WIDTH):
                r,g,b=px[x,y]; cr.set_source_rgb(r/255,g/255,b/255); cr.arc(ox+x*cell+cell/2, oy+y*cell+cell/2, max(1,cell*.28), 0, 6.283); cr.fill()


class FrameDialog(Gtk.Dialog):
    def __init__(self, parent, frame):
        super().__init__(title=f"{frame.frame_type.value} Settings", transient_for=parent, modal=True)
        self.frame=frame; self.settings=frame.merged_settings(); self.widgets={}; self.tick=0
        box=self.get_content_area(); box.set_spacing(10); box.set_margin_top(14); box.set_margin_bottom(14); box.set_margin_start(14); box.set_margin_end(14)
        title=Gtk.Label(label=f"Configure {frame.frame_type.value.lower()}"); title.add_css_class("title-2"); title.set_halign(Gtk.Align.START); box.append(title)
        self.preview=PixelPreview(520, 90); box.append(self.preview)
        self.build(box); self.add_button("Cancel", Gtk.ResponseType.CANCEL); self.add_button("Apply", Gtk.ResponseType.OK)
        self.connect_live_updates(); self.update_preview(); GLib.timeout_add(500, self.preview_tick)
    def row(self, box, label, widget): l=Gtk.Label(label=label); l.set_halign(Gtk.Align.START); box.append(l); box.append(widget)
    def grid_pair(self, box, left_label, left_widget, right_label, right_widget):
        grid=Gtk.Grid(column_spacing=10,row_spacing=6); box.append(grid)
        grid.attach(Gtk.Label(label=left_label),0,0,1,1); grid.attach(left_widget,1,0,1,1); grid.attach(Gtk.Label(label=right_label),2,0,1,1); grid.attach(right_widget,3,0,1,1)
    def combo(self, key, vals):
        c=Gtk.ComboBoxText(); [c.append_text(v) for v in vals]; c.set_active(vals.index(self.settings.get(key, vals[0])) if self.settings.get(key) in vals else 0); self.widgets[key]=c; return c
    def spin(self, key, low, high, step=1): sp=Gtk.SpinButton.new_with_range(low, high, step); sp.set_value(self.settings.get(key, 0)); self.widgets[key]=sp; return sp
    def entry(self, key): e=Gtk.Entry(text=str(self.settings.get(key, "") or "")); self.widgets[key]=e; return e
    def color_button(self, key):
        btn=Gtk.ColorButton(); c=Gdk.RGBA(); r,g,b=self.settings.get(key,(0,0,0)); c.red=r/255; c.green=g/255; c.blue=b/255; c.alpha=1; btn.set_rgba(c); self.widgets[key]=btn; return btn
    def file_button(self, key, label="Select file"):
        btn=Gtk.Button(label=Path(self.settings.get(key) or "").name or label)
        def choose(_):
            dlg=Gtk.FileChooserNative(title=label, transient_for=self, action=Gtk.FileChooserAction.OPEN)
            dlg.connect("response", lambda d,r: self.pick_file(d,r,key,btn)); dlg.show()
        btn.connect("clicked", choose); self.widgets[key]=btn; return btn
    def pick_file(self, dlg, resp, key, btn):
        if resp == Gtk.ResponseType.ACCEPT:
            self.settings[key]=dlg.get_file().get_path(); btn.set_label(Path(self.settings[key]).name); self.update_preview()
    def add_icon_and_colors(self, box):
        self.row(box,"Icon", self.file_button("icon_path", "Select icon"))
        grid=Gtk.Grid(column_spacing=24,row_spacing=8); box.append(grid)
        for i,k in enumerate(("foreground","background")): grid.attach(Gtk.Label(label=k.title()+" color"),i,0,1,1); grid.attach(self.color_button(k),i,1,1,1)
    def add_font_spacing(self, box):
        self.grid_pair(box, "Font", self.combo("font", list(FONT_ALIASES) or ["Default"]), "Font size", self.spin("font_size",7,48))
        self.grid_pair(box, "Horizontal spacing", self.spin("horizontal_spacing",-5,20), "Vertical offset", self.spin("vertical_offset",-16,16))
    def build(self, box):
        ft=self.frame.frame_type
        if ft is FrameType.TEXT:
            tv=Gtk.TextView(); tv.get_buffer().set_text(self.settings.get("message","")); tv.set_size_request(420,70); self.widgets["message"]=tv; self.row(box,"Message",tv)
        elif ft is FrameType.DATE:
            self.row(box,"Date format", self.entry("date_format"))
        elif ft is FrameType.RSS:
            self.row(box,"Feed URL", self.entry("feed_url")); self.row(box,"Number of items", self.spin("item_count",1,20)); self.row(box,"Scroll speed", self.spin("scroll_speed",1,20))
        elif ft is FrameType.LIVE_TEXT:
            self.row(box,"Label", self.entry("label")); self.row(box,"REST URL", self.entry("rest_url"))
        elif ft is FrameType.IMAGE:
            self.row(box,"Image file", self.file_button("path", "Select image")); self.row(box,"Image display", self.combo("display", ["Resize to fit","Stretch to fit","Crop to fit"]))
        if ft in (FrameType.TEXT, FrameType.DATE, FrameType.CLOCK, FrameType.RSS, FrameType.LIVE_TEXT): self.add_icon_and_colors(box)
        if ft in (FrameType.TEXT, FrameType.DATE, FrameType.RSS, FrameType.LIVE_TEXT): self.add_font_spacing(box)
        if ft in (FrameType.TEXT, FrameType.LIVE_TEXT): self.row(box,"Scrolling", self.combo("scrolling", ["None (Wrap)","Right to left","Left to right","Top to bottom","Bottom to top"])); self.row(box,"Scroll speed", self.spin("scroll_speed",1,20))
        if ft is FrameType.CLOCK:
            self.row(box,"12/24 hour time", self.combo("time_mode", ["24-hour","12-hour"]))
            for key,label in (("show_seconds","Show seconds"),("flash_separator","Flash separator")):
                chk=Gtk.CheckButton(label=label); chk.set_active(self.settings.get(key,False)); self.widgets[key]=chk; box.append(chk)
    def collect(self):
        data=dict(self.settings)
        for k,w in self.widgets.items():
            if isinstance(w,Gtk.ColorButton): c=w.get_rgba(); data[k]=(round(c.red*255),round(c.green*255),round(c.blue*255))
            elif isinstance(w,Gtk.ComboBoxText): data[k]=w.get_active_text()
            elif isinstance(w,Gtk.SpinButton): data[k]=int(w.get_value())
            elif isinstance(w,Gtk.Scale): data[k]=int(w.get_value())
            elif isinstance(w,Gtk.CheckButton): data[k]=w.get_active()
            elif isinstance(w,Gtk.Entry): data[k]=w.get_text()
            elif isinstance(w,Gtk.TextView): buf=w.get_buffer(); data[k]=buf.get_text(buf.get_start_iter(),buf.get_end_iter(),False)
        return data
    def apply(self): self.frame.settings=self.collect()
    def update_preview(self, *_):
        temp=FrameConfig(self.frame.frame_type, self.frame.duration, self.collect())
        self.preview.set_image(render_frame(temp, self.tick))
    def preview_tick(self): self.tick+=1; self.update_preview(); return self.get_visible()
    def connect_live_updates(self):
        for w in self.widgets.values():
            if isinstance(w,Gtk.ColorButton): w.connect("color-set", self.update_preview)
            elif isinstance(w,Gtk.ComboBoxText): w.connect("changed", self.update_preview)
            elif isinstance(w,Gtk.SpinButton): w.connect("value-changed", self.update_preview)
            elif isinstance(w,Gtk.Scale): w.connect("value-changed", self.update_preview)
            elif isinstance(w,Gtk.CheckButton): w.connect("toggled", self.update_preview)
            elif isinstance(w,Gtk.Entry): w.connect("changed", self.update_preview)
            elif isinstance(w,Gtk.TextView): w.get_buffer().connect("changed", self.update_preview)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LED Matrix Manager"); self.set_default_size(1040,720)
        self.panels=self.load_state(); self.discovered_count=0; self.discovered_addresses=[]; self.discovery_complete=False
        self.status_label=Gtk.Label(label="Ready"); self.status_bar=Gtk.ActionBar(); self.status_bar.pack_start(self.status_label); self.tabs=Gtk.Notebook(); self.build(); self.start_discovery()
    def build(self):
        root=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); root.set_margin_top(10); root.set_margin_start(10); root.set_margin_end(10); self.set_child(root)
        title=Gtk.Label(label="LED Matrix Manager"); title.add_css_class("title-1"); title.set_halign(Gtk.Align.START); root.append(title); self.tabs.set_vexpand(True); root.append(self.tabs); root.append(self.status_bar); self.refresh_tabs()
    def load_state(self):
        try: raw=json.loads(CONFIG_FILE.read_text())
        except Exception: return [PanelState("Panel 1")]
        panels=[]
        for pd in raw.get("panels", []):
            frames=[FrameConfig(FrameType(fd.get("frame_type", "Text")), fd.get("duration",10), fd.get("settings",{})) for fd in pd.get("frames", [])]
            panels.append(PanelState(pd.get("name","Panel"), pd.get("address"), False, frames or [FrameConfig()], False, pd.get("brightness",80)))
        return panels or [PanelState("Panel 1")]
    def persist_path(self, path):
        if not path: return path
        try:
            ASSET_DIR.mkdir(parents=True, exist_ok=True); src=Path(path); dest=ASSET_DIR / src.name
            if src.exists() and src.resolve() != dest.resolve(): shutil.copy2(src, dest)
            return str(dest)
        except Exception: return path
    def save_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for p in self.panels:
            for fr in p.frames:
                for key in ("path","icon_path"):
                    if fr.settings.get(key): fr.settings[key]=self.persist_path(fr.settings[key])
        data={"panels":[{"name":p.name,"address":p.address,"brightness":p.brightness,"frames":[{"frame_type":f.frame_type.value,"duration":f.duration,"settings":f.settings} for f in p.frames]} for p in self.panels]}
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
    def refresh_tabs(self):
        while self.tabs.get_n_pages(): self.tabs.remove_page(0)
        for p in self.panels: self.tabs.append_page(self.panel_page(p), Gtk.Label(label=p.name))
        add=Gtk.Button(label="✚ Add Panel"); add.connect("clicked", lambda _: (self.panels.append(PanelState(f"Panel {len(self.panels)+1}")), self.save_state(), self.refresh_tabs())); self.tabs.append_page(Gtk.Box(), add)
    def panel_page(self,p):
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); box.set_margin_top(20); box.set_margin_bottom(20); box.set_margin_start(20); box.set_margin_end(20)
        combo=Gtk.ComboBoxText(); p.combo=combo; self.populate_panel_combo(p)
        p.connection_status=Gtk.Label(label="● Connected" if p.connected else "○ Disconnected"); p.connect_button=Gtk.Button(label="Disconnect" if p.connected else "Connect"); p.connect_button.connect("clicked", lambda _: self.toggle_connection(p))
        p.brightness_scale=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,0,100,1); p.brightness_scale.set_value(getattr(p,"brightness",80)); p.brightness_scale.set_size_request(160,-1); p.brightness_scale.set_sensitive(p.connected); p.brightness_scale.connect("value-changed", lambda s: self.brightness_changed(p, int(s.get_value())))
        head=Gtk.Box(spacing=12); [head.append(w) for w in (Gtk.Label(label="Panel"),combo,p.connect_button,p.connection_status,Gtk.Label(label="Brightness"),p.brightness_scale)]; box.append(head)
        p.preview=PixelPreview(); box.append(p.preview)
        p.display_button=Gtk.Button(label="Stop Display" if p.running else "Start Display"); p.display_button.connect("clicked", lambda _: self.toggle_display(p)); p.display_status=Gtk.Label(label="Running" if p.running else "Stopped"); clear=Gtk.Button(label="Clear Display"); clear.connect("clicked", lambda _: self.clear_display(p))
        controls=Gtk.Box(spacing=12); [controls.append(w) for w in (p.display_button,p.display_status,clear)]; box.append(controls)
        p.frames_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); box.append(p.frames_box); self.refresh_frames(p); add=Gtk.Button(label="✚ Add Frame"); add.connect("clicked", lambda _: (p.frames.append(FrameConfig()), self.save_state(), self.refresh_frames(p))); box.append(add); return box
    def refresh_frames(self,p):
        while (child:=p.frames_box.get_first_child()): p.frames_box.remove(child)
        multi=len(p.frames)>1
        for i,f in enumerate(p.frames):
            row=Gtk.Box(spacing=8); row.set_margin_top(10); row.set_margin_bottom(10); row.set_margin_start(10); row.set_margin_end(10)
            typec=Gtk.ComboBoxText(); [typec.append_text(t.value) for t in FrameType]; typec.set_active(list(FrameType).index(f.frame_type)); typec.connect("changed", lambda c,fr=f: (setattr(fr,"frame_type", FrameType(c.get_active_text())), setattr(fr,"settings",{}), self.save_state()))
            dur=Gtk.SpinButton.new_with_range(1,3600,1); dur.set_value(f.duration); dur.set_sensitive(multi); dur.connect("value-changed", lambda s,fr=f: (setattr(fr,"duration",s.get_value()), self.save_state()))
            settings=Gtk.Button(label="🔧 Settings"); settings.connect("clicked", lambda _,fr=f: self.open_settings(fr))
            left=Gtk.Box(spacing=8); [left.append(w) for w in (Gtk.Label(label="Type"), typec, Gtk.Label(label="Duration (secs)"), dur, settings)]; left.set_hexpand(True); row.append(left)
            up=Gtk.Button(label="▲"); down=Gtk.Button(label="▼"); rem=Gtk.Button(label="✖"); up.set_sensitive(multi and i>0); down.set_sensitive(multi and i<len(p.frames)-1); rem.set_sensitive(multi)
            up.connect("clicked", lambda _,idx=i: self.move(p,idx,-1)); down.connect("clicked", lambda _,idx=i: self.move(p,idx,1)); rem.connect("clicked", lambda _,fr=f: (p.frames.remove(fr), self.save_state(), self.refresh_frames(p)))
            [row.append(w) for w in (up,down,rem)]; group=Gtk.Frame(label=f"Frame {i+1}"); group.set_child(row); p.frames_box.append(group)
    def move(self,p,i,d):
        j=i+d
        if 0<=j<len(p.frames): p.frames[i],p.frames[j]=p.frames[j],p.frames[i]; self.save_state(); self.refresh_frames(p)
    def open_settings(self,fr):
        dlg=FrameDialog(self,fr); dlg.connect("response", lambda d,r: (d.apply() if r==Gtk.ResponseType.OK else None, self.save_state() if r==Gtk.ResponseType.OK else None, d.destroy())); dlg.show()
    def populate_panel_combo(self, p):
        p.combo.remove_all()
        if not self.discovery_complete: p.combo.append_text("Discovering panels..."); p.combo.set_active(0); p.combo.set_sensitive(False); return
        if not self.discovered_addresses: p.combo.append_text("No panels found"); p.combo.set_active(0); p.combo.set_sensitive(False); return
        for address in self.discovered_addresses: p.combo.append_text(address)
        if p.address and p.address in self.discovered_addresses: p.combo.set_active(self.discovered_addresses.index(p.address))
        else: p.combo.set_active(0)
        p.combo.set_sensitive(True)
    def start_discovery(self): threading.Thread(target=self.discovery_worker, daemon=True).start()
    def discovery_worker(self): asyncio.run(discover_panels(lambda d: GLib.idle_add(self.add_discovered,d), 4.0)); GLib.idle_add(self.discovery_finished)
    def add_discovered(self,d):
        if d.address in self.discovered_addresses: return
        self.discovered_addresses.append(d.address); self.discovered_count=len(self.discovered_addresses)
        for p in self.panels:
            if hasattr(p,"combo"):
                if self.discovered_count == 1: p.combo.remove_all()
                p.combo.append_text(d.address)
                if p.combo.get_active() < 0: p.combo.set_active(0)
        self.set_status(f"Discovered {d.name} at {d.address}")
    def discovery_finished(self): self.discovery_complete=True; [self.populate_panel_combo(p) for p in self.panels if hasattr(p,"combo")]; self.set_status("No panels found" if self.discovered_count==0 else "Discovery finished")
    def set_status(self, message): self.status_label.set_text(message)
    def selected_address(self, p):
        active = p.combo.get_active_text() if hasattr(p, "combo") else p.address
        if active and active not in PANEL_PLACEHOLDER_ITEMS: p.address = active; self.save_state()
        return p.address
    def toggle_connection(self, p): self.disconnect_panel(p) if getattr(p, "connection", None) else self.connect_panel(p)
    def connect_panel(self, p):
        address = self.selected_address(p)
        if not address: self.set_status("Select a panel before connecting"); return
        p.connect_button.set_sensitive(False); p.connection_status.set_text("◌ Connecting..."); self.set_status(f"Connecting to {address}"); threading.Thread(target=self.connect_worker, args=(p, address), daemon=True).start()
    def connect_worker(self, p, address):
        connection = PanelConnection(address)
        try: connection.connect().result(timeout=30)
        except Exception as exc:
            logger.exception("Bluetooth connection failed")
            try: connection.disconnect()
            except Exception: logger.debug("Bluetooth cleanup failed", exc_info=True)
            GLib.idle_add(self.connection_finished, p, None, f"Bluetooth connection failed; see terminal: {exc}"); return
        GLib.idle_add(self.connection_finished, p, connection, f"Connected to {address}")
    def connection_finished(self, p, connection, message):
        p.connection=connection; p.connected=connection is not None; p.connect_button.set_label("Disconnect" if p.connected else "Connect"); p.connect_button.set_sensitive(True); p.connection_status.set_text("● Connected" if p.connected else "○ Disconnected"); p.brightness_scale.set_sensitive(p.connected); self.set_status(message)
    def disconnect_panel(self, p):
        connection=getattr(p,"connection",None)
        if connection is None: return
        p.connect_button.set_sensitive(False); p.connection_status.set_text("◌ Disconnecting..."); self.set_status(f"Disconnecting from {p.address}"); threading.Thread(target=self.disconnect_worker, args=(p, connection), daemon=True).start()
    def disconnect_worker(self, p, connection):
        try: connection.disconnect(); message=f"Disconnected from {p.address}"
        except Exception as exc: logger.exception("Bluetooth disconnect failed"); message=f"Bluetooth disconnect failed; see terminal: {exc}"
        GLib.idle_add(self.connection_finished, p, None, message)
    def toggle_display(self,p): self.stop_panel(p) if p.running else self.start_panel(p)
    def start_panel(self,p):
        self.selected_address(p); p.running=True; p.display_button.set_label("Stop Display"); p.display_status.set_text("Running"); self.set_status("Display running" if getattr(p,"connection",None) else "Preview running (panel not connected)"); threading.Thread(target=self.run_loop,args=(p,),daemon=True).start()
    def stop_panel(self,p): p.running=False; p.display_button.set_label("Start Display"); p.display_status.set_text("Stopped"); self.set_status("Display stopped")
    def run_loop(self,p): asyncio.run(self.run_loop_async(p))
    async def run_loop_async(self,p):
        tick=0
        last_panel_send = 0.0
        while p.running:
            for fr in list(p.frames):
                end=time.monotonic()+fr.duration
                while p.running and time.monotonic()<end:
                    img=render_frame(fr,tick); GLib.idle_add(p.preview.set_image,img); connection=getattr(p,"connection",None)
                    now = time.monotonic()
                    if connection is not None and now - last_panel_send >= 1.0:
                        try:
                            await asyncio.wrap_future(connection.send_image(img))
                            last_panel_send = now
                        except Exception as exc:
                            logger.exception("Bluetooth send failed")
                            GLib.idle_add(self.set_status, f"Bluetooth send failed; reconnecting: {exc}")
                            last_panel_send = now
                            await asyncio.to_thread(self.reconnect_panel_after_send_failure, p, connection)
                    tick+=1; await asyncio.sleep(.25)

    def reconnect_panel_after_send_failure(self, p, old_connection):
        GLib.idle_add(self.set_status, f"Connection lost; reconnecting to {p.address}")
        try:
            old_connection.disconnect()
        except Exception:
            logger.debug("Bluetooth disconnect during reconnect failed", exc_info=True)
        p.connection = None
        p.connected = False
        GLib.idle_add(self.connection_finished, p, None, f"Disconnected from {p.address}; reconnecting")
        if not p.address:
            return
        new_connection = PanelConnection(p.address)
        try:
            new_connection.connect().result(timeout=30)
        except Exception as exc:
            logger.exception("Bluetooth reconnect failed")
            try:
                new_connection.disconnect()
            except Exception:
                logger.debug("Bluetooth reconnect cleanup failed", exc_info=True)
            GLib.idle_add(self.connection_finished, p, None, f"Bluetooth reconnect failed; see terminal: {exc}")
            return
        p.connection = new_connection
        p.connected = True
        GLib.idle_add(self.connection_finished, p, new_connection, f"Reconnected to {p.address}")
    def clear_display(self,p):
        black=Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0,0,0)); p.preview.set_image(black); connection=getattr(p,"connection",None)
        if connection: threading.Thread(target=lambda: connection.clear().result(timeout=15), daemon=True).start()
        self.set_status("Display cleared")
    def brightness_changed(self,p,value):
        p.brightness=value; self.save_state(); connection=getattr(p,"connection",None)
        if connection: threading.Thread(target=lambda: connection.set_brightness(value).result(timeout=10), daemon=True).start()

def main():
    app=Gtk.Application(application_id="uk.org.ledpanel.manager", flags=Gio.ApplicationFlags.DEFAULT_FLAGS); app.connect("activate", lambda a: MainWindow(a).present()); return app.run()
if __name__ == "__main__": main()
