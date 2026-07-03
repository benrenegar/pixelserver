from __future__ import annotations

import asyncio, base64, io, json, logging, mimetypes, os, shutil, threading, time, uuid, zipfile
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
        "brightness": p.brightness, "started_at": p.started_at, "blanked": p.blanked,
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
        self.lock = threading.RLock(); self.panels = self.load_state(); self.discovered_addresses: list[str] = sorted({p.address for p in self.panels if p.address})
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
            panels.append(PanelState(pd.get("name", "Panel"), pd.get("address"), bool(pd.get("connected", False)), frames or [FrameConfig()], bool(pd.get("running", False)), pd.get("brightness", 80), None, bool(pd.get("blanked", False))))
        return panels or [PanelState("Panel 1")]
    def save_state(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data={"panels":[{"name":p.name,"address":p.address,"connected":p.connected,"running":p.running,"brightness":p.brightness,"blanked":p.blanked,"frames":[{"frame_type":f.frame_type.value,"duration":f.duration,"settings":f.settings} for f in p.frames]} for p in self.panels]}
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
    def start(self):
        for idx, p in enumerate(self.panels):
            if p.address and p.connected:
                should_play = p.running
                p.running = False
                self.connect(idx, autostart=should_play)
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
            self.set_status(f"Connected to {p.address}"); self.save_state()
            if autostart: self.play(idx)
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
                    img=render_frame(fr,self.tick); self.last_images[idx]=Image.new("RGB", (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0)) if p.blanked else img; conn=getattr(p,"connection",None); now=time.monotonic()
                    if conn and now-last_send>=0.25:
                        try: conn.send_image(self.last_images[idx]).result(timeout=20); last_send=now
                        except Exception as exc:
                            logger.exception("Send failed"); p.connected=False; p.connection=None; self.set_status(f"Send failed; reconnecting: {exc}"); self._connect_worker(idx, False); last_send=now
                    self.tick+=1; time.sleep(.25)
    def state(self):
        with self.lock: return {"status": self.status, "messages": self.messages, "discovered": sorted(set(self.discovered_addresses) | {p.address for p in self.panels if p.address}), "fonts": list(FONT_ALIASES), "width": PANEL_WIDTH, "height": PANEL_HEIGHT, "frame_types": [t.value for t in FrameType], "panels": [_jsonable_panel(p) for p in self.panels]}

SERVICE = LedPanelService()


def _image_on_black(path: Path, width: int, height: int) -> Image.Image:
    src = Image.open(path).convert("RGBA")
    if src.size != (width, height):
        src = src.resize((width, height), Image.Resampling.LANCZOS)
    bg = Image.new("RGBA", src.size, (0, 0, 0, 255))
    bg.alpha_composite(src)
    return bg.convert("RGB")

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>LED Matrix Manager</title>
<link rel="stylesheet" href="/static/app.css"></head><body>
<div class=top><h1>LED Matrix Manager</h1></div><div class=toolbar><button data-act=loadprofile>Load Profile</button><button data-act=saveprofile>Save Profile</button><input id=profileFile type=file accept=.zip style="display:none"></div><div id=app></div>
<div id=modal class=modal><div class=dialog><div id=modalBody></div></div></div>
<script>
let S, activePanel=0, edit={panel:0, frame:0}, drawPixels=[], drawW=96, drawH=16, drawing=false; const W=96,H=16, off=[39,43,49];
const api=(p,o={})=>fetch('/api/'+p,{headers:{'content-type':'application/json'},...o}).then(r=>r.json());
const app=document.getElementById('app'), modal=document.getElementById('modal'), modalBody=document.getElementById('modalBody');
function drawCanvas(canvas,pixels,scale=10){let targetW=W*scale,targetH=H*scale;if(canvas.width!==targetW)canvas.width=targetW;if(canvas.height!==targetH)canvas.height=targetH;canvas.style.width=canvas.closest('.modal')?targetW+'px':'100%';canvas.style.height='auto';let ctx=canvas.getContext('2d');ctx.fillStyle='#111';ctx.fillRect(0,0,targetW,targetH);for(let y=0;y<H;y++)for(let x=0;x<W;x++){let c=pixels?.[y]?.[x]||[0,0,0],on=c.some(v=>v),cc=on?c:off;ctx.fillStyle=`rgb(${cc[0]},${cc[1]},${cc[2]})`;ctx.beginPath();ctx.arc(x*scale+scale/2,y*scale+scale/2,scale*.32,0,Math.PI*2);ctx.fill()}}
function drawEditor(canvas,pixels,w,h,scale=18){canvas.width=w*scale;canvas.height=h*scale;let ctx=canvas.getContext('2d');ctx.fillStyle='#111';ctx.fillRect(0,0,canvas.width,canvas.height);for(let y=0;y<h;y++)for(let x=0;x<w;x++){let c=pixels[y][x],cc=c.some(v=>v)?c:off;ctx.fillStyle=`rgb(${cc[0]},${cc[1]},${cc[2]})`;ctx.beginPath();ctx.arc(x*scale+scale/2,y*scale+scale/2,scale*.32,0,Math.PI*2);ctx.fill()}}
function emptyPix(w=W,h=H,bg=[0,0,0]){return Array.from({length:h},()=>Array.from({length:w},()=>bg.slice()))}
function panelOptions(p){return [...new Set([p.address,...S.discovered].filter(Boolean))].map(a=>`<option value="${a}">${a}</option>`).join('')}function updateElapsed(){document.querySelectorAll('[data-started-at]').forEach(el=>el.textContent='Running '+elapsed(+el.dataset.startedAt))}function updateStatus(){let consoleEl=document.getElementById('console'); if(consoleEl){consoleEl.textContent=(S.messages||[]).join('\n'); consoleEl.scrollTop=consoleEl.scrollHeight} updateElapsed()}function render(){if(activePanel>=S.panels.length)activePanel=0;app.innerHTML=`<div class=tabs>${S.panels.map((p,i)=>`<button class="tab ${i==activePanel?'active':''}" data-act=tab data-i=${i}>${p.name}</button>`).join('')}</div>${S.panels.map((p,i)=>`<section class="tab-panel ${i==activePanel?'active':''}">${panelHtml(p,i)}</section>`).join('')}<button data-act=addpanel>＋ Add Panel</button><h3>Status console</h3><pre id=console class=console></pre><p><a href="#" data-act=cleanup>Clean-up unused assets</a></p>`; updateStatus(); S.panels.forEach((p,i)=>{let sel=document.querySelector(`[data-act=addr][data-i="${i}"]`); if(sel)sel.value=p.address||''; if(i==activePanel)preview(i)});}
function panelHtml(p,i){return `<canvas id=prev${i} class=preview></canvas><div class=panel><div class=top><button data-act=scan>Scan</button><select data-act=addr data-i=${i} style="width:280px">${panelOptions(p)}</select><button data-act=connect data-i=${i}>${p.connected?'Disconnect':'Connect'}</button><span>${p.connected?'● Connected':'○ Disconnected'}</span><button data-act=clear data-i=${i}>${p.blanked?'Unblank display':'Blank display'}</button><label>Brightness <input type=range min=0 max=100 value=${p.brightness} data-act=brightness data-i=${i}></label></div><div class=toolbar><button variant=primary data-act=play data-i=${i}>${p.running?'Stop':'Play'}</button><b ${p.running?`data-started-at="${p.started_at}"`:''}>${p.running?'Running '+elapsed(p.started_at):'Stopped'}</b></div><div class=frames>${p.frames.map((f,j)=>frameRow(f,i,j,p.frames.length)).join('')}</div><button data-act=addframe data-i=${i}>＋ Add Frame</button></div>`}
function frameRow(f,i,j,n){return `<div class=frame><b>Frame ${j+1}</b><select data-act=type data-i=${i} data-j=${j}>${S.frame_types.map(t=>`<option value="${t}" ${t==f.frame_type?'selected':''}>${t}</option>`).join('')}</select><label>Duration <input type=number min=1 value="${f.duration}" data-act=duration data-i=${i} data-j=${j}></label><button data-act=settings data-i=${i} data-j=${j}>Settings</button><span style="margin-left:auto"></span><button data-act=up data-i=${i} data-j=${j} ${j==0?'disabled':''}>▲</button><button data-act=down data-i=${i} data-j=${j} ${j==n-1?'disabled':''}>▼</button><button data-act=remove data-i=${i} data-j=${j} ${n==1?'disabled':''}>✖</button></div>`}
function elapsed(t){if(!t)return'';let s=Math.floor(Date.now()/1000-t),h=Math.floor(s/3600),m=Math.floor(s%3600/60);return `(${h}h ${m}m)`}
async function preview(i){let c=document.getElementById('prev'+i); if(!c)return; let r=await api(`preview?panel=${i}`); drawCanvas(c,r.pixels,10)}
async function save(){await api('state',{method:'POST',body:JSON.stringify(S)}); await load(true)} async function load(rerender=false){S=await api('state'); rerender?render():updateStatus()} setInterval(()=>load(false),5000); setInterval(updateElapsed,1000); setInterval(()=>S&&preview(activePanel),500);
document.addEventListener('input',e=>{let a=e.target.dataset.act;if(!a)return;let p=S.panels[e.target.dataset.i],f=p.frames[e.target.dataset.j]; if(a=='brightness')p.brightness=+e.target.value;if(a=='duration')f.duration=+e.target.value; save()});
document.addEventListener('change',e=>{let a=e.target.dataset.act;if(a=='addr')S.panels[e.target.dataset.i].address=e.target.value;if(a=='type'){let f=S.panels[e.target.dataset.i].frames[e.target.dataset.j];f.frame_type=e.target.value;f.settings={}} if(a)save()});document.getElementById('profileFile').addEventListener('change',async e=>{let file=e.target.files[0]; if(!file)return; let data=await readFile(file); await api('load_profile',{method:'POST',body:JSON.stringify({data})}); await load(true); e.target.value='';});
document.addEventListener('click',async e=>{if(e.target.closest('#modal'))return;let b=e.target.closest('[data-act]');if(!b)return;let {act,i,j}=b.dataset;if(act=='tab'){activePanel=+i; render(); return;} if(act=='loadprofile'){document.getElementById('profileFile').click(); return;} if(act=='saveprofile'){location.href='/api/profile'; return;} if(act=='scan'){await api('scan',{method:'POST'}); setTimeout(()=>load(true),6000);} if(act=='connect'){await api(`panel/${i}/${S.panels[i].connected?'disconnect':'connect'}`,{method:'POST'}); setTimeout(()=>load(true),2500);} if(act=='play'){await api(`panel/${i}/${S.panels[i].running?'stop':'play'}`,{method:'POST'}); setTimeout(()=>load(true),1000);} if(act=='clear')await api(`panel/${i}/clear`,{method:'POST'}); if(act=='cleanup'){e.preventDefault(); await api('cleanup_assets',{method:'POST'});} if(act=='addpanel')S.panels.push({name:'Panel '+(S.panels.length+1),frames:[{frame_type:'Text',duration:10,settings:{}}],brightness:80}); if(act=='addframe')S.panels[i].frames.push({frame_type:'Text',duration:10,settings:{}}); if(act=='remove')S.panels[i].frames.splice(j,1); if(act=='up'){let a=S.panels[i].frames;[a[j],a[j-1]]=[a[j-1],a[j]]} if(act=='down'){let a=S.panels[i].frames;[a[j],a[+j+1]]=[a[+j+1],a[j]]} if(['addpanel','addframe','remove','up','down'].includes(act))await save(); if(act=='settings')openSettings(+i,+j); await load(true)});
function input(k,v,type='text'){return `<input data-k="${k}" type=${type} value="${v??''}">`} function selected(name,val,def){return name==(val||def)?'selected':''}
const DEF_FG='#00ffb3', DEF_FONT='Avante 8', DEF_SIZE=8, PALETTE=['#000000','#999999','#FFFFFF','#FE0000','#FF8800','#FFC300','#FFFF00','#00FF01','#00FFB3','#00FFFF','#00B2FF','#0000FE','#9811F2','#FF00FE','#FF017E'];
function editorHtml(title,w,h){return `${title?`<h2>${title}</h2>`:''}<input id=uploadFile type=file accept=image/* style="display:none"><input data-k=path type=hidden><div class=toolbar><button id=uploadBtn>Upload</button><label>Background <input id=bg type=color value=#000000></label><label>Foreground <input id=fg type=color value=${DEF_FG}></label><button id=clr>Clear</button></div><div class=palette>${PALETTE.map(c=>`<button type=button class=swatch style="background:${c}" data-color=${c} title=${c}></button>`).join('')}</div><canvas id=ed class=editor></canvas>`}
async function openSettings(i,j){edit={panel:i,frame:j};let f=S.panels[i].frames[j],s=f.settings||{},body=`<h2>${f.frame_type} settings</h2>${f.frame_type=='Image'?editorHtml('',W,H):'<canvas id=dialogPrev class=preview></canvas>'}<div class=form>`; if(f.frame_type=='Text')body+=`<label>Message</label><textarea data-k=message>${s.message||'Hello'}</textarea>`; if(f.frame_type=='Date')body+=`<label>Date format</label>${input('date_format',s.date_format||'%d/%m/%Y')}`; if(f.frame_type=='Weather')body+=`<label>Location</label>${input('location',s.location||'')}<label>Units</label><span><label><input type=radio name=weather-units data-k=units value="Celsius" ${s.units!='Fahrenheit'?'checked':''}> Celsius</label><label><input type=radio name=weather-units data-k=units value="Fahrenheit" ${s.units=='Fahrenheit'?'checked':''}> Fahrenheit</label></span><label>Foreground</label>${input('foreground',rgbhex(s.foreground||hexrgb(DEF_FG)),'color')}<label>Background</label>${input('background',rgbhex(s.background||[0,0,0]),'color')}`; if(['Text','Date','Clock'].includes(f.frame_type))body+=`<label>Icon</label><span><button data-act=iconedit>Edit/Upload</button><button data-act=clearicon>Clear image</button></span><label>Foreground</label>${input('foreground',rgbhex(s.foreground||hexrgb(DEF_FG)),'color')}<label>Background</label>${input('background',rgbhex(s.background||[0,0,0]),'color')}`; if(['Text','Date','Weather'].includes(f.frame_type))body+=`<label>Font</label><select data-k=font>${S.fonts.map(x=>`<option ${x==(s.font||DEF_FONT)?'selected':''}>${x}</option>`)}</select><label>Font size</label>${input('font_size',s.font_size||DEF_SIZE,'number')}<label>Vertical offset</label>${input('vertical_offset',s.vertical_offset||0,'number')}`; if(f.frame_type=='Text')body+=`<label>Scrolling</label><select data-k=scrolling><option ${selected('None (Wrap)',s.scrolling,'None (Wrap)')}>None (Wrap)</option><option ${selected('Right to left',s.scrolling)}>Right to left</option><option ${selected('Left to right',s.scrolling)}>Left to right</option><option ${selected('Top to bottom',s.scrolling)}>Top to bottom</option><option ${selected('Bottom to top',s.scrolling)}>Bottom to top</option></select><label>Scroll speed</label>${input('scroll_speed',s.scroll_speed||4,'number')}`; if(f.frame_type=='Clock')body+=`<label>12/24 hour time</label><select data-k=time_mode><option ${selected('24-hour',s.time_mode,'24-hour')}>24-hour</option><option ${selected('12-hour',s.time_mode)}>12-hour</option></select><label>Show seconds</label><input data-k=show_seconds type=checkbox ${s.show_seconds?'checked':''}><label>Flash separator</label><input data-k=flash_separator type=checkbox ${s.flash_separator?'checked':''}><label>Digit spacing</label><input data-k=digit_spacing type=range min=0 max=10 value="${s.digit_spacing??2}"><label></label><button data-act=digits>Edit digits</button>`; body+=`</div><div class=toolbar><button variant=primary data-act=apply>${f.frame_type=='Image'?'Keep':'Apply'}</button><button data-act=close>Cancel</button></div>`; modalBody.innerHTML=body; modal.classList.add('open'); if(f.frame_type=='Image')await setupPixelEditor('path',W,H,s.path); else dialogPreview();}
function collect(){let f=S.panels[edit.panel].frames[edit.frame],s={...f.settings}; modalBody.querySelectorAll('[data-k]').forEach(x=>{let v=x.type=='checkbox'?x.checked:x.value;if(x.type=='radio'&&!x.checked)return;if(['foreground','background'].includes(x.dataset.k))v=hexrgb(v); if(['font_size','vertical_offset','scroll_speed','digit_spacing'].includes(x.dataset.k))v=+v; s[x.dataset.k]=v}); f.settings=s;}
async function dialogPreview(){let previewEl=document.getElementById('dialogPrev'); if(!previewEl)return; collect();let r=await api(`preview_frame`,{method:'POST',body:JSON.stringify(S.panels[edit.panel].frames[edit.frame])}); drawCanvas(previewEl,r.pixels,7)}
modal.addEventListener('input',e=>{e.stopPropagation();if(e.target.dataset.k)dialogPreview()}); modal.addEventListener('change',async e=>{e.stopPropagation();if(e.target.id!='uploadFile')return;let file=e.target.files[0]; if(!file)return; let data=await readFile(file); let res=await api('upload_image',{method:'POST',body:JSON.stringify({name:file.name,data,width:drawW,height:drawH})}); drawPixels=res.pixels||drawPixels; drawEditor(document.getElementById('ed'),drawPixels,drawW,drawH,drawW==16?22:12); e.target.value='';});
modal.addEventListener('click',async e=>{e.stopPropagation();let a=e.target.closest('[data-act]')?.dataset.act;if(a=='close')modal.classList.remove('open');if(a=='clearicon'){S.panels[edit.panel].frames[edit.frame].settings.icon_path='';dialogPreview()}if(a=='apply'){collect();let f=S.panels[edit.panel].frames[edit.frame]; f.settings=f.settings||{}; if(f.frame_type=='Image'&&drawPixels.length){let res=await api('save_drawing',{method:'POST',body:JSON.stringify({pixels:drawPixels})});f.settings.path=res.path} modal.classList.remove('open');await save()}if(a=='iconedit')openIconEditor();if(a=='digits')openDraw(true)});
function readFile(file){return new Promise((res,rej)=>{let r=new FileReader();r.onload=()=>res(r.result);r.onerror=rej;r.readAsDataURL(file)})}
async function setupPixelEditor(key,w,h,path){drawW=w;drawH=h;drawPixels=emptyPix(w,h);if(path){try{let res=await api('image_pixels',{method:'POST',body:JSON.stringify({path,width:w,height:h})});drawPixels=res.pixels||drawPixels}catch(e){}}let edEl=document.getElementById('ed'),bgEl=document.getElementById('bg'),fgEl=document.getElementById('fg'),clrEl=document.getElementById('clr'),up=document.getElementById('uploadBtn');let paint=()=>drawEditor(edEl,drawPixels,drawW,drawH,w==16?22:12);paint();up.onclick=()=>document.getElementById('uploadFile').click();modalBody.querySelectorAll('.swatch').forEach(b=>b.onclick=()=>{fgEl.value=b.dataset.color});bgEl.oninput=()=>{drawPixels=emptyPix(drawW,drawH,hexrgb(bgEl.value));paint()};clrEl.onclick=()=>{drawPixels=emptyPix(drawW,drawH,hexrgb(bgEl.value));paint()}; let drawAt=e=>{let r=edEl.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)/(r.width/drawW)),y=Math.floor((e.clientY-r.top)/(r.height/drawH)); if(x>=0&&y>=0&&x<drawW&&y<drawH){drawPixels[y][x]=hexrgb(fgEl.value);paint()}};edEl.onpointerdown=e=>{drawing=true;edEl.setPointerCapture(e.pointerId);drawAt(e)};edEl.onpointermove=e=>{if(drawing)drawAt(e)};edEl.onpointerup=e=>{drawing=false;edEl.releasePointerCapture(e.pointerId)};edEl.onpointerleave=()=>{drawing=false}}
async function openIconEditor(){let f=S.panels[edit.panel].frames[edit.frame];f.settings=f.settings||{};modalBody.innerHTML=editorHtml('Edit/upload icon',16,16)+`<div class=toolbar><button id=keep variant=primary>Keep</button><button id=discard>Close</button></div>`;await setupPixelEditor('icon_path',16,16,f.settings.icon_path);document.getElementById('discard').onclick=()=>openSettings(edit.panel,edit.frame);document.getElementById('keep').onclick=async e=>{e.stopPropagation();let res=await api('save_drawing',{method:'POST',body:JSON.stringify({pixels:drawPixels})});let frame=S.panels[edit.panel].frames[edit.frame];frame.settings=frame.settings||{};frame.settings.icon_path=res.path;await save();openSettings(edit.panel,edit.frame)}}
async function loadDigitPixels(name){let f=S.panels[edit.panel].frames[edit.frame],path=(f.settings.digit_overrides||{})[name];let res=await api('digit_pixels',{method:'POST',body:JSON.stringify({name,path})});drawW=res.width;drawH=res.height;drawPixels=res.pixels;drawEditor(document.getElementById('ed'),drawPixels,drawW,drawH,22)}
function openDraw(digits){drawW=digits?8:W;drawH=digits?16:H;drawPixels=emptyPix(drawW,drawH);let colorTools=digits?'<div class=toolbar><button id=clr>Clear</button></div>':'<div class=toolbar><label>Background <input id=bg type=color value=#000000></label><label>Foreground <input id=fg type=color value='+DEF_FG+'></label><button id=clr>Clear</button></div>';modalBody.innerHTML=`<h2>${digits?'Edit clock digits':'Draw image'}</h2>${digits?'<div class=thumbs>'+['digit-0.png','digit-1.png','digit-2.png','digit-3.png','digit-4.png','digit-5.png','digit-6.png','digit-7.png','digit-8.png','digit-9.png','separator.png','am.png','pm.png'].map(x=>`<button class=thumb data-name=${x}>${x}</button>`).join('')+'</div>':''}${colorTools}<canvas id=ed class=editor></canvas><div class=toolbar><button id=keep variant=primary>${digits?'Save':'Keep'}</button><button id=discard>${digits?'Close':'Discard'}</button>${digits?'<button id=reset>Reset</button>':''}</div>`;let cur='digit-0.png',edEl=document.getElementById('ed'),bgEl=document.getElementById('bg'),fgEl=document.getElementById('fg'),clrEl=document.getElementById('clr'),discardEl=document.getElementById('discard'),keepEl=document.getElementById('keep');let paint=()=>drawEditor(edEl,drawPixels,drawW,drawH,digits?22:12); paint(); if(!digits)bgEl.oninput=()=>{drawPixels=emptyPix(drawW,drawH,hexrgb(bgEl.value));paint()}; let drawAt=e=>{let r=edEl.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)/(r.width/drawW)),y=Math.floor((e.clientY-r.top)/(r.height/drawH)); if(x>=0&&y>=0&&x<drawW&&y<drawH){if(digits){drawPixels[y][x]=drawPixels[y][x].some(v=>v)?[0,0,0]:[255,255,255]}else{drawPixels[y][x]=hexrgb(fgEl.value)}paint()}}; edEl.onpointerdown=e=>{drawing=true;edEl.setPointerCapture(e.pointerId);drawAt(e)}; edEl.onpointermove=e=>{if(drawing)drawAt(e)}; edEl.onpointerup=e=>{drawing=false;edEl.releasePointerCapture(e.pointerId)}; edEl.onpointerleave=()=>{drawing=false}; clrEl.onclick=()=>{drawPixels=emptyPix(drawW,drawH,digits?[0,0,0]:hexrgb(bgEl.value));paint()}; discardEl.onclick=()=>openSettings(edit.panel,edit.frame); let resetEl=document.getElementById('reset'); if(resetEl)resetEl.onclick=async()=>{let f=S.panels[edit.panel].frames[edit.frame],old=(f.settings.digit_overrides||{})[cur]; if(f.settings.digit_overrides)delete f.settings.digit_overrides[cur]; await api('reset_digit',{method:'POST',body:JSON.stringify({path:old})}); await loadDigitPixels(cur); await save()}; keepEl.onclick=async()=>{let res=await api(digits?'save_digit':'save_drawing',{method:'POST',body:JSON.stringify({pixels:drawPixels,name:cur})}); if(digits){let f=S.panels[edit.panel].frames[edit.frame];f.settings.digit_overrides={...(f.settings.digit_overrides||{}),[cur]:res.path}; await save()} else {S.panels[edit.panel].frames[edit.frame].settings.path=res.path; modal.classList.remove('open'); await save()}}; modalBody.querySelectorAll('.thumb').forEach(t=>t.onclick=async()=>{cur=t.dataset.name;await loadDigitPixels(cur)}); if(digits)loadDigitPixels(cur);}
function rgbhex(a){return '#'+a.map(x=>x.toString(16).padStart(2,'0')).join('')} function hexrgb(h){return [1,3,5].map(i=>parseInt(h.slice(i,i+2),16))}
load(true);
</script></body></html>'''


def _profile_data() -> dict:
    data={"panels":[{"name":p.name,"address":p.address,"connected":p.connected,"running":p.running,"brightness":p.brightness,"blanked":p.blanked,"frames":[{"frame_type":f.frame_type.value,"duration":f.duration,"settings":dict(f.settings)} for f in p.frames]} for p in SERVICE.panels]}
    for panel in data["panels"]:
        for frame in panel["frames"]:
            settings=frame.get("settings", {})
            for key in ("path", "icon_path"):
                if settings.get(key):
                    try: settings[key]=str(Path(settings[key]).resolve().relative_to(CONFIG_DIR.resolve()))
                    except Exception: pass
            overrides=settings.get("digit_overrides") or {}
            for name, value in list(overrides.items()):
                try: overrides[name]=str(Path(value).resolve().relative_to(CONFIG_DIR.resolve()))
                except Exception: pass
    return data


def _rewrite_profile_paths(data: dict) -> dict:
    for panel in data.get("panels", []):
        for frame in panel.get("frames", []):
            settings=frame.get("settings", {})
            for key in ("path", "icon_path"):
                if settings.get(key) and not Path(settings[key]).is_absolute(): settings[key]=str(CONFIG_DIR / settings[key])
            overrides=settings.get("digit_overrides") or {}
            for name, value in list(overrides.items()):
                if value and not Path(value).is_absolute(): overrides[name]=str(CONFIG_DIR / value)
    return data

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
        if u.path == '/static/app.css':
            path=Path(__file__).parent/'static'/'app.css'
            return self._send(200, 'text/css', path.read_bytes()) if path.exists() else self._send(404)
        if u.path == '/api/profile':
            CONFIG_DIR.mkdir(parents=True, exist_ok=True); ASSET_DIR.mkdir(parents=True, exist_ok=True)
            payload=io.BytesIO()
            with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('config.json', json.dumps(_profile_data(), indent=2))
                if ASSET_DIR.exists():
                    for asset in ASSET_DIR.rglob('*'):
                        if asset.is_file(): zf.write(asset, asset.resolve().relative_to(CONFIG_DIR.resolve()))
            self.send_response(200); self.send_header('content-type','application/zip'); self.send_header('content-disposition','attachment; filename="ledpanel-profile.zip"'); self.end_headers(); self.wfile.write(payload.getvalue()); return
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
                    existing.blanked = bool(pd.get('blanked', existing.blanked))
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
                path.write_bytes(base64.b64decode(b64))
                result={"path": str(path)}
                if data.get('width') and data.get('height'):
                    src=_image_on_black(path, int(data['width']), int(data['height']))
                    src.save(path)
                    result['pixels']=[[list(src.getpixel((x,y))) for x in range(src.width)] for y in range(src.height)]
                return self._json(result)
            if u.path=="/api/image_pixels":
                data=self._body(); path=Path(data.get('path') or '')
                if not path.exists(): return self._json({"error": "image not found"}, 404)
                w=int(data.get('width') or PANEL_WIDTH); h=int(data.get('height') or PANEL_HEIGHT)
                src=_image_on_black(path, w, h)
                return self._json({"width": w, "height": h, "pixels": [[list(src.getpixel((x,y))) for x in range(w)] for y in range(h)]})
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
            if u.path=="/api/reset_digit":
                data=self._body(); path=data.get('path')
                if path:
                    try:
                        target=Path(path).resolve()
                        if ASSET_DIR.resolve() in target.parents and target.exists(): target.unlink()
                    except Exception:
                        logger.debug("Unable to reset digit asset", exc_info=True)
                SERVICE.set_status("Clock digit reset to bundled bitmap")
                return self._json({"ok": True})
            if u.path=="/api/load_profile":
                data=self._body(); raw=data.get('data',''); _, _, b64=raw.partition(',')
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as zf:
                    zf.extractall(CONFIG_DIR)
                    profile=json.loads((CONFIG_DIR/'config.json').read_text())
                profile=_rewrite_profile_paths(profile)
                CONFIG_FILE.write_text(json.dumps(profile, indent=2))
                SERVICE.panels=SERVICE.load_state(); SERVICE.discovered_addresses=sorted({p.address for p in SERVICE.panels if p.address}); SERVICE.set_status("Profile loaded")
                return self._json({"ok": True})
            if u.path=="/api/cleanup_assets":
                used=set()
                for panel in SERVICE.panels:
                    for frame in panel.frames:
                        settings=frame.settings or {}
                        for key in ("path", "icon_path"):
                            if settings.get(key): used.add(str(Path(settings[key]).resolve()))
                        for value in (settings.get("digit_overrides") or {}).values():
                            if value: used.add(str(Path(value).resolve()))
                removed=0
                if ASSET_DIR.exists():
                    for asset in ASSET_DIR.rglob('*'):
                        if asset.is_file() and str(asset.resolve()) not in used:
                            try:
                                asset.unlink(); removed += 1
                            except Exception:
                                logger.debug("Unable to remove unused asset %s", asset, exc_info=True)
                SERVICE.set_status(f"Clean-up unused assets removed {removed} file(s)")
                return self._json({"removed": removed})
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
                    p=SERVICE.panels[idx]; p.blanked=not p.blanked
                    if p.blanked:
                        conn=getattr(p, 'connection', None)
                        if conn: conn.send_image(Image.new('RGB', (PANEL_WIDTH, PANEL_HEIGHT), (0, 0, 0))).result(timeout=15)
                    SERVICE.save_state(); SERVICE.set_status(f"Display {'blanked' if p.blanked else 'unblanked'} for {p.name}")
                return self._json({"ok":True})
        except Exception as exc:
            logger.exception("API failed"); return self._json({"error":str(exc)}, 500)
        self._send(404)

def main():
    SERVICE.start(); server=ThreadingHTTPServer((HOST, PORT), Handler); logger.info("LED Panel Manager listening on http://%s:%s", HOST, PORT); server.serve_forever()
if __name__ == "__main__": main()
