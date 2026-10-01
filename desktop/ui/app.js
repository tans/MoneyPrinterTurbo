/* DaisyUI workspace. All displayed tasks, files and results come from the local API. */
"use strict";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const state = {view:"create", params:{}, settings:{}, tasks:[], assets:{}, bucket:"materials", pending:new Map(), previewId:null, selectedId:null, detailStamp:null, currentVideo:null, busy:false};
const sourceNames = {pexels:"Pexels · 在线素材",pixabay:"Pixabay · 在线素材",coverr:"Coverr · 在线素材",local:"我的本地素材",wavespeed:"WaveSpeed · AI 视频",volcengine_seedance:"火山 Seedance · AI 视频",ofox:"OFox · AI 视频",metaso_minimax:"秘塔 MiniMax · AI 视频",muapi:"MuAPI · AI 视频",openai_image:"OpenAI 兼容 · AI 图片",loomloom:"胜算云 · AI 视频"};
const ttsNames = {"azure-tts-v1":"Edge TTS · 免费","azure-tts-v2":"Azure Speech",siliconflow:"硅基流动",gemini:"Gemini TTS",mimo:"小米 MiMo",minimax:"MiniMax TTS",elevenlabs:"ElevenLabs",chatterbox:"Chatterbox",kokoro:"Kokoro",fish_audio:"Fish Audio",voxcpm:"ModelBest VoxCPM",none:"无配音",upload:"使用自备音频"};
const statusNames = {queued:"等待中",running:"生成中",publishing:"发布中",completed:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
const kindNames = {video:"完整视频",audio:"配音",subtitle:"字幕",materials:"画面素材",script:"视频文案",terms:"画面关键词",preview:"配音试听",connection:"模型连接测试",voices:"音色列表",social:"发布文案",loomloom_models:"可用视频模型",loomloom_video_quote:"视频素材报价",loomloom_script_quote:"文案报价",loomloom_scripts:"多方案文案"};
const mainFields = ["video_subject","video_language","paragraph_number","video_script","video_terms","video_source","video_aspect","video_fit_mode","video_concat_mode","voice_name","custom_audio_file","voice_rate","voice_volume","bgm_type","bgm_volume","bgm_file","subtitle_enabled","font_name","subtitle_position","font_size","text_fore_color","video_count","video_clip_duration"];
const labels = {video_transition_mode:"转场效果",video_clip_speed:"画面播放速度",match_materials_to_script:"按文案顺序匹配素材",video_music_prompt:"AI 配乐提示词",sonilo_bgm_prompt:"Sonilo 兼容配乐提示词",subtitle_display_mode:"字幕显示方式",subtitle_animation:"字幕动画",custom_position:"自定义字幕位置（%）",text_background_color:"字幕背景（false 或 #色值）",rounded_subtitle_background:"圆角字幕背景",stroke_color:"字幕描边颜色",stroke_width:"描边宽度",n_threads:"编码线程数",video_script_prompt:"文案提示词",custom_system_prompt:"系统提示词"};
const enumNames = {sentence:"按句显示",word_by_word:"逐词显示",none:"无",pop_spring:"弹跳",Shuffle:"随机转场",FadeIn:"淡入",FadeOut:"淡出",SlideIn:"滑入",SlideOut:"滑出",ZoomIn:"放大",ZoomOut:"缩小"};

async function api(path, options={}) {
  const headers = {...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {...options, headers});
  let result;
  try { result = await response.json(); } catch { result = {detail:"本地服务返回了无法读取的响应"}; }
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(Array.isArray(detail) ? detail.map(item => `${item.loc?.slice(1).join(".")}: ${item.msg}`).join("；") : detail || "请求失败");
  }
  return result;
}
function toast(message, error=false) {
  const item = document.createElement("div"); item.className = `alert ${error?"alert-error":"alert-success"}`;
  item.textContent = message; $("#toast-container").append(item); setTimeout(() => item.remove(), 5500);
}
function guarded(action) { return async (...args) => { try { await action(...args); } catch(error) { toast(error.message, true); } }; }
function confirmAction(title, description, button="确认并继续") {
  return new Promise(resolve => {
    const modal = $("#confirm-modal");
    $("#confirm-title").textContent=title; $("#confirm-description").textContent=description; $("#confirm-ok").textContent=button;
    const done = answer => { modal.close(); resolve(answer); };
    $("#confirm-ok").onclick = () => done(true); $("#confirm-cancel").onclick = () => done(false);
    modal.oncancel = event => { event.preventDefault(); done(false); }; modal.showModal();
  });
}
const sizeLabel = bytes => bytes < 1024*1024 ? `${(bytes/1024).toFixed(0)} KB` : `${(bytes/1024/1024).toFixed(1)} MB`;
const isActive = task => ["queued","running","publishing"].includes(task.status);
const badge = task => `<span class="badge ${task.status==="completed"?"badge-success":isActive(task)?"badge-info":task.status==="failed"?"badge-error":"badge-ghost"} badge-soft">${statusNames[task.status]||task.status}</span>`;
function setView(view) {
  if (!["create","assets","history","settings"].includes(view)) return;
  state.view=view; $$(".view").forEach(el=>el.classList.toggle("hidden",el.id!==`view-${view}`));
  $$(".sidebar [data-view]").forEach(el=>el.classList.toggle("active",el.dataset.view===view));
  $("#page-name").textContent={create:"创作工作台",assets:"素材库",history:"任务历史",settings:"设置"}[view];
  if(view==="assets") renderAssets(); if(view==="history") renderHistory(); if(view==="settings") refreshCache();
}
function populate(select, values, value, labelFn=x=>x) {
  select.innerHTML=values.map(item=>`<option value="${escapeHTML(typeof item==="string"?item:item.name)}">${escapeHTML(labelFn(item))}</option>`).join("");
  if(value!==undefined && values.some(item=>(typeof item==="string"?item:item.name)===value)) select.value=value;
}
function applyParams(params) {
  state.params = {...state.params,...params};
  for(const key of mainFields) {
    const el=$(`#${key}`); const value=state.params[key];
    if(el.type==="checkbox") el.checked=Boolean(value);
    else el.value=key==="video_terms" && Array.isArray(value) ? value.join(", ") : value??"";
  }
  renderAdvanced(); updateConditional();
}
function readParams() {
  const result={...state.params};
  for(const key of mainFields) {
    const el=$(`#${key}`);
    result[key] = el.type==="checkbox" ? el.checked : el.type==="number" ? Number(el.value) : el.value;
  }
  $$("[data-param]").forEach(el=> {
    let value = el.type==="checkbox" ? el.checked : el.type==="number" ? Number(el.value) : el.value;
    if(el.dataset.nullable==="true" && value==="") value=null;
    if(el.dataset.param==="text_background_color" && ["false","true"].includes(value)) value=value==="true";
    result[el.dataset.param]=value;
  });
  state.params=result; return result;
}
function resolveSchema(schema) {
  const variants=schema.anyOf||[schema];
  return variants.map(item=>item.$ref?state.boot.schema.$defs[item.$ref.split("/").pop()]:item).find(item=>item.type!=="null")||schema;
}
function renderAdvanced() {
  const fields=Object.entries(state.boot.schema.properties).filter(([key])=>!mainFields.includes(key) && key!=="video_materials");
  $("#advanced-fields").innerHTML=fields.map(([key,raw])=> {
    const schema=resolveSchema(raw), value=state.params[key], title=labels[key]||key;
    const shared=`data-param="${key}" class="${schema.type==="boolean"&&key!=="text_background_color"?"toggle toggle-primary toggle-sm":"input w-full"}"`;
    let control;
    if(schema.enum) control=`<select data-param="${key}" data-nullable="true" class="select w-full">${schema.enum.map(option=>`<option value="${escapeHTML(option??"")}" ${option===value?"selected":""}>${escapeHTML(enumNames[option]||option||"无转场")}</option>`).join("")}</select>`;
    else if(schema.type==="boolean" && key!=="text_background_color") control=`<input ${shared} type="checkbox" ${value?"checked":""}>`;
    else if(key.includes("prompt")) control=`<textarea data-param="${key}" class="textarea w-full" rows="3" maxlength="${schema.maxLength||8000}">${escapeHTML(value)}</textarea>`;
    else control=`<input ${shared} type="${["integer","number"].includes(schema.type)?"number":"text"}" step="${schema.type==="integer"?"1":"0.1"}" value="${escapeHTML(value??"")}">`;
    return `<label><span class="field-label">${escapeHTML(title)}</span>${control}</label>`;
  }).join("");
}
function updateConditional() {
  const source=$("#video_source").value, tts=$("#tts-provider").value;
  $("#local-materials").classList.toggle("hidden",source!=="local");
  $("#selected-materials").textContent=state.params.video_materials?.length ? `已选择 ${state.params.video_materials.length} 份素材` : "尚未选择素材";
  $("#paid-source-note").classList.toggle("hidden",!["wavespeed","volcengine_seedance","ofox","metaso_minimax","muapi","openai_image","loomloom"].includes(source));
  $("#loomloom-options").classList.toggle("hidden",source!=="loomloom");
  $("#voice-field").classList.toggle("hidden",["none","upload"].includes(tts));
  $("#audio-upload-field").classList.toggle("hidden",tts!=="upload");
  $("#preview-voice").disabled=tts==="upload";
  $("#voxcpm-options").classList.toggle("hidden",tts!=="voxcpm");
  $("#bgm_file").classList.toggle("hidden",$("#bgm_type").value!=="custom");
  $("#script-count").textContent=`${$("#video_script").value.length} 字`;
  $("#preview-aspect").textContent=$("#video_aspect").value;
  $("#video-preview").classList.toggle("landscape",$("#video_aspect").value==="16:9");
  $("#video-preview").classList.toggle("square",$("#video_aspect").value==="1:1");
}
async function saveDraft() {
  readParams(); const result=await api("/api/settings",{method:"PUT",body:JSON.stringify({ui:{desktop_params:state.params,tts_server:$("#tts-provider").value}})});
  state.settings=result;
}
let draftTimer;
function scheduleDraft() { clearTimeout(draftTimer); draftTimer=setTimeout(()=>guarded(saveDraft)(),800); }
function jobOptions() {
  const options={};
  if($("#tts-provider").value==="voxcpm" && $("#reference-audio").value) {
    options.voxcpm_reference_audio=$("#reference-audio").value;
    if($("#reference-text").value.trim()) { options.voxcpm_prompt_audio=options.voxcpm_reference_audio; options.voxcpm_prompt_text=$("#reference-text").value; }
  }
  options.model_id=$("#loomloom-model").value.trim();
  options.scene_prompts=$("#loomloom-scenes").value.split("\n").map(x=>x.trim()).filter(Boolean);
  return options;
}
async function submit(kind, extras={}, action=kind) {
  clearTimeout(draftTimer); await saveDraft();
  const body={kind,params:state.params,options:jobOptions(),preview_task_id:state.previewId,...extras};
  const task=await api("/api/tasks",{method:"POST",body:JSON.stringify(body)});
  state.pending.set(task.id,{action,extras}); state.selectedId=task.id; state.detailStamp=null;
  toast(`${kindNames[kind]||"任务"}已加入队列`); await refreshTasks(); return task;
}
async function generate() {
  if(state.submitting) return;
  state.submitting=true; $("#generate-video").disabled=true;
  try {
    readParams(); const kind=$("#output-stage").value;
    if(state.params.video_source==="loomloom" && ["video","materials"].includes(kind)) {
      await submit("loomloom_video_quote",{},`quote:${kind}`); return;
    }
    const paid=["wavespeed","volcengine_seedance","ofox","metaso_minimax","muapi","openai_image"].includes(state.params.video_source)||["sonilo","elevenlabs"].includes(state.params.bgm_type);
    const publish=kind==="video" && state.settings.app.upload_post_enabled && state.settings.app.upload_post_auto_upload;
    if((paid && ["video","materials"].includes(kind))||publish) {
      if(!await confirmAction("确认生成任务",`${paid?"所选 AI 素材或配乐服务会按调用消耗额度。":""}${publish?"视频完成后将自动发布到设置中的平台。":""}取消本地任务后，已提交的远端任务可能仍会继续。`)) return;
    }
    await submit(kind,{confirm_charge:true,confirm_publish:Boolean(publish)});
  } finally { state.submitting=false; $("#generate-video").disabled=false; }
}
async function handleResult(task,pending) {
  const result=task.payload;
  if(task.status!=="completed") { toast(result.error||"任务未完成",true); return; }
  const action=pending.action;
  if(action==="script") { applyParams({video_script:result.script}); await saveDraft(); }
  if(action==="terms") { applyParams({video_terms:result.terms}); await saveDraft(); }
  if(action==="preview") {
    state.previewId=task.id; $("#voice-preview").src=`/api/tasks/${task.id}/files/audio.mp3`;
    $("#voice-preview").classList.remove("hidden"); $("#voice-duration").textContent=`${Number(result.duration).toFixed(1)} 秒`;
  }
  if(action==="voices") {
    const voices=result.voices||[]; $("#voice-list").innerHTML=voices.map(v=>`<option value="${escapeHTML(v)}"></option>`).join("");
    if(voices.length && !voices.includes($("#voice_name").value)) { $("#voice_name").value=voices[0]; await saveDraft(); }
    toast(`读取到 ${voices.length} 个音色，可输入自定义音色 ID`);
  }
  if(action==="connection") toast(`模型连接成功 · ${Number(result.elapsed).toFixed(2)} 秒`);
  if(action==="loomloom_models") {
    $("#loomloom-models").innerHTML=result.models.map(item=>`<option value="${escapeHTML(item.model_id)}">${escapeHTML(item.display_name)}</option>`).join("");
    $("#loomloom-model").value=result.default_model_id; toast(`读取到 ${result.models.length} 个可用模型`);
  }
  if(action.startsWith("quote:")) {
    const quote=result.quote, nextKind=action.slice(6);
    const publish=nextKind==="video" && state.settings.app.upload_post_enabled && state.settings.app.upload_post_auto_upload;
    const description=`本次包含 ${quote.task_count} 项生成任务，报价 ${quote.estimated_buyer_payable_amount} ${quote.currency}。${publish?"成片将自动发布到设置中的平台。":""}参数或账号变化后需要重新询价。`;
    if(await confirmAction("确认服务报价",description,"确认费用并生成")) await submit(nextKind,{...pending.extras,quote_task_id:task.id,confirm_charge:true,confirm_publish:Boolean(publish)},nextKind);
  }
  if(action==="loomloom_scripts") {
    state.candidates=result.candidates||[];
    $("#script-results").innerHTML=state.candidates.map((item,index)=>`<div class="candidate-item"><div class="flex justify-between mb-2"><strong>方案 ${index+1}</strong><button data-candidate="${index}" class="btn btn-ghost btn-xs text-primary">使用此文案</button></div>${escapeHTML(item.script)}</div>`).join("");
    if(result.errors?.length) toast(`${result.errors.length} 个方案失败，已保留成功结果`,true);
  }
  if(action==="video") await showVideo(task.id);
}
async function refreshTasks() {
  if(state.busy) return;
  state.busy=true;
  try {
    state.tasks=await api("/api/tasks");
    $("#task-count").textContent=state.tasks.length;
    const active=state.tasks.filter(isActive); $("#footer-status").textContent=active.length?`${active.length} 个任务正在执行或排队`:"所有更改保存在本机";
    renderActive(active[0]); if(state.view==="history") renderHistory();
    const finished=[];
    for(const task of state.tasks) if(state.pending.has(task.id)&&!isActive(task)) {
      const pending=state.pending.get(task.id); state.pending.delete(task.id); finished.push([task,pending]);
    }
    if(state.selectedId && state.view==="history") await showTask(state.selectedId);
    for(const [task,pending] of finished) await handleResult(task,pending);
  } finally { state.busy=false; }
}
function renderActive(task) {
  const card=$("#active-task-card"); card.classList.toggle("hidden",!task); if(!task) return;
  card.innerHTML=`<div class="flex justify-between mb-3"><h2>${escapeHTML(kindNames[task.kind])}</h2>${badge(task)}</div><p class="text-xs mb-4">${escapeHTML(task.title)}</p><progress class="progress progress-primary w-full" value="${Number(task.payload.progress)||0}" max="100"></progress><div class="flex justify-between items-center mt-3"><span class="muted">${Number(task.payload.progress)||0}%</span><button data-cancel="${task.id}" class="btn btn-ghost btn-xs">取消任务</button><button data-task="${task.id}" class="btn btn-ghost btn-xs">查看日志 ↗</button></div>`;
}
function renderHistory() {
  const search=$("#history-search").value.toLowerCase(), filter=$("#history-status").value;
  const tasks=state.tasks.filter(task=>task.title.toLowerCase().includes(search)&&(!filter||(filter==="active"?isActive(task):filter==="failed"?["failed","interrupted"].includes(task.status):task.status===filter)));
  $("#history-list").innerHTML=tasks.length?tasks.map(task=>`<button class="history-row ${state.selectedId===task.id?"selected":""}" data-task="${task.id}"><span class="history-icon">${task.kind==="video"?"▷":"✦"}</span><div class="min-w-0 flex-1"><div class="history-title truncate">${escapeHTML(task.title)}</div><span class="muted">${escapeHTML(kindNames[task.kind]||task.kind)} · ${new Date(task.created*1000).toLocaleString()}</span></div>${badge(task)}</button>`).join(""):`<div class="empty-state"><span class="empty-symbol">◷</span><h3>这里会记录你的创作</h3><p>生成一条文案、配音或视频后，就能在这里找到它。</p></div>`;
}
async function showTask(id) {
  const task=await api(`/api/tasks/${id}`);
  if(state.detailStamp===`${id}:${task.updated}`) return;
  state.detailStamp=`${id}:${task.updated}`;
  const video=task.files.find(file=>file.name.startsWith("final-")&&file.mime==="video/mp4")||task.files.find(file=>file.mime==="video/mp4");
  $("#task-detail").innerHTML=`<div class="flex justify-between gap-4 mb-3"><h2>${escapeHTML(task.title)}</h2>${badge(task)}</div><p class="muted mb-5">${escapeHTML(kindNames[task.kind]||task.kind)} · ${new Date(task.created*1000).toLocaleString()}</p>${isActive(task)?`<progress class="progress progress-primary w-full mb-4" value="${Number(task.payload.progress)||0}" max="100"></progress>`:""}${task.payload.error?`<div class="alert alert-error mb-4">${escapeHTML(task.payload.error)}</div>`:""}${task.payload.cross_post_error?`<div class="alert alert-warning mb-4">发布失败：${escapeHTML(task.payload.cross_post_error)}</div>`:""}${video?`<video controls preload="metadata" src="${escapeHTML(video.url)}"></video>`:""}${task.payload.script?`<details class="mt-4"><summary class="field-label">生成文案</summary><pre class="result-text">${escapeHTML(task.payload.script)}</pre></details>`:""}${task.payload.quote?`<div class="alert alert-info mt-4">报价：${escapeHTML(task.payload.quote.estimated_buyer_payable_amount)} ${escapeHTML(task.payload.quote.currency)}</div>`:""}<div class="flex gap-2 my-5"><button data-restore="${id}" class="btn btn-outline btn-sm">恢复参数</button>${isActive(task)?`<button data-cancel="${id}" class="btn btn-ghost btn-sm text-error">取消任务</button>`:`<button data-delete-task="${id}" class="btn btn-ghost btn-sm text-error">删除任务</button>`}</div>${task.files.map(file=>`<div class="file-row"><span class="truncate">${escapeHTML(file.name)} <small class="muted">${sizeLabel(file.size)}</small></span><button data-export-task="${id}" data-export-name="${escapeHTML(file.name)}" class="btn btn-ghost btn-xs">导出 ↗</button></div>`).join("")}<details class="mt-5" ${isActive(task)?"open":""}><summary class="field-label">运行日志</summary><pre class="task-log">${escapeHTML(task.logs.join("\n")||"尚无运行日志")}</pre></details>`;
}
async function showVideo(id) {
  const task=await api(`/api/tasks/${id}`), file=task.files.find(file=>file.name.startsWith("final-")&&file.mime==="video/mp4")||task.files.find(file=>file.mime==="video/mp4");
  if(!file) return;
  const poster=task.files.find(item=>item.name==="preview.jpg");
  state.currentVideo={id,name:file.name}; $("#video-preview").innerHTML=`<video controls preload="metadata" ${poster?`poster="${escapeHTML(poster.url)}"`:""} src="${escapeHTML(file.url)}"></video>`;
  $("#video-preview video").addEventListener("error",()=> {
    $("#preview-title").innerHTML=`<span>当前 WebView 无法播放此编码</span> <button class="btn btn-ghost btn-xs" data-open-task="${id}" data-open-name="${escapeHTML(file.name)}">使用系统播放器 ↗</button>`;
  });
  $("#preview-title").textContent=task.title; $("#export-current").disabled=false;
}
async function exportFile(id,name) {
  if(window.pywebview?.api) { const path=await window.pywebview.api.export_file(id,name); if(path) toast("已导出视频或文件"); }
  else { const link=document.createElement("a"); link.href=`/api/tasks/${id}/files/${encodeURIComponent(name)}?download=true`; link.download=name; link.click(); }
}
async function loadAssets() {
  const buckets=["materials","audio","music","fonts","reference"];
  await Promise.all(buckets.map(async bucket=>state.assets[bucket]=await api(`/api/assets/${bucket}`)));
  $("#asset-count").textContent=state.assets.materials.length;
  const populatePaths=(id,bucket,placeholder)=> { const el=$(id), saved=state.params[el.id]||el.value; el.innerHTML=`<option value="">${placeholder}</option>`+state.assets[bucket].map(item=>`<option value="${escapeHTML(item.path)}">${escapeHTML(item.name)}</option>`).join(""); el.value=saved; };
  populate($("#font_name"),state.assets.fonts,state.params.font_name,item=>item.name);
  populatePaths("#custom_audio_file","audio","请选择配音文件"); populatePaths("#bgm_file","music","请选择背景音乐"); populatePaths("#reference-audio","reference","不使用参考音频");
  if(state.view==="assets") renderAssets();
}
function renderAssets() {
  const items=state.assets[state.bucket]||[];
  $("#asset-summary").textContent=`${items.length} 份素材 · ${sizeLabel(items.reduce((sum,item)=>sum+item.size,0))}`;
  $("#asset-grid").innerHTML=items.length?items.map((item,index)=> {
    const selected=state.bucket==="materials" && (state.params.video_materials||[]).some(material=>material.url===item.path);
    const suffix=item.name.split(".").pop().toLowerCase();
    let media=["jpg","jpeg","png","bmp"].includes(suffix)?`<img loading="lazy" src="${escapeHTML(item.url)}" alt="${escapeHTML(item.name)}">`:["mp4","mov","mkv","webm"].includes(suffix)?`<video preload="metadata" muted src="${escapeHTML(item.url)}"></video>`:state.bucket==="fonts"?"Aa":"♫";
    return `<article class="asset-card ${selected?"selected":""}"><div class="asset-media">${media}</div><div class="asset-info"><div class="asset-name" title="${escapeHTML(item.name)}">${escapeHTML(item.name)}</div><div class="asset-meta"><span class="muted">${sizeLabel(item.size)}</span><div><button data-use-asset="${index}" class="btn btn-ghost btn-xs text-primary">${selected?"移出视频":"用于创作"}</button><button data-delete-asset="${index}" class="btn btn-ghost btn-xs" aria-label="删除素材">×</button></div></div></div></article>`;
  }).join(""):`<div class="empty-state"><span class="empty-symbol">▦</span><h3>添加第一份素材</h3><p>导入本地文件后，就可以在创作中使用。</p><button data-import="${state.bucket}" class="btn btn-primary btn-sm mt-5">＋ 导入素材</button></div>`;
}
let uploadBucket;
async function importAssets(bucket) {
  if(window.pywebview?.api) { const files=await window.pywebview.api.pick_files(bucket); if(files?.length) { await loadAssets(); toast(`已导入 ${files.length} 份素材`); } }
  else { uploadBucket=bucket; $("#upload-input").value=""; $("#upload-input").accept=bucket==="fonts"?".ttf,.otf,.ttc":bucket==="materials"?".mp4,.mov,.mkv,.webm,.jpg,.jpeg,.png,.bmp":"audio/*"; $("#upload-input").click(); }
}
function settingField(section,key,title,defaultValue="") {
  const value=state.settings[section]?.[key]??defaultValue, secret=/key|token|password/.test(key);
  const data=`data-section="${section}" data-key="${key}"`;
  const control=Array.isArray(value)?`<textarea ${data} data-array="true" class="textarea setting-field w-full" rows="2" placeholder="每行一个 API Key">${escapeHTML(value.join("\n"))}</textarea>`:typeof value==="boolean"?`<input ${data} class="toggle toggle-primary setting-field" type="checkbox" ${value?"checked":""}>`:`<input ${data} class="input setting-field w-full" type="${secret?"password":typeof value==="number"?"number":"text"}" value="${escapeHTML(value)}" autocomplete="off">`;
  return `<label><span class="field-label">${escapeHTML(title)}</span>${control}</label>`;
}
function renderLLM() {
  const id=$("#llm-provider").value, spec=state.boot.providers.find(item=>item.provider_id===id);
  if(!spec) return;
  const fields=[];
  if(spec.show_api_key) fields.push(settingField("app",`${id}_api_key`,"API Key"));
  if(spec.requires_model_name) fields.push(settingField("app",`${id}_model_name`,"模型",spec.default_model));
  if(spec.show_base_url) fields.push(settingField("app",`${id}_base_url`,"API 地址",spec.default_base_url||spec.service_endpoints?.[0]?.base_url||""));
  for(const field of spec.extra_fields) fields.push(settingField("app",`${id}_${field.config_suffix}`,field.label_key,field.default_value));
  $("#llm-fields").innerHTML=fields.join("");
}
function renderMaterialSettings() {
  const prefix=$("#material-provider").value;
  const keys=Object.keys(state.settings.app).filter(key=>key.startsWith(prefix+"_"));
  $("#material-fields").innerHTML=keys.map(key=>settingField("app",key,key.replace(prefix+"_","").replaceAll("_"," "))).join("")||`<p class="muted">本地素材在素材库管理，无需 API Key。</p>`;
}
function renderTtsSettings() {
  let provider=$("#tts-provider").value, section=provider;
  if(provider==="azure-tts-v2") section="azure";
  if(provider==="minimax") section="minimax_tts";
  const fields=Object.keys(state.settings[section]||{}).map(key=>settingField(section,key,key.replaceAll("_"," ")));
  if(["gemini","mimo"].includes(provider)) Object.keys(state.settings.app).filter(key=>key.startsWith(provider+"_")).forEach(key=>fields.push(settingField("app",key,key.replaceAll("_"," "))));
  fields.push(`<label><span class="field-label">字幕生成方式</span><select data-section="app" data-key="subtitle_provider" class="select setting-field w-full"><option value="edge" ${state.settings.app.subtitle_provider!=="whisper"?"selected":""}>TTS 时间戳 · 快速</option><option value="whisper" ${state.settings.app.subtitle_provider==="whisper"?"selected":""}>本地 Whisper · 首次下载模型</option></select></label>`);
  for(const key of Object.keys(state.settings.whisper)) fields.push(settingField("whisper",key,`Whisper ${key.replaceAll("_"," ")}`));
  $("#tts-settings").innerHTML=fields.join("");
}
function collectSettings() {
  const changes={app:{llm_provider:$("#llm-provider").value},ui:{tts_server:$("#tts-provider").value}};
  $$(".setting-field").forEach(el=> {
    changes[el.dataset.section] ||= {};
    changes[el.dataset.section][el.dataset.key]=el.type==="checkbox"?el.checked:el.dataset.array?el.value.split("\n").map(x=>x.trim()).filter(Boolean):el.type==="number"?Number(el.value):el.value;
  }); return changes;
}
async function saveSettings() {
  state.settings=await api("/api/settings",{method:"PUT",body:JSON.stringify(collectSettings())});
  $("#settings-json").value=JSON.stringify(state.settings,null,2); toast("设置已保存");
}
async function refreshCache() { try { const data=await api("/api/cache"); $("#cache-stats").innerHTML=`${sizeLabel(data.total_size)} <small>· ${data.file_count} 个文件</small>`; } catch(error) { toast(error.message,true); } }

document.addEventListener("click",guarded(async event=> {
  const button=event.target.closest("button"); if(!button) return;
  const data=button.dataset;
  if(data.view) setView(data.view);
  if(data.bucket) { state.bucket=data.bucket; $$("[data-bucket]").forEach(el=>el.classList.toggle("tab-active",el===button)); renderAssets(); }
  if(data.import) await importAssets(data.import);
  if(data.task) { state.selectedId=data.task; state.detailStamp=null; setView("history"); renderHistory(); await showTask(data.task); }
  if(data.cancel && await confirmAction("取消这条任务？","本地生成会停止，已提交到云端的生成或发布可能继续。","取消任务")) { await api(`/api/tasks/${data.cancel}/cancel`,{method:"POST"}); await refreshTasks(); }
  if(data.restore) { const task=await api(`/api/tasks/${data.restore}`); applyParams(task.params); setView("create"); await saveDraft(); toast("已恢复生成参数；参考音频需重新选择"); }
  if(data.deleteTask && await confirmAction("删除任务与生成文件？","该任务的本地生成文件也会删除。","删除任务")) { await api(`/api/tasks/${data.deleteTask}`,{method:"DELETE"}); state.selectedId=null; state.detailStamp=null; $("#task-detail").innerHTML=""; await refreshTasks(); }
  if(data.exportTask) await exportFile(data.exportTask,data.exportName);
  if(data.openTask) { if(window.pywebview?.api) await window.pywebview.api.open_file(data.openTask,data.openName); else await exportFile(data.openTask,data.openName); }
  if(data.candidate!==undefined) { const item=state.candidates[Number(data.candidate)]; applyParams({video_script:item.script,video_terms:item.video_terms}); await saveDraft(); toast("已应用所选方案"); }
  if(data.useAsset!==undefined) {
    const item=state.assets[state.bucket][Number(data.useAsset)]; readParams();
    if(state.bucket==="materials") { const materials=state.params.video_materials||[]; state.params.video_materials=materials.some(entry=>entry.url===item.path)?materials.filter(entry=>entry.url!==item.path):[...materials,{provider:"local",url:item.path,duration:0}]; state.params.video_source="local"; }
    if(state.bucket==="audio") { state.params.custom_audio_file=item.path; $("#tts-provider").value="upload"; }
    if(state.bucket==="music") { state.params.bgm_file=item.path; state.params.bgm_type="custom"; }
    if(state.bucket==="fonts") state.params.font_name=item.name;
    applyParams(state.params); renderAssets(); await saveDraft(); toast("已更新创作素材");
  }
  if(data.deleteAsset!==undefined && await confirmAction("删除这份素材？","历史任务仍保留原参数；再次生成时可能需要重新选择素材。","删除素材")) {
    const item=state.assets[state.bucket][Number(data.deleteAsset)]; await api(`/api/assets/${state.bucket}/${encodeURIComponent(item.name)}`,{method:"DELETE"});
    state.params.video_materials=(state.params.video_materials||[]).filter(entry=>entry.url!==item.path);
    if(state.params.custom_audio_file===item.path) state.params.custom_audio_file="";
    if(state.params.bgm_file===item.path) { state.params.bgm_file=""; state.params.bgm_type=""; }
    await loadAssets(); applyParams(state.params); await saveDraft();
  }
}));
$("#view-create").addEventListener("input",event=> { if(event.target.matches("input,select,textarea")) { readParams(); updateConditional(); scheduleDraft(); } });
$("#tts-provider").addEventListener("change",guarded(async()=> {
  const provider=$("#tts-provider").value; state.params.custom_audio_file="";
  state.params.voice_name=provider==="none"?"none":provider==="azure-tts-v1"?"zh-CN-XiaoxiaoNeural-Female":provider==="azure-tts-v2"?"zh-CN-XiaoxiaoNeural-V2-Female":$("#voice_name").value;
  applyParams(state.params); renderTtsSettings(); await saveDraft();
  if(!["none","upload"].includes(provider)) await submit("voices",{options:{provider}},"voices");
}));
$("#generate-script").onclick=guarded(()=>submit("script"));
$("#generate-terms").onclick=guarded(()=>submit("terms"));
$("#preview-voice").onclick=guarded(()=>submit("preview"));
$("#generate-video").onclick=guarded(generate);
$("#refresh-voices").onclick=guarded(()=>submit("voices",{options:{provider:$("#tts-provider").value}},"voices"));
$("#refresh-models").onclick=guarded(()=>submit("loomloom_models"));
$("#script-candidates").onclick=guarded(()=>submit("loomloom_script_quote",{options:{candidate_count:3,duration_seconds:60}},"quote:loomloom_scripts"));
$("#export-current").onclick=guarded(()=>exportFile(state.currentVideo.id,state.currentVideo.name));
$("#refresh-history").onclick=guarded(refreshTasks); $("#history-search").oninput=renderHistory; $("#history-status").onchange=renderHistory;
$("#import-assets").onclick=guarded(()=>importAssets(state.bucket));
$("#upload-input").onchange=guarded(async event=> { for(const file of event.target.files) { const body=new FormData(); body.append("file",file); await api(`/api/assets/${uploadBucket}`,{method:"POST",body}); } await loadAssets(); toast("素材已导入"); });
$("#llm-provider").onchange=renderLLM; $("#material-provider").onchange=renderMaterialSettings;
$("#save-settings").onclick=guarded(saveSettings);
$("#test-connection").onclick=guarded(async()=> { await saveSettings(); await submit("connection"); });
$("#apply-settings-json").onclick=guarded(async()=> { state.settings=await api("/api/settings",{method:"PUT",body:JSON.stringify(JSON.parse($("#settings-json").value))}); renderLLM(); renderTtsSettings(); renderMaterialSettings(); toast("高级配置已保存"); });
$("#clean-cache").onclick=guarded(async()=> { if(await confirmAction("清理过期缓存？","清理 30 天前的在线素材缓存，保留自备素材和任务产物。","清理缓存")) { const result=await api("/api/cache/clean",{method:"POST"}); await refreshCache(); toast(`已清理 ${result.deleted_count} 个文件`); } });
$("#open-folder").onclick=guarded(async()=> { if(window.pywebview?.api) await window.pywebview.api.open_data_directory(); else toast(state.boot.data_directory); });
$("#open-legacy").onclick=guarded(async()=> { if(window.pywebview?.api) { await saveDraft(); toast("正在启动兼容工作台…"); await window.pywebview.api.open_legacy_workspace(); } else toast("请在桌面窗口中打开兼容工作台"); });
$("#theme-toggle").onclick=()=> { document.documentElement.dataset.theme=document.documentElement.dataset.theme==="dark"?"mpt":"dark"; };
$("#export-preset").onclick=guarded(async()=> {
  readParams(); const params={...state.params};
  delete params.video_materials; delete params.custom_audio_file; delete params.bgm_file;
  if(["custom","preset"].includes(params.bgm_type)) params.bgm_type="";
  const payload={format:"mpt-desktop-preset",version:1,params};
  if(window.pywebview?.api) { if(await window.pywebview.api.export_preset(payload)) toast("预设已保存"); }
  else { const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:"application/json"})); const link=document.createElement("a"); link.href=url; link.download="mpt-preset.json"; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); }
});
$("#import-preset").onclick=guarded(async()=> {
  if(window.pywebview?.api) { const payload=await window.pywebview.api.import_preset(); if(payload) { const params=await api("/api/preset",{method:"POST",body:JSON.stringify(payload)}); applyParams(params); await saveDraft(); toast("预设已导入"); } }
  else $("#preset-input").click();
});
$("#preset-input").onchange=guarded(async event=> { const file=event.target.files[0]; if(!file) return; if(file.size>1024*1024) throw new Error("预设最大 1 MB"); const params=await api("/api/preset",{method:"POST",body:await file.text()}); applyParams(params); await saveDraft(); toast("预设已导入"); event.target.value=""; });
document.addEventListener("keydown",guarded(async event=> { if((event.ctrlKey||event.metaKey)&&event.key==="Enter" && state.view==="create" && !$("#confirm-modal").open) { event.preventDefault(); await generate(); } }));

async function boot() {
  state.boot=await api("/api/bootstrap"); state.settings=state.boot.settings; state.params=state.boot.params;
  const uiFont=state.boot.fonts.find(font=>font.name==="NotoSansCJKsc-Regular.otf");
  if(uiFont) { const style=document.createElement("style"); style.textContent=`@font-face{font-family:MPTNoto;src:url("${uiFont.url}");font-display:swap}body{font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",MPTNoto,"PingFang SC","Microsoft YaHei",sans-serif}`; document.head.append(style); }
  $("#version").textContent=`v${state.boot.version}`; $("#runtime-status").textContent=state.boot.ffmpeg_ready?"生成环境已就绪":"请检查 FFmpeg";
  $("#runtime-status").classList.add(state.boot.ffmpeg_ready?"badge-success":"badge-warning"); $("#data-path").textContent=state.boot.data_directory;
  populate($("#video_source"),state.boot.sources,state.params.video_source,id=>sourceNames[id]||id);
  populate($("#tts-provider"),[...state.boot.tts_providers,"upload"],state.params.custom_audio_file?"upload":state.params.voice_name==="none"?"none":state.settings.ui.tts_server||"azure-tts-v1",id=>ttsNames[id]||id);
  $("#voice-list").innerHTML=state.boot.voices.map(value=>`<option value="${escapeHTML(value)}"></option>`).join("");
  populate($("#llm-provider"),state.boot.providers.map(spec=>spec.provider_id),state.settings.app.llm_provider,id=>state.boot.providers.find(spec=>spec.provider_id===id).default_label);
  populate($("#material-provider"),state.boot.sources,state.params.video_source,id=>sourceNames[id]||id);
  await loadAssets(); applyParams(state.params); renderLLM(); renderTtsSettings(); renderMaterialSettings();
  $("#settings-json").value=JSON.stringify(state.settings,null,2); await refreshTasks();
  const latest=state.tasks.find(task=>task.kind==="video"&&task.status==="completed"); if(latest) await showVideo(latest.id);
  setInterval(()=>guarded(refreshTasks)(),1000);
}
boot().catch(error=> { $("#boot-error").classList.remove("hidden"); $("#boot-error").textContent=`工作台连接失败：${error.message}。请重新启动桌面程序。`; });
