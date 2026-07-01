from __future__ import annotations

import asyncio, base64, json, logging, mimetypes, os, shutil, threading, time, uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from PIL import Image
from .models import FrameConfig, FrameType, PanelState, PANEL_HEIGHT, PANEL_WIDTH
from .rendering import FONT_ALIASES, render_frame
from .ipixel import IPixelClient, discover_panels

LOG_LEVEL = os.environ.get("LEDPANEL_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logging.getLogger("bleak").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("LEDPANEL_CONFIG_DIR", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ledpanel-manager"))
CONFIG_FILE = CONFIG_DIR / "config.json"
ASSET_DIR = CONFIG_DIR / "assets"
DRAWING_DIR = ASSET_DIR / "drawings"
DIGIT_OVERRIDE_DIR = ASSET_DIR / "clock-digits"
HOST = os.environ.get("LEDPANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("LEDPANEL_PORT", "8765"))
PANEL_PLACEHOLDER_ITEMS = {"Discovering panels...", "No panels found"}


def _jsonable_panel(p: PanelState) -> dict:
    return {
        "name": p.name, "address": p.address, "connected": p.connected, "running": p.running,
        "brightness": p.brightness, "started_at": p.started_at,
        "frames": [{"frame_type": f.frame_type.value, "duration": f.duration, "settings": f.settings} for f in p.frames],
    }


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


class LedPanelService:
    def __init__(self):
        self.lock = threading.RLock(); self.panels = self.load_state(); self.discovered_addresses: list[str] = []
        self.discovery_complete = False; self.status = "Starting"; self.messages: list[str] = []; self.tick = 0; self.last_images: dict[int, Image.Image] = {}
        self._stop = threading.Event(); self.set_status("Starting")
    def load_state(self) -> list[PanelState]:
        try: raw = json.loads(CONFIG_FILE.read_text())
        except Exception: return [PanelState("Panel 1")]
        panels=[]
        for pd in raw.get("panels", []):
            frames=[]
            for fd in pd.get("frames", []):
                try: ft = FrameType(fd.get("frame_type", "Text"))
                except ValueError: continue
                frames.append(FrameConfig(ft, fd.get("duration", 10), fd.get("settings", {})))
            panels.append(PanelState(pd.get("name", "Panel"), pd.get("address"), False, frames or [FrameConfig()], False, pd.get("brightness", 80)))
        return panels or [PanelState("Panel 1")]
    def save_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data={"panels":[{"name":p.name,"address":p.address,"brightness":p.brightness,"frames":[{"frame_type":f.frame_type.value,"duration":f.duration,"settings":f.settings} for f in p.frames]} for p in self.panels]}
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
    def start(self):
        threading.Thread(target=self.discovery_worker, daemon=True).start()
        for idx, p in enumerate(self.panels):
            if p.address:
                self.connect(idx, autostart=True)
    def set_status(self, message: str):
        with self.lock:
            self.status = message
            self.messages.append(f"{time.strftime('%H:%M:%S')}  {message}")
            self.messages = self.messages[-200:]
    def scan_once(self):
        try:
            found=[]
            asyncio.run(discover_panels(lambda d: found.append(d.address), 5.0))
            with self.lock:
                self.discovered_addresses = sorted(set(found)); self.discovery_complete = True
            self.set_status(f"Discovered {len(found)} panel(s)")
        except Exception as exc:
            logger.exception("Panel discovery failed"); self.set_status(f"Discovery failed: {exc}")
    def discovery_worker(self):
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(60)
    def connect(self, idx: int, autostart: bool=False):
        threading.Thread(target=self._connect_worker, args=(idx, autostart), daemon=True).start()
    def _connect_worker(self, idx: int, autostart: bool):
        p=self.panels[idx]
        if not p.address:
            self.set_status("Select a panel before connecting")
            return
        try:
            conn=PanelConnection(p.address); conn.connect().result(timeout=30); conn.set_brightness(p.brightness).result(timeout=10)
            with self.lock: p.connection=conn; p.connected=True
            self.set_status(f"Connected to {p.address}")
            if autostart or p.running: self.play(idx)
        except Exception as exc:
            logger.exception("Connect failed"); self.set_status(f"Connect failed: {exc}")
    def disconnect(self, idx:int):
        p=self.panels[idx]; p.running=False; conn=getattr(p,"connection",None)
        if conn: threading.Thread(target=lambda: conn.disconnect(), daemon=True).start()
        p.connected=False; p.connection=None; self.set_status(f"Disconnected from {p.address or p.name}"); self.save_state()
    def play(self, idx:int):
        p=self.panels[idx]
        if p.running: return
        p.running=True; p.started_at=time.time(); self.set_status(f"Playback started for {p.name}"); threading.Thread(target=self._run_panel, args=(idx,), daemon=True).start(); self.save_state()
    def stop(self, idx:int):
        p=self.panels[idx]; p.running=False; p.started_at=None; self.set_status(f"Playback stopped for {p.name}"); self.save_state()
    def _run_panel(self, idx:int):
        p=self.panels[idx]; last_send=0.0
        while p.running:
            for fr in list(p.frames):
                end=time.monotonic()+float(fr.duration)
                while p.running and time.monotonic()<end:
                    img=render_frame(fr,self.tick); self.last_images[idx]=img; conn=getattr(p,"connection",None); now=time.monotonic()
                    if conn and now-last_send>=1:
                        try: conn.send_image(img).result(timeout=20); last_send=now
                        except Exception as exc:
                            logger.exception("Send failed"); p.connected=False; p.connection=None; self.set_status(f"Send failed; reconnecting: {exc}"); self._connect_worker(idx, False); last_send=now
                    self.tick+=1; time.sleep(.25)
    def state(self):
        with self.lock: return {"status": self.status, "messages": self.messages, "discovered": self.discovered_addresses, "fonts": list(FONT_ALIASES), "width": PANEL_WIDTH, "height": PANEL_HEIGHT, "frame_types": [t.value for t in FrameType], "panels": [_jsonable_panel(p) for p in self.panels]}

SERVICE = LedPanelService()

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>LED Matrix Manager</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@shoelace-style/shoelace@2.15.1/cdn/themes/light.css"><script type="module" src="https://cdn.jsdelivr.net/npm/@shoelace-style/shoelace@2.15.1/cdn/shoelace-autoloader.js"></script>
<style>@font-face{font-family:LedFont;src:url('/fonts/Valencia%2012.ttf')}body{font-family:Inter,Arial,sans-serif;margin:24px;background:#f6f7fb;color:#18202a}.top{display:flex;gap:12px;align-items:center}.panel{background:white;border:1px solid #d7dce4;border-radius:14px;padding:18px;margin-top:10px;box-shadow:0 8px 26px #1d27330d}.preview,.editor{background:#111;padding:10px;border-radius:12px;width:max-content;image-rendering:pixelated}.frames{display:grid;gap:10px}.frame{display:flex;gap:8px;align-items:center;border:1px solid #e1e5eb;padding:10px;border-radius:10px}.modal{position:fixed;inset:0;background:#0008;display:none;align-items:center;justify-content:center;z-index:20}.modal.open{display:flex}.dialog{background:white;border-radius:16px;padding:20px;max-height:90vh;overflow:auto;min-width:760px}.form{display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:center}.thumbs{display:flex;gap:8px;flex-wrap:wrap}.thumb{border:1px solid #cfd6df;border-radius:8px;padding:6px;cursor:pointer}.toolbar{display:flex;gap:10px;align-items:center;margin:10px 0}.console{background:#050505;color:#f8fafc;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;border-radius:12px;padding:12px;margin-top:18px;height:150px;overflow:auto;white-space:pre-wrap}.muted{color:#64748b}canvas{display:block}</style></head><body>
<div class=top><h1>LED Matrix Manager</h1></div><div id=app></div><h3>Status console</h3><pre id=console class=console></pre>
<div id=modal class=modal><div class=dialog><div id=modalBody></div></div></div>
<script>
let S, edit={panel:0, frame:0}, drawPixels=[], drawW=96, drawH=16; const W=96,H=16, off=[39,43,49];
const api=(p,o={})=>fetch('/api/'+p,{headers:{'content-type':'application/json'},...o}).then(r=>r.json());
function drawCanvas(canvas,pixels,scale=10){let targetW=W*scale,targetH=H*scale;if(canvas.width!==targetW)canvas.width=targetW;if(canvas.height!==targetH)canvas.height=targetH;canvas.style.width=targetW+'px';canvas.style.height=targetH+'px';let ctx=canvas.getContext('2d');ctx.fillStyle='#111';ctx.fillRect(0,0,targetW,targetH);for(let y=0;y<H;y++)for(let x=0;x<W;x++){let c=pixels?.[y]?.[x]||[0,0,0],on=c.some(v=>v),cc=on?c:off;ctx.fillStyle=`rgb(${cc[0]},${cc[1]},${cc[2]})`;ctx.beginPath();ctx.arc(x*scale+scale/2,y*scale+scale/2,scale*.32,0,Math.PI*2);ctx.fill()}}
function drawEditor(canvas,pixels,w,h,scale=18){canvas.width=w*scale;canvas.height=h*scale;let ctx=canvas.getContext('2d');ctx.fillStyle='#111';ctx.fillRect(0,0,canvas.width,canvas.height);for(let y=0;y<h;y++)for(let x=0;x<w;x++){let c=pixels[y][x],cc=c.some(v=>v)?c:off;ctx.fillStyle=`rgb(${cc[0]},${cc[1]},${cc[2]})`;ctx.beginPath();ctx.arc(x*scale+scale/2,y*scale+scale/2,scale*.32,0,Math.PI*2);ctx.fill()}}
function emptyPix(w=W,h=H,bg=[0,0,0]){return Array.from({length:h},()=>Array.from({length:w},()=>bg.slice()))}
function updateStatus(){let consoleEl=document.getElementById('console'); consoleEl.textContent=(S.messages||[]).join('\n'); consoleEl.scrollTop=consoleEl.scrollHeight}function render(){updateStatus(); app.innerHTML=`<sl-tab-group>${S.panels.map((p,i)=>`<sl-tab slot=nav panel=p${i}>${p.name}</sl-tab><sl-tab-panel name=p${i}>${panelHtml(p,i)}</sl-tab-panel>`).join('')}</sl-tab-group><sl-button data-act=addpanel>＋ Add Panel</sl-button>`; S.panels.forEach((p,i)=>{let sel=document.querySelector(`[data-act=addr][data-i="${i}"]`); if(sel)sel.value=p.address||''; preview(i)});}
function panelHtml(p,i){return `<canvas id=prev${i} class=preview></canvas><div class=panel><div class=top><sl-button data-act=scan><sl-icon name=search></sl-icon> Scan</sl-button><sl-select value="${p.address||''}" data-act=addr data-i=${i} style="width:280px">${S.discovered.map(a=>`<sl-option value="${a}">${a}</sl-option>`).join('')}</sl-select><sl-button data-act=connect data-i=${i}>${p.connected?'Disconnect':'Connect'}</sl-button><span>${p.connected?'● Connected':'○ Disconnected'}</span><label>Brightness <input type=range min=0 max=100 value=${p.brightness} data-act=brightness data-i=${i}></label></div><div class=toolbar><sl-button variant=primary data-act=play data-i=${i}><sl-icon name="${p.running?'stop-fill':'play-fill'}"></sl-icon> ${p.running?'Stop':'Play'}</sl-button><b>${p.running?'Running '+elapsed(p.started_at):'Stopped'}</b><sl-button data-act=clear data-i=${i}>Clear display</sl-button></div><div class=frames>${p.frames.map((f,j)=>frameRow(f,i,j,p.frames.length)).join('')}</div><sl-button data-act=addframe data-i=${i}>＋ Add Frame</sl-button></div>`}
function frameRow(f,i,j,n){return `<div class=frame><b>Frame ${j+1}</b><sl-select value="${f.frame_type}" data-act=type data-i=${i} data-j=${j}>${S.frame_types.map(t=>`<sl-option value="${t}">${t}</sl-option>`).join('')}</sl-select><label>Duration <input type=number min=1 value="${f.duration}" data-act=duration data-i=${i} data-j=${j}></label><sl-button data-act=settings data-i=${i} data-j=${j}><sl-icon name=gear></sl-icon> Settings</sl-button><sl-button data-act=up data-i=${i} data-j=${j} ${j==0?'disabled':''}>▲</sl-button><sl-button data-act=down data-i=${i} data-j=${j} ${j==n-1?'disabled':''}>▼</sl-button><sl-button data-act=remove data-i=${i} data-j=${j} ${n==1?'disabled':''}>✖</sl-button></div>`}
function elapsed(t){if(!t)return'';let s=Math.floor(Date.now()/1000-t),h=Math.floor(s/3600),m=Math.floor(s%3600/60);return `(${h}h ${m}m)`}
async function preview(i){let c=document.getElementById('prev'+i); if(!c)return; let r=await api(`preview?panel=${i}`); drawCanvas(c,r.pixels,10)}
async function save(){await api('state',{method:'POST',body:JSON.stringify(S)}); await load(true)} async function load(rerender=false){S=await api('state'); rerender?render():updateStatus()} setInterval(()=>load(false),5000); setInterval(()=>S&&S.panels.forEach((_,i)=>preview(i)),500);
document.addEventListener('input',e=>{let a=e.target.dataset.act;if(!a)return;let p=S.panels[e.target.dataset.i],f=p.frames[e.target.dataset.j]; if(a=='brightness')p.brightness=+e.target.value;if(a=='duration')f.duration=+e.target.value; save()});
document.addEventListener('sl-change',e=>{let a=e.target.dataset.act;if(a=='addr')S.panels[e.target.dataset.i].address=e.target.value;if(a=='type'){let f=S.panels[e.target.dataset.i].frames[e.target.dataset.j];f.frame_type=e.target.value;f.settings={}} if(a)save()});
document.addEventListener('click',async e=>{let b=e.target.closest('[data-act]');if(!b)return;let {act,i,j}=b.dataset;if(act=='scan'){await api('scan',{method:'POST'}); setTimeout(()=>load(true),6000);} if(act=='connect'){await api(`panel/${i}/${S.panels[i].connected?'disconnect':'connect'}`,{method:'POST'}); setTimeout(()=>load(true),2500);} if(act=='play'){await api(`panel/${i}/${S.panels[i].running?'stop':'play'}`,{method:'POST'}); setTimeout(()=>load(true),1000);} if(act=='clear')await api(`panel/${i}/clear`,{method:'POST'}); if(act=='addpanel')S.panels.push({name:'Panel '+(S.panels.length+1),frames:[{frame_type:'Text',duration:10,settings:{}}],brightness:80}); if(act=='addframe')S.panels[i].frames.push({frame_type:'Text',duration:10,settings:{}}); if(act=='remove')S.panels[i].frames.splice(j,1); if(act=='up'){let a=S.panels[i].frames;[a[j],a[j-1]]=[a[j-1],a[j]]} if(act=='down'){let a=S.panels[i].frames;[a[j],a[+j+1]]=[a[+j+1],a[j]]} if(['addpanel','addframe','remove','up','down'].includes(act))await save(); if(act=='settings')openSettings(+i,+j); await load(true)});
function input(k,v,type='text'){return `<input data-k="${k}" type=${type} value="${v??''}">`} function selected(name,val,def){return name==(val||def)?'selected':''}
async function openSettings(i,j){edit={panel:i,frame:j};let f=S.panels[i].frames[j],s=f.settings||{},body=`<h2>${f.frame_type} settings</h2><canvas id=dialogPrev class=preview></canvas><div class=form>`; if(f.frame_type=='Text')body+=`<label>Message</label><textarea data-k=message>${s.message||'Hello'}</textarea>`; if(f.frame_type=='Date')body+=`<label>Date format</label>${input('date_format',s.date_format||'%d/%m/%Y')}`; if(f.frame_type=='Image')body+=`<label>Image file</label><span><input type=file data-upload=path accept=image/*><input data-k=path value="${s.path||''}" readonly></span><label></label><sl-button data-act=draw><sl-icon name=palette></sl-icon> Draw</sl-button><label>Image display</label><select data-k=display><option ${selected('Resize to fit',s.display,'Resize to fit')}>Resize to fit</option><option ${selected('Stretch to fit',s.display)}>Stretch to fit</option><option ${selected('Crop to fit',s.display)}>Crop to fit</option></select>`; if(['Text','Date','Clock'].includes(f.frame_type))body+=`<label>Icon</label><span><input type=file data-upload=icon_path accept=image/*><input data-k=icon_path value="${s.icon_path||''}" readonly><sl-button data-act=clearicon>Clear image</sl-button></span><label>Foreground</label>${input('foreground',rgbhex(s.foreground||[255,255,0]),'color')}<label>Background</label>${input('background',rgbhex(s.background||[0,0,0]),'color')}`; if(['Text','Date'].includes(f.frame_type))body+=`<label>Font</label><select data-k=font>${S.fonts.map(x=>`<option ${x==(s.font||'VCR OSD Mono')?'selected':''}>${x}</option>`)}</select><label>Font size</label>${input('font_size',s.font_size||16,'number')}<label>Horizontal spacing</label>${input('horizontal_spacing',s.horizontal_spacing||0,'number')}<label>Vertical offset</label>${input('vertical_offset',s.vertical_offset||0,'number')}`; if(f.frame_type=='Text')body+=`<label>Scrolling</label><select data-k=scrolling><option ${selected('None (Wrap)',s.scrolling,'None (Wrap)')}>None (Wrap)</option><option ${selected('Right to left',s.scrolling)}>Right to left</option><option ${selected('Left to right',s.scrolling)}>Left to right</option><option ${selected('Top to bottom',s.scrolling)}>Top to bottom</option><option ${selected('Bottom to top',s.scrolling)}>Bottom to top</option></select><label>Scroll speed</label>${input('scroll_speed',s.scroll_speed||4,'number')}`; if(f.frame_type=='Clock')body+=`<label>12/24 hour time</label><select data-k=time_mode><option ${selected('24-hour',s.time_mode,'24-hour')}>24-hour</option><option ${selected('12-hour',s.time_mode)}>12-hour</option></select><label>Show seconds</label><input data-k=show_seconds type=checkbox ${s.show_seconds?'checked':''}><label>Flash separator</label><input data-k=flash_separator type=checkbox ${s.flash_separator?'checked':''}><label>Digit spacing</label><input data-k=digit_spacing type=range min=0 max=10 value="${s.digit_spacing??2}"><label></label><sl-button data-act=digits>Edit digits</sl-button>`; body+=`</div><div class=toolbar><sl-button variant=primary data-act=apply>Apply</sl-button><sl-button data-act=close>Cancel</sl-button></div>`; modalBody.innerHTML=body; modal.classList.add('open'); dialogPreview();}
function collect(){let f=S.panels[edit.panel].frames[edit.frame],s={...f.settings}; modalBody.querySelectorAll('[data-k]').forEach(x=>{let v=x.type=='checkbox'?x.checked:x.value;if(['foreground','background'].includes(x.dataset.k))v=hexrgb(v); if(['font_size','horizontal_spacing','vertical_offset','scroll_speed','digit_spacing'].includes(x.dataset.k))v=+v; s[x.dataset.k]=v}); f.settings=s;}
async function dialogPreview(){if(!document.getElementById('dialogPrev'))return; collect();let r=await api(`preview_frame`,{method:'POST',body:JSON.stringify(S.panels[edit.panel].frames[edit.frame])}); drawCanvas(dialogPrev,r.pixels,7)}
modal.addEventListener('input',e=>{if(e.target.dataset.k)dialogPreview()}); modal.addEventListener('change',async e=>{let key=e.target.dataset.upload;if(!key)return;let file=e.target.files[0]; if(!file)return; let data=await readFile(file); let res=await api('upload_image',{method:'POST',body:JSON.stringify({name:file.name,data})}); modalBody.querySelector(`[data-k=${key}]`).value=res.path; dialogPreview()});
modal.addEventListener('click',async e=>{let a=e.target.closest('[data-act]')?.dataset.act;if(a=='close')modal.classList.remove('open');if(a=='clearicon'){modalBody.querySelector('[data-k=icon_path]').value='';dialogPreview()}if(a=='apply'){collect();modal.classList.remove('open');await save()}if(a=='draw')openDraw(false);if(a=='digits')openDraw(true)});
function readFile(file){return new Promise((res,rej)=>{let r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(file)})}
async function loadDigitPixels(name){let f=S.panels[edit.panel].frames[edit.frame],path=(f.settings.digit_overrides||{})[name];let res=await api('digit_pixels',{method:'POST',body:JSON.stringify({name,path})});drawW=res.width;drawH=res.height;drawPixels=res.pixels;drawEditor(ed,drawPixels,drawW,drawH,22)}
function openDraw(digits){drawW=digits?8:W;drawH=digits?16:H;drawPixels=emptyPix(drawW,drawH);modalBody.innerHTML=`<h2>${digits?'Edit clock digits':'Draw image'}</h2>${digits?'<div class=thumbs>'+['digit-0.png','digit-1.png','digit-2.png','digit-3.png','digit-4.png','digit-5.png','digit-6.png','digit-7.png','digit-8.png','digit-9.png','separator.png','am.png','pm.png'].map(x=>`<button class=thumb data-name=${x}>${x}</button>`).join('')+'</div>':''}<div class=toolbar><label>Background <input id=bg type=color value=#000000></label><label>Foreground <input id=fg type=color value=#ffff00></label><sl-button id=clr>Clear</sl-button></div><canvas id=ed class=editor></canvas><div class=toolbar><sl-button id=keep variant=primary>${digits?'Save':'Keep'}</sl-button><sl-button id=discard>Discard</sl-button></div>`;let cur='digit-0.png';let paint=()=>drawEditor(ed,drawPixels,drawW,drawH,digits?22:12); paint(); bg.oninput=()=>{drawPixels=emptyPix(drawW,drawH,hexrgb(bg.value));paint()}; ed.onclick=e=>{let r=ed.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)/(r.width/drawW)),y=Math.floor((e.clientY-r.top)/(r.height/drawH)); if(x>=0&&y>=0&&x<drawW&&y<drawH){drawPixels[y][x]=hexrgb(fg.value);paint()}}; clr.onclick=()=>{drawPixels=emptyPix(drawW,drawH,hexrgb(bg.value));paint()}; discard.onclick=()=>modal.classList.remove('open'); keep.onclick=async()=>{let res=await api(digits?'save_digit':'save_drawing',{method:'POST',body:JSON.stringify({pixels:drawPixels,name:cur})}); if(digits){let f=S.panels[edit.panel].frames[edit.frame];f.settings.digit_overrides={...(f.settings.digit_overrides||{}),[cur]:res.path}} else S.panels[edit.panel].frames[edit.frame].settings.path=res.path; modal.classList.remove('open'); await save()}; modalBody.querySelectorAll('.thumb').forEach(t=>t.onclick=async()=>{cur=t.dataset.name;await loadDigitPixels(cur)}); if(digits)loadDigitPixels(cur);}
function rgbhex(a){return '#'+a.map(x=>x.toString(16).padStart(2,'0')).join('')} function hexrgb(h){return [1,3,5].map(i=>parseInt(h.slice(i,i+2),16))}
load(true);
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def _send(self, code=200, ctype="application/json", body=b""):
        self.send_response(code); self.send_header("content-type", ctype); self.end_headers(); self.wfile.write(body)
    def _json(self, obj, code=200): self._send(code, "application/json", json.dumps(obj).encode())
    def _body(self): return json.loads(self.rfile.read(int(self.headers.get("content-length", 0) or 0)) or b"{}")
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=="/": return self._send(200,"text/html",HTML.encode())
        if u.path.startswith('/fonts/'):
            path=Path(__file__).parent/'fonts'/Path(u.path).name
            return self._send(200, mimetypes.guess_type(path)[0] or 'font/ttf', path.read_bytes()) if path.exists() else self._send(404)
        if u.path=="/api/state": return self._json(SERVICE.state())
        if u.path=="/api/preview":
            idx=int(parse_qs(u.query).get('panel',[0])[0]); tick=int(time.time()*4); img=SERVICE.last_images.get(idx) if SERVICE.panels[idx].running else None; img=img or render_frame(SERVICE.panels[idx].frames[0], tick); return self._json({"pixels": [[list(img.getpixel((x,y))) for x in range(PANEL_WIDTH)] for y in range(PANEL_HEIGHT)]})
        self._send(404)
    def do_POST(self):
        u=urlparse(self.path); parts=u.path.strip('/').split('/')
        try:
            if u.path=="/api/state":
                raw=self._body(); updated=[]
                for idx, pd in enumerate(raw.get('panels',[])):
                    existing = SERVICE.panels[idx] if idx < len(SERVICE.panels) else PanelState(pd.get('name','Panel'))
                    existing.name = pd.get('name', existing.name)
                    existing.address = pd.get('address')
                    existing.brightness = int(pd.get('brightness', existing.brightness))
                    existing.frames = [FrameConfig(FrameType(fd.get('frame_type','Text')), fd.get('duration',10), fd.get('settings',{})) for fd in pd.get('frames',[])] or [FrameConfig()]
                    updated.append(existing)
                SERVICE.panels = updated or [PanelState("Panel 1")]
                SERVICE.save_state(); return self._json({"ok":True})
            if u.path=="/api/scan":
                threading.Thread(target=SERVICE.scan_once, daemon=True).start(); return self._json({"ok": True})
            if u.path=="/api/upload_image":
                data=self._body(); raw=data.get('data',''); _, _, b64=raw.partition(',')
                safe=Path(data.get('name') or f"upload-{uuid.uuid4().hex}.png").name
                ASSET_DIR.mkdir(parents=True, exist_ok=True); path=ASSET_DIR / f"{uuid.uuid4().hex}-{safe}"
                path.write_bytes(base64.b64decode(b64)); return self._json({"path": str(path)})
            if u.path=="/api/digit_pixels":
                data=self._body(); name=Path(data.get('name') or 'digit-0.png').name; override=data.get('path')
                path=Path(override) if override else Path(__file__).parent / 'digits' / name
                if not path.exists(): return self._json({"error": "digit not found"}, 404)
                src=Image.open(path).convert('RGBA'); pixels=[]
                for y in range(src.height):
                    row=[]
                    for x in range(src.width):
                        r,g,b,a=src.getpixel((x,y)); row.append([r,g,b] if a and (r or g or b) else [0,0,0])
                    pixels.append(row)
                return self._json({"width": src.width, "height": src.height, "pixels": pixels})
            if u.path=="/api/preview_frame":
                fd=self._body(); img=render_frame(FrameConfig(FrameType(fd.get('frame_type','Text')), fd.get('duration',10), fd.get('settings',{})), int(time.time()*4)); return self._json({"pixels":[[list(img.getpixel((x,y))) for x in range(PANEL_WIDTH)] for y in range(PANEL_HEIGHT)]})
            if u.path in ("/api/save_drawing","/api/save_digit"):
                data=self._body(); pix=data['pixels']; h=len(pix); w=len(pix[0]); img=Image.new('RGB',(w,h)); [img.putpixel((x,y), tuple(pix[y][x])) for y in range(h) for x in range(w)]
                d=DIGIT_OVERRIDE_DIR if u.path.endswith('digit') else DRAWING_DIR; d.mkdir(parents=True, exist_ok=True); name=data.get('name') if u.path.endswith('digit') else f"drawing-{uuid.uuid4().hex}.png"; path=d/name; img.save(path); return self._json({"path":str(path)})
            if len(parts)==4 and parts[:2]==['api','panel']:
                idx=int(parts[2]); act=parts[3]
                if act=='connect': SERVICE.connect(idx)
                elif act=='disconnect': SERVICE.disconnect(idx)
                elif act=='play': SERVICE.play(idx)
                elif act=='stop': SERVICE.stop(idx)
                elif act=='clear':
                    conn=getattr(SERVICE.panels[idx], 'connection', None)
                    if conn: conn.clear().result(timeout=15)
                    SERVICE.set_status(f"Display cleared for {SERVICE.panels[idx].name}")
                return self._json({"ok":True})
        except Exception as exc:
            logger.exception("API failed"); return self._json({"error":str(exc)}, 500)
        self._send(404)

def main():
    SERVICE.start(); server=ThreadingHTTPServer((HOST, PORT), Handler); logger.info("LED Panel Manager listening on http://%s:%s", HOST, PORT); server.serve_forever()
if __name__ == "__main__": main()
