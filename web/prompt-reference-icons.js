// Shared visual labels. Exact source identities remain in the reference registry.
export const REFERENCE_ICONS = Object.freeze({object:'🧊',node:'🧩',annotation:'📍',image:'🖼️',pose:'🧍',pose_edit:'✏️',time:'🕒',scene:'🏞️'});
export const REFERENCE_NAMES = Object.freeze({object:'物体',node:'部件',annotation:'标记',image:'图片',pose:'人体',pose_edit:'修正',time:'时间戳',scene:'场景'});
export const ALIAS_PATTERN = '【[^【】\\r\\n]{1,128}】|(?:🧊|🧩|📍|🖼️|🧍|✏️|🕒|🏞️)[1-9]\\d{0,6}(?!\\d)';
export const ALIAS_AT_END = new RegExp('('+ALIAS_PATTERN+')[ \\t]+$','u');

export function iconAliasParts(alias) {
  for(const [kind,icon] of Object.entries(REFERENCE_ICONS)) {
    if(typeof alias!=='string' || !alias.startsWith(icon))continue;
    const digits=alias.slice(icon.length);
    if(/^[1-9]\d{0,6}$/.test(digits) && Number(digits)<=1000000)return {kind,icon,number:Number(digits)};
  }
  return null;
}
export function referenceAliasLabel(alias) {
  const parts=iconAliasParts(alias);
  return parts ? REFERENCE_NAMES[parts.kind]+parts.number : alias?.replace(/^【|】$/gu,'') || '';
}
