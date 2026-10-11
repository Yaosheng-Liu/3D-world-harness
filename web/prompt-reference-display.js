// Presentation only. The saved note and its model-facing tokens stay intact.
import {REFERENCE_ICONS,REFERENCE_NAMES,ALIAS_PATTERN,iconAliasParts} from './prompt-reference-icons.js';
const TOKEN = /\[\[(object|annotation|node|image|pose|pose_edit|scene):([^\]\r\n]{0,256})(?:\]\]|$)/g;
const ALIAS = /【([^【】\r\n]{1,64})】[ \t]*$/;

function shortName(value) {
  const name=typeof value === 'string' ? value.replace(/[\x00-\x1f\x7f【】]/g,'').replace(/\s+/g,' ').trim() : '';
  if (!name || /^[a-f0-9_-]{24,}$/i.test(name)) return '';
  const chars=Array.from(name);
  return chars.length>16 ? chars.slice(0,15).join('')+'…' : name;
}

function knownLabels(reference) {
  return [reference?.display_label,...(Array.isArray(reference?.display_labels)?reference.display_labels:[]),
    reference?.image?.label,reference?.annotation?.name,reference?.object?.name,
    reference?.scene_node?.node_name,reference?.scene_reference?.name,reference?.label]
    .filter(value=>typeof value==='string' && value.length && !/[\r\n]/.test(value))
    .sort((a,b)=>b.length-a.length);
}

function removeKnownLabel(text,reference) {
  const end=text.replace(/[ \t]+$/,'');
  for (const label of knownLabels(reference)) {
    if (!end.endsWith(label)) continue;
    const start=end.length-label.length;
    // A generated label is separated from preceding prose. Do not consume a
    // similarly named suffix in a user's word, or arbitrary parenthetical text.
    if (start && !/[\s，。！？：；、（(]/.test(end[start-1])) continue;
    return end.slice(0,start);
  }
  return text;
}

function aliasBase(kind,reference) {
  if (kind==='image') return {base:reference?.image?.pane==='reference'?'参考图':reference?.image?.pane==='scene'?'截图':'图片',numbered:true};
  if (kind==='annotation') return {base:shortName(reference?.annotation?.name)||'标记',numbered:!shortName(reference?.annotation?.name)};
  if (kind==='object') return {base:shortName(reference?.object?.name)||'物体',numbered:!shortName(reference?.object?.name)};
  if (kind==='node') return {base:shortName(reference?.scene_node?.node_name)||'部件',numbered:!shortName(reference?.scene_node?.node_name)};
  if (kind==='scene') return {base:'场景',numbered:true};
  return {base:kind==='pose_edit'?'手部修正':'人体',numbered:true};
}

export function compactReferenceMessage(text,inlineReferences=[],{icons=false}={}) {
  if(icons)return compactIconMessage(text,inlineReferences);
  const source=String(text ?? ''), matches=[...source.matchAll(TOKEN)];
  if (!matches.length) return source;
  const metadata=new Map();
  for (const reference of Array.isArray(inlineReferences)?inlineReferences:[]) {
    if (reference && typeof reference.token==='string') metadata.set(reference.token,reference);
  }
  const aliases=new Map(),used=new Set();
  // Reserve explicit aliases before generating legacy labels, including ones
  // that occur later in the note. Two different references never gain one name.
  for (const match of matches) {
    const alias=ALIAS.exec(source.slice(0,match.index));
    if (alias) {aliases.set(match[0],alias[1]);used.add(alias[1]);}
  }
  let result='',position=0;
  for (const match of matches) {
    const token=match[0],reference=metadata.get(token),chunk=source.slice(position,match.index);
    const explicit=ALIAS.exec(chunk);
    if (explicit) result+=chunk.replace(/[ \t]+$/,'');
    else {
      let alias=aliases.get(token);
      if (!alias) {
        const {base,numbered}=aliasBase(match[1],reference);
        let ordinal=numbered?1:0;
        alias=base+(ordinal || '');
        while(used.has(alias)) {ordinal=ordinal?ordinal+1:2;alias=base+ordinal;}
        used.add(alias);aliases.set(token,alias);
      }
      result+=removeKnownLabel(chunk,reference)+'【'+alias+'】';
    }
    position=match.index+token.length;
  }
  return result+source.slice(position);
}

function compactIconMessage(text,inlineReferences) {
  // Old timestamps are presentation labels, not new source registrations.
  const source=String(text ?? '').replace(/【时间戳([1-9]\d{0,6})】/gu,(_,n)=>REFERENCE_ICONS.time+n);
  const matches=[...source.matchAll(TOKEN)],metadata=new Map();
  for(const reference of Array.isArray(inlineReferences)?inlineReferences:[]) {
    if(reference && typeof reference.token==='string')metadata.set(reference.token,reference);
  }
  const explicitPattern=new RegExp('('+ALIAS_PATTERN+')[ \\t]*$','u');
  const aliases=new Map(),used=new Set(),numbers=new Map();
  function displayKind(kind,alias,reference) {
    if(kind!=='node')return kind;
    const explicit=iconAliasParts(alias)?.kind || /^【(物体|部件)[1-9]\d{0,6}】$/u.exec(alias || '')?.[1];
    if(explicit==='object' || explicit==='物体')return 'object';
    if(explicit==='node' || explicit==='部件')return 'node';
    const saved=reference?.displayKind ?? reference?.display_kind ?? reference?.scene_node?.display_kind;
    return saved==='object' ? 'object' : 'node';
  }
  const bindings=matches.map(match=> {
    const token=match[0],explicit=explicitPattern.exec(source.slice(0,match.index));
    const kind=displayKind(match[1],explicit?.[1],metadata.get(token));
    return {match,kind,key:token+'\u0000'+kind,explicit};
  });
  function preferred(kind,alias) {
    const parts=iconAliasParts(alias);
    if(parts?.kind===kind)return alias;
    const match=new RegExp('^【'+REFERENCE_NAMES[kind]+'([1-9]\\d{0,6})】$').exec(alias || '');
    return match && Number(match[1])<=1000000 ? REFERENCE_ICONS[kind]+match[1] : null;
  }
  // Reserve explicit numbers throughout the note before numbering old named
  // labels, so different archived sources cannot acquire the same icon.
  for(const {kind,key,explicit} of bindings) {
    const alias=preferred(kind,explicit?.[1]);
    if(alias && !aliases.has(key) && !used.has(alias)) {aliases.set(key,alias);used.add(alias);}
  }
  let result='',position=0;
  for(const {match,kind,key} of bindings) {
    const token=match[0],chunk=source.slice(position,match.index);
    const explicit=explicitPattern.exec(chunk);
    let alias=aliases.get(key);
    if(!alias) {
      let number=numbers.get(kind) || 1;
      do {alias=REFERENCE_ICONS[kind]+number++;}while(used.has(alias));
      aliases.set(key,alias);used.add(alias);numbers.set(kind,number);
    }
    result+=(explicit ? chunk.slice(0,explicit.index) : removeKnownLabel(chunk,metadata.get(token)))+alias;
    position=match.index+token.length;
  }
  return result+source.slice(position);
}

// Recover the exact former annotation label only from this feedback's frozen
// sources. A current scene or a current reference must not rename old messages.
export function savedReferenceDisplayMetadata(feedback) {
  const references=Array.isArray(feedback?.inline_references)?feedback.inline_references:[];
  return references.map(reference=> {
    const mark=reference.annotation;
    if (!mark?.name) return reference;
    const shot=feedback.scene_snapshots?.find(item=>item.id===mark.snapshot_id);
    const moment=feedback.dynamic_frames?.find(item=>item.id===mark.frame_id);
    const ref=feedback.reference_images?.find(item=>item.id===mark.reference_image_id);
    const capturedImage=feedback.image_refs?.find(item=>item.reference_id===mark.reference_image_id);
    const frameIndex=Number.isInteger(moment?.frame_index)?moment.frame_index:Number.isInteger(mark.frame_index)?mark.frame_index:null;
    const viewName=mark.view_name || moment?.view_name || '';
    const time=Number.isFinite(mark.time_sec)?(viewName?'机位 '+viewName+' · ':'')+
      (frameIndex!==null?'片段第 '+(frameIndex+1)+' 帧 · ':'')+mark.time_sec.toFixed(3)+' s':'';
    const referenceName=ref?.name || capturedImage?.reference_name;
    // Frame names are omitted from older saved packets. Keep their unknown
    // prose rather than guessing where the user's description starts or ends.
    if (mark.pane==='reference' && !ref && !referenceName) return reference;
    if (mark.snapshot_id && !mark.frame_id && !shot) return reference;
    const location=[mark.pane==='reference'?'参考图'+(referenceName?' '+referenceName:''):'',shot?.name || time].filter(Boolean).join(' · ');
    return {...reference,display_label:mark.name+(location?'（'+location+'）':'')};
  });
}
