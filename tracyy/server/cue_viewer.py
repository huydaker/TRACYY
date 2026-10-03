from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import mimetypes
import os
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar


def brand_logo_candidates() -> list[Path]:
    """Every place the viewer logo might be, most likely first.

    The server also runs as its own frozen executable, so it cannot assume the
    package layout is present; ``tracyy.core.paths`` is used when it imports
    and the module's own location is the fallback.
    """
    roots: list[Path] = []
    try:
        from tracyy.core import paths

        roots.append(paths.resource_root())
        roots.append(paths.app_root())
    except ImportError:
        pass

    module_dir = Path(__file__).resolve().parent
    roots.extend([module_dir.parents[1], module_dir.parent, module_dir])

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        roots.extend([executable_dir, executable_dir.parent])

    candidates = [
        root / "icon" / name
        for root in roots
        for name in ("logo.png", "Tracyy.png", "tracyy.png")
    ]

    unique = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def brand_logo_path() -> Path:
    for path in brand_logo_candidates():
        if path.is_file():
            return path
    return brand_logo_candidates()[0]


_logo_cache: tuple[str, int, bytes, str] | None = None
_logo_lock = threading.Lock()


def brand_logo_bytes() -> tuple[bytes, str] | None:
    """The logo file and its content type, read from disk at most once.

    V1 re-read the file for every request. The bundled logo is several
    megabytes, so a room full of phones refreshing turned into repeated
    multi-megabyte disk reads on the show machine.
    """
    global _logo_cache
    path = brand_logo_path()
    try:
        stat = path.stat()
    except OSError:
        return None

    with _logo_lock:
        cached = _logo_cache
        if cached is not None and cached[0] == str(path) and cached[1] == int(stat.st_mtime_ns):
            return cached[2], cached[3]
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        content_type = mimetypes.guess_type(str(path))[0] or "image/png"
        _logo_cache = (str(path), int(stat.st_mtime_ns), payload, content_type)
        return payload, content_type


VIEWER_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f0d15">
<meta name="mobile-web-app-capable" content="yes">
<title>Tracyy Cue Viewer</title>
<style>
*{box-sizing:border-box}

:root{
  color-scheme:dark;
  --bg:#0f0d15;
  --bg-deep:#09080d;
  --line:#2d2a35;
  --text:#fff;
  --muted:#a5a8b2;
  --live:#22c55e;
  --alarm:#ff3b30;
  --clock:#46DC00;
  --pad:16px;
  --footer-h:60px;
  /* Zero on every browser that has no display cutout, so the same rules
     serve a desktop monitor and a notched phone in landscape. */
  --safe-t:env(safe-area-inset-top,0px);
  --safe-b:env(safe-area-inset-bottom,0px);
  --safe-l:env(safe-area-inset-left,0px);
  --safe-r:env(safe-area-inset-right,0px);
}

html,body{margin:0;background:var(--bg);color:var(--text)}
body{
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
  min-height:100vh;
  min-height:100dvh;
  -webkit-tap-highlight-color:transparent;
  /* A pull-to-refresh in the middle of a show would blank the cue list. */
  overscroll-behavior-y:contain
}

.shell{
  min-height:100vh;
  min-height:100dvh;
  display:flex;
  flex-direction:column;
  /* The footer is fixed; the shell reserves its height so the last cue is
     never hidden under it, home indicator included. */
  padding-bottom:calc(var(--footer-h) + var(--safe-b))
}

header{
  display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:calc(13px + var(--safe-t))
          calc(var(--pad) + var(--safe-r))
          4px
          calc(var(--pad) + var(--safe-l))
}
.brand{
  display:flex;align-items:center;gap:12px;
  font-weight:800;letter-spacing:.08em;min-width:0
}
.brand span{font-size:clamp(18px,4.4vw,26px)}
.brand-logo{
  width:clamp(34px,9vw,52px);height:clamp(34px,9vw,52px);
  object-fit:contain;display:block;flex:none
}
.connection{
  font-size:clamp(11px,2.6vw,13px);color:var(--live);
  font-weight:700;letter-spacing:.04em;white-space:nowrap
}
.connection.offline{color:var(--alarm)}
.connection.error{color:var(--alarm)}

.stage{flex:1;display:flex;flex-direction:column;min-height:0}
.clock-pane{display:flex;flex-direction:column;justify-content:center;min-width:0}

.timecode{
  font-family:Consolas,"Courier New",monospace;
  font-variant-numeric:tabular-nums;
  text-align:center;color:var(--clock);
  text-shadow:0 0 18px rgba(70,220,0,.24);
  font-size:clamp(40px,13vw,96px);
  line-height:1;padding:10px 8px 0
}
/* Red means the number on screen is not a live playing clock: either the
   link to the app is down, or the track ran to its end. Both matter from
   the back of a dark room, so they get the same unmistakable colour. No
   transition — an alarm has to be right on the first frame drawn. */
.timecode.alarm{
  color:var(--alarm);
  text-shadow:0 0 18px rgba(255,59,48,.30)
}
.state-ended{color:var(--alarm);font-weight:700}

.subline{
  display:flex;justify-content:center;align-items:center;
  gap:8px;flex-wrap:wrap;
  padding:8px 12px;color:var(--muted);
  font-size:clamp(12px,3vw,15px)
}
.track{
  text-align:center;
  /* Spacing has to be margin, not padding: -webkit-box clips at the padding
     box, so bottom padding would let the top of the next, clamped-away line
     bleed through under the title. */
  padding:0 14px;margin:0 0 12px;
  font-size:clamp(17px,4.4vw,26px);font-weight:600;
  /* 1.3, not 1.2 — Vietnamese stacks diacritics and they get clipped. */
  line-height:1.3;
  /* Two lines on a phone instead of one truncated one: a show title is
     worth the extra row. */
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden
}

.loading-list{
  display:flex;flex-direction:column;gap:6px;min-height:0;
  padding:6px calc(10px + var(--safe-r)) 10px calc(10px + var(--safe-l));
  contain:layout paint;
  overflow-anchor:none
}
.loading-list.track-changing{pointer-events:none}
.loading-bar{
  position:relative;min-height:52px;overflow:hidden;border-radius:12px;
  transition:
    transform .20s ease,
    opacity .16s ease,
    max-height .20s ease,
    margin .20s ease;
}
.loading-bar.completed{
  opacity:0;
  transform:translateY(-12px) scale(.985);
  max-height:0;
  min-height:0;
  margin:0;
}
.loading-base{position:absolute;inset:0;border-radius:12px;opacity:.20}
.loading-fill{
  position:absolute;left:0;top:0;bottom:0;width:0%;
  border-radius:12px;opacity:.95;
  transition:width .10s linear;
  will-change:width
}
.loading-fill.active{box-shadow:0 0 12px rgba(255,255,255,.12) inset}
.loading-content{
  position:relative;z-index:1;min-height:52px;
  display:grid;grid-template-columns:minmax(0,1fr) auto auto;
  align-items:center;gap:14px;padding:0 16px;
  text-shadow:1px 1px 2px #000
}
.loading-title{
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  font-size:clamp(15px,3.9vw,19px);font-weight:650
}
.loading-type{font-weight:500;opacity:.82}
/* Absolute cue timecode. Hidden where the row is narrow — the countdown is
   the number you act on; this one is for checking against a rundown, which
   only happens on a screen wide enough to show it. */
.loading-abs{
  display:none;
  font-family:Consolas,"Courier New",monospace;
  font-size:14px;font-weight:600;opacity:.70;white-space:nowrap
}
.loading-time{
  font-family:Consolas,"Courier New",monospace;
  font-size:clamp(14px,3.6vw,18px);font-weight:700;
  white-space:nowrap;text-align:right;min-width:5.5ch
}
.empty{text-align:center;color:#777b86;padding:35px}

.footer{
  position:fixed;left:0;right:0;bottom:0;z-index:1000;
  min-height:calc(var(--footer-h) + var(--safe-b));
  display:flex;align-items:center;justify-content:center;
  padding:10px
          calc(var(--pad) + var(--safe-r))
          calc(10px + var(--safe-b))
          calc(var(--pad) + var(--safe-l));
  background:var(--bg-deep);
  border-top:2px solid var(--line);
  box-shadow:0 -8px 24px rgba(0,0,0,.72);
  color:#f2f3f6;
  font-size:20px;font-weight:700;letter-spacing:.02em;
  white-space:nowrap;overflow:hidden;text-overflow:clip
}

/* Phone portrait. */
@media(max-width:560px){
  .loading-list{
    gap:5px;
    padding-left:calc(8px + var(--safe-l));
    padding-right:calc(8px + var(--safe-r))
  }
  .loading-bar,.loading-content{min-height:46px}
  .loading-content{padding:0 12px;gap:10px}
  .footer{font-size:16px}
}

/* Desktop and tablet landscape. One stretched column wastes the width, so
   the clock takes a left rail and the cue list gets its own scroll next to
   it — both stay readable from the back of the room. */
@media(min-width:960px),
      (min-width:760px) and (max-height:560px){
  .shell{height:100vh;height:100dvh}
  .stage{
    display:grid;
    grid-template-columns:minmax(340px,48%) minmax(0,1fr);
    gap:24px;min-height:0;
    padding:0 calc(20px + var(--safe-r)) 10px calc(20px + var(--safe-l))
  }
  .clock-pane{align-self:center;padding-bottom:18px}
  /* vw is only a fallback: in two columns the clock has to fit its own
     column, not the window, or eleven monospace glyphs run under the cue
     list. The container query below is the real rule. */
  .timecode{font-size:clamp(46px,5.4vw,120px)}
  .subline{font-size:15px}
  .track{font-size:clamp(18px,1.7vw,28px);-webkit-line-clamp:3}
  .loading-list{overflow-y:auto;gap:8px;padding:0 4px 8px}
  .loading-bar,.loading-content{min-height:58px}
  .loading-title{font-size:19px}
  .loading-time{font-size:19px}
  .loading-abs{display:block}
}

@supports(container-type:inline-size){
  @media(min-width:960px),
        (min-width:760px) and (max-height:560px){
    .clock-pane{container-type:inline-size}
    /* 12.6% of the column keeps HH:MM:SS:FF inside it at every column
       width the grid can produce, with room to spare for the wider
       monospace fallbacks on Android and Linux. The clock now grows with
       the monitor instead of stopping at a size that suits a laptop. */
    .timecode{font-size:clamp(46px,12.6cqw,132px)}
  }
}

@media(min-width:1600px){
  .stage{grid-template-columns:minmax(420px,46%) minmax(0,1fr);gap:32px}
  .loading-bar,.loading-content{min-height:66px}
  .loading-title{font-size:22px}
  .loading-time{font-size:22px}
  .loading-abs{font-size:16px}
}

/* Short viewport: a phone on its side, or a laptop whose browser chrome has
   eaten the screen. Height is the constraint here, not width, so everything
   gives back its padding. Placed after the grid rules on purpose — a
   landscape phone gets both the two-column layout and this compaction. */
@media(max-height:560px){
  header{padding-top:calc(6px + var(--safe-t));padding-bottom:0}
  .subline{padding:4px 10px}
  .track{margin-bottom:6px;-webkit-line-clamp:1}
  .clock-pane{padding-bottom:6px}
  .stage{gap:14px}
  .loading-bar,.loading-content{min-height:42px}
  .loading-title{font-size:15px}
  .loading-time{font-size:15px}
  .loading-abs{font-size:12px}
}

/* Still one column because the screen is short *and* narrow: size the clock
   off the height it actually has. */
@media(max-height:560px) and (max-width:759px){
  .timecode{
    font-size:clamp(30px,min(9vw,11vh),64px);
    padding-top:2px
  }
}

@media(prefers-reduced-motion:reduce){
  .loading-bar,.loading-fill{transition:none}
}
</style>
</head>
<body>
<div class="shell">
  <header>
    <div class="brand">
      <img
        class="brand-logo"
        src="/logo.png?v=3"
        alt=""
        onerror="this.alt='TRACYY';this.style.display='block'"
      >
      <span>TRACYY</span>
    </div>
    <div id="conn" class="connection">LIVE</div>
  </header>

  <div class="stage">
    <section class="clock-pane">
      <div id="tc" class="timecode">00:00:00:00</div>
      <div class="subline">
        <span id="state">STOPPED</span><span>•</span>
        <span id="offset">OFFSET 00:00:00:00</span><span>•</span>
        <span id="elapsed">00:00:00 / 00:00:00</span>
      </div>
      <div id="track" class="track">No track selected</div>
    </section>
    <main id="cues" class="loading-list"></main>
  </div>
</div>
<footer id="standby" class="footer"></footer>

<script>
const $=id=>document.getElementById(id);
const cueNodes=new Map();

const STATE_CACHE_KEY=
  "tracyy.viewer.transport.v4";
const MANIFEST_FALLBACK_KEY=
  "tracyy.viewer.manifest.v4";
const DB_NAME="tracyy-cue-viewer";
const DB_VERSION=1;
const DB_STORE="viewer_cache";
const localWindowSize=40;

let latestState={};
let localCueSchedule=[];
let localWindowStart=-1;
let manifestRevision="";
let timelineGeneration=0;
let lastWindowCheck=0;

let mediaAnchorPosition=0;
let mediaAnchorAppEpochMs=0;
let mediaAnchorState="STOPPED";
let mediaFps=30;
let mediaOffsetSeconds=0;
let displayedPosition=0;

let clockOffsetPerfMs=
  Date.now()-performance.now();
let clockOffsetWallMs=0;
let clockBestRttMs=Infinity;
let clockSynchronized=false;

let currentSessionId="";
let currentTransportSequence=-1;
let eventSource=null;
let liveConnected=false;
let restoringCache=false;

let connectionMode="live";

function refreshTimecodeAlarm(){
  const alarm=
    connectionMode!=="live"
    ||mediaAnchorState==="ENDED";
  $("tc").classList.toggle(
    "alarm",
    alarm
  );
  $("state").classList.toggle(
    "state-ended",
    mediaAnchorState==="ENDED"
  );
}

function setConnectionStatus(
  text,
  mode="live"
){
  const node=$("conn");
  node.textContent=text;
  node.classList.toggle(
    "offline",
    mode==="offline"
  );
  node.classList.toggle(
    "error",
    mode==="error"
  );
  connectionMode=mode;
  refreshTimecodeAlarm();
}

function framesToTimecode(totalFrames,fps){
  const safeFps=Math.max(
    1,
    Math.round(Number(fps)||30)
  );
  const framesPer100Hours=
    100*60*60*safeFps;
  let value=Math.max(
    0,
    Math.round(totalFrames)
  );
  value%=framesPer100Hours;

  const ff=value%safeFps;
  const totalSeconds=
    Math.floor(value/safeFps);
  const ss=totalSeconds%60;
  const totalMinutes=
    Math.floor(totalSeconds/60);
  const mm=totalMinutes%60;
  const hh=
    Math.floor(totalMinutes/60)%100;

  return [hh,mm,ss,ff]
    .map(
      number=>
        String(number).padStart(2,"0")
    )
    .join(":");
}

function secondsToClock(seconds){
  const whole=Math.max(
    0,
    Math.floor(Number(seconds)||0)
  );
  const ss=whole%60;
  const totalMinutes=
    Math.floor(whole/60);
  const mm=totalMinutes%60;
  const hh=Math.floor(totalMinutes/60);

  return [hh,mm,ss]
    .map(
      number=>
        String(number).padStart(2,"0")
    )
    .join(":");
}

function estimatedAppEpochMs(){
  if(document.hidden||!liveConnected){
    return Date.now()+clockOffsetWallMs;
  }
  return performance.now()+clockOffsetPerfMs;
}

function rebasePerformanceClockFromWall(){
  clockOffsetPerfMs=
    Date.now()
    +clockOffsetWallMs
    -performance.now();
}

function estimatedMediaPosition(){
  if(mediaAnchorState!=="PLAYING"){
    return Math.max(
      0,
      mediaAnchorPosition
    );
  }

  const elapsed=Math.max(
    0,
    (
      estimatedAppEpochMs()
      -mediaAnchorAppEpochMs
    )/1000
  );
  return Math.max(
    0,
    mediaAnchorPosition+elapsed
  );
}

function openViewerDatabase(){
  return new Promise(
    (resolve,reject)=>{
      if(!("indexedDB" in window)){
        reject(
          new Error(
            "IndexedDB unavailable"
          )
        );
        return;
      }

      const request=indexedDB.open(
        DB_NAME,
        DB_VERSION
      );

      request.onupgradeneeded=()=>{
        const database=request.result;
        if(
          !database.objectStoreNames
            .contains(DB_STORE)
        ){
          database.createObjectStore(
            DB_STORE
          );
        }
      };

      request.onsuccess=()=>{
        resolve(request.result);
      };
      request.onerror=()=>{
        reject(request.error);
      };
    }
  );
}

async function databasePut(key,value){
  const database=
    await openViewerDatabase();

  return new Promise(
    (resolve,reject)=>{
      const transaction=
        database.transaction(
          DB_STORE,
          "readwrite"
        );
      transaction.objectStore(
        DB_STORE
      ).put(value,key);
      transaction.oncomplete=()=>{
        database.close();
        resolve();
      };
      transaction.onerror=()=>{
        const error=transaction.error;
        database.close();
        reject(error);
      };
    }
  );
}

async function databaseGet(key){
  const database=
    await openViewerDatabase();

  return new Promise(
    (resolve,reject)=>{
      const transaction=
        database.transaction(
          DB_STORE,
          "readonly"
        );
      const request=
        transaction.objectStore(
          DB_STORE
        ).get(key);

      request.onsuccess=()=>{
        const value=request.result;
        database.close();
        resolve(value);
      };
      request.onerror=()=>{
        const error=request.error;
        database.close();
        reject(error);
      };
    }
  );
}

function saveTransportCache(payload){
  const cached={
    state:payload,
    clock_offset_wall_ms:
      clockOffsetWallMs,
    saved_device_epoch_ms:
      Date.now()
  };

  try{
    localStorage.setItem(
      STATE_CACHE_KEY,
      JSON.stringify(cached)
    );
  }catch(error){
    // The live viewer continues even when storage is blocked.
  }
}

function loadTransportCache(){
  try{
    const raw=localStorage.getItem(
      STATE_CACHE_KEY
    );
    if(!raw)return null;

    const cached=JSON.parse(raw);
    if(
      !cached
      ||typeof cached!=="object"
      ||!cached.state
    ){
      return null;
    }
    return cached;
  }catch(error){
    return null;
  }
}

async function saveManifestCache(payload){
  try{
    await databasePut(
      "manifest",
      payload
    );
    return;
  }catch(error){
    // localStorage is only a fallback for browsers without IndexedDB.
  }

  try{
    localStorage.setItem(
      MANIFEST_FALLBACK_KEY,
      JSON.stringify(payload)
    );
  }catch(error){
    // Very large manifests may exceed localStorage quota.
  }
}

async function loadManifestCache(){
  try{
    const value=await databaseGet(
      "manifest"
    );
    if(value)return value;
  }catch(error){
    // Try the smaller compatibility fallback.
  }

  try{
    const raw=localStorage.getItem(
      MANIFEST_FALLBACK_KEY
    );
    return raw?JSON.parse(raw):null;
  }catch(error){
    return null;
  }
}

async function syncClockSample(){
  const perfStart=performance.now();
  const wallStart=Date.now();

  try{
    const response=await fetch(
      `/api/clock?nonce=${Math.random()}`,
      {cache:"no-store"}
    );
    if(!response.ok){
      throw new Error(
        `Clock HTTP ${response.status}`
      );
    }

    const payload=await response.json();
    const perfEnd=performance.now();
    const wallEnd=Date.now();
    const rtt=perfEnd-perfStart;
    const appEpoch=Number(
      payload.app_epoch_ms
      ||payload.server_epoch_ms
    );

    const perfMidpoint=
      (perfStart+perfEnd)/2;
    const wallMidpoint=
      (wallStart+wallEnd)/2;

    const candidatePerfOffset=
      appEpoch-perfMidpoint;
    const candidateWallOffset=
      appEpoch-wallMidpoint;

    if(rtt<clockBestRttMs){
      clockBestRttMs=rtt;
      clockOffsetPerfMs=
        candidatePerfOffset;
      clockOffsetWallMs=
        candidateWallOffset;
    }else{
      clockOffsetPerfMs=
        clockOffsetPerfMs*0.92
        +candidatePerfOffset*0.08;
      clockOffsetWallMs=
        clockOffsetWallMs*0.92
        +candidateWallOffset*0.08;
    }

    clockSynchronized=true;
    return true;
  }catch(error){
    return false;
  }
}

async function synchronizeClock(){
  let success=false;
  for(let index=0;index<5;index+=1){
    success=
      (await syncClockSample())
      ||success;
  }
  return success;
}

function fitFooterText(){
  const footer=$("standby");
  if(!footer)return;

  const maxSize=20;
  const minSize=10;
  const available=Math.max(
    80,
    footer.clientWidth-32
  );

  footer.style.fontSize=
    `${maxSize}px`;
  let size=maxSize;

  while(
    footer.scrollWidth>available
    &&size>minSize
  ){
    size-=1;
    footer.style.fontSize=
      `${size}px`;
  }
}

window.addEventListener(
  "resize",
  fitFooterText
);

function createCueNode(cueId){
  const section=
    document.createElement("section");
  section.className="loading-bar";
  section.dataset.cueId=
    String(cueId);

  const base=
    document.createElement("div");
  base.className="loading-base";

  const fill=
    document.createElement("div");
  fill.className="loading-fill";

  const content=
    document.createElement("div");
  content.className="loading-content";

  const title=
    document.createElement("div");
  title.className="loading-title";

  const abs=
    document.createElement("div");
  abs.className="loading-abs";

  const timing=
    document.createElement("div");
  timing.className="loading-time";

  content.append(title,abs,timing);
  section.append(base,fill,content);

  const node={
    section,
    base,
    fill,
    title,
    abs,
    timing,
    source:null,
    completionGeneration:-1
  };
  cueNodes.set(
    String(cueId),
    node
  );
  return node;
}

function clearCueNodes(){
  for(const [,node] of cueNodes){
    node.section.remove();
  }
  cueNodes.clear();
  $("cues").innerHTML="";
}

function firstFutureIndex(position){
  let low=0;
  let high=localCueSchedule.length;
  const threshold=
    Number(position)+0.0005;

  while(low<high){
    const middle=(low+high)>>1;

    if(
      Number(
        localCueSchedule[middle]
          .seconds
      )<=threshold
    ){
      low=middle+1;
    }else{
      high=middle;
    }
  }
  return low;
}

function syncRows(
  rows,
  hardReset=false
){
  const list=$("cues");
  if(hardReset){
    clearCueNodes();
  }

  const activeIds=new Set();

  if(!rows.length){
    if(
      mediaAnchorState
      ==="PLAYING"
    ){
      return;
    }

    clearCueNodes();
    list.innerHTML=
      '<div class="empty">'
      +'Không có cue sắp tới'
      +'</div>';
    return;
  }

  const empty=
    list.querySelector(".empty");
  if(empty)empty.remove();

  rows.forEach(cue=>{
    const id=String(cue.id);
    activeIds.add(id);

    const node=
      cueNodes.get(id)
      ||createCueNode(id);

    node.source={...cue};
    node.completionGeneration=-1;
    node.section.classList.remove(
      "completed"
    );

    const color=String(
      cue.color||"#4568d8"
    );
    const title=String(
      cue.label
      ||cue.name
      ||"Cue"
    );
    const type=String(
      cue.type||""
    );

    node.base.style.backgroundColor=
      color;
    node.fill.style.backgroundColor=
      color;
    node.title.textContent=
      type
        ?`${type} · ${title}`
        :title;
    node.abs.textContent=
      String(cue.timecode||"");

    list.appendChild(
      node.section
    );
  });

  requestAnimationFrame(()=>{
    for(const [id,node] of cueNodes){
      if(activeIds.has(id))continue;

      if(
        node.section.classList
          .contains("completed")
      ){
        continue;
      }

      node.section.remove();
      cueNodes.delete(id);
    }
  });
}

function updateLocalCueWindow(
  force=false,
  hardReset=false
){
  const position=
    estimatedMediaPosition();
  const firstFuture=
    firstFutureIndex(position);

  if(
    !force
    &&firstFuture
      ===localWindowStart
  ){
    return;
  }

  localWindowStart=firstFuture;
  const rows=
    localCueSchedule.slice(
      firstFuture,
      firstFuture+localWindowSize
    );

  syncRows(
    rows,
    hardReset
  );
}

function scrollBackToCurrentCue(){
  requestAnimationFrame(()=>{
    const first=
      $("cues").querySelector(
        ".loading-bar"
      );

    if(first){
      first.scrollIntoView({
        behavior:"smooth",
        block:"start"
      });
    }
  });
}

function applyManifest(
  payload,
  options={}
){
  if(
    !payload
    ||typeof payload!=="object"
  ){
    return false;
  }

  manifestRevision=String(
    payload.manifest_revision||""
  );
  localCueSchedule=(
    Array.isArray(payload.cues)
      ?payload.cues
      :[]
  )
    .map(cue=>({
      ...cue,
      seconds:Number(
        cue.seconds||0
      )
    }))
    .sort(
      (left,right)=>
        left.seconds-right.seconds
    );

  timelineGeneration+=1;
  localWindowStart=-1;
  updateLocalCueWindow(
    true,
    true
  );

  if(options.persist!==false){
    saveManifestCache(payload);
  }
  return true;
}

function stateIsNewer(payload){
  const nextSession=String(
    payload.session_id||"legacy"
  );
  const nextSequence=Number(
    payload.transport_sequence||0
  );

  if(
    currentSessionId
    &&nextSession===currentSessionId
    &&nextSequence
      <currentTransportSequence
  ){
    return false;
  }
  return true;
}

function applyTransportState(
  payload,
  options={}
){
  if(
    !payload
    ||typeof payload!=="object"
  ){
    return false;
  }

  if(
    options.fromNetwork
    &&!stateIsNewer(payload)
  ){
    return false;
  }

  const before=
    estimatedMediaPosition();
  const previousTrack=String(
    latestState.track||""
  );

  const nextTrack=String(
    payload.track
    ||"No track selected"
  );
  const nextPosition=Number(
    payload.position||0
  );
  const nextState=String(
    payload.state||"STOPPED"
  );
  const nextAppEpoch=Number(
    payload.app_epoch_ms
    ||payload.server_epoch_ms
    ||Date.now()
  );
  const nextSession=String(
    payload.session_id||"legacy"
  );
  const nextSequence=Number(
    payload.transport_sequence||0
  );

  if(
    options.fromNetwork
    &&!clockSynchronized
  ){
    // First-state fallback. Clock sync will refine this later.
    clockOffsetWallMs=
      nextAppEpoch-Date.now();
    rebasePerformanceClockFromWall();
  }

  latestState={...payload};
  currentSessionId=nextSession;
  currentTransportSequence=
    nextSequence;

  const trackChanged=
    previousTrack!==nextTrack;
  const correction=
    nextPosition-before;
  const timelineJump=
    trackChanged
    ||Math.abs(correction)>0.18;
  const movedBackward=
    !trackChanged
    &&correction<-0.12;

  mediaAnchorPosition=
    nextPosition;
  mediaAnchorAppEpochMs=
    nextAppEpoch;
  mediaAnchorState=
    nextState;
  mediaFps=Math.max(
    1,
    Number(payload.fps||30)
  );
  mediaOffsetSeconds=Number(
    payload.offset_seconds||0
  );

  $("state").textContent=
    nextState;
  refreshTimecodeAlarm();
  $("offset").textContent=
    "OFFSET "
    +String(
      payload.offset
      ||"00:00:00:00"
    );
  $("track").textContent=
    nextTrack;

  const standby=String(
    payload.standby||""
  );
  $("standby").textContent=
    standby
      ?`STANDBY ${standby}`
      :"";
  requestAnimationFrame(
    fitFooterText
  );

  if(timelineJump){
    timelineGeneration+=1;
    localWindowStart=-1;
    updateLocalCueWindow(
      true,
      true
    );

    if(movedBackward){
      scrollBackToCurrentCue();
    }
  }else{
    updateLocalCueWindow();
  }

  if(options.persist!==false){
    saveTransportCache(payload);
  }
  return true;
}

function completeCueNode(id,node){
  if(
    node.section.classList
      .contains("completed")
  ){
    return;
  }

  const generation=
    timelineGeneration;
  node.completionGeneration=
    generation;
  node.section.classList.add(
    "completed"
  );

  window.setTimeout(()=>{
    if(
      cueNodes.get(id)!==node
    )return;

    if(
      node.completionGeneration
      !==generation
    )return;

    if(
      generation
      !==timelineGeneration
    )return;

    if(
      Number(
        node.source?.seconds||0
      )
      >estimatedMediaPosition()
      +0.01
    ){
      node.section.classList.remove(
        "completed"
      );
      return;
    }

    node.section.remove();
    cueNodes.delete(id);
  },220);
}

function animateCueBars(now){
  displayedPosition=
    estimatedMediaPosition();

  const absoluteSeconds=
    displayedPosition
    +mediaOffsetSeconds;

  $("tc").textContent=
    framesToTimecode(
      absoluteSeconds*mediaFps,
      mediaFps
    );

  const duration=Number(
    latestState
      .duration_seconds||0
  );
  $("elapsed").textContent=
    `${secondsToClock(displayedPosition)} / `
    +`${secondsToClock(duration)}`;

  for(const [id,node] of cueNodes){
    const cue=node.source;
    if(!cue)continue;

    const remaining=
      Number(cue.seconds||0)
      -displayedPosition;

    if(remaining<=0){
      node.fill.classList.add(
        "active"
      );
      node.fill.style.width="100%";
      node.timing.textContent="0.0s";
      completeCueNode(id,node);
      continue;
    }

    node.section.classList.remove(
      "completed"
    );
    node.completionGeneration=-1;

    const inside=
      remaining<=5.0;
    const progress=
      inside
        ?Math.max(
            0,
            Math.min(
              1,
              (5.0-remaining)/5.0
            )
          )
        :0;

    node.fill.classList.toggle(
      "active",
      inside
    );
    node.fill.style.width=
      `${progress*100}%`;
    node.timing.textContent=
      `-${remaining.toFixed(1)}s`;
  }

  if(now-lastWindowCheck>=50){
    lastWindowCheck=now;
    updateLocalCueWindow();
  }

  requestAnimationFrame(
    animateCueBars
  );
}
requestAnimationFrame(
  animateCueBars
);

async function fetchCurrentFromApp(){
  try{
    await synchronizeClock();

    const [
      manifestResponse,
      stateResponse
    ]=await Promise.all([
      fetch(
        "/api/manifest",
        {cache:"no-store"}
      ),
      fetch(
        "/api/state",
        {cache:"no-store"}
      )
    ]);

    if(
      !manifestResponse.ok
      ||!stateResponse.ok
    ){
      throw new Error(
        "Viewer state request failed"
      );
    }

    const manifest=
      await manifestResponse.json();
    const state=
      await stateResponse.json();

    applyManifest(manifest);
    applyTransportState(
      state,
      {fromNetwork:true}
    );

    liveConnected=true;
    rebasePerformanceClockFromWall();
    setConnectionStatus(
      "LIVE",
      "live"
    );
    return true;
  }catch(error){
    liveConnected=false;
    rebasePerformanceClockFromWall();
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
    updateLocalCueWindow(
      true,
      true
    );
    return false;
  }
}

function closeEventSource(){
  if(eventSource){
    eventSource.close();
    eventSource=null;
  }
}

function connectEvents(){
  closeEventSource();

  try{
    eventSource=
      new EventSource("/events");
  }catch(error){
    liveConnected=false;
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
    return;
  }

  const parseEvent=event=>{
    try{
      return JSON.parse(
        event.data
      );
    }catch(error){
      setConnectionStatus(
        "DATA ERROR",
        "error"
      );
      return null;
    }
  };

  eventSource.addEventListener(
    "manifest",
    event=>{
      const payload=
        parseEvent(event);
      if(payload){
        applyManifest(payload);
      }
    }
  );

  eventSource.addEventListener(
    "state",
    event=>{
      const payload=
        parseEvent(event);
      if(!payload)return;

      liveConnected=true;
      applyTransportState(
        payload,
        {fromNetwork:true}
      );
      setConnectionStatus(
        "LIVE",
        "live"
      );
    }
  );

  eventSource.onopen=()=>{
    setConnectionStatus(
      "SYNCING",
      "live"
    );
  };

  eventSource.onerror=()=>{
    liveConnected=false;
    rebasePerformanceClockFromWall();
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
  };
}

async function restoreOfflineCache(){
  restoringCache=true;

  const cachedTransport=
    loadTransportCache();
  if(cachedTransport){
    clockOffsetWallMs=Number(
      cachedTransport
        .clock_offset_wall_ms||0
    );
    rebasePerformanceClockFromWall();

    applyTransportState(
      cachedTransport.state,
      {
        fromCache:true,
        persist:false
      }
    );
  }

  const cachedManifest=
    await loadManifestCache();
  if(cachedManifest){
    applyManifest(
      cachedManifest,
      {persist:false}
    );
  }

  restoringCache=false;

  if(
    cachedTransport
    ||cachedManifest
  ){
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
  }
}

async function resumeViewer(){
  rebasePerformanceClockFromWall();
  updateLocalCueWindow(
    true,
    true
  );
  scrollBackToCurrentCue();

  if(
    navigator.onLine!==false
  ){
    await fetchCurrentFromApp();
    connectEvents();
  }else{
    liveConnected=false;
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
  }
}

document.addEventListener(
  "visibilitychange",
  ()=>{
    if(!document.hidden){
      resumeViewer();
    }
  }
);

window.addEventListener(
  "pageshow",
  ()=>{
    rebasePerformanceClockFromWall();
    updateLocalCueWindow(
      true,
      true
    );
  }
);

window.addEventListener(
  "online",
  ()=>{
    resumeViewer();
  }
);

window.addEventListener(
  "offline",
  ()=>{
    liveConnected=false;
    rebasePerformanceClockFromWall();
    setConnectionStatus(
      "OFFLINE — ESTIMATED",
      "offline"
    );
  }
);

async function registerOfflineShell(){
  if(
    !("serviceWorker" in navigator)
  ){
    return false;
  }

  try{
    await navigator.serviceWorker
      .register(
        "/service-worker.js",
        {scope:"/"}
      );
    return true;
  }catch(error){
    // LAN HTTP pages are not secure contexts on most phones.
    return false;
  }
}

(async()=>{
  setConnectionStatus(
    "RESTORING",
    "offline"
  );

  await restoreOfflineCache();
  await registerOfflineShell();

  const online=
    await fetchCurrentFromApp();

  if(online){
    connectEvents();
  }else{
    // EventSource can reconnect automatically when LAN returns.
    connectEvents();
  }

  window.setInterval(
    ()=>{
      if(liveConnected){
        syncClockSample()
          .then(success=>{
            if(success){
              saveTransportCache(
                latestState
              );
            }
          });
      }
    },
    30000
  );
})();
</script>
</body>
</html>"""


SERVICE_WORKER_JS = r"""const CACHE_NAME="tracyy-cue-viewer-shell-v4-responsive";
const SHELL_URLS=[
  "/",
  "/index.html",
  "/logo.png?v=4"
];

self.addEventListener("install",event=>{
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache=>cache.addAll(SHELL_URLS))
      .then(()=>self.skipWaiting())
  );
});

self.addEventListener("activate",event=>{
  event.waitUntil(
    caches.keys()
      .then(keys=>Promise.all(
        keys
          .filter(key=>key!==CACHE_NAME)
          .map(key=>caches.delete(key))
      ))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener("fetch",event=>{
  const request=event.request;
  if(request.method!=="GET")return;

  const url=new URL(request.url);
  if(url.origin!==self.location.origin)return;

  if(
    url.pathname.startsWith("/api/")
    ||url.pathname==="/events"
  ){
    return;
  }

  if(request.mode==="navigate"){
    event.respondWith(
      fetch(request)
        .then(response=>{
          const copy=response.clone();
          caches.open(CACHE_NAME)
            .then(cache=>cache.put("/",copy));
          return response;
        })
        .catch(()=>
          caches.match("/")
            .then(cached=>
              cached
              ||caches.match("/index.html")
            )
        )
    );
    return;
  }

  event.respondWith(
    caches.match(request)
      .then(cached=>{
        if(cached)return cached;
        return fetch(request)
          .then(response=>{
            const copy=response.clone();
            caches.open(CACHE_NAME)
              .then(cache=>cache.put(request,copy));
            return response;
          });
      })
  );
});
"""


class StateReader:
    """Watches the state file once, for every connected viewer.

    V1 gave each SSE connection its own loop: every 80 ms it stat()ed the state
    file, and on any change it re-read the file, re-parsed the JSON — cues and
    all — and re-serialised the events. Ten phones on the LAN meant ten threads
    doing identical work ten times, on the show machine.

    V2 reads and parses once in a single watcher thread, pre-serialises the two
    SSE frames, and publishes a revision number. Connections block on a
    condition variable and write bytes that are already built, so a viewer
    costs one socket write per change instead of a permanent poll loop.
    """

    DEFAULT_STATE: ClassVar[dict[str, object]] = {
        "timecode": "00:00:00:00",
        "state": "STOPPED",
        "offset": "00:00:00:00",
        "elapsed": "00:00:00",
        "duration": "00:00:00",
        "track": "No track selected",
        "cues": [],
    }

    def __init__(self, path: Path, *, poll_interval: float = 0.05) -> None:
        self.path = path
        self.poll_interval = max(0.01, float(poll_interval))
        self._condition = threading.Condition()
        self._cached = dict(self.DEFAULT_STATE)
        self._revision = 0
        self._modified_ns = -1
        self._state_frame = b""
        self._manifest_frame = b""
        self._manifest_signature: tuple[str, str, str] = ("", "", "")
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh(force=True)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch,
            name="TracyyStateWatcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._condition:
            self._condition.notify_all()

    def _watch(self) -> None:
        while not self._stopping.is_set():
            self._refresh()
            self._stopping.wait(self.poll_interval)

    # -- reading ---------------------------------------------------------

    def _refresh(self, *, force: bool = False) -> None:
        try:
            modified = self.path.stat().st_mtime_ns
        except OSError:
            modified = 0
        if not force and modified == self._modified_ns:
            return

        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A half-written state file is normal: the app replaces it while
            # viewers are connected. Keep serving the last good snapshot.
            self._modified_ns = modified
            return
        if not isinstance(value, dict):
            self._modified_ns = modified
            return

        state_frame = _sse_frame("state", Handler.state_payload(value))
        manifest_payload = Handler.manifest_payload(value)
        signature = (
            str(value.get("manifest_revision", "")),
            str(value.get("track", "")),
            str(value.get("session_id", "")),
        )
        manifest_frame = _sse_frame("manifest", manifest_payload)

        with self._condition:
            self._cached = value
            self._modified_ns = modified
            self._state_frame = state_frame
            self._manifest_frame = manifest_frame
            self._manifest_signature = signature
            self._revision += 1
            self._condition.notify_all()

    def read(self) -> dict:
        """The most recent parsed state, for the plain JSON endpoints."""
        with self._condition:
            return dict(self._cached)

    def modified_ns(self) -> int:
        return self._modified_ns

    # -- change feed -----------------------------------------------------

    def snapshot(self) -> tuple[int, bytes, bytes, tuple[str, str, str]]:
        with self._condition:
            return (
                self._revision,
                self._state_frame,
                self._manifest_frame,
                self._manifest_signature,
            )

    def wait_for_change(self, since_revision: int, timeout: float) -> bool:
        """Block until the state changes. False if the timeout expired."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._revision == since_revision and not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return self._revision != since_revision


#: Comment frame browsers ignore, sent so idle connections stay open through
#: NAT and proxy timeouts.
KEEPALIVE_FRAME = b": keepalive\n\n"

#: How long an idle event stream sleeps between keepalive comments.
#:
#: This doubles as how quickly a viewer that closed its page stops being
#: counted, so it is deliberately shorter than the usual 15-30s: the count is
#: read by a human watching a number on screen, and half a minute of "2 devices"
#: with nobody connected reads as a bug. The cost is a 13-byte comment per
#: viewer per interval.
KEEPALIVE_SECONDS = 4.0


def _sse_frame(event_name: str, payload: dict) -> bytes:
    """Serialise one server-sent event once, for every connected viewer."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {body}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @staticmethod
    def state_payload(
        source: dict,
    ) -> dict:
        payload = dict(source)
        payload.pop("cues", None)
        return payload

    @staticmethod
    def manifest_payload(
        source: dict,
    ) -> dict:
        return {
            "manifest_revision": str(
                source.get(
                    "manifest_revision",
                    "",
                )
            ),
            "session_id": str(
                source.get(
                    "session_id",
                    "",
                )
            ),
            "track": str(
                source.get(
                    "track",
                    "No track selected",
                )
            ),
            "fps": int(
                source.get(
                    "fps",
                    30,
                )
                or 30
            ),
            "offset": str(
                source.get(
                    "offset",
                    "00:00:00:00",
                )
            ),
            "offset_seconds": float(
                source.get(
                    "offset_seconds",
                    0.0,
                )
                or 0.0
            ),
            "cues": list(
                source.get(
                    "cues",
                    [],
                )
            ),
        }

    def send_event(
        self,
        event_name: str,
        payload: dict,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        message = (f"event: {event_name}\ndata: {body}\n\n").encode()
        self.wfile.write(message)
        self.wfile.flush()

    def serve_events(self) -> None:
        """Server-sent events, driven by the shared watcher.

        The connection now sleeps on a condition variable instead of polling
        the state file, so an idle viewer costs nothing while it waits.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        reader = self.server.state_reader
        revision, state_frame, manifest_frame, signature = reader.snapshot()

        # A viewer that hung up must stop being counted promptly, and waiting
        # for a write to fail does not do that: the first write onto a closed
        # socket still lands in the send buffer and only the one after it
        # raises, so a phone that left would sit in the count for two whole
        # keepalive intervals. _peer_hung_up watches the read side instead,
        # which sees the close itself. The socket stays blocking on purpose —
        # a phone on weak WiFi should throttle the writer, not be dropped.

        # One event stream per open viewer, held for as long as the page is
        # open — which makes this the honest place to count them.
        self.server.viewer_joined()
        try:
            self.wfile.write(manifest_frame)
            self.wfile.write(state_frame)
            self.wfile.flush()

            while True:
                changed = reader.wait_for_change(revision, timeout=KEEPALIVE_SECONDS)

                if self._peer_hung_up():
                    return

                if not changed:
                    self.wfile.write(KEEPALIVE_FRAME)
                    self.wfile.flush()
                    continue

                revision, state_frame, manifest_frame, new_signature = reader.snapshot()
                if new_signature != signature:
                    signature = new_signature
                    self.wfile.write(manifest_frame)
                self.wfile.write(state_frame)
                self.wfile.flush()
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            OSError,
        ):
            return
        finally:
            self.server.viewer_left()

    def _peer_hung_up(self) -> bool:
        """True once the far end of this event stream has closed.

        An SSE connection is one-way once the headers are out, so anything
        arriving on the read side is either the close itself or a client that
        does not understand the protocol. Only end-of-file is treated as a
        hang-up; unexpected bytes are left alone rather than used as an excuse
        to drop a viewer that is still watching.
        """
        connection = self.connection
        try:
            readable, _, _ = select.select([connection], [], [], 0)
            if not readable:
                return False
            return connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    #: Below this, framing and CPU cost more than the bytes saved.
    GZIP_MIN_BYTES = 900

    def _accepts_gzip(self) -> bool:
        encodings = self.headers.get("Accept-Encoding", "")
        return "gzip" in encodings.lower()

    def send_bytes(
        self,
        payload: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        extra_headers: dict[str, str] | None = None,
        etag: str | None = None,
    ) -> None:
        """Send one response, compressed and revalidated where it helps.

        V1 sent every response uncompressed and without a validator, so each
        viewer re-downloaded the whole 20 kB page and the logo on every reload,
        over the venue WiFi that the show also depends on. Compression and an
        ETag cost the server almost nothing and turn a reconnecting phone into
        a pair of 304s.
        """
        if etag is None and cache_control != "no-store":
            etag = f'W/"{hashlib.blake2b(payload, digest_size=12).hexdigest()}"'

        if etag is not None and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return

        encoding: str | None = None
        if len(payload) >= self.GZIP_MIN_BYTES and self._accepts_gzip():
            compressed = gzip.compress(payload, compresslevel=6)
            if len(compressed) < len(payload):
                payload = compressed
                encoding = "gzip"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        if encoding is not None:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        if etag is not None:
            self.send_header("ETag", etag)

        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)

        self.end_headers()

        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(
        self,
        payload: dict,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_bytes(
            body,
            "application/json; charset=utf-8",
            extra_headers={
                "Access-Control-Allow-Origin": "*",
            },
        )

    def do_GET(self) -> None:
        route = self.path.split(
            "?",
            1,
        )[0]

        if route == "/logo.png":
            logo = brand_logo_bytes()
            if logo is None:
                self.send_error(404)
                return

            payload, content_type = logo
            self.send_bytes(
                payload,
                content_type,
                cache_control="public, max-age=86400",
            )
            return

        if route == "/service-worker.js":
            self.send_bytes(
                SERVICE_WORKER_JS.encode("utf-8"),
                ("application/javascript; charset=utf-8"),
                cache_control="no-cache",
                extra_headers={
                    "Service-Worker-Allowed": "/",
                },
            )
            return

        if route == "/events":
            self.serve_events()
            return

        if route in ("/", "/index.html"):
            self.send_bytes(
                VIEWER_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
                cache_control="no-cache",
                extra_headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; "
                        "script-src 'self' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; "
                        "img-src 'self' data:; "
                        "connect-src 'self'; "
                        "object-src 'none'; "
                        "base-uri 'none'; "
                        "form-action 'none'"
                    ),
                },
            )
            return

        if route == "/api/clock":
            app_epoch_ms = time.time() * 1000.0
            self.send_json(
                {
                    "app_epoch_ms": app_epoch_ms,
                    "server_epoch_ms": app_epoch_ms,
                    "server_monotonic": (time.monotonic()),
                }
            )
            return

        current = self.server.state_reader.read()

        if route == "/api/state":
            self.send_json(self.state_payload(current))
            return

        if route == "/api/manifest":
            self.send_json(self.manifest_payload(current))
            return

        self.send_error(404)

    def reject_control_request(
        self,
    ) -> None:
        payload = json.dumps(
            {
                "ok": False,
                "error": ("READ_ONLY_VIEWER"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(405)
        self.send_header(
            "Allow",
            "GET",
        )
        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )
        self.send_header(
            "Content-Length",
            str(len(payload)),
        )
        self.send_header(
            "Cache-Control",
            "no-store",
        )
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (
            BrokenPipeError,
            ConnectionResetError,
        ):
            pass

    def do_POST(self) -> None:
        self.reject_control_request()

    def do_PUT(self) -> None:
        self.reject_control_request()

    def do_PATCH(self) -> None:
        self.reject_control_request()

    def do_DELETE(self) -> None:
        self.reject_control_request()

    def log_message(
        self,
        fmt: str,
        *args,
    ) -> None:
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        state_reader: StateReader,
        status_path: Path | None = None,
    ) -> None:
        self.state_reader = state_reader
        self._status_path = status_path
        self._viewers = 0
        self._viewer_lock = threading.Lock()
        super().__init__(address, Handler)
        # Publish zero straight away so the application can tell "nobody has
        # connected yet" from "the server never started".
        with self._viewer_lock:
            self._publish_viewers(0)

    def viewer_joined(self) -> None:
        with self._viewer_lock:
            self._viewers += 1
            self._publish_viewers(self._viewers)

    def viewer_left(self) -> None:
        with self._viewer_lock:
            self._viewers = max(0, self._viewers - 1)
            self._publish_viewers(self._viewers)

    def _publish_viewers(self, count: int) -> None:
        """Report the live viewer count back to the application.

        The application writes state down to this process through a file; this
        is the only channel going the other way, and a file keeps it that way —
        no port to open, nothing to authenticate, and a server that dies simply
        stops updating a file the application deletes when it stops the server.

        Call this while holding ``_viewer_lock``, as both callers do. Counting
        under the lock and publishing outside it looks equivalent and is not:
        two viewers leaving together would each compute a count correctly and
        then race to write, and whichever landed second would win. Losing that
        race once leaves the file reading one too high until the next join or
        leave, which for an audience that has all gone home is forever. Holding
        the lock across the write also means one shared temporary name is safe,
        because only one write is ever in flight.
        """
        if self._status_path is None:
            return

        payload = json.dumps(
            {"viewers": int(count), "at": time.time()},
            separators=(",", ":"),
        ).encode("utf-8")

        temporary = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, self._status_path)
        except OSError:
            # A viewer count is not worth failing a request over.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def handle_error(self, request, client_address) -> None:
        """Stay quiet when a viewer walks away.

        A phone that locks its screen or leaves WiFi drops the SSE socket
        mid-write. socketserver's default prints a full traceback for each
        one, which fills the log with noise that means nothing.
        """
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bind-address",
        default="0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
    )
    parser.add_argument(
        "--state-file",
        required=True,
    )
    args = parser.parse_args()

    state_file = Path(args.state_file)
    state_reader = StateReader(state_file)
    state_reader.start()
    server = Server(
        (str(args.bind_address), args.port),
        state_reader,
        status_path=state_file.with_name(state_file.name + ".viewers"),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        state_reader.stop()
        server.server_close()


if __name__ == "__main__":
    main()
