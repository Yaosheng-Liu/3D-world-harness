const prefixes = Object.freeze({
  point:'点', rectangle:'方框', line:'线段', arrow:'箭头', text:'文字', freehand:'笔迹',livewire:'智能轮廓',
  other:'标记'
});
const validName = name => typeof name === 'string' && name.trim().length > 0 &&
  name.length <= 64 && !/[\u0000-\u001f\u007f]/u.test(name);
// Treat implausibly large persisted values as damaged metadata. Otherwise ++ can
// stop advancing beyond integer precision and trap the reserved-name loop.
const counterValue = value => Number.isSafeInteger(value) && value >= 0 && value <= 1e9 ? value : 0;

// Persist counters per session independently of undo history and the current draft.
// Two passes reserve existing names first, so migrating an old unnamed mark cannot
// steal a later mark's name. Unchanged objects retain their identity for undo/redo.
export function assignAnnotationNames(annotations, counters = {}) {
  const next = Object.fromEntries(Object.keys(prefixes).map(type =>
    [type, counterValue(counters?.[type])]));
  const reserved = new Set();
  const existing = annotations.map(annotation => {
    const name = annotation.name;
    if (!validName(name) || reserved.has(name)) return null;
    reserved.add(name);
    for (const [type, prefix] of Object.entries(prefixes)) {
      if (!name.startsWith(prefix)) continue;
      const suffix = name.slice(prefix.length);
      if (!/^[1-9]\d*$/u.test(suffix)) continue;
      next[type] = Math.max(next[type], counterValue(Number(suffix)));
    }
    return name;
  });
  return {
    annotations:annotations.map((annotation, index) => {
      if (existing[index]) return annotation;
      const type = Object.hasOwn(prefixes, annotation.type) ? annotation.type : 'other';
      let name;
      do { name = prefixes[type] + (++next[type]); } while (reserved.has(name));
      reserved.add(name);
      return {...annotation, name};
    }),
    counters:next
  };
}
