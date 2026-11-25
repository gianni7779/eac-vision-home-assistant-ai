'use strict';

const $ = id => document.getElementById(id);
const base = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
const apiUrl = path => base + path;
let state = null, page = 'live', players = [], visibleIds = '', selectedCam = null, toastTimer = 0, hideTimer = 0, storageDirty = false;
let recordingSegments = [], recordingEvents = [], recordingIndex = -1, pendingSeek = 0, recordingLoad = 0;
let previewIndex = -1, previewOffset = 0, previewLast = 0, previewDragging = false, previewSecond = 0, skipTimelineClick = false;
let detailCamId = null, detailPlayer = null, detailCams = [], detailAudioAvailable = false, detailAudioChecked = false, detailAudioOn = false, preferredRecordingCam = null;
let aiState = null, eventCursor = 0, preferredRecordingDay = null, preferredRecordingSecond = null;
let eventRetryTimer = 0, eventRetryDeadline = 0;
let budget = Number(localStorage.getItem('eac-live-budget') || '4');
if (![4, 8, 16].includes(budget)) budget = 4;
$('live-budget').value = String(budget);

function toast(message) {
  const el = $('toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove('show'), 3500);
}
async function request(path, options = {}) {
  const response = await fetch(apiUrl(path), {cache:'no-store', ...options});
  if (!response.ok) throw new Error((await response.text()).slice(0, 250) || `HTTP ${response.status}`);
  return response.json();
}
function showControls() {
  document.body.classList.add('controls-visible');
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => { if (page === 'live' && !document.body.classList.contains('menu-open')) document.body.classList.remove('controls-visible'); }, 3500);
}
function setPage(next) {
  if (next !== 'live' && detailCamId) closeCameraDetail(false);
  page = next;
  document.body.classList.toggle('live-mode', page === 'live');
  document.body.classList.remove('menu-open');
  document.body.classList.remove('nav-hover');
  $('nav-backdrop').hidden = true;
  document.body.classList.add('controls-visible');
  for (const name of ['live','recordings','events','settings']) $(name+'-page').hidden = name !== page;
  for (const el of document.querySelectorAll('.nav')) el.classList.toggle('active', el.dataset.page === page);
  $('page-title').textContent = ({live:'Telecamere',recordings:'Registrazioni',events:'Eventi AI',settings:'Impostazioni'})[page];
  $('top-subtitle').textContent = page === 'live' ? 'Vista Principale' : 'EAC Vision AI Home Assistant';
  $('live-budget-label').hidden = page !== 'live';
  if (page !== 'live') { stopPlayers(); visibleIds = ''; }
  if (page !== 'recordings') stopRecordingPlayback();
  if (page === 'live') renderLive(true);
  if (page === 'recordings') loadRecordings();
  if (page === 'settings') renderSettings();
  if (page === 'settings') loadAISettings();
  if (page === 'events') loadEvents();
  showControls();
}

function stopPlayers() {
  players.forEach(p => p.stop()); players = [];
  for (const video of document.querySelectorAll('#camera-grid video')) { video.pause(); video.removeAttribute('src'); video.load(); video.style.display='none'; }
}
function cameraRows(n) {
  const w = $('camera-grid').clientWidth || innerWidth, h = $('camera-grid').clientHeight || innerHeight;
  let best = 1, score = Infinity;
  for (let rows=1; rows<=n; rows++) {
    const avg = n/rows, ratio = (w/avg)/(h/rows);
    const cost = Math.abs(Math.log(ratio/(16/9))) + (rows * Math.ceil(avg)-n)*.06;
    if (cost < score) { score=cost; best=rows; }
  }
  const result=[], quotient=Math.floor(n/best), extra=n%best;
  for (let i=0;i<best;i++) result.push(quotient+(i<extra?1:0));
  return result;
}
function makeTile(cam, live) {
  const tile = document.createElement('div'); tile.className='tile'; tile.dataset.id=cam.id;
  const image = document.createElement('img'); image.alt=''; image.loading='lazy'; image.src=apiUrl('api/snapshot/'+encodeURIComponent(cam.id)+'?t='+Date.now()); tile.append(image);
  if (live) {
    const video=document.createElement('video'); video.autoplay=true; video.muted=true; video.playsInline=true; video.style.opacity='0';
    video.addEventListener('playing',()=>{video.style.opacity='1'});
    tile.append(video);
    const error=document.createElement('div'); error.className='tile-error'; error.hidden=true; tile.append(error);
    const player=new MsePlayer(video, cam.id, message => { error.textContent=message; error.hidden=false; video.style.display='none'; });
    players.push(player); player.start();
  }
  const shade=document.createElement('div'); shade.className='tile-shade';
  const name=document.createElement('span'); name.textContent=cam.name; shade.append(name);
  const badge=document.createElement('span'); badge.className='badge'; badge.textContent=live?'LIVE':'ANTEPRIMA'; shade.append(badge);
  tile.append(shade);
  if (cam.recording) { const rec=document.createElement('span'); rec.className='badge rec'; rec.textContent='● REC'; rec.style.cssText='position:absolute;top:12px;right:12px'; tile.append(rec); }
  tile.tabIndex=0;tile.setAttribute('role','button');tile.setAttribute('aria-label','Apri '+cam.name);
  tile.addEventListener('click', () => openCameraDetail(cam.id));
  tile.addEventListener('keydown', event => {if(event.key==='Enter'||event.key===' '){event.preventDefault();openCameraDetail(cam.id)}});
  return tile;
}
function renderLive(force=false) {
  if (!state || page !== 'live') return;
  if (detailCamId) return;
  const cams=state.cameras.filter(c=>c.selected&&c.has_source).slice(0,16);
  const signature=cams.map(c=>c.id+':'+c.name+':'+c.recording).join('|')+':'+budget;
  if (!force && signature===visibleIds) return;
  visibleIds=signature; stopPlayers();
  const grid=$('camera-grid'); grid.replaceChildren();
  $('empty-live').hidden=cams.length>0; grid.hidden=cams.length===0;
  if (!cams.length) return;
  let offset=0;
  const counts=cameraRows(cams.length),columns=Math.max(...counts);
  for (const count of counts) {
    const row=document.createElement('div'); row.className='grid-row';
    for (let i=0;i<count;i++) { const cam=cams[offset];const tile=makeTile(cam,offset<budget);tile.style.flexBasis=`calc((100% - ${(columns-1)*5}px) / ${columns})`;row.append(tile);offset++; }
    grid.append(row);
  }
}

function detailStatus(message) {
  $('detail-status').textContent=message;
  $('detail-status').hidden=!message;
}
function updateDetailAudioButton() {
  const button=$('detail-audio');
  button.disabled=!detailAudioAvailable;
  button.textContent=!detailAudioChecked?'🔇 Audio in attesa':detailAudioAvailable?(detailAudioOn?'🔊 Audio attivo':'🔇 Attiva audio'):'🔇 Audio non disponibile';
  button.setAttribute('aria-pressed',String(detailAudioOn));
}
function startDetailStream(id, withAudio) {
  if(detailCamId!==id)return;
  if(detailPlayer){detailPlayer.stop();detailPlayer=null}
  const video=$('detail-video');
  video.pause();video.removeAttribute('src');video.load();video.style.opacity='0';video.muted=true;
  const player=new MsePlayer(video,id,message=>{
    if(detailCamId!==id||detailPlayer!==player)return;
    if(withAudio){
      detailAudioOn=false;detailAudioAvailable=false;detailAudioChecked=true;updateDetailAudioButton();
      detailStatus('Audio non compatibile; avvio il video…');
      startDetailStream(id,false);
      return;
    }
    video.style.opacity='0';detailStatus(message);
  },{audio:withAudio,onMime:mime=>{
    if(detailCamId!==id||detailPlayer!==player)return;
    detailAudioChecked=true;
    detailAudioAvailable=withAudio&&/(?:mp4a\.40\.|flac|opus)/i.test(mime);
    updateDetailAudioButton();
  },onAutoplayBlocked:()=>{if(detailCamId===id&&detailPlayer===player)detailStatus('Tocca il video per avviare la riproduzione')}});
  detailPlayer=player;
  player.start();
}
function loadDetailCamera(id) {
  const cam=detailCams.find(item=>item.id===id);
  if(!cam)return;
  detailCamId=id;detailAudioOn=false;detailAudioAvailable=false;detailAudioChecked=false;updateDetailAudioButton();
  $('detail-name').textContent=cam.name;
  $('camera-detail').setAttribute('aria-label','Telecamera '+cam.name);
  $('detail-counter').textContent=(detailCams.indexOf(cam)+1)+' / '+detailCams.length;
  $('detail-rec').hidden=!cam.recording;
  $('detail-prev').disabled=detailCams.length<2;$('detail-next').disabled=detailCams.length<2;
  $('detail-snapshot').src=apiUrl('api/snapshot/'+encodeURIComponent(id)+'?t='+Date.now());
  detailStatus('Collegamento al video…');
  startDetailStream(id,true);
}
function openCameraDetail(id) {
  if(!state||page!=='live')return;
  detailCams=state.cameras.filter(cam=>cam.selected&&cam.has_source).slice(0,16);
  if(!detailCams.some(cam=>cam.id===id))return;
  stopPlayers();
  $('camera-detail').hidden=false;
  for(const area of ['live-page','topbar','sidebar','mobile-nav'])$(area).inert=true;
  loadDetailCamera(id);
  $('detail-close').focus();
}
function closeCameraDetail(resume=true) {
  if(!detailCamId)return;
  const oldId=detailCamId;
  if(document.fullscreenElement=== $('camera-detail'))document.exitFullscreen().catch(()=>{});
  if(detailPlayer){detailPlayer.stop();detailPlayer=null}
  const video=$('detail-video');video.pause();video.removeAttribute('src');video.load();
  $('camera-detail').hidden=true;
  for(const area of ['live-page','topbar','sidebar','mobile-nav'])$(area).inert=false;
  detailCamId=null;detailCams=[];detailAudioAvailable=false;detailAudioChecked=false;detailAudioOn=false;
  if(resume&&page==='live'){
    renderLive(true);
    const tile=[...document.querySelectorAll('#camera-grid .tile')].find(el=>el.dataset.id===oldId);
    if(tile)tile.focus();
  }
}
function changeDetailCamera(step) {
  if(!detailCamId||detailCams.length<2)return;
  const index=detailCams.findIndex(cam=>cam.id===detailCamId);
  loadDetailCamera(detailCams[(index+step+detailCams.length)%detailCams.length].id);
}

class MsePlayer {
  constructor(video,id,onError,options={}) { this.video=video;this.id=id;this.onError=onError;this.options=options;this.queue=[];this.closed=false;this.started=false;this.lastData=Date.now(); }
  start() {
    if (!window.MediaSource) { this.onError('Anteprima disponibile'); return; }
    const videoCodecs=['avc1.640029','avc1.64002A','avc1.640033','avc1.42E01E','hvc1.1.6.L153.B0'];
    const audioCodecs=this.options.audio?['mp4a.40.2','mp4a.40.5','flac','opus']:[];
    const codecs=videoCodecs.concat(audioCodecs).filter(c=>{try{return MediaSource.isTypeSupported('video/mp4; codecs="'+c+'"')}catch{return false}}).join(',');
    const url=new URL(apiUrl('api/live/'+encodeURIComponent(this.id)),location.origin); url.protocol=location.protocol==='https:'?'wss:':'ws:';
    const ws=new WebSocket(url); this.ws=ws; ws.binaryType='arraybuffer';
    ws.onopen=()=>ws.send(JSON.stringify({type:'mse',value:codecs}));
    ws.onmessage=event=>{
      if (this.closed) return;
      this.lastData=Date.now();
      if (typeof event.data==='string') { let message;try{message=JSON.parse(event.data)}catch{return} if(message.type==='mse'){if(this.options.onMime)this.options.onMime(message.value);this.setup(message.value)}else if(message.type==='error')this.onError('Video non disponibile');return; }
      this.queue.push(new Uint8Array(event.data));this.flush();
    };
    ws.onclose=()=>{if(!this.closed)this.onError('Live interrotto')};
    this.watch=setInterval(()=>{if(!this.closed&&Date.now()-this.lastData>15000)this.onError('Il live non parte')},3000);
  }
  setup(mime) {
    try {
      this.ms=new MediaSource(); const objectURL=URL.createObjectURL(this.ms); this.objectURL=objectURL;this.video.src=objectURL;
      this.ms.addEventListener('sourceopen',()=>{if(this.closed)return;try{this.sb=this.ms.addSourceBuffer(mime);this.sb.addEventListener('updateend',()=>this.updated());this.flush()}catch{this.onError('Codec non supportato sul dispositivo')}});
    } catch { this.onError('Video non supportato'); }
  }
  flush() {
    if(!this.sb||this.sb.updating||!this.queue.length||this.closed)return;
    let total=0;for(const q of this.queue)total+=q.length;
    const data=new Uint8Array(total);let at=0;for(const q of this.queue){data.set(q,at);at+=q.length}this.queue=[];
    try{this.sb.appendBuffer(data)}catch{try{const b=this.sb.buffered;if(b.length)this.sb.remove(b.start(0),Math.max(b.start(0),b.end(b.length-1)-3))}catch{}this.queue.unshift(data)}
  }
  updated() {
    if(this.closed||!this.sb)return;
    try{const b=this.sb.buffered;if(b.length){const start=b.start(0),end=b.end(b.length-1);if(!this.started&&end>start){this.started=true;this.video.currentTime=Math.max(start,end-.6);this.video.play().catch(()=>{if(this.options.onAutoplayBlocked)this.options.onAutoplayBlocked()})}if(end-this.video.currentTime>4)this.video.currentTime=end-.5;if(end-start>30){this.sb.remove(start,end-15);return}}}catch{}
    this.flush();
  }
  stop() {this.closed=true;clearInterval(this.watch);if(this.ws)this.ws.close();if(this.video)this.video.pause();if(this.objectURL)URL.revokeObjectURL(this.objectURL)}
}

async function update() {
  try {
    const firstLoad=!state;
    state=await request('api/status');
    const eventSelect=$('events-cam'),eventCam=eventSelect.value;
    eventSelect.replaceChildren();
    const allOption=document.createElement('option');allOption.value='';allOption.textContent='Tutte';eventSelect.append(allOption);
    for(const cam of state.cameras.filter(item=>item.has_source)){const option=document.createElement('option');option.value=cam.id;option.textContent=cam.name;eventSelect.append(option)}
    if([...eventSelect.options].some(option=>option.value===eventCam))eventSelect.value=eventCam;
    document.body.classList.toggle('hide-camera-name',state.show_names===false);
    $('ha-dot').classList.toggle('ok',!state.ha_error);
    $('ha-label').textContent=state.ha_error?'Home Assistant non disponibile':'Home Assistant collegato';
    if(page==='settings'){renderSettings();loadAISettings()}
    if(page==='live')renderLive();
    if(page==='recordings'&&firstLoad)loadRecordings();
  } catch(error) { toast('Servizio non disponibile: '+error.message); }
}
function renderSettings() {
  if(!state)return;
  $('ha-error').hidden=!state.ha_error;$('ha-error').textContent=state.ha_error||'';
  $('source-error').hidden=!state.source_error;$('source-error').textContent=state.source_error||'';
  $('video-error').hidden=state.relay&&state.ffmpeg&&!state.memory_paused;$('video-error').textContent=!state.relay?'Motore video go2rtc non avviato. Controlla il registro dell’app.':!state.ffmpeg?'FFmpeg non disponibile: il live funziona, la registrazione no.':state.memory_paused?'Memoria disponibile bassa: registrazione sospesa per proteggere Home Assistant.':'';
  $('cam-count').textContent=state.cameras.filter(c=>c.has_source).length+' / '+state.max_cameras;
  $('metric-cameras').textContent=state.cameras.filter(c=>c.has_source).length+' / '+state.max_cameras;
  $('metric-cameras-detail').textContent=state.cameras.filter(c=>c.selected&&c.has_source).length+' nella vista Principale';
  $('metric-recording').textContent=state.cameras.filter(c=>c.recording).length;
  $('metric-storage').textContent=state.storage.ready?'Pronto':'Non pronto';
  $('metric-storage-detail').textContent=state.storage.ready?`${state.storage.free_gb} GB liberi`:state.storage.message;
  $('metric-memory').textContent=state.memory_available_mb==null?'—':Math.round(state.memory_available_mb)+' MB';
  $('metric-memory-detail').textContent=state.memory_paused?'Registrazione sospesa per memoria bassa':'Memoria disponibile sul dispositivo';
  $('metric-app-disk').textContent=state.storage.used_gb==null?'—':state.storage.used_gb+' GB';
  $('metric-app-disk-detail').textContent=state.storage.total_gb==null?'Solo registrazioni':`Disco ${state.storage.total_gb} GB · riserva ${state.storage.reserve_gb} GB`;
  $('metric-app-ram').textContent=state.app_memory_mb==null?'—':state.app_memory_mb+' MB';
  $('metric-app-cpu').textContent=state.app_cpu_percent==null?'—':state.app_cpu_percent+'%';
  $('app-version').textContent=`${state.name} · versione ${state.version}`;
  const list=$('camera-list');list.replaceChildren();
  if(!state.cameras.length){const p=document.createElement('p');p.className='muted';p.style.gridColumn='1 / -1';p.textContent='Nessuna telecamera ONVIF trovata. Aggiungila prima in Impostazioni → Dispositivi e servizi di Home Assistant.';list.append(p)}
  for(const cam of state.cameras){
    const card=document.createElement('div');card.className='camera-item';
    const head=document.createElement('div');head.className='camera-item-head';
    const name=document.createElement('strong');name.textContent=cam.name;head.append(name);
    const mark=document.createElement('span');mark.className='state'+(cam.has_source?' ok':'');mark.textContent=cam.has_source?'● ONVIF pronto':'○ ONVIF non disponibile';head.append(mark);card.append(head);
    const info=document.createElement('small');info.textContent=cam.entity_id;card.append(info);
    const bottom=document.createElement('div');bottom.className='camera-item-bottom';
    const detail=document.createElement('span');detail.className='muted';detail.textContent=cam.mode==='continuous'?'Registrazione continua':cam.mode==='motion'?'Registrazione al movimento':'Solo visione';bottom.append(detail);
    const button=document.createElement('button');button.textContent='Configura';button.addEventListener('click',()=>openCamera(cam));bottom.append(button);card.append(bottom);list.append(card);
    if(cam.recorded_audio===false){const warning=document.createElement('small');warning.className='state';warning.textContent='Audio assente nel flusso ONVIF: attiva il microfono della telecamera e il profilo ONVIF con audio.';card.append(warning)}
    if(cam.recorded_audio===true){const audio=document.createElement('small');audio.className='state ok';audio.textContent='Audio incluso nelle registrazioni';card.append(audio)}
    if(cam.record_error){const warning=document.createElement('small');warning.className='state';warning.textContent=cam.record_error;card.append(warning)}
  }
  if(!storageDirty){$('storage-path').value=state.storage.path||'';$('retention-days').value=state.retention_days;$('max-gb').value=state.max_gb}
  $('storage-pill').textContent=state.storage.ready?'● Disco pronto':'● Disco non pronto';$('storage-pill').classList.toggle('ok',state.storage.ready);
  $('storage-message').textContent=state.storage.ready?`Spazio libero: ${state.storage.free_gb} GB · usato dalle registrazioni: ${state.storage.used_gb} GB`:state.storage.message;
  $('client-key').textContent=state.client_key;
  $('settings-live-budget').value=String(budget);
  $('show-camera-names').checked=state.show_names!==false;
}
function openCamera(cam) {
  selectedCam=cam;$('dialog-title').textContent=cam.name;$('dialog-entity').textContent=cam.entity_id;
  $('cam-name').value=cam.name;$('cam-mode').value=cam.mode;
  $('cam-selected').checked=cam.selected;
  $('source-hint').textContent=cam.has_source?'Il profilo video arriva dall’integrazione ONVIF di Home Assistant.':'Flusso non disponibile: controlla la telecamera nell’integrazione ONVIF di Home Assistant.';
  $('camera-dialog').showModal();
}
async function saveCamera(event) {
  event.preventDefault();if(!selectedCam)return;
  const body={name:$('cam-name').value,mode:$('cam-mode').value,selected:$('cam-selected').checked};
  try{await request('api/cameras/'+encodeURIComponent(selectedCam.id),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('camera-dialog').close();toast('Telecamera salvata');await update()}catch(error){toast(error.message)}
}
async function saveStorage(event) {
  event.preventDefault();
  const body={path:$('storage-path').value.trim(),retention_days:Number($('retention-days').value),max_gb:Number($('max-gb').value)};
  try{await request('api/storage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});storageDirty=false;toast('Archivio pronto');await update()}catch(error){toast(error.message)}
}
function clipClock(segment) {
  const match = /^\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})\.mp4$/.exec(segment.name);
  return match ? Number(match[1])*3600+Number(match[2])*60+Number(match[3]) : 0;
}
function clockText(seconds, withSeconds=false) {
  const value=Math.max(0,Math.min(86399,Math.floor(seconds)));
  return String(Math.floor(value/3600)).padStart(2,'0')+':'+String(Math.floor(value%3600/60)).padStart(2,'0')+(withSeconds?':'+String(value%60).padStart(2,'0'):'');
}
function stopRecordingPlayback() {
  hideRecordingPreview();
  const video=$('playback'); video.pause(); video.removeAttribute('src'); video.load(); video.hidden=true;
  $('player-empty').hidden=false;
  $('player-empty').querySelector('strong').textContent='Pronto per la riproduzione';
  $('player-empty').querySelector('small').textContent='Seleziona un orario sulla barra per iniziare.';
  $('playback-title').textContent='Scegli un orario';
  recordingIndex=-1;pendingSeek=0;
  $('timeline-marker').hidden=true;$('timeline-clock').textContent='--:--:--';
  $('record-timeline').setAttribute('aria-valuenow','0');
  $('record-timeline').setAttribute('aria-valuetext','00:00');
  for(const button of $('record-hours').children)button.classList.remove('active');
}
function showRecordingState(message) {
  recordingSegments=[];recordingEvents=[];stopRecordingPlayback();
  $('record-count').textContent='Nessun video';
  $('record-summary').textContent=message;
  $('timeline-coverage').replaceChildren();
  $('timeline-events').replaceChildren();
  $('record-hours').replaceChildren();
  $('record-day-label').textContent=$('record-day').value||'Nessuna data';
}
function updatePlaybackPosition(seconds) {
  const at=Math.max(0,Math.min(86399,seconds));
  $('timeline-marker').hidden=false;
  $('timeline-marker').style.left=(at/86400*100)+'%';
  $('timeline-clock').textContent=clockText(at,true);
  $('record-timeline').setAttribute('aria-valuenow',String(Math.floor(at/60)));
  $('record-timeline').setAttribute('aria-valuetext',clockText(at));
  const hour=Math.floor(at/3600);
  for(const button of $('record-hours').children)button.classList.toggle('active',Number(button.dataset.hour)===hour);
}
function renderRecordingTimeline() {
  const coverage=$('timeline-coverage');coverage.replaceChildren();
  const hours=$('record-hours');hours.replaceChildren();
  const ticks=$('timeline-ticks');ticks.replaceChildren();
  for(let hour=0;hour<=24;hour+=3){const tick=document.createElement('span');tick.textContent=String(hour).padStart(2,'0')+':00';ticks.append(tick)}
  for(const segment of recordingSegments){
    const start=clipClock(segment),end=Math.min(86400,start+Number(segment.dur||0));
    if(end<=start)continue;
    const bar=document.createElement('span');bar.className='timeline-span';
    bar.style.left=(start/86400*100)+'%';bar.style.width=((end-start)/86400*100)+'%';coverage.append(bar);
  }
  const events=$('timeline-events');events.replaceChildren();
  for(const event of recordingEvents){
    if(!['persona','animale','veicolo'].includes(event.label))continue;
    const at=new Date(event.start),second=at.getHours()*3600+at.getMinutes()*60+at.getSeconds();
    const mark=document.createElement('span');mark.className='timeline-event '+event.label;
    mark.style.left=(second/86400*100)+'%';
    events.append(mark);
  }
  for(let hour=0;hour<24;hour++){
    const button=document.createElement('button');button.type='button';button.className='hour-button';
    button.dataset.hour=String(hour);button.textContent=String(hour).padStart(2,'0')+':00';
    const available=recordingSegments.some(s=>clipClock(s)<(hour+1)*3600&&clipClock(s)+s.dur>hour*3600);
    button.disabled=!available;
    if(available){button.classList.add('available');button.setAttribute('aria-label','Riproduci dalle '+button.textContent);button.addEventListener('click',()=>seekRecording(hour*3600))}
    hours.append(button);
  }
  const total=recordingSegments.reduce((sum,s)=>sum+Math.max(0,Math.min(86400,clipClock(s)+s.dur)-clipClock(s)),0);
  const minutes=Math.round(total/60);
  $('record-count').textContent=`${Math.floor(minutes/60)} h ${String(minutes%60).padStart(2,'0')} min`;
  $('record-summary').textContent=`Il blu indica le ore disponibili. ${recordingEvents.length} eventi AI in questo giorno. Durante la visione, il video prosegue automaticamente.`;
  $('record-day-label').textContent=$('record-day').value;
}
function playRecording(index, offset=0) {
  if(index<0||index>=recordingSegments.length)return;
  const segment=recordingSegments[index],video=$('playback');
  const url=apiUrl('api/recording/'+encodeURIComponent($('record-cam').value)+'/'+encodeURIComponent(segment.name));
  recordingIndex=index;pendingSeek=Math.max(0,offset);
  $('playback-title').textContent=$('record-day').value+' · '+clockText(clipClock(segment));
  $('player-empty').hidden=true;video.hidden=false;
  updatePlaybackPosition(clipClock(segment)+pendingSeek);
  if(video.src===new URL(url,location.href).href&&video.readyState>=1){
    video.currentTime=Math.min(pendingSeek,Math.max(0,video.duration-.1));video.play().catch(()=>{});
  }else{video.src=url;video.load()}
}
function seekRecording(seconds) {
  if(!recordingSegments.length){toast('Nessuna registrazione disponibile a quest’ora');return}
  const target=Math.max(0,Math.min(86399,seconds));
  let index=recordingSegments.findIndex(s=>clipClock(s)<=target&&target<clipClock(s)+s.dur);
  if(index<0)index=recordingSegments.findIndex(s=>clipClock(s)>target);
  if(index<0)index=recordingSegments.length-1;
  const start=clipClock(recordingSegments[index]);
  playRecording(index,target>=start&&target<start+recordingSegments[index].dur?target-start:0);
}
function hideRecordingPreview(){
  const preview=$('timeline-preview'),video=$('timeline-preview-video');
  preview.hidden=true;previewIndex=-1;video.pause();video.removeAttribute('src');video.load();
}
function previewRecording(seconds){
  if(!recordingSegments.length)return;
  previewSecond=Math.max(0,Math.min(86399,seconds));
  const preview=$('timeline-preview'),video=$('timeline-preview-video');
  preview.hidden=false;preview.style.left=Math.max(9,Math.min(91,previewSecond/86400*100))+'%';
  $('timeline-preview-clock').textContent=clockText(previewSecond,true);
  let index=recordingSegments.findIndex(s=>clipClock(s)<=previewSecond&&previewSecond<clipClock(s)+s.dur);
  if(index<0)index=recordingSegments.findIndex(s=>clipClock(s)>previewSecond);
  if(index<0)index=recordingSegments.length-1;
  const segment=recordingSegments[index];
  previewOffset=Math.max(0,Math.min(previewSecond-clipClock(segment),segment.dur-.1));
  if(previewIndex===index){
    if(Date.now()-previewLast>=140&&video.readyState>=1&&Math.abs(video.currentTime-previewOffset)>.3){
      previewLast=Date.now();try{video.currentTime=previewOffset}catch{}
    }
    return;
  }
  previewIndex=index;previewLast=Date.now();
  video.src=apiUrl('api/recording/'+encodeURIComponent($('record-cam').value)+'/'+encodeURIComponent(segment.name));
  video.load();
}
function timelineSecond(event){
  const rect=$('record-timeline').getBoundingClientRect();
  return Math.max(0,Math.min(86399,(event.clientX-rect.left)/rect.width*86400));
}
async function loadRecordings() {
  if(!state)return;
  const select=$('record-cam'),old=preferredRecordingCam||select.value;select.replaceChildren();
  for(const cam of state.cameras.filter(c=>c.has_source)){const opt=document.createElement('option');opt.value=cam.id;opt.textContent=cam.name;select.append(opt)}
  if([...select.options].some(o=>o.value===old))select.value=old;
  preferredRecordingCam=null;
  const daySelect=$('record-day');
  if(!select.value){daySelect.replaceChildren();showRecordingState('Configura una telecamera ONVIF per vedere le registrazioni.');return}
  const token=++recordingLoad;
  try{
    const days=await request('api/days/'+encodeURIComponent(select.value));
    if(token!==recordingLoad)return;
    const previous=preferredRecordingDay||daySelect.value;daySelect.replaceChildren();
    for(const item of days){const opt=document.createElement('option');opt.value=item.day;opt.textContent=item.day;daySelect.append(opt)}
    if([...daySelect.options].some(o=>o.value===previous))daySelect.value=previous;
    if(preferredRecordingSecond!=null && previous && daySelect.value!==previous){
      $('record-summary').textContent='Il video dell’evento è in preparazione…';scheduleEventRecordingRetry();return;
    }
    preferredRecordingDay=null;
    await loadSegments();
  }catch(error){if(token===recordingLoad)showRecordingState('Impossibile caricare i giorni: '+error.message)}
}
async function loadSegments() {
  const cid=$('record-cam').value,day=$('record-day').value,token=++recordingLoad;
  stopRecordingPlayback();
  recordingEvents=[];$('timeline-events').replaceChildren();
  if(!cid||!day){showRecordingState('Nessuna registrazione disponibile. Controlla che la registrazione continua e il disco siano attivi.');return}
  try{
    const [segments,events]=await Promise.all([
      request('api/recordings/'+encodeURIComponent(cid)+'?day='+encodeURIComponent(day)),
      request('api/ai/events?cam='+encodeURIComponent(cid)+'&day='+encodeURIComponent(day)).catch(()=>[])
    ]);
    if(token!==recordingLoad)return;
    recordingSegments=segments.filter(s=>!s.live&&Number(s.dur)>0).sort((a,b)=>clipClock(a)-clipClock(b));
    recordingEvents=events;
    if(!recordingSegments.length){showRecordingState(preferredRecordingSecond!=null?'Il video dell’evento è in preparazione…':'Nessuna registrazione completata in questo giorno.');if(preferredRecordingSecond!=null)scheduleEventRecordingRetry();return}
    renderRecordingTimeline();
    if(preferredRecordingSecond!=null){
      const second=preferredRecordingSecond;
      const match=recordingSegments.find(s=>clipClock(s)<=second+5&&clipClock(s)+s.dur>second+5);
      if(match){preferredRecordingSecond=null;clearTimeout(eventRetryTimer);seekRecording(Math.max(second,clipClock(match)))}
      else{$('record-summary').textContent='Il video dell’evento è in preparazione…';scheduleEventRecordingRetry()}
    }
  }catch(error){if(token===recordingLoad)showRecordingState('Impossibile caricare le registrazioni: '+error.message)}
}

async function loadAISettings(){
  try{
    aiState=await request('api/ai/state');
    if(page!=='settings')return;
    $('ai-enabled').checked=aiState.enabled;
    $('ai-max-cameras').value=String(aiState.max_cameras);
    $('ai-status').textContent=`${aiState.status} · ${aiState.inferences} analisi · ${aiState.ms} ms in media`;
    const list=$('ai-camera-list');list.replaceChildren();
    for(const [id,cam] of Object.entries(aiState.cams)){
      const box=document.createElement('div');box.className='ai-camera';
      const heading=document.createElement('strong');heading.textContent=cam.name;box.append(heading);
      const small=document.createElement('small');
      let aiStatus=!cam.detect?'Rilevamento disattivato':cam.limited?'Fuori dal limite delle camere AI: aumenta «Telecamere analizzate insieme»':cam.error||(!cam.watching?'In attesa del video':cam.frame_age_s>5?'Video fermo da '+Math.round(cam.frame_age_s)+' s':cam.motion?'Movimento rilevato: analisi in corso':'In attesa di movimento');
      if(cam.watching)aiStatus+=` · ${cam.frames} fotogrammi · ${cam.inferences} analisi · ${cam.persona} persone nel fotogramma · ${cam.source||'Profilo principale'}`;
      small.textContent=aiStatus;box.append(small);
      const options=document.createElement('div');options.className='ai-camera-options';
      for(const [field,label] of [['detect','Rileva'],['persona','Persone'],['veicolo','Veicoli'],['animale','Animali']]){
        const input=document.createElement('input');input.type='checkbox';input.checked=field==='detect'?cam.detect:cam.labels[field];
        input.addEventListener('change',()=>saveAI({cam:id,[field]:input.checked}));
        const wrapper=document.createElement('label');wrapper.append(input,' '+label);options.append(wrapper);
      }
      const select=document.createElement('select');
      for(const [value,label] of [[0,'Bassa'],[1,'Normale'],[2,'Alta']]){const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option)}
      select.value=String(cam.sensitivity);select.addEventListener('change',()=>saveAI({cam:id,sensitivity:Number(select.value)}));
      const wrapper=document.createElement('label');wrapper.append('Sensibilità ',select);options.append(wrapper);box.append(options);list.append(box);
    }
  }catch(error){$('ai-status').textContent='AI non disponibile: '+error.message}
}
async function saveAI(body){
  try{await request('api/ai/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});await loadAISettings();toast('Rilevamento AI aggiornato')}
  catch(error){toast(error.message);await loadAISettings()}
}
async function loadEvents(more=false){
  if(!more){eventCursor=0;$('events-list').replaceChildren()}
  $('events-status').textContent='Caricamento eventi…';
  const params=new URLSearchParams({cam:$('events-cam').value,label:$('events-label').value,limit:'60'});
  if(more&&eventCursor)params.set('before',String(eventCursor));
  try{
    const events=await request('api/ai/events?'+params);
    if(page!=='events')return;
    $('events-status').textContent=events.length?`${events.length} eventi caricati`:'Nessun evento per questi filtri';
    $('events-more').hidden=events.length<60;
    if(events.length)eventCursor=events[events.length-1].start;
    for(const ev of events){
      const card=document.createElement('article');card.className='event-card';
      const image=document.createElement('img');image.alt='';image.loading='lazy';image.src=apiUrl('api/ai/event/'+encodeURIComponent(ev.id)+'.jpg');card.append(image);
      const body=document.createElement('div');body.className='event-body';
      const heading=document.createElement('strong');heading.textContent=({persona:'Persona',veicolo:'Veicolo',animale:'Animale'})[ev.label]||ev.label;body.append(heading);
      const desc=document.createElement('small');desc.textContent=`${ev.camName} · ${new Date(ev.start).toLocaleString('it-IT')} · ${Math.round(ev.score*100)}%`;body.append(desc);
      const actions=document.createElement('div');actions.className='event-actions';
      const rec=document.createElement('button');rec.textContent='◷ Registrazione';rec.addEventListener('click',()=>openEventRecording(ev));actions.append(rec);
      const live=document.createElement('button');live.textContent='◉ Live';live.addEventListener('click',()=>{setPage('live');openCameraDetail(ev.cam)});actions.append(live);
      body.append(actions);card.append(body);$('events-list').append(card);
    }
  }catch(error){$('events-status').textContent='Impossibile caricare gli eventi: '+error.message}
}
function openEventRecording(ev){
  const at=new Date(ev.start);
  preferredRecordingCam=ev.cam;
  preferredRecordingDay=[at.getFullYear(),String(at.getMonth()+1).padStart(2,'0'),String(at.getDate()).padStart(2,'0')].join('-');
  preferredRecordingSecond=Math.max(0,at.getHours()*3600+at.getMinutes()*60+at.getSeconds()-5);
  clearTimeout(eventRetryTimer);eventRetryDeadline=Date.now()+90000;
  setPage('recordings');
}
function scheduleEventRecordingRetry(){
  clearTimeout(eventRetryTimer);
  if(Date.now()>=eventRetryDeadline){preferredRecordingSecond=null;toast('Video ancora non disponibile. Riprova tra poco.');return}
  eventRetryTimer=setTimeout(()=>{if(page==='recordings'&&preferredRecordingSecond!=null)loadRecordings()},3000);
}

document.querySelectorAll('.nav').forEach(el=>el.addEventListener('click',()=>setPage(el.dataset.page)));
$('ai-enabled').addEventListener('change',event=>saveAI({enabled:event.target.checked}));
$('ai-max-cameras').addEventListener('change',event=>saveAI({max_cameras:Number(event.target.value)}));
$('events-cam').addEventListener('change',()=>loadEvents());
$('events-label').addEventListener('change',()=>loadEvents());
$('events-refresh').addEventListener('click',()=>loadEvents());
$('events-more').addEventListener('click',()=>loadEvents(true));
$('detail-close').addEventListener('click',()=>closeCameraDetail());
$('detail-prev').addEventListener('click',()=>changeDetailCamera(-1));
$('detail-next').addEventListener('click',()=>changeDetailCamera(1));
$('detail-reload').addEventListener('click',()=>{if(detailCamId)loadDetailCamera(detailCamId)});
$('detail-audio').addEventListener('click',()=>{
  if(!detailCamId||!detailAudioAvailable)return;
  detailAudioOn=!detailAudioOn;
  const video=$('detail-video');video.muted=!detailAudioOn;
  updateDetailAudioButton();
  const id=detailCamId;
  if(detailAudioOn)video.play().catch(()=>{if(detailCamId===id)detailStatus('Tocca il video per attivare l’audio')});
});
$('detail-recordings').addEventListener('click',()=>{if(!detailCamId)return;preferredRecordingCam=detailCamId;closeCameraDetail(false);setPage('recordings')});
$('detail-fullscreen').addEventListener('click',async()=>{
  try{if(document.fullscreenElement)await document.exitFullscreen();else await $('camera-detail').requestFullscreen()}
  catch{toast('Schermo intero non disponibile in questo browser')}
});
document.addEventListener('fullscreenchange',()=>{$('detail-fullscreen').textContent=document.fullscreenElement?'⛶ Esci da schermo intero':'⛶ Schermo intero'});
$('detail-video').addEventListener('playing',()=>{if(detailCamId){$('detail-video').style.opacity='1';detailStatus('')}});
$('detail-stage').addEventListener('click',()=>{if(detailCamId)$('detail-video').play().catch(()=>detailStatus('Riproduzione non disponibile'))});
document.addEventListener('keydown',event=>{
  if(!detailCamId)return;
  if(event.key==='Escape'&&!document.fullscreenElement){event.preventDefault();closeCameraDetail()}
  if(event.key==='ArrowLeft'){event.preventDefault();changeDetailCamera(-1)}
  if(event.key==='ArrowRight'){event.preventDefault();changeDetailCamera(1)}
});
function toggleMenu(force) {
  const open=force===undefined?!document.body.classList.contains('menu-open'):force;
  document.body.classList.toggle('menu-open',open);$('nav-backdrop').hidden=!open;
  $('menu-button').setAttribute('aria-expanded',String(open));showControls();
}
$('menu-button').addEventListener('click',()=>toggleMenu());
$('nav-backdrop').addEventListener('click',()=>toggleMenu(false));
$('edge-hotspot').addEventListener('mouseenter',()=>{if(page==='live')document.body.classList.add('nav-hover')});
$('sidebar').addEventListener('mouseleave',()=>document.body.classList.remove('nav-hover'));
$('setup-button').addEventListener('click',()=>setPage('settings'));
function setBudget(value){budget=Number(value);if(![4,8,16].includes(budget))budget=4;localStorage.setItem('eac-live-budget',String(budget));$('live-budget').value=String(budget);$('settings-live-budget').value=String(budget);renderLive(true)}
$('live-budget').addEventListener('change',()=>setBudget($('live-budget').value));
$('settings-live-budget').addEventListener('change',()=>setBudget($('settings-live-budget').value));
$('refresh-button').addEventListener('click',async()=>{try{state=await request('api/refresh',{method:'POST'});renderSettings();renderLive(true);toast('Telecamere aggiornate')}catch(error){toast(error.message)}});
$('discover-button').addEventListener('click',()=>$('refresh-button').click());
$('camera-form').addEventListener('submit',saveCamera);
$('dialog-close').addEventListener('click',()=>$('camera-dialog').close());
$('storage-form').addEventListener('submit',saveStorage);
for(const id of ['storage-path','retention-days','max-gb'])$(id).addEventListener('input',()=>{storageDirty=true});
$('copy-key').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(state.client_key);toast('Codice copiato')}catch{toast('Seleziona e copia il codice mostrato')}});
$('key-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const key=$('new-client-key').value.trim();
  try{await request('api/client-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key})});$('new-client-key').value='';toast('Codice aggiornato: riconnetti le app già collegate');await update()}
  catch(error){toast(error.message)}
});
$('generate-client-key').addEventListener('click',()=>{
  const bytes=new Uint8Array(12);crypto.getRandomValues(bytes);
  $('new-client-key').value=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('').slice(0,16);
  $('new-client-key').focus();
});
$('show-camera-names').addEventListener('change',async event=>{
  try{await request('api/view',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({show_names:event.target.checked})});await update();toast('Vista aggiornata')}
  catch(error){event.target.checked=!event.target.checked;toast(error.message)}
});
$('record-cam').addEventListener('change',loadRecordings);$('record-day').addEventListener('change',loadSegments);$('record-refresh').addEventListener('click',loadRecordings);
for(const [id,step] of [['record-prev',1],['record-next',-1]])$(id).addEventListener('click',()=>{
  const select=$('record-day'),next=select.selectedIndex+step;
  if(next>=0&&next<select.options.length){select.selectedIndex=next;loadSegments()}
});
$('timeline-preview-video').addEventListener('loadedmetadata',()=>{try{$('timeline-preview-video').currentTime=previewOffset}catch{}});
$('record-timeline').addEventListener('pointerdown',event=>{
  if(event.button!==0)return;
  previewDragging=true;previewRecording(timelineSecond(event));
  event.currentTarget.setPointerCapture(event.pointerId);event.preventDefault();
});
$('record-timeline').addEventListener('pointermove',event=>{
  if(previewDragging||event.pointerType==='mouse')previewRecording(timelineSecond(event));
});
$('record-timeline').addEventListener('pointerup',event=>{
  if(!previewDragging)return;
  previewDragging=false;previewRecording(timelineSecond(event));
  const target=previewSecond;hideRecordingPreview();seekRecording(target);skipTimelineClick=true;
});
$('record-timeline').addEventListener('pointercancel',()=>{previewDragging=false;hideRecordingPreview()});
$('record-timeline').addEventListener('pointerleave',()=>{if(!previewDragging)hideRecordingPreview()});
$('record-timeline').addEventListener('click',event=>{
  if(skipTimelineClick){skipTimelineClick=false;return}
  seekRecording(timelineSecond(event));
});
$('record-timeline').addEventListener('keydown',event=>{
  const current=recordingIndex<0?0:clipClock(recordingSegments[recordingIndex])+$('playback').currentTime;
  const delta=event.key==='ArrowRight'?60:event.key==='ArrowLeft'?-60:event.key==='PageUp'?3600:event.key==='PageDown'?-3600:0;
  if(delta){event.preventDefault();seekRecording(current+delta)}
  else if(event.key==='Home'){event.preventDefault();seekRecording(0)}
  else if(event.key==='End'){event.preventDefault();seekRecording(86399)}
});
$('playback').addEventListener('loadedmetadata',()=>{
  const video=$('playback');
  if(recordingIndex<0)return;
  if(Number.isFinite(video.duration))video.currentTime=Math.min(pendingSeek,Math.max(0,video.duration-.1));
  video.playbackRate=Number($('record-speed').value);
  video.play().catch(()=>{});
});
$('record-audio').addEventListener('click',()=>{
  const video=$('playback');video.muted=!video.muted;
  $('record-audio').textContent=video.muted?'🔇 Muto':'🔊 Audio';
  $('record-audio').setAttribute('aria-pressed',String(!video.muted));
});
$('record-speed').addEventListener('change',()=>{$('playback').playbackRate=Number($('record-speed').value)});
let recordingZoom=1;
function setRecordingZoom(value){recordingZoom=Math.max(1,Math.min(4,value));$('playback').style.transform='scale('+recordingZoom+')'}
$('record-zoom-in').addEventListener('click',()=>setRecordingZoom(recordingZoom+.5));
$('record-zoom-out').addEventListener('click',()=>setRecordingZoom(recordingZoom-.5));
$('record-zoom-reset').addEventListener('click',()=>setRecordingZoom(1));
$('playback').addEventListener('timeupdate',()=>{
  if(recordingIndex>=0)updatePlaybackPosition(clipClock(recordingSegments[recordingIndex])+$('playback').currentTime);
});
$('playback').addEventListener('ended',()=>{
  if(recordingIndex+1<recordingSegments.length)playRecording(recordingIndex+1);
  else toast('Fine delle registrazioni disponibili');
});
$('playback').addEventListener('error',()=>{
  if(recordingIndex<0)return;
  $('player-empty').hidden=false;$('player-empty').querySelector('strong').textContent='Video non riproducibile';
  $('player-empty').querySelector('small').textContent='Aggiorna le registrazioni o scegli un altro orario.';
  $('playback').hidden=true;
});
document.querySelectorAll('[data-jump]').forEach(el=>el.addEventListener('click',()=>$((el.dataset.jump)).scrollIntoView({behavior:'smooth',block:'start'})));
document.addEventListener('mousemove',showControls);document.addEventListener('touchstart',event=>{if(event.touches[0]&&event.touches[0].clientY<85)showControls()},{passive:true});
window.addEventListener('resize',()=>{if(page==='live')renderLive(true)});
setPage(['live','recordings','events','settings'].includes(new URLSearchParams(location.search).get('page'))?new URLSearchParams(location.search).get('page'):'live');update();setInterval(update,10000);setInterval(()=>{if(page==='live')for(const image of document.querySelectorAll('.tile img'))image.src=image.src.replace(/([?&])t=\d+/, '$1t='+Date.now())},30000);
