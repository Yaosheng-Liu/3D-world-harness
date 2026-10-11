// Keep editable prose in a native textarea while preserving exact source tokens
// outside it. Only registered, complete aliases are expanded for submission.
import {REFERENCE_ICONS,ALIAS_PATTERN,ALIAS_AT_END,iconAliasParts} from './prompt-reference-icons.js';
const TOKEN_SCAN = /\[\[(?:object|node|annotation|image|pose|pose_edit|time|scene):[^\[\]\r\n]{1,512}\]\]/g;
const ALIAS_SCAN = new RegExp(ALIAS_PATTERN,'gu');
const DEFAULT_NAMES = {object:'物体',node:'部件',annotation:'标记',image:'图片',pose:'人体',pose_edit:'修正',time:'时间戳',scene:'场景'};
const NUMBERED_NAMES = {...DEFAULT_NAMES};
const segmenter = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter(undefined,{granularity:'grapheme'}) : null;
const characters = (value) => segmenter ? [...segmenter.segment(value)].map(item=>item.segment) : Array.from(value);

function tokenKind(token) {
  if (typeof token !== 'string' || token.length > 512) return null;
  if (/^\[\[(object|annotation|image):[A-Za-z0-9_-]{1,64}\]\]$/.test(token)) return token.slice(2,token.indexOf(':'));
  if (/^\[\[node:[A-Za-z0-9_-]{1,64}:\d+(?:\/\d+)*\]\]$/.test(token)) return 'node';
  if (/^\[\[pose:[0-9a-f]{32}:[0-9a-f]{32}\]\]$/.test(token)) return 'pose';
  if (/^\[\[pose_edit:[0-9a-f]{32}\]\]$/.test(token)) return 'pose_edit';
  if (/^\[\[scene:[0-9a-f]{32}\]\]$/.test(token)) return 'scene';
  if (/^\[\[time:t[1-9]\d{0,6}\]\]$/.test(token)) return 'time';
  return null;
}
function displayKind(type,kind) {
  return type==='node' && ['object','node'].includes(kind) ? kind : type;
}
function aliasKind(type,alias) {
  if(type!=='node')return type;
  const explicit=iconAliasParts(alias)?.kind || /^【(物体|部件)[1-9]\d{0,6}】$/u.exec(alias || '')?.[1];
  return displayKind(type,explicit==='物体'?'object':explicit==='部件'?'node':explicit);
}
function bindingKey(token,kind) { return token+'\u0000'+kind; }
function cleanText(value,limit=1024) {
  return typeof value === 'string' && value.length <= limit && !/[\x00-\x1f\x7f]/.test(value) && value.trim() ? value : '';
}
function shortName(value) {
  const name=(cleanText(value) || '').replace(/[【】\[\]]/g,'').replace(/\s+/g,' ').trim();
  const chars=characters(name);
  return chars.length > 12 ? chars.slice(0,11).join('')+'…' : name;
}
function readableName(value,kind) {
  const name=cleanText(value)?.trim() || '';
  const opaque=/^(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|\d{6,})$/i.test(name) ||
    /^(?:object|node|annotation|image|pose|pose_edit|ref|img|id)[_-][0-9a-f_-]{16,}$/i.test(name) || tokenKind(name);
  return !name || opaque ? DEFAULT_NAMES[kind] : name;
}
function validAlias(alias) {
  if(iconAliasParts(alias))return true;
  if (typeof alias !== 'string' || alias.length > 130 || !/^【[^【】\[\]\x00-\x1f\x7f]+】$/u.test(alias)) return false;
  const body=alias.slice(1,-1);
  return body === body.trim() && characters(body).length <= 12;
}
function imageNumber(alias) {
  const number=/^【图([1-9]\d{0,6})】$/.exec(alias || '');
  return number && Number(number[1]) <= 1000000 ? Number(number[1]) : 0;
}
function timeNumber(alias) {
  const number=/^【时间戳([1-9]\d{0,6})】$/.exec(alias || '');
  return number && Number(number[1]) <= 1000000 ? Number(number[1]) : 0;
}
function timeText(value) {
  return typeof value==='string' && value.length<=10000 && value.trim() &&
    !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value) ? value : '';
}
function standalone(text,index,length) {
  return !['[','【'].includes(text[index-1]) && ![']','】'].includes(text[index+length]);
}
function outsideToken(text,index) {
  return text.lastIndexOf('[[',index) <= text.lastIndexOf(']]',index);
}

export function createPromptReferenceText({resolve=()=>null,numbered=false,icons=false}={}) {
  numbered ||= icons;
  const byBinding=new Map(), byAlias=new Map(), byTimeKey=new Map();
  const nextNumbers=new Map();
  let legacyAliases=new Map();
  let nextImage=1,nextTime=1,nextTimeToken=1,reserved=new Set();
  function numberedAlias(kind,alias) {
    if(icons) {
      const parts=iconAliasParts(alias);
      return parts?.kind===kind ? parts.number : 0;
    }
    const match=new RegExp('^【'+NUMBERED_NAMES[kind]+'([1-9]\\d{0,6})】$').exec(alias || '');
    return match && Number(match[1])<=1000000 ? Number(match[1]) : 0;
  }
  function preferredAlias(kind,alias) {
    if(!icons)return alias;
    const number=new RegExp('^【'+NUMBERED_NAMES[kind]+'([1-9]\\d{0,6})】$').exec(alias || '');
    return number && Number(number[1])<=1000000 ? REFERENCE_ICONS[kind]+number[1] : alias;
  }
  function acceptsAlias(kind,alias) {
    return numbered ? !!numberedAlias(kind,alias) :
      (kind!=='image' || !!imageNumber(alias)) && (kind!=='time' || !!timeNumber(alias));
  }

  function allocate(kind,name,preferred) {
    if (validAlias(preferred) && !byAlias.has(preferred) && acceptsAlias(kind,preferred)) {
      if(numbered)nextNumbers.set(kind,Math.max(nextNumbers.get(kind) || 1,numberedAlias(kind,preferred)+1));
      if (kind === 'image') nextImage=Math.max(nextImage,imageNumber(preferred)+1);
      if (kind === 'time') nextTime=Math.max(nextTime,timeNumber(preferred)+1);
      return preferred;
    }
    if(numbered) {
      let number=nextNumbers.get(kind) || 1,alias;
      do {alias=icons ? REFERENCE_ICONS[kind]+number++ : `【${NUMBERED_NAMES[kind]}${number++}】`;} while(byAlias.has(alias) || reserved.has(alias));
      nextNumbers.set(kind,number);return alias;
    }
    if (kind === 'image') {
      let alias;
      do { alias=`【图${nextImage++}】`; } while (byAlias.has(alias) || reserved.has(alias));
      return alias;
    }
    if(kind==='time') {
      let alias;
      do {alias=`【时间戳${nextTime++}】`;} while(byAlias.has(alias) || reserved.has(alias));
      return alias;
    }
    const base=shortName(name) || DEFAULT_NAMES[kind];
    let alias=`【${base}】`, number=1;
    while (byAlias.has(alias) || reserved.has(alias)) {
      const suffix=String(++number);
      alias='【'+characters(base).slice(0,Math.max(1,12-suffix.length)).join('')+suffix+'】';
    }
    return alias;
  }
  function register(label,token,{preferred,metadata,useResolver=true,kind}={}) {
    const type=tokenKind(token);
    if (!type) return null;
    // Display identity can differ for one exact node path. Keep both aliases
    // alive so native textarea undo never loses its original source binding.
    const shownKind=displayKind(type,kind),key=bindingKey(token,shownKind);
    const previous=byBinding.get(key);
    let descriptor=metadata || null;
    if (useResolver && type!=='time') {
      try { descriptor=resolve(token,cleanText(label) || previous?.label || '') || null; }
      catch { descriptor=null; } // Source disappearance must not erase its token.
    }
    if (!descriptor || typeof descriptor !== 'object' || Array.isArray(descriptor)) descriptor={};
    const capturedTime=type==='time' ? previous?.timeText || timeText(descriptor.timeText) : null;
    if(type==='time' && !capturedTime)return null;
    const name=readableName(cleanText(descriptor.name) || previous?.name,shownKind);
    const fullLabel=capturedTime || cleanText(descriptor.label) || cleanText(label) || previous?.label || '';
    const labels=[...new Set([...(previous?.labels || []),...(Array.isArray(descriptor.labels) ? descriptor.labels.slice(-16) : []),
      cleanText(label),fullLabel].filter(item=>cleanText(item)))].slice(-16);
    const extra={};
    for (const [key,value] of Object.entries(descriptor)) {
      if (['__proto__','prototype','constructor'].includes(key)) continue;
      if (typeof value === 'boolean' || typeof value === 'number' && Number.isFinite(value) || value === null ||
          typeof value === 'string' && value.length <= 512 && !value.startsWith('data:')) extra[key]=value;
    }
    const entry={...previous,...extra,token,label:fullLabel,
      alias:previous?.alias || allocate(shownKind,name,preferred),
      kind:shownKind,displayKind:shownKind,
      title:capturedTime || cleanText(descriptor.title,2048) || fullLabel || previous?.title || name,name,labels};
    if(type==='time') {
      entry.timeText=capturedTime;
      entry.timeKey=previous?.timeKey || cleanText(descriptor.timeKey,32768) || capturedTime;
      byTimeKey.set(entry.timeKey+'\u0000'+capturedTime,entry);
      nextTimeToken=Math.max(nextTimeToken,Number(/^\[\[time:t(\d+)\]\]$/.exec(token)[1])+1);
    }
    byBinding.set(key,entry); byAlias.set(entry.alias,entry);
    return entry;
  }
  function remember(label,token,{kind}={}) { return register(label,token,{kind}); }
  function rememberTime(text,key=text) {
    const captured=timeText(text),sourceKey=cleanText(key,32768);
    if(!captured || !sourceKey)return null;
    const previous=byTimeKey.get(sourceKey+'\u0000'+captured);
    if(previous)return previous;
    return register(captured,`[[time:t${nextTimeToken++}]]`,{useResolver:false,
      metadata:{kind:'time',name:'时间戳',timeText:captured,timeKey:sourceKey}});
  }

  function compact(value) {
    let text=String(value ?? '');
    if(legacyAliases.size) {
      // Apply only at draft restoration. Keeping old aliases active afterwards
      // could steal a newly allocated numbered alias from a different source.
      const legacy=legacyAliases;legacyAliases=new Map();
      text=text.replace(ALIAS_SCAN,(alias,index)=>legacy.has(alias) &&
        standalone(text,index,alias.length) && outsideToken(text,index) ? legacy.get(alias) : alias);
    }
    let output='', cursor=0;
    for (const match of text.matchAll(TOKEN_SCAN)) {
      const token=match[0];
      if (!tokenKind(token) || !standalone(text,match.index,token.length) ||
          match.index > 0 && !outsideToken(text,match.index-1)) continue;
      let prefix=text.slice(cursor,match.index);
      // Inspect original text, not already compacted output: two adjacent raw
      // tokens must never mistake the first generated alias for the second's.
      const beforeAlias=ALIAS_AT_END.exec(prefix);
      const preferred=beforeAlias && validAlias(beforeAlias[1]) &&
        standalone(prefix,beforeAlias.index,beforeAlias[1].length) ? beforeAlias[1] : null;
      const bound=byAlias.get(preferred);
      const kind=bound?.token===token ? bound.displayKind : aliasKind(tokenKind(token),preferred);
      const entry=register('',token,{kind,preferred:preferredAlias(kind,preferred)});
      if(!entry)continue;
      if (preferred) prefix=prefix.slice(0,beforeAlias.index);
      else {
        const label=[...entry.labels].sort((a,b)=>b.length-a.length).find(item=>{
          if (!prefix.endsWith(item+' ')) return false;
          const start=prefix.length-item.length-1;
          return start === 0 || /[\s，。；：、！？,.;:!?（）()“”‘’"'《》]/u.test(prefix[start-1]);
        });
        if (label) prefix=prefix.slice(0,-label.length-1);
      }
      output+=prefix+entry.alias;
      cursor=match.index+token.length;
    }
    const normalized=output+text.slice(cursor);
    output='';cursor=0;
    // Saved drafts contain expanded time prose. Only remove the exact payload
    // belonging to its registered alias; never scan arbitrary user timestamps.
    for(const match of aliasMatches(normalized)) {
      const entry=byAlias.get(match[0]),end=match.index+match[0].length;
      const space=/^[ \t]+/.exec(normalized.slice(end));
      if(tokenKind(entry.token)!=='time' || !space || !normalized.slice(end+space[0].length).startsWith(entry.timeText))continue;
      output+=normalized.slice(cursor,match.index)+entry.alias;
      cursor=end+space[0].length+entry.timeText.length;
    }
    return output+normalized.slice(cursor);
  }
  function aliasMatches(text) {
    return [...text.matchAll(ALIAS_SCAN)].filter(match=>byAlias.has(match[0]) &&
      standalone(text,match.index,match[0].length) && outsideToken(text,match.index));
  }
  function ranges(value) {
    const text=String(value ?? '');
    return aliasMatches(text).map(match=>({start:match.index,end:match.index+match[0].length,entry:byAlias.get(match[0])}));
  }
  function expand(value) {
    const text=String(value ?? '');
    let output='',cursor=0;
    for (const match of aliasMatches(text)) {
      const entry=byAlias.get(match[0]), end=match.index+match[0].length;
      const alreadyExpanded=text.slice(end).match(/^[ \t]+/);
      const payload=tokenKind(entry.token)==='time'?entry.timeText:entry.token;
      const hasPayload=alreadyExpanded && text.slice(end+alreadyExpanded[0].length).startsWith(payload);
      output+=text.slice(cursor,match.index)+entry.alias+(hasPayload ? '' : ' '+payload);
      cursor=end;
    }
    return output+text.slice(cursor);
  }
  function entries(value) {
    const text=compact(value), found=[], seen=new Set();
    for (const match of aliasMatches(text)) {
      const entry=byAlias.get(match[0]);
      const key=bindingKey(entry.token,entry.displayKind);
      if (!seen.has(key)) { seen.add(key); found.push(entry); }
    }
    return found;
  }
  function remove(value,token,alias) {
    const text=compact(value);
    if(alias!==undefined && byAlias.get(alias)?.token!==token)return text;
    let output='',cursor=0;
    for (const match of aliasMatches(text)) {
      if (byAlias.get(match[0]).token!==token || alias!==undefined && match[0]!==alias) continue;
      output+=text.slice(cursor,match.index); cursor=match.index+match[0].length;
    }
    return output+text.slice(cursor);
  }
  function exportRecords() {
    return [...byBinding.values()].map(({token,label,alias,kind,displayKind,title,name,labels,timeText,timeKey})=>({token,label,alias,kind,displayKind,title,name,labels:[...labels],
      ...(kind==='time'?{timeText,timeKey}:{})}));
  }
  function reset(records=[]) {
    byBinding.clear();byAlias.clear();byTimeKey.clear();nextNumbers.clear();legacyAliases.clear();nextImage=1;nextTime=1;nextTimeToken=1;
    const usable=(Array.isArray(records) ? records : []).slice(0,4096).filter(record=>record &&
      typeof record === 'object' && tokenKind(record.token) && (tokenKind(record.token)!=='time' || timeText(record.timeText)));
    const owners=new Map();
    for (const record of usable) {
      const kind=displayKind(tokenKind(record.token),record.displayKind ?? record.kind);
      const alias=preferredAlias(kind,record.alias);
      if (validAlias(alias) && acceptsAlias(kind,alias) && !owners.has(alias)) {
        owners.set(alias,bindingKey(record.token,kind));
      }
    }
    reserved=new Set(owners.keys());
    const oldOwners=new Set();
    for (const record of usable) {
      const kind=displayKind(tokenKind(record.token),record.displayKind ?? record.kind),key=bindingKey(record.token,kind);
      if (byBinding.has(key)) continue;
      const alias=preferredAlias(kind,record.alias);
      const preferred=owners.get(alias) === key ? alias : null;
      const entry=register(record.label,record.token,{kind,metadata:record,preferred,useResolver:false});
      if(numbered && validAlias(record.alias) && !oldOwners.has(record.alias)) {
        oldOwners.add(record.alias);
        if(entry.alias!==record.alias)legacyAliases.set(record.alias,entry.alias);
      }
    }
    reserved.clear();
  }
  return {compact,expand,entries,ranges,remember,rememberTime,reset,exportRecords,remove};
}
