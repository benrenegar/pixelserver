from __future__ import annotations

import asyncio, logging, threading, time
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Gio
from PIL import Image
from .models import FrameConfig, FrameType, PanelState, PANEL_WIDTH, PANEL_HEIGHT
from .rendering import render_frame, FONT_ALIASES
from .ipixel import IPixelClient, discover_panels

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

class PixelPreview(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(); self.image = Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT)); self.set_content_width(720); self.set_content_height(180); self.set_draw_func(self.draw)
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
        self.frame=frame; self.settings=frame.merged_settings(); box=self.get_content_area(); box.set_spacing(12); box.set_margin_top(16); box.set_margin_bottom(16); box.set_margin_start(16); box.set_margin_end(16)
        title=Gtk.Label(label=f"Configure {frame.frame_type.value.lower()}"); title.add_css_class("title-2"); title.set_halign(Gtk.Align.START); box.append(title)
        self.widgets={}; self.build(box); self.add_button("Cancel", Gtk.ResponseType.CANCEL); self.add_button("Apply", Gtk.ResponseType.OK)
    def color_button(self, key):
        btn=Gtk.ColorButton(); c=Gdk.RGBA(); r,g,b=self.settings.get(key,(0,0,0)); c.red=r/255; c.green=g/255; c.blue=b/255; c.alpha=1; btn.set_rgba(c); self.widgets[key]=btn; return btn
    def row(self, box, label, widget):
        l=Gtk.Label(label=label); l.set_halign(Gtk.Align.START); box.append(l); box.append(widget)
    def combo(self, key, vals):
        c=Gtk.ComboBoxText(); [c.append_text(v) for v in vals]; c.set_active(vals.index(self.settings.get(key, vals[0])) if self.settings.get(key) in vals else 0); self.widgets[key]=c; return c
    def build(self, box):
        ft=self.frame.frame_type
        if ft is FrameType.TEXT:
            tv=Gtk.TextView(); tv.get_buffer().set_text(self.settings.get("message","")); tv.set_size_request(420,80); self.widgets["message"]=tv; self.row(box,"Message",tv)
        if ft in (FrameType.TEXT, FrameType.CLOCK, FrameType.DATE):
            grid=Gtk.Grid(column_spacing=24,row_spacing=8); box.append(grid)
            for i,k in enumerate(("foreground","background")): grid.attach(Gtk.Label(label=k.title()+" color"),i,0,1,1); grid.attach(self.color_button(k),i,1,1,1)
        if ft is FrameType.TEXT or ft is FrameType.DATE:
            if ft is FrameType.DATE:
                e=Gtk.Entry(text=self.settings.get("date_format","%d/%m/%Y")); self.widgets["date_format"]=e; self.row(box,"Date format",e)
            self.row(box,"Font", self.combo("font", list(FONT_ALIASES) or ["Default"])); sp=Gtk.SpinButton.new_with_range(7,48,1); sp.set_value(self.settings.get("font_size",16)); self.widgets["font_size"]=sp; self.row(box,"Size",sp); hs=Gtk.SpinButton.new_with_range(-5,20,1); hs.set_value(self.settings.get("horizontal_spacing",0)); self.widgets["horizontal_spacing"]=hs; self.row(box,"Horizontal spacing",hs); vs=Gtk.SpinButton.new_with_range(-5,20,1); vs.set_value(self.settings.get("vertical_spacing",0)); self.widgets["vertical_spacing"]=vs; self.row(box,"Vertical spacing",vs)
        if ft is FrameType.TEXT:
            self.row(box,"Scrolling", self.combo("scrolling", ["None","Right to left","Left to right","Top to bottom","Bottom to top"])); sc=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,1,20,1); sc.set_value(self.settings.get("scroll_speed",4)); self.widgets["scroll_speed"]=sc; self.row(box,"Scroll speed",sc)
        if ft is FrameType.IMAGE:
            img=Gtk.Image(); self.widgets["preview"]=img
            def choose(_):
                dlg=Gtk.FileChooserNative(title="Select image", transient_for=self, action=Gtk.FileChooserAction.OPEN); dlg.connect("response", lambda d,r: self.pick(d,r,img)); dlg.show()
            b=Gtk.Button(label="Select image"); b.connect("clicked", choose); self.row(box,"Image file",b); self.row(box,"Preview",img); self.row(box,"Image display", self.combo("display", ["Resize to fit","Stretch to fit","Crop to fit"]))
        if ft is FrameType.CLOCK:
            self.row(box,"12/24 hour time", self.combo("time_mode", ["24-hour","12-hour"]));
            for key,label in (("show_seconds","Show seconds"),("flash_separator","Flash separator")):
                chk=Gtk.CheckButton(label=label); chk.set_active(self.settings.get(key,False)); self.widgets[key]=chk; box.append(chk)
    def pick(self, dlg, resp, img):
        if resp == Gtk.ResponseType.ACCEPT: self.settings["path"]=dlg.get_file().get_path(); img.set_from_file(self.settings["path"])
    def apply(self):
        for k,w in self.widgets.items():
            if isinstance(w,Gtk.ColorButton): c=w.get_rgba(); self.settings[k]=(round(c.red*255),round(c.green*255),round(c.blue*255))
            elif isinstance(w,Gtk.ComboBoxText): self.settings[k]=w.get_active_text()
            elif isinstance(w,Gtk.SpinButton): self.settings[k]=int(w.get_value())
            elif isinstance(w,Gtk.Scale): self.settings[k]=int(w.get_value())
            elif isinstance(w,Gtk.CheckButton): self.settings[k]=w.get_active()
            elif isinstance(w,Gtk.Entry): self.settings[k]=w.get_text()
            elif isinstance(w,Gtk.TextView): buf=w.get_buffer(); self.settings[k]=buf.get_text(buf.get_start_iter(),buf.get_end_iter(),False)
        self.frame.settings=self.settings

class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="LED Matrix Manager"); self.set_default_size(900,650); self.panels=[PanelState("Panel 1")]; self.clients={}; self.discovered_count=0; self.discovered_addresses=[]; self.discovery_complete=False; self.status_label=Gtk.Label(label="Ready"); self.status_bar=Gtk.ActionBar(); self.status_bar.pack_start(self.status_label); self.tabs=Gtk.Notebook(); self.build(); self.start_discovery()
    def build(self):
        root=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); root.set_margin_top(10); root.set_margin_start(10); root.set_margin_end(10); self.set_child(root); title=Gtk.Label(label="LED Matrix Manager"); title.add_css_class("title-1"); title.set_halign(Gtk.Align.START); root.append(title); self.tabs.set_vexpand(True); root.append(self.tabs); root.append(self.status_bar); self.refresh_tabs()
    def refresh_tabs(self):
        while self.tabs.get_n_pages(): self.tabs.remove_page(0)
        for p in self.panels: self.tabs.append_page(self.panel_page(p), Gtk.Label(label=p.name))
        add=Gtk.Button(label="✚ Add Panel"); add.connect("clicked", lambda _: (self.panels.append(PanelState(f"Panel {len(self.panels)+1}")), self.refresh_tabs())); self.tabs.append_page(Gtk.Box(), add)
    def panel_page(self,p):
        box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); box.set_margin_top(20); box.set_margin_bottom(20); box.set_margin_start(20); box.set_margin_end(20); combo=Gtk.ComboBoxText(); p.combo=combo; self.populate_panel_combo(p)
        conn=Gtk.Label(label="● Connected" if p.connected else "○ Disconnected"); start=Gtk.Button(label="▶ Start Display"); stop=Gtk.Button(label="■ Stop Display"); start.connect("clicked", lambda _: self.start_panel(p)); stop.connect("clicked", lambda _: self.stop_panel(p)); head=Gtk.Box(spacing=12); [head.append(w) for w in (Gtk.Label(label="Panel"),combo,conn,start,stop)]; box.append(head)
        p.preview=PixelPreview(); box.append(p.preview); p.frames_box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); box.append(p.frames_box); self.refresh_frames(p); add=Gtk.Button(label="✚ Add Frame"); add.connect("clicked", lambda _: (p.frames.append(FrameConfig()), self.refresh_frames(p))); box.append(add); return box
    def refresh_frames(self,p):
        while (child:=p.frames_box.get_first_child()): p.frames_box.remove(child)
        for i,f in enumerate(p.frames):
            row=Gtk.Box(spacing=8); row.set_margin_top(10); row.set_margin_bottom(10); row.set_margin_start(10); row.set_margin_end(10); typec=Gtk.ComboBoxText(); [typec.append_text(t.value) for t in FrameType]; typec.set_active(list(FrameType).index(f.frame_type)); typec.connect("changed", lambda c,fr=f: (setattr(fr,"frame_type", FrameType(c.get_active_text())), setattr(fr,"settings",{})))
            dur=Gtk.SpinButton.new_with_range(1,3600,1); dur.set_value(f.duration); dur.connect("value-changed", lambda s,fr=f: setattr(fr,"duration",s.get_value()))
            settings=Gtk.Button(label="🔧 Settings"); settings.connect("clicked", lambda _,fr=f: self.open_settings(fr))
            up=Gtk.Button(label="▲"); down=Gtk.Button(label="▼"); rem=Gtk.Button(label="✖"); up.connect("clicked", lambda _,idx=i: self.move(p,idx,-1)); down.connect("clicked", lambda _,idx=i: self.move(p,idx,1)); rem.connect("clicked", lambda _,fr=f: (p.frames.remove(fr), self.refresh_frames(p)))
            [row.append(w) for w in (Gtk.Label(label="Type"), typec, Gtk.Label(label="Duration (secs)"), dur, settings, up, down, rem)]
            group=Gtk.Frame(label=f"Frame {i+1}"); group.set_child(row); p.frames_box.append(group)
    def move(self,p,i,d):
        j=i+d
        if 0<=j<len(p.frames): p.frames[i],p.frames[j]=p.frames[j],p.frames[i]; self.refresh_frames(p)
    def open_settings(self,fr):
        dlg=FrameDialog(self,fr); dlg.connect("response", lambda d,r: (d.apply() if r==Gtk.ResponseType.OK else None, d.destroy())); dlg.show()
    def populate_panel_combo(self, p):
        p.combo.remove_all()
        if not self.discovery_complete:
            p.combo.append_text("Discovering panels..."); p.combo.set_active(0); p.combo.set_sensitive(False); return
        if not self.discovered_addresses:
            p.combo.append_text("No panels found"); p.combo.set_active(0); p.combo.set_sensitive(False); return
        for address in self.discovered_addresses: p.combo.append_text(address)
        p.combo.set_active(0); p.combo.set_sensitive(True)
    def start_discovery(self): threading.Thread(target=self.discovery_worker, daemon=True).start()
    def discovery_worker(self):
        asyncio.run(discover_panels(lambda d: GLib.idle_add(self.add_discovered,d), 4.0))
        GLib.idle_add(self.discovery_finished)
    def add_discovered(self,d):
        if d.address in self.discovered_addresses:
            return
        self.discovered_addresses.append(d.address)
        self.discovered_count = len(self.discovered_addresses)
        for p in self.panels:
            if hasattr(p,"combo"):
                if self.discovered_count == 1: p.combo.remove_all()
                p.combo.append_text(d.address)
                if p.combo.get_active() < 0: p.combo.set_active(0)
        self.set_status(f"Discovered {d.name} at {d.address}")
    def discovery_finished(self):
        self.discovery_complete = True
        for p in self.panels:
            if hasattr(p,"combo"): self.populate_panel_combo(p)
        if self.discovered_count == 0: self.set_status("No panels found")
    def set_status(self, message): self.status_label.set_text(message)
    def start_panel(self,p):
        active = p.combo.get_active_text() if hasattr(p, "combo") else p.address
        if active and active not in ("Panel MAC Address", "Discovering panels...", "No panels found"): p.address = active
        p.running=True; self.set_status("Display running"); threading.Thread(target=self.run_loop,args=(p,),daemon=True).start()
    def stop_panel(self,p): p.running=False; self.set_status("Display stopped")
    def run_loop(self,p):
        client = None
        if p.address:
            try:
                client = IPixelClient(p.address)
                asyncio.run(client.connect())
                GLib.idle_add(self.set_status, f"Connected to {p.address}")
            except Exception as exc:
                logger.exception("Bluetooth connection failed")
                GLib.idle_add(self.set_status, f"Preview only: Bluetooth connection failed; see terminal: {exc}")
                client = None
        tick=0
        while p.running:
            for fr in list(p.frames):
                end=time.time()+fr.duration
                while p.running and time.time()<end:
                    img=render_frame(fr,tick)
                    GLib.idle_add(p.preview.set_image,img)
                    if client is not None:
                        try: asyncio.run(client.send_image(img))
                        except Exception as exc:
                            logger.exception("Bluetooth send failed")
                            GLib.idle_add(self.set_status, f"Bluetooth send failed; see terminal: {exc}"); client = None
                    tick+=1; time.sleep(.25)
        if client is not None:
            try: asyncio.run(client.disconnect())
            except Exception: pass

def main():
    app=Gtk.Application(application_id="uk.org.ledpanel.manager", flags=Gio.ApplicationFlags.DEFAULT_FLAGS); app.connect("activate", lambda a: MainWindow(a).present()); return app.run()
if __name__ == "__main__": main()
