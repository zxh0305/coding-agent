/* Skill selections are immutable per message; scope only controls future messages. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.SkillState = factory();
})(typeof self !== 'undefined' ? self : this, function() {
  function normalize(items) {
    const seen = new Set();
    return (Array.isArray(items) ? items : []).filter(s => s && typeof s.id === 'string' && typeof s.version === 'string' && !seen.has(s.id) && seen.add(s.id)).slice(0, 6).map(s => ({...s, scope:s.scope === 'session' ? 'session' : 'once'}));
  }
  const afterSend = items => normalize(items).filter(s => s.scope === 'session');
  const snapshot = items => normalize(items).map(({id, version, name, body, scope}) => ({id, version, name, body:body || '', scope}));
  const update = (items, skill) => normalize(items).map(s => s.id === skill.id ? {...skill, scope:s.scope} : s);
  function autoId(name, ids) {
    const base = String(name || '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || 'skill';
    let id = base, n = 2;
    while (ids.includes(id)) id = `${base}-${n++}`;
    return id;
  }
  return {normalize, afterSend, snapshot, update, autoId};
});
