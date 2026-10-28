/* Read-only, on-demand owner notebook. No chat polling or send receipts. */
(() => {
  const $ = id => document.getElementById(id);
  const kinds = {idea:'想法', question:'疑问', reflection:'感受与反思', later:'想留到聊天时说'};
  const states = {active:'进行中', paused:'暂放', completed:'已完成', retired:'不再继续'};
  let offset = 0, next = null, busy = false;
  const element = (tag, text, className) => {
    const node = document.createElement(tag); node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const stamp = value => new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}).format(new Date(value*1000));
  function fill(id, rows, render, empty) {
    $(id).replaceChildren(...(rows.length ? rows.map(render) : [element('p', empty, 'hint')]));
  }
  async function load() {
    if (busy) return;
    busy = true; $('status').className = ''; $('status').textContent = '正在读取…';
    const query = new URLSearchParams({offset:String(offset)});
    if ($('scope').value) query.set('scope', $('scope').value);
    if ($('day').value) query.set('day', $('day').value);
    try {
      const response = await fetch('/api/personal?'+query, {credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error('暂时无法读取小本（HTTP '+response.status+'），请刷新或返回主页重试。');
      const data = await response.json();
      $('scope').replaceChildren(...data.scopes.map(row => {
        const option = element('option', row.label); option.value = row.scope; option.selected = row.scope === data.scope; return option;
      }));
      next = data.next_offset;
      fill('notes', data.notes, row => {
        const card = element('article', '');
        card.append(element('span', kinds[row.kind] || row.kind, 'tag'), element('p', row.text, 'note-text'),
          element('p', stamp(row.updated_at)+' · '+(row.shared_at ? '已通过聊天送达' : '尚未通过聊天分享')+' · 第'+row.revision+'版', 'meta'));
        if (row.source_ref) card.append(element('small', '来源引用：'+row.source_ref));
        return card;
      }, '这段时间还没有留下随笔。');
      fill('interests', data.interests, row => {
        const card = element('article', '');
        card.append(element('span', states[row.status] || row.status, 'tag'), element('h3', row.description));
        for (const [label, value] of [['为什么感兴趣', row.motivation], ['最近进展', row.progress], ['下一步', row.next_step]]) {
          if (value) card.append(element('p', label+'：'+value));
        }
        card.append(element('small', stamp(row.updated_at))); return card;
      }, '暂时没有登记兴趣，也可以先休息。');
      fill('artifacts', data.artifacts, row => {
        const card = element('article', '');
        const link = element('a', row.name); link.href = '/api/personal/artifact?'+new URLSearchParams({scope:data.scope, id:row.id});
        link.setAttribute('download', ''); card.append(link, element('p', row.media_type+' · 第'+row.revision+'版', 'meta')); return card;
      }, '还没有登记文件。');
      $('status').textContent = data.available ? '已更新 · 只读查看不会触发模型或标记为已分享。' : '尚未登记可查看的本人私聊。';
    } catch (error) { $('status').className = 'error'; $('status').textContent = error.message; }
    finally { busy = false; $('newer').disabled = offset === 0; $('older').disabled = next == null; }
  }
  $('filters').addEventListener('submit', event => { event.preventDefault(); offset=0; load(); });
  $('scope').addEventListener('change', () => { offset=0; load(); });
  $('all-days').addEventListener('click', () => { $('day').value=''; offset=0; load(); });
  $('newer').addEventListener('click', () => { offset=Math.max(0, offset-20); load(); });
  $('older').addEventListener('click', () => { if (next != null) { offset=next; load(); } });
  load();
})();
