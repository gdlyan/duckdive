"""DuckDive table component on duckdb-wasm: data never leaves the parquet,
all operations (search, filters, sorting, paging, value popups, export)
run as SQL queries in the worker. For reports with millions of rows.

The single-file recipe comes from build_html_dashboard.py / ixi_dashboard.html:
fetch is forbidden on file://, so duckdb-mvp.wasm is embedded into the worker
code as base64, and JS libraries are assembled into blob modules with
rewritten specifiers. Row identity for checkboxes is the service column
__rn (ROW_NUMBER, added when generating the parquet).
"""
import gzip
import json
from pathlib import Path

from .table_ui import _CSS, _POPUP_SRC, _inject_icons, _json_escape_script, _xlsx_src

_BOOT_CSS = """
.dd-boot{padding:26px 14px;font-size:12px;color:var(--dd-muted,#71717a)}
.dd-boot-lbl{margin-bottom:9px;font-weight:550}
.dd-bar{height:8px;background:var(--dd-soft,#f6f6f8);border-radius:999px;overflow:hidden;max-width:340px}
.dd-fill{height:100%;background:var(--dd-accent,#e8590c);border-radius:999px;transition:width .2s}
.dd-fill.ind{position:relative;width:40%!important;animation:ddInd 1.1s ease-in-out infinite}
@keyframes ddInd{0%{margin-left:-40%}100%{margin-left:100%}}
.dd-fill.err{background:var(--dd-danger,#d64545)}
.dd-boot-err{margin-top:9px;color:var(--dd-danger,#d64545);max-width:520px;line-height:1.5;user-select:text;cursor:text}
"""

# duckdb-wasm runtime is vendored inside the package: DuckDive must be
# self-contained and build reports without any external project or network
_DUCKDB_DIR = Path(__file__).resolve().parent / "assets" / "wasm"
_LIBS_DIR = _DUCKDB_DIR

_BUNDLE_CACHE: dict | None = None

# fetch is forbidden on file:// — wasm is served from embedded base64
_WORKER_SHIM = """(function(){
  const of=self.fetch?self.fetch.bind(self):null;
  self.addEventListener('message',e=>{
    const d=e.data;
    if(d&&d.__ddWasmBytes)self.__WASM_BYTES=d.__ddWasmBytes;
  });
  self.fetch=function(u,o){
    const url=(typeof u==='string')?u:(u&&u.url)||'';
    if(url.indexOf('.wasm')>=0){
      if(self.__WASM_BYTES)return Promise.resolve(new Response(self.__WASM_BYTES,{status:200,headers:{'Content-Type':'application/wasm'}}));
      return Promise.reject(new Error('wasm bytes not delivered to worker'));
    }
    return of?of(u,o):Promise.reject(new Error('no fetch'));
  };
})();
"""

_JS = r"""
(async function(){
const root=document.getElementById('duckdive-table');
const B=JSON.parse(document.getElementById('duckdive-boot').textContent);
const STAGE_PCT={modules:8,decode:35,decompress:95,compile:96,register:99,ready:100};
let bootStage='modules',bootDone=false,lastProg=Date.now(),watchdog=null,errShown=false,lastErr='';
function bootUI(lbl,pct,ind,err){
  if(err){errShown=true;lastErr=err}
  else if(errShown)return;
  const e=s=>(s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  root.innerHTML=`<div class="dd-boot">
    <div class="dd-boot-lbl">${e(lbl)}</div>
    <div class="dd-bar"><div class="dd-fill${ind?' ind':''}${err?' err':''}" style="width:${pct==null?100:pct}%"></div></div>
    ${err?`<div class="dd-boot-err">${e(err)}</div>`:''}
  </div>`;
}
function watchdogStart(){
  lastProg=Date.now();
  if(watchdog)clearInterval(watchdog);
  watchdog=setInterval(()=>{
    if(bootDone)return;
    if(Date.now()-lastProg>5000){
      const st={modules:'loading modules',decode:'decoding base64',decompress:'decompressing wasm',compile:'compiling wasm',register:'registering parquet'}[bootStage]||bootStage;
      bootUI('Boot stalled for 5s at: '+st,STAGE_PCT[bootStage],false,
        lastErr||('Stage: '+st));
    }
  },1000);
}
function onProg(name,pct){
  lastProg=Date.now();
  bootStage=name;
  bootUI(name==='decode'?'Decoding wasm (base64)...':name==='decompress'?'Decompressing DuckDB-WASM (gzip)...':name==='compile'?'Compiling DuckDB-WASM...':'Registering data...',
    pct,name!=='decode'&&name!=='decompress');
}
const _onDbMessage=e=>{
  const d=e.data;
  if(d&&d.type==='LOG'&&d.data&&d.data.ddBoot){lastProg=Date.now()}
};
const _onWorkerError=e=>{
  lastErr='Worker error: '+(e.message||'unknown');
  if(!bootDone)bootUI('DuckDB-WASM init failed',100,false,lastErr);
};
window.__ddFilters=window.__ddFilters||{};

/* ── global cross-filter context (GFC) ──
   Column atoms: window.__ddFilters (shared pool, compatibility with older
   templates) + __ddFiltersSrc (who set it) + __ddFiltersCond (ready-made
   SQL predicate built by the source). Row atoms (selection/search of
   a source): temporary tables __ddrow_<source name>; receivers subscribe
   via use_filter_keys ("*" or "source:key1,key2|...").
   A component's own atoms do not affect its own base — feedback loops
   are impossible by construction. cross_filters=false — the component
   is outside GFC. */
window.__ddFiltersSrc=window.__ddFiltersSrc||{};
window.__ddFiltersCond=window.__ddFiltersCond||{};
const GFC={rows:{},subs:[],views:[],remap:[],resrc:[]};
window.__ddGFC=GFC;
const gfcQid=n=>'"'+String(n).replace(/"/g,'""')+'"';
function parseWant(want){
  if(want==null)return null;
  if(String(want).trim()==='*')return '*';
  const m={};
  String(want).split('|').forEach(part=>{
    const p=part.split(':');const src=(p[0]||'').trim();if(!src)return;
    m[src]=(p[1]||'').split(',').map(s=>s.trim()).filter(Boolean);
  });
  return m;
}
window.__ddGFCRegister=function(who,want){
  GFC.subs=GFC.subs.filter(s=>s.who!==who);
  const w=parseWant(want);
  if(!w)return;
  if(w==='*'){GFC.subs.push({who:who,src:'*'});return}
  for(const[src,keys]of Object.entries(w))GFC.subs.push({who:who,src:src,keys:keys});
};
function rowWantsFor(srcName,explicitOnly){
  let keys=null,all=false,any=false;
  for(const s of GFC.subs){
    if(s.src==='*'){
      /* a "*" subscriber consumes what was published under explicit
         subscriptions; when publishing, "*" itself sets no keys (otherwise
         all table columns are published and EXISTS bloats) */
      if(explicitOnly)continue;
      all=true;any=true;continue
    }
    if(s.src!==srcName)continue;
    any=true;keys=keys||[];
    (s.keys||[]).forEach(k=>{if(!keys.includes(k))keys.push(k)});
  }
  return any?{all:all,keys:keys||[]}:null;
}
function gfcWhereFor(myName,want,ref,allow,cols){
  ref=ref||'';
  const baseCols={};(B.columns||[]).forEach(c=>baseCols[c.name]=1);
  const w=parseWant(want);
  const allowArr=allow?String(allow).split(',').map(s=>s.trim()).filter(Boolean):null;
  const parts=[];
  for(const[col,f]of Object.entries(window.__ddFilters)){
    const src=window.__ddFiltersSrc[col];
    if(myName&&src&&src===myName){
      /* the atom's own source is skipped entirely, except the toggle part:
         it is external to the table and must apply to it as well */
      const tc=window.__ddToggleCond?window.__ddToggleCond(col):'';
      if(tc&&(!cols||cols.includes(col)))parts.push(tc);
      continue;
    }
    /* filter_columns is an allowlist: foreign column atoms on columns outside
       the list (e.g. metrics) are not applied to this component's base */
    if(allowArr&&!allowArr.includes(col))continue;
    /* atoms on columns this component does not have cannot apply (e.g. a
       grouped SQL view without the filtered column) */
    if(cols&&!cols.includes(col))continue;
    const cond=window.__ddFiltersCond[col];
    if(cond)parts.push(cond);
  }
  if(w){
    for(const ra of Object.values(GFC.rows)){
      let keys=w==='*'?ra.cols.slice():((w[ra.src]||[]).filter(k=>ra.cols.includes(k)));
      keys=keys.filter(k=>baseCols[k]);
      if(cols)keys=keys.filter(k=>cols.includes(k));
      if(!keys.length)continue;
      parts.push('EXISTS (SELECT 1 FROM '+ra.table+' k WHERE '
        +keys.map(k=>gfcQid(k)+' IS NOT DISTINCT FROM '+ref+gfcQid(k)).join(' AND ')+')');
    }
  }
  return parts.join(' AND ');
}
async function buildGfcView(v){
  if(v.hasSrc){
    if(!v.cols){
      await DQ(`CREATE OR REPLACE VIEW __bf${v.idx} AS SELECT b.* FROM t b WHERE TRUE`);
      const inner0=v.sql.split('{{src}}').join('__bf'+v.idx);
      const dc=await DQ(`SELECT * FROM (${inner0}) LIMIT 0`);
      v.cols=dc.schema.fields.map(f=>f.name);
    }
    const where=gfcWhereFor(v.name,v.want,'b.',v.allow,v.cols);
    await DQ(`CREATE OR REPLACE VIEW __bf${v.idx} AS SELECT b.* FROM t b WHERE ${where||'TRUE'}`);
  }
  const inner=v.hasSrc?v.sql.split('{{src}}').join('__bf'+v.idx):v.sql;
  await DQ(`CREATE OR REPLACE VIEW ${v.view} AS SELECT * FROM (${inner})`);
  const d0=await DQ(`DESCRIBE ${v.view}`);
  /* __rn coming from the base t is stable across rebuilds — selections of
     such a component need no capture/restore; a synthetic ROW_NUMBER does */
  v.stableRn=d0.toArray().some(r=>r.column_name==='__rn');
  if(!v.stableRn){
    await DQ(`CREATE OR REPLACE VIEW ${v.view} AS SELECT s.*, ROW_NUMBER() OVER () AS __rn FROM (${inner}) s`);
  }
}
let gfcPend=false;
async function gfcChanged(){
  if(gfcPend)return;gfcPend=true;
  await new Promise(r=>setTimeout(r,0));
  gfcPend=false;
  const caps=[];
  for(const r of GFC.remap){try{caps.push(await r.capture())}catch(e){caps.push(null)}}
  for(const v of GFC.views){if(!v.hasSrc)continue;try{await buildGfcView(v)}catch(e){console.warn('duckdive: gfc view rebuild failed:',e)}}
  for(const fn of GFC.resrc){try{await fn()}catch(e){}}
  for(let i=0;i<GFC.remap.length;i++){try{await GFC.remap[i].restore(caps[i])}catch(e){}}
  document.dispatchEvent(new CustomEvent('dd-gfc-changed',{}));
}
window.__ddGFCWhere=gfcWhereFor;
/* ── duckdb-wasm: blob modules following the ixi_dashboard recipe ── */
function b64ToBytes(b64){
  const s=atob(b64),b=new Uint8Array(s.length);
  for(let i=0;i<s.length;i++)b[i]=s.charCodeAt(i);
  return b;
}
function blobUrl(text){return URL.createObjectURL(new Blob([text],{type:'text/javascript'}))}
async function decodeWasm(){
  onProg('decode',1);
  const gzB64=WASM_GZ,CH=1<<22;
  const parts=[];
  for(let i=0;i<gzB64.length;i+=CH){
    parts.push(atob(gzB64.slice(i,i+CH)));
    onProg('decode',Math.round(Math.min(100,(i+CH)/gzB64.length*35)));
  }
  const gzParts=parts.map(p=>{const b=new Uint8Array(p.length);for(let j=0;j<p.length;j++)b[j]=p.charCodeAt(j);return b});
  const raw=new Uint8Array(WASM_RAW);
  const src=new Blob(gzParts).stream().pipeThrough(new DecompressionStream('gzip'));
  const rd=src.getReader();let out=0;
  while(true){
    const {done,value}=await rd.read();
    if(done)break;
    if(out+value.byteLength>WASM_RAW)throw new Error('decompressed wasm exceeds expected size ('+WASM_RAW+')');
    raw.set(value,out);out+=value.byteLength;
    onProg('decompress',35+Math.round(Math.min(1,out/WASM_RAW)*60));
  }
  if(out!==WASM_RAW)throw new Error('decompressed wasm size ('+out+') mismatch, expected ('+WASM_RAW+')');
  return raw;
}
let db=null,conn=null;
/* query serialization to the worker: concurrent DQ from several instances
   breaks the duckdb-wasm protocol (unassociated response, dangling promises) —
   all queries go through a single pipeline */
let dbq=Promise.resolve();
function DQ(sql){
  const p=dbq.then(()=>{const t0=performance.now();
    const to=setTimeout(()=>console.warn('duckdive: slow query '+(performance.now()-t0).toFixed(0)+'ms',sql.slice(0,120)),10000);
    return conn.query(sql).then(r=>{clearTimeout(to);return r},e=>{clearTimeout(to);throw e});
  });
  dbq=p.catch(()=>{});
  return p;
}
async function initDB(){
  const {AsyncDuckDB,ConsoleLogger}=await import(blobUrl(DUCKDB_SRC));
  const wasmBytes=await decodeWasm();
  const _w=new Worker(blobUrl(WORKER_SRC));
  _w.addEventListener('message',_onDbMessage);
  _w.addEventListener('error',_onWorkerError);
  _w.postMessage({__ddWasmBytes:wasmBytes},[wasmBytes.buffer]);
  db=new AsyncDuckDB(new ConsoleLogger(),_w);
  bootStage='compile';
  await db.instantiate('blob:duckdb-mvp.wasm',null,()=>{
    lastProg=Date.now();
  });
  onProg('register',STAGE_PCT.register);
  conn=await db.connect();
  await db.registerFileBuffer('data.parquet',b64ToBytes(B.parquet));
  /* __rn is already baked into the parquet at build time (core.py) — no window
     function needed here: otherwise every query against t would materialize
     the whole table again */
  await conn.query("CREATE OR REPLACE VIEW t AS SELECT * FROM 'data.parquet'");
  /* multi-table input (DuckDive(data={t1: df1, t2: df2})): extra tables are
     registered as their own parquet buffers so SQL views can JOIN across them */
  if(B.tables)for(const[tname,tb64]of Object.entries(B.tables)){
    const fn=String(tname).replace(/[^\w]/g,'_')+'.parquet';
    await db.registerFileBuffer(fn,b64ToBytes(tb64));
    await conn.query('CREATE OR REPLACE VIEW "'+String(tname).replace(/"/g,'""')+'" AS SELECT * FROM \''+fn+'\'');
  }
}

/* ── duckdive_table instance: its own view, its own UI filters;
   cross-filtering via the shared window.__ddFilters ── */
async function initInstance(cfg){
const root=document.getElementById(cfg.root);
const P=cfg.pfx??'';
const $=id=>document.getElementById(P+id);
const V=cfg.view;
const escH=s=>(s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const escA=s=>escH(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const fmtN=n=>n==null||n===''?'—':Number(n).toLocaleString('ru-RU');
const COLS=(cfg.columns||[]).filter(c=>c.name!=='__rn');
const PSIZES=[5,10,25,50,100];
const ICONS={cols:"__ICON_COLUMNS__",dl:"__ICON_DOWNLOAD__",eye:"__ICON_EYE__",eyeoff:"__ICON_EYEOFF__",table:"__ICON_TABLE__",grid:"__ICON_GRID__",braces:"__ICON_BRACES__",chd:"__ICON_CHD__",chl:"__ICON_CHL__",chr:"__ICON_CHR__",chdl:"__ICON_CHDL__",chdr:"__ICON_CHDR__",search:"__ICON_SEARCH__",x:"__ICON_X__",copy:"__ICON_COPY__",check:"__ICON_CHECK__",filter:"__ICON_FILTER__",updown:"__ICON_UPDOWN__",upwide:"__ICON_UPWIDE__",downwide:"__ICON_DOWNWIDE__"};
const sharedF=window.__ddFilters;
const gfc=!!cfg.name&&cfg.cross!==false;
const state={filters:gfc?{}:(cfg.cross?sharedF:{}),page:1,pageSize:cfg.page_size||B.page_size||20,sortCol:null,sortDir:'asc',colvis:new Set(COLS.map(c=>c.name)),search:'',sel:new Set()};
const CPOPUP={};(cfg.cell_popup||[]).forEach(cp=>{if(cp&&cp.col)CPOPUP[cp.col]=cp});
function xfp(){document.dispatchEvent(new CustomEvent('dd-filters-changed',{detail:{src:cfg.root}}))}
let rafP=false;
function schedRefresh(){if(rafP)return;rafP=true;setTimeout(()=>{rafP=false;refresh()},0)}
/* column atom: own filter -> shared pool (+ready predicate and author) */
function publishCol(col,f){
  if(cfg.cross===false)return;
  /* duckdive_toggles of this column merge into the atom and survive
     removal of the table filter */
  const tc=window.__ddToggleCond?window.__ddToggleCond(col):'';
  if(gfc){
    if(f){sharedF[col]=f;window.__ddFiltersSrc[col]=cfg.name}
    else if(window.__ddFiltersSrc[col]===cfg.name){
      if(tc){window.__ddFilters[col]={op:'in',values:[],blank:false,cmp:null,min:'',max:''};window.__ddFiltersSrc[col]=cfg.name}
      else{delete sharedF[col];delete window.__ddFiltersSrc[col]}
    }
  }
  if(f||tc)window.__ddFiltersCond[col]=[(f?colCond(col,f):''),tc].filter(Boolean).join(' AND ');
  else if(!gfc)delete window.__ddFiltersCond[col];
  gfcChanged();
}
/* row atom: selection/search -> temp table of keys (300ms debounce) */
const rowPub={deb:null,hash:''};
function rowCond(){
  const sel=[...state.sel];
  let c='';
  if(sel.length)c='__rn IN ('+sel.join(',')+')';
  if(state.search){
    const s=likeEsc(state.search).toLowerCase();
    const e='('+COLS.map(cc=>qid(cc.name)+" ILIKE '%"+s+"%'").join(' OR ')+')';
    c=c?'('+c+' OR '+e+')':e;
  }
  return c;
}
function scheduleRowPub(){
  if(!gfc)return;
  clearTimeout(rowPub.deb);
  rowPub.deb=setTimeout(publishRows,300);
}
async function publishRows(){
  if(!gfc)return;
  const wants=rowWantsFor(cfg.name,true);
  const hash=JSON.stringify([wants,[...state.sel].sort((a,b)=>a-b),state.search]);
  if(hash===rowPub.hash)return;
  rowPub.hash=hash;
  const table='__ddrow_'+String(cfg.name).replace(/[^\w]/g,'_');
  const cond=rowCond();
  if(!wants||!cond){
    if(GFC.rows[cfg.name]){try{await DQ('DROP TABLE IF EXISTS '+table)}catch(e){}delete GFC.rows[cfg.name];gfcChanged()}
    return;
  }
  try{
    const keys=wants.all?COLS.map(c=>c.name):wants.keys.filter(k=>hasCol(k));
    if(!keys.length)return;
    await DQ(`CREATE OR REPLACE TABLE ${table} AS SELECT DISTINCT ${keys.map(qid).join(',')} FROM ${V} WHERE ${cond}`);
    GFC.rows[cfg.name]={src:cfg.name,cols:keys,table:table};
    gfcChanged();
  }catch(e){console.warn('duckdive: row publish failed:',e)}
}
/* selection remap on view rebuild (__rn of aggregates is synthetic) */
const myGfcView=()=>GFC.views.find(v=>v.view===V);
async function captureSel(){
  if(!state.sel.size)return null;
  const mv=myGfcView();
  if(mv?mv.stableRn:V==='t')return {stable:true,rns:[...state.sel]};
  const wants=rowWantsFor(cfg.name);
  const keys=wants?(wants.all?COLS.map(c=>c.name):wants.keys.filter(k=>hasCol(k))):[];
  if(!keys.length)return null;
  return {keys:keys,vals:await q(`SELECT DISTINCT ${keys.map(k=>castExpr(k,true)).join(',')} FROM ${V} WHERE __rn IN (${[...state.sel].join(',')})`)};
}
async function restoreSel(cap){
  if(!cap){state.sel.clear();return}
  if(cap.stable){state.sel=new Set(cap.rns);return}
  if(!cap.vals.length){state.sel.clear();return}
  const cond=cap.vals.map(r=>'('+cap.keys.map(k=>{
    const v=r[k];
    if(v==null||v==='')return qid(k)+' IS NULL';
    return qid(k)+' = '+(isNum(k)?Number(v):qv(String(v)));
  }).join(' AND ')+')').join(' OR ');
  const rows=await q(`SELECT __rn FROM ${V} WHERE ${cond}`);
  state.sel=new Set(rows.map(r=>r.__rn));
}
if(gfc){
  window.__ddGFCRegister(cfg.name,cfg.use_filter_keys||null);
  GFC.remap.push({capture:captureSel,restore:restoreSel});
  /* the search index is not rebuilt right away (expensive on large data) —
     mark it stale; rebuild lazily in refresh() while search is active */
  GFC.resrc.push(()=>{srcStale=true});
}
const hasCol=c=>COLS.some(cc=>cc.name===c);
let pop=null,popOutside=null,tip=null,tipTimer=null,pinCell=null;
let total=0,pageRows=[],srcReady=false,srcStale=false;
const SRC='__src'+(cfg.idx||'');

const typeOf=n=>{const c=COLS.find(c=>c.name===n)||{};return (c.type||'').toUpperCase()};
const typeTag=n=>{const t=typeOf(n);return /INT|HUGEINT/.test(t)?'int':/DOUBLE|FLOAT|DEC|REAL/.test(t)?'float':/DATE/.test(t)?'date':/TIME/.test(t)?'time':'str'};
const isNum=n=>/INT|DOUBLE|FLOAT|DEC|REAL|HUGEINT/.test(typeOf(n));
const isDate=n=>/DATE|TIME/.test(typeOf(n));
const qid=n=>'"'+n.replace(/"/g,'""')+'"';
const qv=v=>"'"+String(v).replace(/'/g,"''")+"'";
const likeEsc=s=>String(s).replace(/'/g,"''").replace(/[%_\\]/g,m=>'\\'+m);
const castExpr=(n,al)=>isDate(n)?`CAST(${qid(n)} AS VARCHAR)${al?' AS '+qid(n):''}`:qid(n);

async function q(sql){
  if(srcReady&&!srcStale&&state.search)sql=sql.replace(new RegExp('\\bFROM '+V+'\\b','gi'),'FROM '+SRC).replace(new RegExp('ORDER BY '+V+'\\.','gi'),'ORDER BY ');
  const arr=await DQ(sql);
  const conv=v=>{
    if(typeof v==='bigint')return Number(v);
    if(v instanceof Date)return v.toISOString().slice(0,10);
    if(v&&typeof v==='object'&&!(v instanceof Date)){
      const n=Number(v.toString());
      if(Number.isFinite(n))return n;
    }
    return v;
  };
  return arr.toArray().map(r=>{const o={};for(const f of arr.schema.fields)o[f.name]=conv(r[f.name]);return o});
}
async function buildSearchIndex(){
  try{
    const expr=COLS.map(c=>qid(c.name)).join(',')||"''";
    await DQ(`CREATE OR REPLACE TABLE ${SRC} AS SELECT *, lower(concat_ws(chr(31),`+expr+`)) AS __search FROM ${V}`);
    srcReady=true;
  }catch(e){console.warn('duckdive: search index skipped:',e)}
}

/* ── WHERE ── */
function colCond(c,f){
  const qt=qid(c);
  const parts=[];
  const vals=f.values||[];
  const textCol=!isNum(c)&&!isDate(c);
  const num=isNum(c);
  if(vals.length||f.blank){
    const sub=[];
    /* numeric columns get numeric literals: the mvp duckdb build does not cast
       string literals in IN ("col" IN ('1') returns 0 rows) */
    const lit=v=>num?String(Number(v)):qv(v);
    if(vals.length)sub.push(qt+' IN ('+vals.map(lit).join(',')+')');
    if(f.blank)sub.push(textCol?`(${qt} IS NULL OR ${qt} = '')`:`${qt} IS NULL`);
    const e='('+sub.join(' OR ')+')';
    /* notin: NULL rows are not "excluded values" — keep them unless the user
       explicitly excluded (Blanks) */
    parts.push(f.op==='notin'?(!f.blank?`(${qt} IS NULL OR NOT ${e})`:'NOT '+e):e);
  }
  const hasRange=f.cmp&&((f.min??'')!==''||(f.cmp==='between'&&(f.max??'')!==''));
  if(hasRange){
    const num=isNum(c);
    const lit=x=>num?Number(x):qv(x);
    const rng=[];
    if(f.cmp==='between'){
      if((f.min??'')!=='')rng.push(qt+' >= '+lit(f.min));
      if((f.max??'')!=='')rng.push(qt+' <= '+lit(f.max));
    }else{
      const ops={eq:'=',gt:'>',gte:'>=',lt:'<',lte:'<='};
      rng.push(qt+' '+ops[f.cmp]+' '+lit(f.min));
    }
    const rg=rng.join(' AND ');
    /* Exclude also applies to the numeric condition ("exclude = 1" means NOT (= 1)) */
    parts.push(f.op==='notin'?`(${qt} IS NULL OR NOT (${rg}))`:rg);
  }
  return parts.join(' AND ');
}
function where(excl){
  const parts=[];
  for(const[c,f]of Object.entries(state.filters)){
    if(c===excl||!hasCol(c))continue;
    const cond=colCond(c,f);
    if(cond)parts.push('('+cond+')');
  }
  if(state.search){
    const qv2=state.search.toLowerCase().replace(/'/g,"''").replace(/[%_\\]/g,m=>'\\'+m);
    parts.push(srcReady&&!srcStale?"(\"__search\" LIKE '%"+qv2+"%')":"("+COLS.map(c=>qid(c.name)+" ILIKE '%"+qv2+"%'").join(' OR ')+")");
  }
  return parts.join(' AND ');
}
function orderSql(){
  if(!state.sortCol)return '';
  return ' ORDER BY '+qid(state.sortCol)+' '+(state.sortDir==='desc'?'DESC':'ASC')+', __rn ASC';
}
/* coalescing: if a refresh is already running (DQ is serialized, each ~0.3-2s),
   new calls do not queue up but mark dirty — one fresh run happens after the
   current one finishes. Otherwise fast typing in search would pile up dozens
   of pending redraws and "throw away" the scroll/page position */
let inRefresh=false,refreshDirty=false;
function fullWhere(){
  /* own UI filters + foreign GFC atoms (column + row). Own atoms are skipped
     inside gfcWhereFor — feedback loops are impossible by construction. */
  const foreign=gfc?gfcWhereFor(cfg.name,cfg.use_filter_keys,'',cfg.filter_columns||null,COLS.map(c=>c.name)):'';
  return [where(),foreign].filter(Boolean).join(' AND ');
}
async function refresh(){
  if(inRefresh){refreshDirty=true;return}
  inRefresh=true;
  try{
    await (async()=>{
  const w=fullWhere()||'TRUE';
  total=(await q('SELECT COUNT(*) n FROM '+V+' WHERE '+w))[0].n;
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  if(state.page>pages)state.page=pages;
  const vis=visibleCols();
  const selList=vis.length?vis.map(c=>castExpr(c.name,true)).join(','):'__rn';
  pageRows=vis.length?await q('SELECT '+selList+', __rn FROM '+V+' WHERE '+w+orderSql()+' LIMIT '+state.pageSize+' OFFSET '+(state.page-1)*state.pageSize):[];
  render();
  if(cfg.idx===0&&!window.__ddTableReady){window.__ddTableReady=true;document.dispatchEvent(new CustomEvent('dd-table-ready'))}
  window.__ddTableRendered=true;
  /* in gfc mode labels recompute on dd-gfc-changed; here — legacy mode only
     (labels follow the table's filters/search) */
  if(!gfc&&window.__ddLabelsRecompute)window.__ddLabelsRecompute();
    })();
  }finally{
    inRefresh=false;
    if(refreshDirty){refreshDirty=false;refresh()}
  }
}

/* ── popups/tooltips (as in json mode) ── */
function hideTip(){pinCell=null;if(tip){tip.remove();tip=null}if(tipTimer){clearTimeout(tipTimer);tipTimer=null}}
function fallbackCopy(txt,done){
  const ta=document.createElement('textarea');
  ta.value=txt;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy')}catch(e){}
  ta.remove();done();
}
function showTip(td,pin){
  hideTip();
  const txt=td.textContent;
  if(!txt)return;
  if(!pin&&td.scrollWidth<=td.clientWidth+1&&td.scrollHeight<=td.clientHeight+1)return;
  tipTimer=setTimeout(()=>{
    tip=document.createElement('div');
    tip.className='dd-tip';
    if(pin){
      tip.innerHTML='<div class="tiptxt"></div><div class="tipacts"><button class="cp" title="Copy">'+ICONS.copy+'</button><button class="cl" title="Close">'+ICONS.x+'</button></div>';
      tip.querySelector('.tiptxt').textContent=txt;
      tip.querySelector('.cp').onclick=e=>{
        e.stopPropagation();
        const btn=e.currentTarget;
        const done=()=>{btn.classList.add('ok');btn.innerHTML=ICONS.check;setTimeout(()=>{btn.classList.remove('ok');btn.innerHTML=ICONS.copy},1200)};
        if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(txt).then(done,()=>fallbackCopy(txt,done));
        else fallbackCopy(txt,done);
      };
      tip.querySelector('.cl').onclick=e=>{e.stopPropagation();hideTip()};
    }else tip.textContent=txt;
    document.body.appendChild(tip);
    const r=td.getBoundingClientRect(),tr=tip.getBoundingClientRect();
    let left=Math.max(8,Math.min(r.left,window.innerWidth-tr.width-8));
    let top=r.bottom+4;
    if(top+tr.height>window.innerHeight-8)top=Math.max(8,r.top-tr.height-4);
    tip.style.left=left+'px';tip.style.top=top+'px';
    if(pin)pinCell=td;
    tip.addEventListener('mouseleave',()=>{if(!pinCell)hideTip()});
  },150);
}
function closePop(){
  hideTip();
  if(pop){pop.remove();pop=null}
  if(popOutside){document.removeEventListener('click',popOutside);popOutside=null}
  root.querySelectorAll('.dd-iconbtn.open,.pgbtn.open').forEach(b=>b.classList.remove('open'));
}
function anchorMenu(menu,btn,estW){
  const r=btn.getBoundingClientRect();
  let left=Math.max(8,Math.min(r.right-estW,window.innerWidth-estW-8));
  let top=r.bottom+4;
  const h=menu.offsetHeight||280;
  if(top+h>window.innerHeight-8)top=Math.max(8,r.top-h-4);
  menu.style.left=left+'px';menu.style.top=top+'px';
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}

/* ── export/columns/page/size menus ── */
function baseName(){return (B.title||'duckdive').replace(/[^\wа-яА-ЯёЁ]+/g,'_').slice(0,40)}
function downloadBlob(text,mime,ext){
  const blob=new Blob([text],{type:mime});
  const url=URL.createObjectURL(blob);
  const a=document.createElement('a');
  a.href=url;a.download=baseName()+ext;document.body.appendChild(a);a.click();
  a.remove();setTimeout(()=>URL.revokeObjectURL(url),5000);
}
function toDelim(rows,cs,sep){
  return rows.map(r=>cs.map(c=>{
    const v=r[c.name];
    const s=v==null?'':String(v);
    return /["\n\r;,]|^\s|\s$/.test(s)?'"'+s.replace(/"/g,'""')+'"':s;
  }).join(sep)).join('\r\n');
}
async function exportData(fmt){
  const cs=visibleCols();
  let rows;
  if(state.sel.size){
    rows=await q(`SELECT ${cs.map(c=>castExpr(c.name,true)).join(',')} FROM ${V} WHERE __rn IN (`+[...state.sel].join(',')+`)`+orderSql().replace('ORDER BY','ORDER BY '+V+'.'));
  }else{
    const w=fullWhere()||'TRUE';
    rows=await q(`SELECT ${cs.map(c=>castExpr(c.name,true)).join(',')} FROM ${V} WHERE ${w}`+orderSql().replace('ORDER BY','ORDER BY '+V+'.'));
  }
  if(fmt==='xlsx'){
    const data=rows.map(r=>{const o={};cs.forEach(c=>{let v=r[c.name];
      if(typeof v==='string'&&v.length>32767)v=v.slice(0,32766)+'…';o[c.name]=v});return o});
    const wb=XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb,XLSX.utils.json_to_sheet(data),'Data');
    XLSX.writeFile(wb,baseName()+'.xlsx');
    return;
  }
  const hdr=Object.fromEntries(cs.map(c=>[c.name,c.name]));
  if(fmt==='csv')downloadBlob('\ufeff'+toDelim([hdr],cs,',')+'\r\n'+toDelim(rows,cs,','),'text/csv;charset=utf-8','.csv');
  else if(fmt==='tsv')downloadBlob(toDelim([hdr],cs,'\t')+'\r\n'+toDelim(rows,cs,'\t'),'text/tab-separated-values;charset=utf-8','.tsv');
  else if(fmt==='json')downloadBlob(JSON.stringify(rows,null,2),'application/json','.json');
}
function openExportMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  btn.classList.add('open');
  pop=document.createElement('div');
  pop.className='dd-menu';
  pop.innerHTML=`<div class="dd-menulabel">Download data</div>
    <div class="dd-menuitem" data-fmt="csv">${ICONS.table}<span class="lbl">CSV</span><span class="desc">Comma-separated values</span></div>
    <div class="dd-menuitem" data-fmt="tsv">${ICONS.grid}<span class="lbl">TSV</span><span class="desc">Best for Excel and Google Sheets</span></div>
    <div class="dd-menuitem" data-fmt="xlsx">${ICONS.dl}<span class="lbl">Excel</span><span class="desc">Self-contained .xlsx</span></div>
    <div class="dd-menuitem" data-fmt="json">${ICONS.braces}<span class="lbl">JSON</span><span class="desc">Raw JSON data</span></div>`;
  document.body.appendChild(pop);
  anchorMenu(pop,btn,260);
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    closePop();exportData(el.dataset.fmt);
  });
}
function openColvis(ev){
  closePop();
  const btn=ev.currentTarget;
  btn.classList.add('open');
  pop=document.createElement('div');
  pop.className='dd-pop';
  pop.innerHTML=`<div class="srch"><input id="${P}dd-cv-q" placeholder="Search columns..."></div>
    <div class="vals dd-pop2" id="${P}dd-cv-list"></div>`;
  document.body.appendChild(pop);
  const r=btn.getBoundingClientRect();
  let left=Math.max(8,Math.min(r.right-260,window.innerWidth-268));
  let top=r.bottom+4;
  if(top+320>window.innerHeight-8)top=Math.max(8,window.innerHeight-330);
  pop.style.left=left+'px';pop.style.top=top+'px';
  const list=$('dd-cv-list');
  function renderList(qs){
    const cols=COLS.filter(c=>!qs||c.name.toLowerCase().includes(qs));
    const anyVisible=visibleCols().length>0;
    list.innerHTML=`<div class="cvrow act" id="${P}dd-cv-all" ${COLS.length===state.colvis.size?'style="opacity:.45;pointer-events:none"':''}>${ICONS.eye}<span>Show all</span></div>
      <div class="cvrow act" id="${P}dd-cv-none" ${!anyVisible?'style="opacity:.45;pointer-events:none"':''}>${ICONS.eyeoff}<span>Hide all</span></div>
      <div class="sep"></div>`
      +(cols.map(c=>{
        const vis=state.colvis.has(c.name);
        return `<div class="cvrow" data-c="${escA(c.name)}"><input type="checkbox" ${vis?'checked':''}><span>${escH(c.name)}</span><span class="typ">${typeTag(c.name)}</span></div>`;
      }).join('')||'<div class="load">No results.</div>');
    const toggle=c=>{
      if(state.colvis.has(c))state.colvis.delete(c);else state.colvis.add(c);
      renderList($('dd-cv-q').value.toLowerCase());
      refresh();
    };
    list.querySelectorAll('.cvrow[data-c]').forEach(el=>{
      const cb=el.querySelector('input');
      cb.onchange=()=>toggle(el.dataset.c);
      el.onclick=e=>{if(e.target!==cb){e.preventDefault();cb.checked=!cb.checked;toggle(el.dataset.c)}};
    });
    list.querySelector('#'+P+'dd-cv-all').onclick=()=>{
      COLS.forEach(c=>state.colvis.add(c.name));
      renderList($('dd-cv-q').value.toLowerCase());refresh();
    };
    list.querySelector('#'+P+'dd-cv-none').onclick=()=>{
      state.colvis.clear();
      renderList($('dd-cv-q').value.toLowerCase());refresh();
    };
  }
  renderList('');
  const q=$('dd-cv-q');
  q.oninput=()=>renderList(q.value.toLowerCase());
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}
function openPageMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  pop=document.createElement('div');
  pop.className='dd-menu';
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  /* jump input + small window: no DOM node per page on million-row tables */
  const near=new Set([1,2,3,pages-2,pages-1,pages,state.page-2,state.page-1,state.page,state.page+1,state.page+2]);
  const list=[...near].filter(p=>p>=1&&p<=pages).sort((a,b)=>a-b);
  let prev=0,items='';
  for(const pn of list){
    if(pn-prev>1)items+='<div class="dd-menulabel" style="text-align:center;padding-top:2px">…</div>';
    items+=`<div class="dd-menuitem" data-p="${pn}"><span class="lbl"${pn===state.page?' style="font-weight:700;color:var(--dd-accent-text)"':''}>${fmtN(pn)}</span></div>`;
    prev=pn;
  }
  pop.innerHTML=`<div class="dd-menulabel">Page</div>
    <div class="dd-jump"><input type="number" min="1" max="${pages}" value="${state.page}"><button>Go</button></div>`+items;
  document.body.appendChild(pop);
  anchorMenu(pop,btn,180);
  const inp=pop.querySelector('input');
  const go=()=>{const pn=Math.max(1,Math.min(pages,Number(inp.value)||1));state.page=pn;closePop();refresh()};
  pop.querySelector('.dd-jump button').onclick=go;
  inp.onkeydown=e=>{if(e.key==='Enter')go()};
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    state.page=Number(el.dataset.p);closePop();refresh();
  });
  inp.focus();inp.select();
}
function openSizeMenu(ev){
  closePop();
  const btn=ev.currentTarget;
  pop=document.createElement('div');
  pop.className='dd-menu';
  const sizes=[...new Set([...PSIZES,state.pageSize])].sort((a,b)=>a-b);
  pop.innerHTML=`<div class="dd-menulabel">Rows per page</div>`
    +sizes.map(sz=>`<div class="dd-menuitem" data-sz="${sz}"><span class="lbl"${sz===state.pageSize?' style="font-weight:600;background:#fff4e6"':''}>${sz}</span></div>`).join('');
  document.body.appendChild(pop);
  anchorMenu(pop,btn,120);
  pop.querySelectorAll('.dd-menuitem').forEach(el=>el.onclick=()=>{
    state.pageSize=Number(el.dataset.sz);state.page=1;closePop();refresh();
  });
}

/* ── filter popup: values from SQL ── */
function openPopup(col,ev){
  closePop();
  pop=document.createElement('div');
  pop.className='dd-pop';
  pop.dataset.col=col;
  let kind;
  if(isNum(col))kind='num';
  else if(isDate(col))kind='date';
  else kind='values';
  pop.dataset.kind=kind;
  const f=state.filters[col]||{};
  const cmp0=f.cmp&&f.cmp!=='none'?f.cmp:(kind==='num'?'between':(kind==='date'?'between':null));
  pop._d={vals:new Set(f.values||[]),blank:!!f.blank,mode:f.op==='notin'?'notin':'in',cmp:cmp0,min:f.min??'',max:f.max??'',value:''};
  let inner='';
  if(kind==='num'){
    inner=`<div class="num">
      <select name="op">
        <option value="none"${cmp0?'':' selected'}>&mdash;</option>
        <option value="eq"${cmp0==='eq'?' selected':''}>=</option>
        <option value="gt"${cmp0==='gt'?' selected':''}>&gt;</option>
        <option value="gte"${cmp0==='gte'?' selected':''}>&ge;</option>
        <option value="lt"${cmp0==='lt'?' selected':''}>&lt;</option>
        <option value="lte"${cmp0==='lte'?' selected':''}>&le;</option>
        <option value="between"${cmp0==='between'?' selected':''}>&ndash;</option>
      </select>
      <input type="number" data-k="min" value="${escA(f.min??'')}" placeholder="Value">
      <input type="number" data-k="max" value="${escA(f.max??'')}" placeholder="Max" style="display:${cmp0==='between'?'':'none'}">
    </div>`;
  }else if(kind==='date'){
    inner=`<div class="date"><label>From <input type="date" data-k="min" value="${escA(f.min??'')}"></label>
      <label>To <input type="date" data-k="max" value="${escA(f.max??'')}"></label></div>`;
  }
  inner+=`<div class="mode"><label><input type="radio" name="m" value="in" ${f.op!=='notin'?'checked':''}> Include</label>
      <label><input type="radio" name="m" value="notin" ${f.op==='notin'?'checked':''}> Exclude</label></div>
      <div class="srch"><input class="qs" placeholder="Search values..."></div>
      <div class="vals"><div class="load" style="padding:20px;text-align:center;color:#90A4AE">Loading...</div></div>`;
  pop.innerHTML=`<div class="srch" style="font-weight:600;border-bottom:1px solid #f4f4f5;padding:8px 10px">${escH(col)}</div>`+inner
    +`<div class="acts"><button class="b act-clear">Clear</button><button class="b act-close">Close</button>
      <button class="bp act-apply">Apply</button></div>`;
  document.body.appendChild(pop);
  const r=ev.target.getBoundingClientRect();
  let left=r.left-100,top=r.bottom+6;
  left=Math.max(8,Math.min(left,window.innerWidth-(pop.offsetWidth||300)-8));
  if(top+380>window.innerHeight-8)top=Math.max(8,window.innerHeight-390);
  pop.style.left=left+'px';pop.style.top=top+'px';
  renderVals(col,f);
  pop.querySelectorAll('input[data-k],select[data-k]').forEach(inp=>{
    pop._d[inp.dataset.k]=inp.value;
    inp.oninput=()=>{pop._d[inp.dataset.k]=inp.value};
  });
  const opSel=pop.querySelector('select[name=op]');
  if(opSel){
    const mx=pop.querySelector('input[data-k=max]');
    opSel.onchange=()=>{pop._d.cmp=opSel.value==='none'?null:opSel.value;if(mx)mx.style.display=opSel.value==='between'?'':'none'};
  }
  pop.querySelectorAll('input[name=m]').forEach(rb=>rb.onchange=()=>{pop._d.mode=rb.value});
  pop.querySelector('.act-apply').onclick=()=>applyPop();
  pop.querySelector('.act-close').onclick=closePop;
  pop.querySelector('.act-clear').onclick=()=>{delete state.filters[col];state.page=1;closePop();publishCol(col,null);refresh();xfp()};
  setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
  function outside(e){if(pop&&!pop.contains(e.target)&&e.target.isConnected){closePop();document.removeEventListener('click',outside)}}
}
function renderVals(col,f){
  const wrap=pop.querySelector('.vals');
  (async()=>{
    const textCol=!isNum(col)&&!isDate(col);
    const nnb=textCol?`${qid(col)} IS NOT NULL AND ${qid(col)} != ''`:`${qid(col)} IS NOT NULL`;
    const opts=await q(`SELECT ${castExpr(col,false)} AS v, COUNT(*) AS c FROM ${V} WHERE ${[where(col),gfc?gfcWhereFor(cfg.name,cfg.use_filter_keys,'',cfg.filter_columns||null,COLS.map(c=>c.name)):''].filter(Boolean).join(' AND ')||'TRUE'} AND ${nnb} GROUP BY 1 ORDER BY c DESC LIMIT 300`);
    const bl=await q(`SELECT COUNT(*) c FROM ${V} WHERE ${[where(col),gfc?gfcWhereFor(cfg.name,cfg.use_filter_keys,'',cfg.filter_columns||null,COLS.map(c=>c.name)):''].filter(Boolean).join(' AND ')||'TRUE'} AND ${textCol?`(${qid(col)} IS NULL OR ${qid(col)} = '')`:`${qid(col)} IS NULL`}`);
    const blanks=bl.length?bl[0].c:0;
    const blankRow=blanks?`<div class="val" data-v="__dd_blank__"><input type="checkbox" ${f.blank?'checked':''}>
       <span>(Blanks)</span><span class="cnt">${fmtN(blanks)}</span></div>`:'';
    wrap.innerHTML=(opts.map(o=>
      `<div class="val" data-v="${escA(o.v)}"><input type="checkbox" ${f.op&&f.values&&f.values.includes(o.v)?'checked':''}>
       <span>${escH(o.v)}</span><span class="cnt">${fmtN(o.c)}</span></div>`).join('')
      ||'<div class="load">No values</div>')+blankRow;
    wrap.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
      const v=cb.closest('.val').dataset.v,d=pop._d;
      if(v==='__dd_blank__'){d.blank=cb.checked;return}
      if(cb.checked)d.vals.add(v);else d.vals.delete(v);
    });
    const qs=pop.querySelector('.qs');
    if(qs)qs.oninput=()=>{const s=qs.value.toLowerCase();
      pop.querySelectorAll('.val').forEach(el=>{el.style.display=el.dataset.v.toLowerCase().includes(s)?'':'none'})};
  })();
}
function applyPop(){
  const col=pop.dataset.col,d=pop._d,kind=pop.dataset.kind;
  const cmp=kind==='date'?'between':d.cmp;
  const hasVals=d.vals.size||d.blank;
  const hasRange=cmp&&((d.min??'')!==''||(cmp==='between'&&(d.max??'')!==''));
  let nf=null;
  if(hasVals||hasRange)nf={op:d.mode,values:[...d.vals],blank:!!d.blank,cmp:hasRange?cmp:null,min:d.min??'',max:d.max??''};
  if(nf)state.filters[col]=nf;else delete state.filters[col];
  publishCol(col,nf);
  state.page=1;closePop();refresh();xfp();
}

/* ── render ── */
function badgeLabel(c,f){
  if(f.op==='in'||f.op==='notin'){
    let s=(f.op==='notin'?'excl. ':'')+c+': ';
    const p=[];
    if(f.values&&f.values.length)p.push(f.values.join(', '));
    if(f.blank)p.push('(Blanks)');
    s+=p.join(', ');
    if(f.cmp&&f.cmp!=='between')s+=' '+({eq:'=',gt:'>',gte:'\u2265',lt:'<',lte:'\u2264'}[f.cmp]||'')+' '+f.min;
    else if(f.cmp==='between')s+=' '+[f.min,f.max].filter(x=>x!==''&&x!=null).join(' \u2013 ');
    return s;
  }
  if(f.op==='contains')return c+" contains '"+f.value+"'";
  return c+' '+[f.min,f.max].filter(x=>x!==''&&x!=null).join(' – ');
}
function renderBadges(){
  const el=root.querySelector('.dd-badges');
  if(!el)return;
  const entries=Object.entries(state.filters).filter(([c,f])=>
    hasCol(c)&&!(f.op==='in'&&!(f.values||[]).length&&!f.blank&&!f.cmp));
  const isMarker=f=>f&&f.op==='in'&&!(f.values||[]).length&&!f.blank&&!f.cmp;
  const incoming=cfg.name?Object.keys(window.__ddFilters).filter(c=>{
    const src=window.__ddFiltersSrc[c];
    return src&&src!==cfg.name&&window.__ddFiltersCond[c]&&!isMarker(window.__ddFilters[c]);
  }):[];
  el.innerHTML=entries.map(([c,f])=>
    `<span class="dd-badge"><span>${escH(badgeLabel(c,f))}</span><span class="x" data-c="${escA(c)}">&times;</span></span>`).join('')
    + incoming.map(c=>`<span class="dd-badge dd-badge-in" title="Cross-filter from component &quot;${escA(window.__ddFiltersSrc[c])}&quot;">&larr;${escH(window.__ddFiltersSrc[c])}: ${escH(c)}</span>`).join('')
    + ((entries.length||incoming.length)?`<span class="dd-clear" id="${P}dd-clear">&times; Clear all</span>`:"");
  el.querySelectorAll('.x').forEach(x=>x.onclick=()=>{delete state.filters[x.dataset.c];state.page=1;publishCol(x.dataset.c,null);refresh();xfp()});
  const cl=$('dd-clear');
  if(cl)cl.onclick=()=>{const ks=Object.keys(state.filters);ks.forEach(k=>delete state.filters[k]);ks.forEach(k=>publishCol(k,null));state.page=1;refresh();xfp()};
}
function bottomLeft(nCols,hidden){
  const n=state.sel.size;
  if(n>0){
    const allSel=n===total;
    return `<span>${fmtN(n)} selected</span> `
      +(pageRows.length&&pageRows.every(r=>state.sel.has(r.__rn))&&!allSel
        ?`<button class="rowlink" id="${P}dd-selallrows">Select all ${fmtN(total)}</button> `
        :'')
      +`<button class="rowlink" id="${P}dd-selclear">Reset</button>`;
  }
  return `${fmtN(total)} rows · ${fmtN(nCols)} cols${hidden?` <span class="hid">+${hidden} hidden</span>`:''}`;
}
function visibleCols(){return COLS.filter(c=>state.colvis.has(c.name))}
function tableHtml(cs){
  const heads=`<th class="dd-selcol"><input type="checkbox" id="${P}dd-selall"></th>`+cs.map(c=>{
    const f=state.filters[c.name];
    const srt=state.sortCol===c.name
      ?(state.sortDir==='asc'?ICONS.upwide:ICONS.downwide)
      :ICONS.updown;
    const srtCls=state.sortCol===c.name?' srt-on':'';
    return `<th><span class="hwrap"><span class="cn" data-c="${escA(c.name)}">${escH(c.name)}<span class="srt${srtCls}">${srt}</span></span><span class="fb${f?' on':''}" data-c="${escA(c.name)}" title="Filter">${ICONS.filter}</span></span></th>`;
  }).join('');
  const body=pageRows.length?pageRows.map((r,pagei)=>{
    const tds='<td class="dd-selcol"><input type="checkbox" class="dd-selrow" data-i="'+r.__rn+'"'+(state.sel.has(r.__rn)?' checked':'')+'></td>'
      +cs.map(c=>{
        const v=r[c.name];
        if(v==null||v==='')return '<td></td>';
        return `<td${isNum(c.name)?' class="num"':''}>${escH(typeof v==='number'?fmtN(v):v)}</td>`;
      }).join('');
    return '<tr data-dd-ri="'+pagei+'"'+(state.sel.has(r.__rn)?' class="sel"':'')+'>'+tds+'</tr>';
  }).join(''):'<tr><td class="dd-empty" colspan="'+(cs.length+1)+'">No rows</td></tr>';
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  const hidden=COLS.length-cs.length;
  return `<div class="dd-badges"></div>
    <div class="dd-tscroll"><table><thead><tr>${heads}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function pagerHtml(cs){
  const pages=Math.max(1,Math.ceil(total/state.pageSize));
  const hidden=COLS.length-cs.length;
  return `<div class="dd-pager">
      <span class="rowcount">${bottomLeft(cs.length,hidden)}</span>
      <div class="pgctl">
        <button class="szbtn" id="${P}dd-sz">${state.pageSize} / page${ICONS.chd}</button>
        <button class="pgbtn" id="${P}dd-first" ${state.page<=1?'disabled':''} title="First page">${ICONS.chdl}</button>
        <button class="pgbtn" id="${P}dd-prev" ${state.page<=1?'disabled':''} title="Previous page">${ICONS.chl}</button>
        <span class="pginfo">Page <button class="pgbtn pgnum" id="${P}dd-page">${state.page}</button> of ${fmtN(pages)}</span>
        <button class="pgbtn" id="${P}dd-next" ${state.page>=pages?'disabled':''} title="Next page">${ICONS.chr}</button>
        <button class="pgbtn" id="${P}dd-last" ${state.page>=pages?'disabled':''} title="Last page">${ICONS.chdr}</button>
      </div>
      <div class="bbspace"></div>
    </div>`;
}
function topHtml(){
  return `<div class="dd-topbar">
      <div class="dd-srch">${ICONS.search}<input id="${P}dd-q" type="text" placeholder="Search..." value="" autocomplete="off"><button class="qx" id="${P}dd-qx" title="Clear">${ICONS.x}</button></div>
      <div class="spacer"></div>
      <button class="dd-iconbtn" id="${P}dd-cv">${ICONS.cols}Columns</button>
      <button class="dd-iconbtn" id="${P}dd-xlsx">${ICONS.dl}Export</button>
    </div>`;
}
function bindTable(){
  const selAll=$('dd-selall');
  if(selAll){
    const pageIdx=pageRows.map(r=>r.__rn);
    const onPg=pageIdx.filter(i=>state.sel.has(i)).length;
    selAll.checked=pageIdx.length>0&&onPg===pageIdx.length;
    selAll.indeterminate=onPg>0&&onPg<pageIdx.length;
    selAll.onchange=()=>{
      if(selAll.checked)pageIdx.forEach(i=>state.sel.add(i));else pageIdx.forEach(i=>state.sel.delete(i));
      render();scheduleRowPub();
    };
  }
  root.querySelectorAll('.dd-selrow').forEach(cb=>cb.onchange=()=>{
    const i=Number(cb.dataset.i);
    if(cb.checked)state.sel.add(i);else state.sel.delete(i);
    render();scheduleRowPub();
  });
  const sa=$('dd-selallrows');
  if(sa)sa.onclick=async()=>{
    const all=await q(`SELECT __rn FROM ${V} WHERE `+(fullWhere()||'TRUE'));
    all.forEach(r=>state.sel.add(r.__rn));
    render();scheduleRowPub();
  };
  const sc=$('dd-selclear');
  if(sc)sc.onclick=()=>{state.sel.clear();render();scheduleRowPub()};
  root.querySelectorAll('.cn').forEach(el=>el.onclick=()=>{
    const c=el.dataset.c;
    if(state.sortCol!==c){state.sortCol=c;state.sortDir='asc'}
    else if(state.sortDir==='asc')state.sortDir='desc';
    else state.sortCol=null;
    state.page=1;refresh();
  });
  root.querySelectorAll('.fb').forEach(el=>el.onclick=e=>{e.stopPropagation();openPopup(el.dataset.c,e)});
  $('dd-first').onclick=()=>{state.page=1;refresh()};
  $('dd-prev').onclick=()=>{if(state.page>1){state.page--;refresh()}};
  $('dd-next').onclick=()=>{if(state.page<Math.ceil(total/state.pageSize)){state.page++;refresh()}};
  $('dd-last').onclick=()=>{state.page=Math.ceil(total/state.pageSize);refresh()};
  $('dd-page').onclick=e=>{e.stopPropagation();openPageMenu(e)};
  $('dd-sz').onclick=e=>{e.stopPropagation();openSizeMenu(e)};
}
function bindTop(){
  const q=$('dd-q'),qx=$('dd-qx');
  let deb=null;
  q.oninput=()=>{qx.classList.toggle('has',!!q.value);clearTimeout(deb);deb=setTimeout(async()=>{
    if(srcStale){srcStale=false;await buildSearchIndex()}
    state.search=q.value;state.page=1;refresh();scheduleRowPub();
  },300)};
  q.onkeydown=e=>{if(e.key==='Escape'){q.value='';qx.classList.remove('has');state.search='';refresh();scheduleRowPub()}};
  qx.onclick=()=>{q.value='';qx.classList.remove('has');state.search='';refresh();scheduleRowPub()};
  $('dd-cv').onclick=e=>{e.stopPropagation();openColvis(e)};
  $('dd-xlsx').onclick=e=>{e.stopPropagation();openExportMenu(e)};
}
function render(){
  const cs=visibleCols();
  hideTip();
  /* redraw resets horizontal scroll — preserve and restore it */
  const sc0=root.querySelector('.dd-tscroll');
  const sl0=sc0?sc0.scrollLeft:0;
  if(!$('dd-tbl')){
    root.innerHTML=topHtml()+`<div id="${P}dd-tbl"></div><div id="${P}dd-pagerbox"></div>`;
    bindTop();
  }
  $('dd-tbl').innerHTML=tableHtml(cs);
  $('dd-pagerbox').innerHTML=pagerHtml(cs);
  renderBadges();
  bindTable();
  const sc1=root.querySelector('.dd-tscroll');
  if(sc1&&sl0)sc1.scrollLeft=sl0;
}

function colByCell(td){
  const table=td.closest('table');
  const th=table&&table.querySelectorAll('thead th')[td.cellIndex];
  const cn=th&&th.querySelector('.cn');
  return cn?(cn.dataset.c||cn.textContent.trim()):null;
}
function openCellPopup(td,cp){
  const tr=td.closest('tr');
  const ri=tr?Number(tr.dataset.ddRi):NaN;
  const row=(pageRows&&Number.isInteger(ri))?pageRows[ri]:null;
  const val=window.__ddGetValue?__ddGetValue(row,cp.col):(td.textContent||'');
  let html;
  const fn=cp.formatter?window[cp.formatter]:null;
  if(typeof fn==='function'){
    try{html=fn(row,cp.col,cp.opts||{})}
    catch(err){console.error('duckdive: cell_popup formatter failed:',err);html=null}
  }
  if(html==null)html='<pre class="dd-cpopup-raw">'+escH(val)+'</pre>';
  if(window.__ddPopup)__ddPopup.open({html:html,raw:String(val)});
}
root.addEventListener('mouseover',e=>{
  if(pinCell)return;
  const td=e.target.closest('.duckdive-root td');
  if(!td||td.classList.contains('dd-selcol')){if(!pinCell)hideTip();return}
  if(CPOPUP[colByCell(td)])return;
  showTip(td,false);
});
root.addEventListener('mouseleave',()=>{if(!pinCell)hideTip()});
root.addEventListener('click',e=>{
  const td=e.target.closest('.duckdive-root td');
  if(!td||td.classList.contains('dd-selcol'))return;
  e.stopPropagation();
  const cp=CPOPUP[colByCell(td)];
  if(cp){hideTip();openCellPopup(td,cp);return}
  if(pinCell===td){hideTip();return}
  showTip(td,true);
});
document.addEventListener('click',e=>{
  if(pinCell&&!pinCell.contains(e.target)&&!(tip&&tip.contains(e.target))&&!e.target.closest('.duckdive-root td'))hideTip();
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  if(pop)closePop();
  else if(pinCell)hideTip();
});
document.addEventListener('dd-filters-changed',e=>{if(cfg.name)return;if(!e.detail||e.detail.src!==cfg.root)schedRefresh()});
if(gfc)document.addEventListener('dd-gfc-changed',()=>{schedRefresh()});
if(cfg.idx===0){window.__ddq=q;window.__ddTableApi={q:s=>q(s),where:()=>fullWhere()||''}}
(window.__ddInstances=window.__ddInstances||[]).push({name:cfg.name,root:cfg.root,refresh:refresh,view:V,src:SRC,ready:()=>srcReady});
await refresh();
buildSearchIndex();
}

/* ── duckdive_filter: a standalone selector with the column filter UI ──
   Publishes a column atom into the shared pool under its own src; value
   counts take all other GFC atoms into account (its own excluded). */
async function initFilter(cfg){
  const root=document.getElementById(cfg.root);
  const col=cfg.column,myName=cfg.name||('flt_'+col);
  const meta=(B.columns||[]).find(c=>c.name===col);
  if(!meta){console.warn('duckdive_filter: column not found:',col);root.style.display='none';return}
  const tU=(meta.type||'').toUpperCase();
  const isN=/INT|HUGEINT|DOUBLE|FLOAT|DEC|REAL/.test(tU),isD=/DATE|TIME/.test(tU);
  const qv=v=>"'"+String(v).replace(/'/g,"''")+"'";
  const castE=isD?`CAST(${gfcQid(col)} AS VARCHAR)`:gfcQid(col);
  const escH=s=>(s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const escA=s=>escH(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  const fmtN=n=>n==null||n===''?'—':Number(n).toLocaleString('ru-RU');
  const IC={filter:"__ICON_FILTER__"};
  let f=null,pop=null,popOutside=null;
  function cond(){
    if(!f)return '';
    const qt=gfcQid(col),parts=[],vals=f.values||[];
    const textCol=!isN&&!isD;
    if(vals.length||f.blank){
      const sub=[];
      const lit=v=>isN?String(Number(v)):qv(v);
      if(vals.length)sub.push(qt+' IN ('+vals.map(lit).join(',')+')');
      if(f.blank)sub.push(textCol?`(${qt} IS NULL OR ${qt} = '')`:`${qt} IS NULL`);
      const e='('+sub.join(' OR ')+')';
      parts.push(f.op==='notin'?(!f.blank?`(${qt} IS NULL OR NOT ${e})`:'NOT '+e):e);
    }
    const hasRange=f.cmp&&((f.min??'')!==''||(f.cmp==='between'&&(f.max??'')!==''));
    if(hasRange){const lit=x=>isN?Number(x):qv(x);
      const rng=[];
      if(f.cmp==='between'){if((f.min??'')!=='')rng.push(qt+' >= '+lit(f.min));if((f.max??'')!=='')rng.push(qt+' <= '+lit(f.max))}
      else{const ops={eq:'=',gt:'>',gte:'>=',lt:'<',lte:'<='};rng.push(qt+' '+ops[f.cmp]+' '+lit(f.min))}
      const rg=rng.join(' AND ');
      parts.push(f.op==='notin'?`(${qt} IS NULL OR NOT (${rg}))`:rg);
    }
    return parts.join(' AND ');
  }
  function apply(nf){
    f=nf;
    /* duckdive_toggles of the same column merge into the shared atom:
       user cond AND toggles cond (NULL semantics inside) */
    const tc=window.__ddToggleCond?window.__ddToggleCond(col):'';
    if(f||tc){
      window.__ddFilters[col]=f||{op:'in',values:[],blank:false,cmp:null,min:'',max:''};
      window.__ddFiltersSrc[col]=myName;
      window.__ddFiltersCond[col]=[(f?cond():''),tc].filter(Boolean).join(' AND ');
    }else{
      delete window.__ddFilters[col];delete window.__ddFiltersSrc[col];delete window.__ddFiltersCond[col];
    }
    renderBtn();gfcChanged();
  }
  /* toggles of the column notify the filter about their switching */
  window.__ddToggleRepublish=window.__ddToggleRepublish||{};
  window.__ddToggleRepublish[col]=()=>apply(f);
  function closePop(){
    if(pop){pop.remove();pop=null}
    if(popOutside){document.removeEventListener('click',popOutside);popOutside=null}
  }
  function renderBtn(){
    root.innerHTML=`<button class="dd-fbtn${f?' on':''}" title="Filter: ${escA(cfg.title||col)}">${IC.filter}<span>${escH(cfg.title||col)}</span></button>`;
    root.querySelector('button').onclick=e=>{e.stopPropagation();openPop(e)};
  }
  async function openPop(ev){
    closePop();
    pop=document.createElement('div');
    pop.className='dd-pop';
    const kind=isN?'num':(isD?'date':'values');
    pop._d={vals:new Set(f?f.values||[]:[]),blank:f?!!f.blank:false,mode:f&&f.op==='notin'?'notin':'in',
      cmp:f&&f.cmp&&f.cmp!=='between'?f.cmp:null,min:(f&&f.min)??'',max:(f&&f.max)??''};
    let inner='';
    if(kind==='num'){
      const cmp0=pop._d.cmp||'between';
      pop._d.cmp=cmp0;
      inner=`<div class="num">
        <select name="op">
          <option value="none"${cmp0?'':' selected'}>&mdash;</option>
          <option value="eq"${cmp0==='eq'?' selected':''}>=</option>
          <option value="gt"${cmp0==='gt'?' selected':''}>&gt;</option>
          <option value="gte"${cmp0==='gte'?' selected':''}>&ge;</option>
          <option value="lt"${cmp0==='lt'?' selected':''}>&lt;</option>
          <option value="lte"${cmp0==='lte'?' selected':''}>&le;</option>
          <option value="between"${cmp0==='between'?' selected':''}>&ndash;</option>
        </select>
        <input type="number" data-k="min" value="${escA(pop._d.min)}" placeholder="Value">
        <input type="number" data-k="max" value="${escA(pop._d.max)}" placeholder="Max" style="display:${cmp0==='between'?'':'none'}">
      </div>`;
    }else if(kind==='date'){
      inner=`<div class="date"><label>From <input type="date" data-k="min" value="${escA(pop._d.min)}"></label>
        <label>To <input type="date" data-k="max" value="${escA(pop._d.max)}"></label></div>`;
    }
    inner+=`<div class="mode"><label><input type="radio" name="m" value="in" ${(f&&f.op==='notin')?'':'checked'}> Include</label>
        <label><input type="radio" name="m" value="notin" ${f&&f.op==='notin'?'checked':''}> Exclude</label></div>`
      +(kind==='values'?`<div class="srch"><input class="qs" placeholder="Search values..."></div>`:'')
      +`<div class="vals"><div class="load" style="padding:20px;text-align:center;color:#90A4AE">Loading...</div></div>`;
    pop.innerHTML=`<div class="srch" style="font-weight:600;border-bottom:1px solid #f4f4f5;padding:8px 10px">${escH(cfg.title||col)}</div>`+inner
      +`<div class="acts"><button class="b act-clear">Clear</button><button class="b act-close">Close</button>
        <button class="bp act-apply">Apply</button></div>`;
    document.body.appendChild(pop);
    const r=ev.target.getBoundingClientRect();
    let left=r.left-100,top=r.bottom+6;
    left=Math.max(8,Math.min(left,window.innerWidth-(pop.offsetWidth||300)-8));
    if(top+380>window.innerHeight-8)top=Math.max(8,window.innerHeight-390);
    pop.style.left=left+'px';pop.style.top=top+'px';
    pop.querySelectorAll('input[data-k],select[data-k]').forEach(inp=>{
      inp.oninput=()=>{pop._d[inp.dataset.k]=inp.value};
      inp.onchange=()=>{pop._d[inp.dataset.k]=inp.value};
    });
    const opSel=pop.querySelector('select[name=op]');
    if(opSel){const mx=pop.querySelector('input[data-k=max]');
      opSel.onchange=()=>{pop._d.cmp=opSel.value==='none'?null:opSel.value;if(mx)mx.style.display=opSel.value==='between'?'':'none'}}
    pop.querySelectorAll('input[name=m]').forEach(rb=>rb.onchange=()=>{pop._d.mode=rb.value});
    pop.querySelector('.act-apply').onclick=()=>{
      const d=pop._d,cmp=kind==='date'?'between':d.cmp;
      const hasVals=d.vals.size||d.blank;
      const hasRange=cmp&&((d.min??'')!==''||(cmp==='between'&&(d.max??'')!==''));
      closePop();
      apply(hasVals||hasRange?{op:d.mode,values:[...d.vals],blank:!!d.blank,cmp:hasRange?cmp:null,min:d.min??'',max:d.max??''}:null);
    };
    pop.querySelector('.act-close').onclick=closePop;
    pop.querySelector('.act-clear').onclick=()=>{closePop();apply(null)};
    setTimeout(()=>{document.removeEventListener('click',outside);popOutside=outside;document.addEventListener('click',outside)},0);
    function outside(e){if(pop&&!pop.contains(e.target)&&!root.contains(e.target)){closePop();document.removeEventListener('click',outside)}}
    /* the values list is shown for numbers/dates too — as in the table popup */
    renderVals();
    function renderVals(){
      const wrap=pop.querySelector('.vals');
      (async()=>{
        try{
          const w=gfcWhereFor(myName,null,'')||'TRUE';
          /* mvp-duckdb on "number" != '' returns an empty result — compare with
             an empty string only for text columns */
          const nnb=(!isN&&!isD)?`${gfcQid(col)} IS NOT NULL AND ${gfcQid(col)} != ''`:`${gfcQid(col)} IS NOT NULL`;
          const arr=await DQ(`SELECT ${castE} v, COUNT(*) c FROM t WHERE ${w} AND ${nnb} GROUP BY 1 ORDER BY c DESC LIMIT 300`);
          const opts=arr.toArray().map(r=>({v:String(r.v),c:Number(r.c)}));
          const bl=await DQ(`SELECT COUNT(*) c FROM t WHERE ${w} AND ${gfcQid(col)} IS NULL`);
          const b0=bl.toArray()[0];
          const blanks=b0?Number(b0.c):0;
          const blankRow=blanks?`<div class="val" data-v="__dd_blank__"><input type="checkbox" ${pop._d.blank?'checked':''}>
             <span>(Blanks)</span><span class="cnt">${fmtN(blanks)}</span></div>`:'';
          wrap.innerHTML=(opts.map(o=>
            `<div class="val" data-v="${escA(o.v)}"><input type="checkbox" ${pop._d.vals.has(o.v)?'checked':''}>
             <span>${escH(o.v)}</span><span class="cnt">${fmtN(o.c)}</span></div>`).join('')
            ||'<div class="load">No values</div>')+blankRow;
          wrap.querySelectorAll('input').forEach(cb=>cb.onchange=()=>{
            const v=cb.closest('.val').dataset.v,d=pop._d;
            if(v==='__dd_blank__'){d.blank=cb.checked;return}
            if(cb.checked)d.vals.add(v);else d.vals.delete(v);
          });
          const qs=pop.querySelector('.qs');
          if(qs)qs.oninput=()=>{const s=qs.value.toLowerCase();
            pop.querySelectorAll('.val').forEach(el=>{el.style.display=el.dataset.v.toLowerCase().includes(s)?'':'none'})};
        }catch(e){wrap.innerHTML='<div class="load" style="color:#d64545">'+escH(e.message||e)+'</div>'}
      })();
    }
  }
  document.addEventListener('dd-gfc-changed',()=>{renderBtn()});
  renderBtn();
}

/* ── duckdive_toggles: exclusion toggle buttons on a column ──
   Each active toggle adds (col IS DISTINCT FROM value) — NULL rows and
   values not explicitly excluded remain. The column atom is shared with
   duckdive_filter: if a filter component for the same column exists, it
   performs the publication (cond merge), otherwise the toggles publish
   the atom themselves. */
async function initToggles(cfg){
  const root=document.getElementById(cfg.root);
  if(!root){console.warn('duckdive_toggles: root not found:',cfg.root);return}
  const col=cfg.column,myName=cfg.name||('tgl_'+col);
  const meta=(B.columns||[]).find(c=>c.name===col);
  if(!meta){console.warn('duckdive_toggles: column not found:',col);root.style.display='none';return}
  const tU=(meta.type||'').toUpperCase();
  const isN=/INT|HUGEINT|DOUBLE|FLOAT|DEC|REAL/.test(tU);
  const escH=s=>(s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const items=(cfg.items||[]).filter(it=>it&&it.id!=null&&it.label!=null&&it.value!=null);
  const state=window.__ddToggles=window.__ddToggles||{};
  items.forEach(it=>{if(typeof state[it.id]!=='boolean')state[it.id]=it.on===true});
  function condParts(){
    const qt='"'+col.replace(/"/g,'""')+'"';
    return items.filter(it=>state[it.id]).map(it=>{
      const v=isN?Number(it.value):"'"+String(it.value).replace(/'/g,"''")+"'";
      return '('+qt+' IS DISTINCT FROM '+v+')';
    }).join(' AND ');
  }
  window.__ddToggleCond=window.__ddToggleCond||function(){return ''};
  const _tcPrev=window.__ddToggleCond;
  window.__ddToggleCond=function(c){return c===col?condParts():_tcPrev(c)};
  function republish(){
    const rp=window.__ddToggleRepublish&&window.__ddToggleRepublish[col];
    if(rp){rp();return}
    const tc=condParts();
    if(tc){
      window.__ddFilters[col]=window.__ddFilters[col]||{op:'in',values:[],blank:false,cmp:null,min:'',max:''};
      window.__ddFiltersSrc[col]=myName;
      window.__ddFiltersCond[col]=tc;
    }else if(window.__ddFiltersSrc[col]===myName){
      delete window.__ddFilters[col];delete window.__ddFiltersSrc[col];delete window.__ddFiltersCond[col];
    }
    gfcChanged();
  }
  function renderBtns(){
    root.innerHTML=items.map(it=>
      `<button class="dd-tbtn${state[it.id]?' on':''}" data-t="${escH(it.id)}" title="${escH(it.label)}">${escH(it.label)}</button>`).join('');
    root.querySelectorAll('.dd-tbtn').forEach(btn=>{
      btn.onclick=e=>{e.stopPropagation();
        const id=btn.dataset.t;
        state[id]=!state[id];
        renderBtns();republish();
      };
    });
  }
  document.addEventListener('dd-gfc-changed',()=>{renderBtns()});
  renderBtns();
  /* toggles enabled by default publish the atom right after initialization
     (filter components initialize before toggles) */
  if(items.some(it=>state[it.id]))republish();
}

/* ── common initialization: one DB, one instance per view ── */
try{
  await initDB();
}catch(err){
  bootUI('DuckDB-WASM init failed',100,false,String(err&&err.message||err));
  throw err;
}
onProg('ready',STAGE_PCT.ready);
bootDone=true;
if(watchdog)clearInterval(watchdog);
const VIEWS=B.views&&B.views.length?B.views:[{}];
for(let i=0;i<VIEWS.length;i++){
  const vw=VIEWS[i];
  let cols=B.columns,view='t';
  if(vw.sql){
    view=i>0?'t'+i:'t0';
    if(vw.name||String(vw.sql).indexOf('{{src}}')>=0){
      const gv={idx:i,view:view,sql:vw.sql,hasSrc:String(vw.sql).indexOf('{{src}}')>=0,
        name:vw.name||null,want:vw.use_filter_keys||null,allow:vw.filter_columns||null};
      GFC.views.push(gv);
      await buildGfcView(gv);
    }else{
      await DQ(`CREATE OR REPLACE VIEW ${view} AS SELECT *, ROW_NUMBER() OVER () AS __rn FROM (${vw.sql})`);
    }
    const d=await DQ(`DESCRIBE ${view}`);
    cols=d.toArray().map(r=>({name:r.column_name,type:r.column_type}));
  }
  await initInstance({idx:i,root:vw.root||'duckdive-table',pfx:vw.pfx??(i?'dd'+(i+1):''),
    view,cross:vw.cross!==false,name:vw.name||null,use_filter_keys:vw.use_filter_keys||null,
    filter_columns:vw.filter_columns||null,columns:cols,
    page_size:vw.page_size,title:vw.title,cell_popup:vw.cell_popup});
}
const FILTERS=B.filters||[];
for(let i=0;i<FILTERS.length;i++){
  try{await initFilter(FILTERS[i])}catch(e){console.warn('duckdive: filter init failed:',e)}
}
const TOGGLES=B.toggles||[];
for(let i=0;i<TOGGLES.length;i++){
  try{await initToggles(TOGGLES[i])}catch(e){console.warn('duckdive: toggles init failed:',e)}
}
})();
"""


def wasm_mount(view: dict) -> str:
    """Mount div of a single duckdive_table instance."""
    return f'<div id="{view["root"]}" class="duckdive-root"></div>'


def wasm_table_block(views: list[dict], base_boot: dict, filters: list[dict] | None = None,
                     toggles: list[dict] | None = None) -> str:
    """Shared boot (parquet embedded once) + duckdb-wasm runtime + all instances.

    views: placeholder specs (root, pfx, sql, cross, page_size, title, name,
    use_filter_keys); sql views are created in the shared connection as t1,
    t2, ...; filters: duckdive_filter specs (root, column, name, title);
    toggles: duckdive_toggles specs (root, column, name, items).
    """
    if _BUNDLE_CACHE is None:
        _load_bundle()

    b = _BUNDLE_CACHE
    boot = dict(base_boot)
    boot["views"] = views
    if filters:
        boot["filters"] = filters
    if toggles:
        boot["toggles"] = toggles
    js = _inject_icons(_JS)
    worker_text = _WORKER_SHIM + b["worker"]
    module = (
        f"const DUCKDB_SRC={_json_escape_script(b['duckdb'])};\n"
        f"const WORKER_SRC={_json_escape_script(worker_text)};\n"
        f"const WASM_GZ={json.dumps(b['wasm_gz_b64'])};\n"
        f"const WASM_RAW={json.dumps(b['wasm_raw'])};\n"
        + js
    )
    payload = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return (
        f"<style>{_CSS + _BOOT_CSS}</style>"
        f'<script id="duckdive-boot" type="application/json">{payload}</script>'
        f"<script>{_xlsx_src()}</script>"
        f"<script>{_inject_icons(_POPUP_SRC)}</script>"
        f'<script type="module">{module}</script>'
    )


def _load_bundle() -> None:
    """Read the vendored duckdb-wasm runtime once (module-level singleton).

    assets/wasm/ contains: duckdb-mvp.wasm (binary), duckdb-browser-mvp.worker.js
    (web worker) and duckdb-bundle.js (duckdb-wasm browser API + apache-arrow +
    tslib + flatbuffers, pre-bundled with esbuild into one import-free ESM file).
    """
    global _BUNDLE_CACHE
    wasm_b64 = (_DUCKDB_DIR / "duckdb-mvp.wasm").read_bytes()
    import base64 as _b64

    _BUNDLE_CACHE = {
        "duckdb": (_DUCKDB_DIR / "duckdb-bundle.js").read_text(encoding="utf-8"),
        "worker": (_DUCKDB_DIR / "duckdb-browser-mvp.worker.js").read_text(encoding="utf-8"),
        "wasm_gz_b64": _b64.b64encode(gzip.compress(wasm_b64, 6)).decode("ascii"),
        "wasm_raw": len(wasm_b64),
    }
