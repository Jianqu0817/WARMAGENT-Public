/* Local orchestration only. No model calls, chat polling or credential forms. */
(() => {
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const titles = {home:'把一切，接回同一个入口。',channels:'连接日常，也连接行动。',settings:'按自己的方式，慢慢配置。',runs:'每一步，都有下文。',docs:'入口与说明，都在这里。'};
  let data = null, config = null, selectedRun = null, refreshing = false;
  const pending = new Set();
  const node = (tag, text, cls) => { const item=document.createElement(tag); if(text!=null)item.textContent=text;if(cls)item.className=cls;return item; };
  function notice(text, error=false) { $('notice').hidden=false; $('notice').textContent=text; $('notice').className=error?'error':''; }
  async function api(path, body) {
    const response=await fetch(path,{method:body===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',
      headers:body===undefined?{}:{'Content-Type':'application/json','X-Console-CSRF':csrf},body:body===undefined?undefined:JSON.stringify(body)});
    const result=await response.json(); if(!response.ok)throw new Error(result.error||'本机操作暂时不可用，请刷新。');return result;
  }
  function view(name) {
    document.querySelectorAll('.view').forEach(item=>{item.hidden=item.id!=='view-'+name;});
    document.querySelectorAll('[data-view]').forEach(item=>item.classList.toggle('active',item.dataset.view===name));
    $('page-title').textContent=titles[name];
    if(name==='settings'&&!config)loadSettings();
  }
  async function start(id) {
    if(pending.has(id))return; pending.add(id); actionButtons();
    try {const result=await api('/api/actions',{id});selectedRun=result.id;notice(result.message);await refresh();view('runs');await showLog();}
    catch(error){notice(error.message,true);}finally{pending.delete(id);actionButtons();}
  }
  function actionButtons(){document.querySelectorAll('[data-action]').forEach(button=>{button.disabled=pending.has(button.dataset.action);});}
  function cards(target, group) {
    const rows=data.actions.filter(row=>row.group===group);
    $(target).replaceChildren(...rows.map(row=>{
      const card=node('article',null,'action-card');card.append(node('h3',row.label),node('p',row.description));
      const button=node('button',row.impact==='live'?'启动正式模式':row.impact==='install'?'安装 / 构建':'打开 / 执行');button.dataset.action=row.id;card.append(button);
      if(row.cmd)card.append(node('code',row.cmd));return card;
    }));
  }
  function paint() {
    $('project-name').textContent=data.project;
    const services=[['聊天管理',data.admin_port],['微信后端',30001],['消息Reader',5031],['本地聊天',8769]];
    $('health').replaceChildren(...services.map(([label,port])=>{const on=data.ports[String(port)],card=node('div',null,'health-card'+(on?' on':''));card.append(node('small',label+' · '+port),node('strong',on?'端口已监听':'尚未监听'));return card;}));
    $('page-links').replaceChildren(...data.links.map(link=>{const a=node('a',link.label+(link.available?' ↗':' · 未启动'),'page-link'+(link.available?'':' off'));a.href=link.url;a.target='_blank';a.rel='noopener noreferrer';return a;}));
    cards('chat-actions','聊天模式');cards('channel-actions','渠道与浏览器');cards('observe-actions','观察与资料');cards('setup-actions','配置与安装');cards('maintenance-actions','维护与检查');
    $('footer').textContent=data.note;
    $('edit-files').replaceChildren(...data.config_files.map(row=>{const button=node('button',row.label+(row.exists?'':' · 新建/补齐'),'quiet');button.dataset.edit=row.id;return button;}));
    renderRuns();renderDocs();actionButtons();
  }
  function renderRuns() {
    const active=data.runs.filter(row=>row.state==='running').length;$('run-count').textContent=String(active);$('task-summary').textContent=active?'运行中 '+active+' 项':'本次共 '+data.runs.length+' 项';
    $('run-list').replaceChildren(...(data.runs.length?data.runs.map(row=>{
      const button=node('button',null,'run-item'+(row.id===selectedRun?' selected':''));button.dataset.run=row.id;
      const label={running:'运行中',completed:'入口已退出',failed:'入口报错'}[row.state];
      button.append(node('strong',row.label),node('small',label+(row.interactive?' · 独立窗口':'')+' · '+new Date(row.started*1000).toLocaleTimeString()));return button;
    }):[node('p','还没有执行操作。','empty')]));
  }
  async function refresh(){if(refreshing)return;refreshing=true;try{data=await api('/api/state');paint();if(data.config_error)notice(data.config_error,true);}catch(error){notice(error.message,true);}finally{refreshing=false;}}
  async function showLog(){if(!selectedRun)return;try{const result=await api('/api/runs/'+encodeURIComponent(selectedRun));$('output').textContent=(result.truncated?'[仅显示末尾输出]\n':'')+result.text;$('output-title').textContent=data.runs.find(row=>row.id===selectedRun)?.label||'操作输出';renderRuns();}catch(error){notice(error.message,true);}}
  async function loadSettings(){try{config=await api('/api/settings');renderSettings();}catch(error){notice(error.message,true);}}
  function renderSettings(){
    const labels={bridge:'主配置 · 账号、活动与设备',engine:'模型与预算',wechat:'微信接收',qq:'QQ接收'};
    $('settings-form').replaceChildren(...Object.keys(labels).map(file=>{
      const panel=node('form',null,'config-group');panel.dataset.file=file;panel.append(node('h3',labels[file]));
      if(config.errors[file]){panel.append(node('p',config.errors[file],'hint'));return panel;}
      const fields=node('div',null,'fields');
      config.fields.filter(spec=>spec.file===file).forEach(spec=>{
        const label=node('label',spec.label,'field');let input;
        if(spec.kind==='lines'){input=node('textarea');input.value=spec.value.join('\n');}
        else if(spec.kind==='choice'){input=node('select');spec.choices.forEach(value=>{const option=node('option',value);option.value=value;input.append(option);});input.value=spec.value;}
        else {input=node('input');input.type=spec.kind==='bool'?'checkbox':['int','float'].includes(spec.kind)?'number':'text';if(spec.kind==='bool')input.checked=spec.value;else input.value=spec.value;if(spec.min!==undefined){input.min=spec.min;input.max=spec.max;input.step=spec.kind==='int'?'1':'any';}}
        input.name=spec.id;input.dataset.kind=spec.kind;label.append(input);fields.append(label);
      });
      const save=node('button','保存这一组','primary');save.type='submit';panel.append(fields,save);return panel;
    }));
  }
  async function saveSettings(form){
    const file=form.dataset.file,changes={};
    form.querySelectorAll('[name]').forEach(input=>{const spec=config.fields.find(row=>row.id===input.name);let value=input.type==='checkbox'?input.checked:input.value;
      if(input.dataset.kind==='lines')value=value.split('\n').map(item=>item.trim()).filter(Boolean);
      if(['int','float'].includes(input.dataset.kind))value=Number(value);
      if(JSON.stringify(value)!==JSON.stringify(spec.value))changes[input.name]=value;
    });
    if(!Object.keys(changes).length){notice('这一组没有改动。');return;}
    const button=form.querySelector('[type=submit]');button.disabled=true;
    try{notice((await api('/api/settings',{file,revision:config.revisions[file],changes})).message);await loadSettings();}catch(error){notice(error.message,true);}finally{button.disabled=false;}
  }
  function renderDocs(){
    const search=$('doc-search').value.toLowerCase();
    $('doc-list').replaceChildren(...data.documents.filter(row=>row.title.toLowerCase().includes(search)).map(row=>{const button=node('button',row.title,'doc-item');button.dataset.doc=row.id;return button;}));
    const rows=data.actions.filter(row=>row.cmd&&[row.cmd,row.label].some(text=>text.toLowerCase().includes(search))).map(row=>{const tr=node('tr'),a=node('td');a.append(node('code',row.cmd));const c=node('td'),button=node('button',row.label,'quiet');button.dataset.action=row.id;c.append(button);tr.append(a,node('td',row.group),c);return tr;});
    if(!search||'start.cmd'.includes(search)){const tr=node('tr');tr.append(node('td','Start.cmd'),node('td','打开当前操作台'),node('td','当前页面'));rows.unshift(tr);}
    $('cmd-map').replaceChildren(...rows);
  }
  document.addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.action)start(button.dataset.action);
    if(button.dataset.view)view(button.dataset.view);
    if(button.dataset.run){selectedRun=button.dataset.run;showLog();}
    if(button.dataset.edit){try{notice((await api('/api/edit',{id:button.dataset.edit})).message);}catch(error){notice(error.message,true);}}
    if(button.dataset.doc){try{const doc=await api('/api/document?id='+encodeURIComponent(button.dataset.doc));$('doc-title').textContent=doc.title;$('doc-text').textContent=doc.text;}catch(error){notice(error.message,true);}}
  });
  $('settings-form').addEventListener('submit',event=>{event.preventDefault();saveSettings(event.target);});
  $('refresh').onclick=refresh;$('refresh-log').onclick=showLog;$('load-settings').onclick=loadSettings;$('doc-search').oninput=()=>data&&renderDocs();
  $('read-service-log').onclick=async()=>{try{const result=await api('/api/logs/'+$('service-log').value);selectedRun=null;renderRuns();$('output-title').textContent=$('service-log').selectedOptions[0].textContent;$('output').textContent=(result.truncated?'[仅显示末尾输出]\n':'')+result.text;}catch(error){notice(error.message,true);}};
  refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
