"use strict";
// Bug controls use the same authenticated API helper and text-only rendering.
let bugsListEpoch=0, bugDetailEpoch=0, bugsNext=null, bugIntakeEpoch=0, bugIntakeWatch=null;
let bugSearchEpoch=0, bugSearchWatch=null;
const bugSearchErrors={login_required:"专用 Project 身份需要重新登录",search_authorization_changed:"查询授权已过期，或配置与模式已变化",search_read_failed:"官方读取失败或结果不符合已核验的接口格式",search_unavailable:"查询服务暂不可用",interrupted_read_limit:"多次读取中断，请重新查询"};
function showBugSearch(value,epoch,area){
  if(epoch!==bugSearchEpoch)return;
  const states={queued:"等待后台查询",running:"正在只读查询飞书项目",succeeded:"查询完成",failed:"查询失败",blocked:"查询已阻止"};
  area.replaceChildren(node("p",`${states[value.state]||"状态待核对"}${value.error_code?" · "+(bugSearchErrors[value.error_code]||"请检查后台记录"):""}`));
  if(["queued","running"].includes(value.state)){
    if(value.authorization_expired)area.append(node("p","本次查询授权已到期，等待后台停止。"));
    else if(value.lease_expired)area.append(node("p","后台读取中断，等待恢复核对。"));
    bugSearchWatch={epoch,id:value.search_id,target:area,busy:false};return;
  }
  if(value.state!=="succeeded"||!value.result)return;
  area.append(node("p",`本页 ${value.result.items.length} 条。各页是独立读取，期间同事修改可能影响结果；此列表不代表修复或验证结论。`));
  for(const item of value.result.items){
    const card=row(item.title,`${item.item_id} · ${item.status.label}`,"飞书项目"),link=node("a","查看官方 Bug");
    link.href=item.url;link.target="_blank";link.rel="noopener noreferrer";
    const intake=node("button","填入导入表单");intake.addEventListener("click",()=>{if(epoch===bugSearchEpoch)loadBugIntakes("",item.url);});
    card.append(link,intake);area.append(card);
  }
  if(value.result.next_after_id!==null){
    const next=node("button","按相同条件查询下一页"),message=node("div");next.disabled=Boolean(value.authorization_expired);
    if(value.authorization_expired)area.append(node("p","查询授权已到期，请重新发起查询。"));
    area.append(next,message);let pending=null,sending=false;
    next.addEventListener("click",async()=>{
      if(epoch!==bugSearchEpoch||sending||next.disabled)return;
      pending??={search_id:value.search_id,request_id:crypto.randomUUID()};sending=true;next.disabled=true;
      try{const result=await api("/api/project-bugs/search-next",pending);if(epoch===bugSearchEpoch)showBugSearch(result,epoch,area);}
      catch(error){if(epoch===bugSearchEpoch){message.replaceChildren(node("p","下一页提交未确认："+error.message));next.textContent="重试同一翻页请求";next.disabled=false;}}
      finally{sending=false;}
    });
  }
}
async function pollBugSearch(){
  const watch=bugSearchWatch;if(!watch||watch.epoch!==bugSearchEpoch||watch.busy||$("console").hidden)return;
  watch.busy=true;
  try{const value=await api("/api/project-bugs/search-status",{search_id:watch.id});
    if(bugSearchWatch!==watch||watch.epoch!==bugSearchEpoch)return;
    bugSearchWatch=null;showBugSearch(value,watch.epoch,watch.target);
  }catch(error){if(bugSearchWatch===watch&&watch.epoch===bugSearchEpoch)watch.target.replaceChildren(node("p","查询进度暂不可读，请重新打开查询记录核对。"));}
  finally{watch.busy=false;}
}
async function loadBugSearch(){
  const epoch=++bugSearchEpoch,area=$("bugs-search");bugSearchWatch=null;area.replaceChildren(node("p","正在读取查询范围…"));
  try{const value=await api("/api/project-bugs/search-options",{});if(epoch===bugSearchEpoch)renderBugSearch(value,epoch,area);}
  catch(error){if(epoch===bugSearchEpoch)area.replaceChildren(node("p","查询入口暂不可用："+error.message));}
}
function renderBugSearch(value,epoch,area){
  area.replaceChildren();const section=node("section",undefined,"note"),scope=node("select"),keyword=node("input"),submit=node("button","只读查询"),message=node("div");
  section.append(node("h3","查找飞书项目 Bug"),node("p","仅查询管理员明确配置的范围，每次查询及翻页授权有效 30 分钟。结果不会自动导入或执行修复。"));
  for(const [index,item] of value.scopes.entries()){const option=node("option",`${item.simple_name} · ${item.type_key} · ${item.item_limit===null?"已配置的整类缺陷":item.item_limit+" 条指定缺陷"}`);option.value=String(index);scope.append(option);}scope.value="0";
  for(const [label,input] of [["查询范围",scope],["标题包含（可留空）",keyword]]){const wrapper=node("label",label);input.setAttribute("aria-label",label);wrapper.append(input);section.append(wrapper);}keyword.value="";keyword.maxLength=200;
  submit.disabled=!value.available||!value.scopes.length;if(submit.disabled)section.append(node("p","尚未配置查询范围，或当前模式禁止读取。链接导入权限不等于列表查询权限。"));
  section.append(submit,message);area.append(section);let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugSearchEpoch||sending||submit.disabled)return;
    if(!pending){const selected=value.scopes[Number(scope.value)];if(!selected)return;pending={simple_name:selected.simple_name,type_key:selected.type_key,keyword:keyword.value.trim(),request_id:crypto.randomUUID()};scope.disabled=keyword.disabled=true;}
    sending=true;submit.disabled=true;
    try{const result=await api("/api/project-bugs/search",pending);if(epoch!==bugSearchEpoch)return;showBugSearch(result,epoch,message);submit.textContent="查询请求已记录";}
    catch(error){if(epoch===bugSearchEpoch){if([400,403,409].includes(error.http_status)){pending=null;scope.disabled=keyword.disabled=false;submit.textContent="只读查询";}else submit.textContent="重试同一查询请求";message.replaceChildren(node("p","提交未确认："+error.message));submit.disabled=false;}}
    finally{sending=false;}
  });
  area.append(node("h4",`最近查询（最多 ${value.history_limit||20} 条）`));
  for(const record of value.history){const open=node("button",`${record.scope.simple_name} · ${record.keyword||"所有标题"} · ${record.created_at}`);open.addEventListener("click",async()=>{
    if(epoch!==bugSearchEpoch)return;bugSearchWatch=null;
    const historyEpoch=++bugSearchEpoch;area.replaceChildren(node("p","正在核对查询记录…"));
    try{const result=await api("/api/project-bugs/search-status",{search_id:record.search_id});if(historyEpoch===bugSearchEpoch)showBugSearch(result,historyEpoch,area);}
    catch(error){if(historyEpoch===bugSearchEpoch)area.replaceChildren(node("p","记录暂不可读："+error.message));}
  });area.append(open);}
}
setInterval(pollBugSearch,5000);
const bugStates={planned:"待计划",running:"执行中",paused:"已暂停",human:"人工处理",blocked:"受阻",succeeded:"本轮执行成功",failed:"失败",cancelled:"已取消",unknown:"结果未知",not_started:"未开始修复",in_progress:"修复中",ready:"补丁已就绪",not_applicable:"不适用",not_run:"未验证",partial:"部分完成",passed:"验证通过",stale:"需重新验证",prepared:"待写回",dispatched:"已发送，待核对",confirmed:"写回已确认",rejected:"远端拒绝",conflict:"存在冲突",satisfied:"当前值已符合，无需写入"};
async function loadBugs(after="") {
  const epoch=++bugsListEpoch;
  $("bugs-next").disabled=true;
  $("bugs-status").textContent="正在读取已绑定的 Bug…";
  try {
    const result=await api("/api/project-bugs/list",{after_id:after});
    if(epoch!==bugsListEpoch)return;
    bugsNext=result.next_cursor;
    $("bugs-next").disabled=!bugsNext;
    $("bugs-list").replaceChildren(...result.items.map(item=>{
      const card=row(item.title,`${item.project_key} · ${item.item_id}`,"已绑定");
      const open=node("button","查看 Bug");
      open.addEventListener("click",async()=>{
        await loadBugDetail(item.bug_id);
        const heading=$("bug-detail").firstElementChild;
        if(heading){
          heading.tabIndex=-1;
          heading.focus({preventScroll:true});
          heading.scrollIntoView({block:"start"});
        }
      });
      card.append(open);return card;
    }));
    $("bugs-status").textContent=result.items.length?`本页 ${result.items.length} 条` : "尚无已绑定的 Bug，可通过链接导入已有缺陷。";
  } catch(error) {
    if(epoch!==bugsListEpoch)return;
    bugsNext=null;$("bugs-list").replaceChildren();
    $("bugs-status").textContent="读取失败："+error.message;
  }
}
function bugValue(value,present=true){return present?JSON.stringify(value):"未读取到";}
function bugFieldValue(key,value,present,snapshot){
  if(present===false)return "未读取到（不能当作空值）";
  if(value===null)return "空值";
  const e=snapshot?.read_evidence||{};
  if(["select","tree-select"].includes(e.field_types?.[key])){
    if(value&&typeof value==="object"&&!Array.isArray(value)&&typeof value.label==="string")return value.label;
    const id=typeof value==="string"?value:value?.value;
    const option=(e.field_options?.[key]||[]).find(o=>o.value===id);
    if(option)return option.label;
  }
  return bugValue(value,present);
}
function renderBugRoles(snapshot,area){
  const membership=snapshot?.read_evidence?.role_membership;
  if(!membership)return;
  const section=node("section",undefined,"note");section.append(node("h3","已读取的流程角色"));
  for(const role of membership.roles||[]){
    const names=role.members_observed?(role.members||[]).map(p=>p.name||p.key).join("、"):"";
    section.append(node("p",`${role.name||role.key}：${role.members_observed?(names||"本次返回人员列表为空"):"本次未取得完整人员信息"}`));
  }
  if(!(membership.roles||[]).length)section.append(node("p","本次未返回角色记录。"));
  section.append(node("p","仅展示本次读取的角色；未返回的角色不能推断为无人负责。角色调整尚未接通，不使用普通字段写回。"));area.append(section);
}
const bugIntakeStates={queued:"等待后台受理",running:"正在核对空间和读取缺陷",succeeded:"已建立本地绑定与读取授权",failed:"导入失败",blocked:"导入已阻止"};
const bugIntakeErrors={login_required:"专用 Project 身份需要重新登录",intake_read_or_identity_failed:"读取失败或空间身份不匹配",intake_authorization_changed:"授权已过期，或配置与模式已变化",newer_snapshot_or_conflict:"已有更新数据，请重新受理",intake_unavailable:"后台受理不可用",interrupted_read_limit:"多次读取中断"};
function showBugIntake(value,area){
  const text=value.authorization_expired?"本次导入授权已到期":value.lease_expired?"读取租约已过期，等待后台核对":bugIntakeStates[value.state]||"状态待核对";
  area.replaceChildren(node("p",`${text}${value.reused?" · 复用已有 Bug 记录":""}${value.error_code?" · "+(bugIntakeErrors[value.error_code]||"请检查后台状态"):""}`));
  if(value.state==="succeeded"&&value.bug_id){const open=node("button","查看导入的 Bug");open.addEventListener("click",()=>loadBugDetail(value.bug_id));area.append(open);}
}
async function pollBugIntake(){
  const watch=bugIntakeWatch;if(!watch||watch.epoch!==bugIntakeEpoch||watch.busy||$("console").hidden)return;
  watch.busy=true;
  try{
    const result=await api("/api/project-bugs/intake-status",{intake_id:watch.id});
    if(bugIntakeWatch!==watch||watch.epoch!==bugIntakeEpoch)return;
    showBugIntake(result,watch.target);
    if(!["queued","running"].includes(result.state)){bugIntakeWatch=null;if(watch.onTerminal)watch.onTerminal(result);}
  }catch(error){if(bugIntakeWatch===watch&&watch.epoch===bugIntakeEpoch)watch.target.replaceChildren(node("p","导入进度暂不可读，请重新打开导入记录核对。"));}
  finally{watch.busy=false;}
}
async function loadBugIntakes(after="",prefill=""){
  const epoch=++bugIntakeEpoch,area=$("bugs-intake");bugIntakeWatch=null;area.replaceChildren(node("p","正在读取导入权限与记录…"));
  try{
    const result=await api("/api/project-bugs/intakes",{after_id:after});if(epoch!==bugIntakeEpoch)return;
    renderBugIntakeForm(result,epoch,area,prefill);
  }catch(error){if(epoch===bugIntakeEpoch)area.replaceChildren(node("p","导入入口暂不可用："+error.message));}
}
function renderBugIntakeForm(value,epoch,area,prefill=""){
  area.replaceChildren();const section=node("section",undefined,"note"),url=node("input"),hours=node("select"),priority=node("select"),submit=node("button","只读导入并建立授权"),message=node("div");
  const labelled=(label,input)=>{const wrapper=node("label",label);input.setAttribute("aria-label",label);wrapper.append(input);section.append(wrapper);};
  section.append(node("h3","通过飞书项目链接导入"),node("p","读取指定缺陷，建立本地任务和对应读取授权。修复任务需在导入后另行提交。"));
  url.type="url";url.value=prefill;labelled("Bug 详情链接",url);
  for(const [key,text] of [[1,"1 小时"],[8,"8 小时"],[24,"24 小时"]]){const o=node("option",text);o.value=String(key);hours.append(o);}hours.value="8";labelled("只读授权有效期（从提交起）",hours);
  for(const key of ["P0","P1","P2","P3"]){const o=node("option",key);o.value=key;priority.append(o);}priority.value="P2";labelled("新建本地任务的排队优先级",priority);
  section.append(node("p","默认本地优先级 P2；已有本地任务保留原设置。飞书项目的优先级保持原值。"));
  submit.disabled=!value.available;if(!value.available)section.append(node("p","管理员尚未配置可导入的空间与类型，或当前模式禁止读取。"));
  section.append(submit,message);area.append(section);
  let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugIntakeEpoch||sending||submit.disabled)return;
    if(!pending){if(!url.value.trim()){message.replaceChildren(node("p","请填写 Bug 详情链接。"));return;}pending={url:url.value.trim(),read_hours:Number(hours.value),local_priority:priority.value,request_id:crypto.randomUUID()};[url,hours,priority].forEach(x=>x.disabled=true);}
    sending=true;submit.disabled=true;message.replaceChildren(node("p","正在记录导入请求…"));
    try{
      const result=await api("/api/project-bugs/intake-link",pending);if(epoch!==bugIntakeEpoch)return;
      showBugIntake(result,message);submit.textContent="导入请求已记录";
      if(["queued","running"].includes(result.state))bugIntakeWatch={epoch,id:result.intake_id,target:message,busy:false};
    }catch(error){if(epoch===bugIntakeEpoch){
      if([400,403,409].includes(error.http_status)){pending=null;[url,hours,priority].forEach(x=>x.disabled=false);message.replaceChildren(node("p","导入未提交："+error.message));submit.textContent="只读导入并建立授权";}
      else{message.replaceChildren(node("p","提交结果待核对："+error.message));submit.textContent="重试同一导入请求";}submit.disabled=false;
    }}finally{sending=false;}
  });
  area.append(node("h4","导入记录"));
  for(const record of value.items){const row=node("section",undefined,"note"),state=node("div");row.append(node("p",record.url));showBugIntake(record,state);row.append(state);
    if(["queued","running"].includes(record.state)){const follow=node("button","跟踪这次导入");follow.addEventListener("click",()=>{if(epoch!==bugIntakeEpoch)return;bugIntakeWatch={epoch,id:record.intake_id,target:state,busy:false};pollBugIntake();});row.append(follow);}area.append(row);
  }
  const next=node("button","下一页导入记录");next.disabled=!value.next_cursor;next.addEventListener("click",()=>{if(epoch===bugIntakeEpoch&&value.next_cursor)loadBugIntakes(value.next_cursor);});area.append(next);
  renderCreateDraftConsole(area);
}
setInterval(pollBugIntake,5000);
function renderBugGrants(value,epoch,area) {
  const section=node("section",undefined,"note"), status=node("p"), list=node("div"),listDetails=node("details"),listSummary=node("summary","已创建的授权（读取中）");
  listDetails.append(listSummary,list);
  section.append(node("h3","持续授权"),node("p","授权只限定允许范围，不会立即执行；远端权限、运行模式和验证要求仍须满足。撤销阻止后续派发，在途操作仍需核对结果。"));
  const refresh=node("button","刷新授权"),next=node("button","下一页授权");
  let cursor=null,readEpoch=0;
  next.disabled=true;
  async function load(after="") {
    const read=++readEpoch;next.disabled=true;status.textContent="正在读取授权…";
    try {
      const response=await api("/api/project-bugs/list-grants",{bug_id:value.bug_id,after_id:after});
      if(epoch!==bugDetailEpoch||read!==readEpoch)return;
      cursor=response.next_cursor;list.replaceChildren();
      const labels={active:"有效",expired:"已到期",revoked:"已撤销"};
      for(const grant of response.items) {
        const card=node("section",undefined,"note"),scope=grant.scope;
        card.append(node("h4",`${labels[grant.status]||grant.status} · ${grant.grant_id}`),node("p",`到期：${grant.expires_at}`),
          node("p",`空间：${scope.project_key} · 类型：${scope.type_key} · Bug：${scope.bug_ids.join("、")}`),
          node("p",`动作：${scope.actions.map(a=>bugGrantActions[a]||a).join("、")}`),
          node("p",`字段：${scope.fields.join("、")||"无"} · 流转：${scope.transitions.join("、")||"无"}`));
        for(const repo of scope.repositories)card.append(node("p",`仓库 ${repo.name} · 节点 ${repo.node} · 分支 ${repo.branches.join("、")} · 路径 ${repo.paths.join("、")}`));
        for(const device of scope.devices)card.append(node("p",`设备 ${device.id} · 节点 ${device.node}`));
        if(grant.can_revoke) {
          const revoke=node("button","撤销此授权");
          revoke.addEventListener("click",async()=>{
            if(epoch!==bugDetailEpoch||read!==readEpoch)return;
            revoke.disabled=true;
            try {
              await api("/api/project-bugs/revoke-grant",{grant_id:grant.grant_id});
              if(epoch===bugDetailEpoch&&read===readEpoch)await load(after);
            } catch(error) {
              if(epoch===bugDetailEpoch&&read===readEpoch){card.append(node("p","撤销结果待核对："+error.message));revoke.disabled=false;}
            }
          });card.append(revoke);
        }
        list.append(card);
      }
      next.disabled=!cursor;status.textContent=response.items.length?`本页 ${response.items.length} 份授权` : "尚无你创建的授权。";
      listSummary.textContent=response.items.length?`查看本页 ${response.items.length} 份授权` : "暂无授权记录";
    } catch(error) {
      if(epoch===bugDetailEpoch&&read===readEpoch){cursor=null;list.replaceChildren();status.textContent="授权读取失败："+error.message;listSummary.textContent="授权记录读取失败";}
    }
  }
  refresh.addEventListener("click",()=>{if(epoch===bugDetailEpoch)load();});
  next.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&cursor)load(cursor);});
  section.append(refresh,next,status,listDetails,node("h4","创建当前 Bug 的授权"));
  section.append(node("p","本表单支持读取、评论和指定字段；流程关闭、代码推送合并和设备操作仍需各自的独立策略。"));
  const controls=[],comment=node("input"),duration=node("select"),message=node("p"),submit=node("button","创建持续授权");
  comment.type="checkbox";comment.checked=false;controls.push(comment,duration);
  const commentLabel=node("label");commentLabel.append(comment,node("span","允许添加评论"));section.append(commentLabel);
  const fields=[];
  const available=Object.keys(value.snapshot?.fields||{}).sort();
  const fieldDetails=node("details"),fieldSummary=node("summary",`选择可改字段（${Math.min(available.length,200)} 个已读取）`);
  fieldDetails.append(fieldSummary,node("p","始终包含读取；勾选下列字段才允许修改。此处为已读取的字段标识，不代表远端写权限。"));
  for(const key of available.slice(0,200)) {
    const input=node("input"),label=node("label");input.type="checkbox";input.checked=false;
    controls.push(input);fields.push({key,input});label.append(input,node("span",key));fieldDetails.append(label);
  }
  if(available.length>200)fieldDetails.append(node("p","仅列出前 200 个字段；未显示的字段不会获得授权。"));
  section.append(fieldDetails);
  for(const [hours,label] of [[1,"1 小时"],[8,"8 小时"],[24,"24 小时"]]) {
    const option=node("option",label);option.value=String(hours);duration.append(option);
  }
  duration.value="8";duration.setAttribute("aria-label","持续授权有效期");
  section.append(node("p",`仅限当前 Bug ${value.item_id}，空间 ${value.project_key}，类型 ${value.type_key}。`),duration,submit,message);
  let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending)return;
    if(!pending) {
      const selected=fields.filter(f=>f.input.checked).map(f=>f.key);
      pending={request_id:crypto.randomUUID(),expires_at:new Date(Date.now()+Number(duration.value)*3600000).toISOString(),
        scope:{host:value.host,project_key:value.project_key,type_key:value.type_key,bug_ids:[value.bug_id],
          actions:["bug.read",...(comment.checked?["bug.comment"]:[]),...(selected.length?["bug.fields"]:[])],
          fields:selected,transitions:[],repositories:[],devices:[]}};
      controls.forEach(input=>input.disabled=true);
    }
    sending=true;submit.disabled=true;message.textContent="正在记录授权…";
    try {
      const grant=await api("/api/project-bugs/issue-grant",pending);
      if(epoch!==bugDetailEpoch)return;
      message.textContent=`授权已记录：${grant.grant_id}；状态 ${{active:"有效",expired:"已到期",revoked:"已撤销"}[grant.status]||"请刷新核对"}；到期 ${grant.expires_at}。未触发任何执行。`;
      submit.textContent="授权已记录";await load();
      if(epoch===bugDetailEpoch)area.dispatchEvent(new Event("bug-grants-changed"));
    } catch(error) {
      if(epoch===bugDetailEpoch){message.textContent="授权结果待核对："+error.message;submit.textContent="重试同一授权请求";submit.disabled=false;}
    } finally {sending=false;}
  });
  area.append(section);load();
}
function renderBugFieldEditor(value,epoch,area){
  const snapshot=value.snapshot,section=node("section",undefined,"note"),status=node("p"),grant=node("select"),field=node("select"),preview=node("button","查看字段差异"),prepare=node("button","准备此字段写回"),message=node("div"),next=node("button","读取下一页授权"),refresh=node("button","重新读取授权"),diff=node("section"),prepareStatus=node("p");
  let proposed=node("textarea");
  section.append(node("h3","准备字段写回"),node("p","支持已观测的文本、单选和关联字段。单选值只使用官方选项；关联项按数字 ID 写入，显示名称只作提示。明确空值与未返回值分别处理。预览只显示本地差异；单独提交准备后仍不会发送到飞书项目。"));
  grant.setAttribute("aria-label","选择字段授权");field.setAttribute("aria-label","要修改的字段");proposed.setAttribute("aria-label","新字段值");
  const grantLabel=node("label","字段写回授权"),fieldLabel=node("label","字段");grantLabel.append(grant);fieldLabel.append(field);section.append(grantLabel,fieldLabel);
  const fields=snapshot&&snapshot.fields&&typeof snapshot.fields==="object"?snapshot.fields:{};
  const evidence=snapshot?.read_evidence||{},names=evidence.field_names&&typeof evidence.field_names==="object"?evidence.field_names:{};
  const unobserved=new Set(Array.isArray(evidence.unobserved_field_keys)?evidence.unobserved_field_keys:[]),attachments=evidence.attachment_fields&&typeof evidence.attachment_fields==="object"?evidence.attachment_fields:{};
  const types=evidence.field_types&&typeof evidence.field_types==="object"?evidence.field_types:{};
  const omitted=new Set(Array.isArray(evidence.omitted_value_field_keys)?evidence.omitted_value_field_keys:[]);
  const options=evidence.field_options&&typeof evidence.field_options==="object"?evidence.field_options:{};
  function isSelect(key){return ["select","tree-select"].includes(types[key])&&Array.isArray(options[key])&&options[key].length>0&&options[key].every(o=>o&&typeof o.value==="string"&&o.value&&typeof o.label==="string")&&new Set(options[key].map(o=>o.value)).size===options[key].length;}
  function relatedKind(key){const type=types[key];if(["workitem_related_select","work_item_related_select"].includes(type))return "single";if(["workitem_related_multi_select","work_item_related_multi_select"].includes(type))return "multi";return "";}
  function relatedId(value){if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).sort().join(",")!=="id,name"||typeof value.name!=="string")return null;const id=value.id;return typeof id==="number"&&Number.isSafeInteger(id)&&id>=1&&id<=2**53-1?String(id):null;}
  function validRelatedSearchId(value){if(typeof value!=="string"||!/^[1-9][0-9]*$/.test(value))return false;const id=Number(value);return Number.isSafeInteger(id)&&id<=2**53-1&&String(id)===value;}
  function validRelatedValue(key){const kind=relatedKind(key),value=fields[key];if(!kind)return false;if(value===null)return true;if(kind==="single")return relatedId(value)!==null;if(!Array.isArray(value)||value.length>20)return false;const ids=value.map(relatedId);return ids.every(id=>id!==null)&&new Set(ids).size===ids.length;}
  function selectValue(key){const v=fields[key];return typeof v==="string"?v:(v&&typeof v==="object"&&!Array.isArray(v)&&typeof v.value==="string"?v.value:"");}
  const editableFields=Object.keys(fields).filter(key=>((["text","multi_text","multi-text"].includes(types[key])&&(typeof fields[key]==="string"||fields[key]===null))||(isSelect(key)&&(fields[key]===null||selectValue(key)!==""))||validRelatedValue(key))&&!unobserved.has(key)&&!omitted.has(key)&&!Object.prototype.hasOwnProperty.call(attachments,key)).sort();
  const displayName=key=>typeof names[key]==="string"&&names[key].trim()?names[key]:key;
  function fillFields(keys){field.replaceChildren(...keys.map(key=>{const option=node("option",displayName(key)+" · "+key);option.value=key;return option;}));field.value=keys[0]||"";}
  fillFields(editableFields);
  section.append(node("p",editableFields.length?"字段名称与类型沿用官方读取结果。":"当前快照没有可编辑的已观测且支持编辑的字段；旧快照缺少字段类型时，请先刷新 Bug。"));
  const unsupported=Object.keys(fields).filter(key=>!editableFields.includes(key));
  if(unsupported.length)section.append(node("p","以下字段不可编辑："+unsupported.map(key=>displayName(key)+" · "+key+"（类型不支持或未知、未观测或附件字段）").join("；")));
  const proposedLabel=node("label","新字段值");proposedLabel.append(proposed);section.append(proposedLabel);
  let grants=[],cursor=null,readEpoch=0,loadedAll=false,previewBody=null,pending=null,sending=false,readBusy=false,relatedSearching=false,relatedSearchEpoch=0,relatedSearchBox=null,relatedResults=null,relatedChosen=null,relatedState=[],relatedLabels=new Map();
  preview.disabled=true;prepare.disabled=true;grant.disabled=field.disabled=proposed.disabled=next.disabled=refresh.disabled=true;
  function resetPreview(){previewBody=null;pending=null;diff.replaceChildren();prepare.disabled=true;prepareStatus.textContent="";}
  function relationLabel(item,id){return item&&typeof item.name==="string"&&item.name.trim()?item.name.trim():(relatedLabels.get(id)||id);}
  function relationIds(key){const raw=fields[key];if(raw===null)return [];return relatedKind(key)==="single"?[relatedId(raw)]:raw.map(relatedId);}
  function relatedOutput(key){return relatedKind(key)==="single"?(relatedState[0]?.value??null):relatedState.map(item=>Number(item.value));}
  function relatedUnchanged(key,output){const raw=fields[key];if(relatedKind(key)==="single")return raw===null?output===null:relatedId(raw)===output;const old=raw===null?[]:relationIds(key),next=output.map(String);return old.length===next.length&&old.every(id=>next.includes(id));}
  function relationDisplay(key,value){if(value===null)return "空值";const ids=relatedKind(key)==="single"?(value?[String(value)]:[]):Array.isArray(value)?value.map(String):[];return ids.length?ids.map(id=>`${relatedLabels.get(id)||id} · ${id}`).join("、"):"空列表";}
  function relatedDisabled(){const disabled=sending||readBusy||relatedSearching||!!pending,items=[relatedSearchBox?.input,relatedSearchBox?.button];for(const row of Array.from(relatedChosen?.children||[]))items.push(...Array.from(row?.children||[]));for(const item of items)if(item)item.disabled=disabled;for(const item of Array.from(relatedResults?.children||[]))item.disabled=disabled;}
  function invalidateRelatedSearch(){relatedSearchEpoch++;relatedSearching=false;relatedSearchBox=null;relatedResults=null;relatedChosen=null;}
  function relationWidget(key){
    const kind=relatedKind(key),wrap=node("div"),searchLabel=node("label","搜索关联项"),input=node("input"),search=node("button","搜索关联项"),chosen=node("div"),results=node("div");
    input.type="search";input.value="";input.maxLength=128;input.setAttribute("aria-label","搜索关联项");search.type="button";
    chosen.setAttribute("aria-label","已选关联项");results.setAttribute("aria-label","关联项搜索结果");searchLabel.append(input);wrap.append(searchLabel,search,chosen,results);
    const ids=relationIds(key);relatedLabels=new Map();
    const raw=fields[key],initial=kind==="single"?(raw===null?[]:[raw]):(Array.isArray(raw)?raw:[]);
    for(const item of initial){const id=relatedId(item);if(id!==null){const label=relationLabel(item,id);relatedLabels.set(id,label);relatedState.push({value:id,label});}}
    const render=()=>{
      chosen.replaceChildren(...relatedState.map(item=>{const row=node("span",`${item.label} · ${item.value}`),remove=node("button","移除");remove.type="button";remove.setAttribute("aria-label",`移除 ${item.label}`);remove.addEventListener("click",()=>{if(epoch!==bugDetailEpoch||sending||readBusy||pending)return;relatedState=relatedState.filter(entry=>entry.value!==item.value);render();resetPreview();});row.append(remove);return row;}));
      relatedDisabled();
    };
    search.addEventListener("click",async()=>{
      if(epoch!==bugDetailEpoch||sending||readBusy||relatedSearching||pending)return;
      const selected=grants.find(item=>item.value===grant.value),fieldKey=field.value;
      if(!selected||!selected.fields.includes(fieldKey)){message.replaceChildren(node("p","所选授权不包含此字段，请重新读取授权。"));return;}
      const searchEpoch=++relatedSearchEpoch;relatedSearching=true;search.disabled=input.disabled=true;results.replaceChildren();message.replaceChildren();
      try{
        const response=await api("/api/project-bugs/search-field-related",{bug_id:value.bug_id,grant_id:selected.value,field_key:fieldKey,query:input.value.trim()});
        if(epoch!==bugDetailEpoch||searchEpoch!==relatedSearchEpoch||field.value!==fieldKey||grant.value!==selected.value)return;
        const entries=response?.options;
        if(!Array.isArray(entries)||entries.length>100||entries.some(item=>!item||!validRelatedSearchId(item.value)||typeof item.label!=="string"||!item.label.trim())||new Set(entries.map(item=>item.value)).size!==entries.length)throw new Error("关联项结果格式无效");
        for(const item of entries)relatedLabels.set(item.value,item.label.trim());
        results.replaceChildren(...entries.map(item=>{const button=node("button",`${kind==="multi"?"添加":"选择"} ${item.label} · ${item.value}`);button.type="button";button.addEventListener("click",()=>{if(epoch!==bugDetailEpoch||sending||readBusy||pending)return;if(kind==="single")relatedState=[{value:item.value,label:item.label.trim()}];else if(!relatedState.some(existing=>existing.value===item.value)){if(relatedState.length>=20){message.replaceChildren(node("p","最多只能选择 20 个关联项。"));return;}relatedState=[...relatedState,{value:item.value,label:item.label.trim()}];}render();resetPreview();});return button;}));
        if(response.narrow_query===true)message.replaceChildren(node("p","结果较多，请缩小搜索词后再搜索。"));
      }catch(error){if(epoch===bugDetailEpoch&&searchEpoch===relatedSearchEpoch)message.replaceChildren(node("p","关联项搜索失败："+error.message));}
      finally{if(searchEpoch===relatedSearchEpoch){relatedSearching=false;relatedSearchBox={input,button:search};relatedResults=results;relatedChosen=chosen;relatedDisabled();}}
    });
    relatedSearchBox={input,button:search};relatedResults=results;relatedChosen=chosen;render();return wrap;
  }
  function resetValue(){
    invalidateRelatedSearch();
    const key=field.value;
    const kind=relatedKind(key);
    proposed=node(kind?"div":isSelect(key)?"select":"textarea");proposed.setAttribute("aria-label","新字段值");
    if(kind){relatedState=[];proposed=relationWidget(key);proposed.setAttribute("aria-label","新字段值");}
    else if(isSelect(key)){
      const blank=node("option","请选择官方选项");blank.value="";proposed.append(blank);
      for(const item of options[key]){const opt=node("option",item.label+" · "+item.value);opt.value=item.value;proposed.append(opt);}
      const old=selectValue(key);proposed.value=options[key].some(o=>o.value===old)?old:"";
    }else proposed.value=typeof fields[key]==="string"?fields[key]:"";
    proposed.disabled=sending||readBusy||!key||!!pending;
    if(!kind)for(const event of ["input","change"])proposed.addEventListener(event,()=>{if(epoch===bugDetailEpoch&&!sending&&!readBusy&&!pending)resetPreview();});
    proposedLabel.replaceChildren(node("span","新字段值"),proposed);
  }
  function eligible(item){
    const scope=item.scope||{},expires=Date.parse(item.expires_at||"");
    return item.status==="active"&&Number.isFinite(expires)&&expires>Date.now()&&scope.host===value.host&&scope.project_key===value.project_key&&scope.type_key===value.type_key&&Array.isArray(scope.bug_ids)&&scope.bug_ids.includes(value.bug_id)&&Array.isArray(scope.actions)&&scope.actions.includes("bug.fields")&&Array.isArray(scope.fields);
  }
  function refreshOptions(){
    const old=grant.value;grant.replaceChildren();
    for(const item of grants){const option=node("option",item.label);option.value=item.value;grant.append(option);}
    grant.value=grants.some(item=>item.value===old)?old:(grants[0]?.value||"");
    const selected=grants.find(item=>item.value===grant.value);
    if(selected)fillFields(selected.fields);
    status.textContent=loadedAll?("已读完 "+grants.length+" 份授权"+(selected?"，已选授权符合当前 Bug 范围。":"；没有符合条件的字段授权。")):(grants.length+" 份符合范围的授权已读取"+(cursor?"；仍有更早授权，请点“读取下一页授权”继续核对。":"。"));
    if(selected&&!pending)resetValue();
    grant.disabled=sending||readBusy||!!pending||grants.length===0;field.disabled=proposed.disabled=sending||readBusy||!!pending||editableFields.length===0;preview.disabled=sending||readBusy||!!pending||editableFields.length===0||grants.length===0;refresh.disabled=sending||readBusy||!!pending;next.disabled=sending||readBusy||!!pending||!cursor;relatedDisabled();
  }
  async function loadGrants(after="",restart=false){
    if(sending)return;
    const read=++readEpoch;readBusy=true;next.disabled=refresh.disabled=true;preview.disabled=true;prepare.disabled=true;
    if(restart){grants=[];cursor=null;loadedAll=false;refreshOptions();}
    status.textContent="正在读取当前操作者的字段授权…";
    try{
      const response=await api("/api/project-bugs/list-grants",{bug_id:value.bug_id,after_id:after});
      if(epoch!==bugDetailEpoch||read!==readEpoch)return;
      for(const item of response.items||[]){
        if(!eligible(item))continue;
        const permitted=editableFields.filter(key=>item.scope.fields.includes(key));if(!permitted.length)continue;
        grants.push({value:item.grant_id,label:item.grant_id+" · 到期 "+item.expires_at,fields:permitted});
      }
      cursor=response.next_cursor||null;loadedAll=!cursor;readBusy=false;refreshOptions();
    }catch(error){if(epoch===bugDetailEpoch&&read===readEpoch){readBusy=false;status.textContent="授权读取失败："+error.message;next.disabled=!cursor;refresh.disabled=false;}}
  }
  grant.addEventListener("change",()=>{if(epoch!==bugDetailEpoch||sending||readBusy||pending)return;invalidateRelatedSearch();resetPreview();const selected=grants.find(item=>item.value===grant.value);fillFields(selected?.fields||[]);resetValue();});
  field.addEventListener("change",()=>{if(epoch===bugDetailEpoch&&!sending&&!readBusy&&!pending){invalidateRelatedSearch();resetPreview();resetValue();}});
  preview.addEventListener("click",()=>{
    if(epoch!==bugDetailEpoch||sending||readBusy||preview.disabled)return;
    const selected=grants.find(item=>item.value===grant.value),key=field.value;
    if(!selected||!selected.fields.includes(key)){message.replaceChildren(node("p","所选授权不包含此字段，请重新读取授权。"));return;}
    const kind=relatedKind(key),newValue=kind?relatedOutput(key):proposed.value;
    if(isSelect(key)&&!options[key].some(o=>o.value===proposed.value)){message.replaceChildren(node("p","请选择当前官方选项。"));return;}
    if(isSelect(key)&&selectValue(key)===proposed.value){message.replaceChildren(node("p","选项未变化，无需写回。"));return;}
    if(kind&&!relatedState.every(item=>validRelatedSearchId(item.value))){message.replaceChildren(node("p","请选择有效的数字关联项。"));return;}
    if(kind&&relatedUnchanged(key,newValue)){message.replaceChildren(node("p","关联项未变化，无需写回。"));return;}
    const oldValue=fields[key];
    previewBody={bug_id:value.bug_id,snapshot_id:snapshot.snapshot_id,expected_revision:value.revision,grant_id:selected.value,action:"bug.fields",change:{fields:{[key]:newValue}}};
    diff.replaceChildren(node("h4","本地字段差异"),node("p","字段："+displayName(key)+" · "+key),node("p","原值："+(kind?relationDisplay(key,relatedKind(key)==="single"?(oldValue===null?null:relatedId(oldValue)):oldValue===null?null:relationIds(key)):bugFieldValue(key,oldValue,true,snapshot))),node("p","新值："+(kind?relationDisplay(key,newValue):bugFieldValue(key,newValue,true,snapshot))),node("p","快照 "+snapshot.snapshot_id+" · 本地版本 "+value.revision+"。此预览尚未向后台提交。"));
    prepare.disabled=false;message.replaceChildren();
  });
  prepare.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending||readBusy||prepare.disabled||!previewBody)return;
    if(!pending){pending={...previewBody,request_id:crypto.randomUUID()};grant.disabled=field.disabled=proposed.disabled=preview.disabled=true;}
    sending=true;next.disabled=refresh.disabled=grant.disabled=field.disabled=proposed.disabled=preview.disabled=prepare.disabled=true;relatedDisabled();prepareStatus.textContent="正在记录字段写回准备请求…";
    try{await api("/api/project-bugs/prepare-write",pending);if(epoch!==bugDetailEpoch)return;prepareStatus.textContent="写回操作已准备；详情将重新读取，以显示既有的发送控制。";await loadBugDetail(value.bug_id);}
    catch(error){if(epoch===bugDetailEpoch){
      prepareStatus.textContent="准备请求结果待核对："+error.message;
      if([400,403,409].includes(error.http_status)){sending=false;pending=null;resetPreview();refreshOptions();prepareStatus.textContent="准备请求未成立："+error.message+"。请重新读取详情后预览。";}
      else{prepare.textContent="重试同一准备请求";prepare.disabled=false;}
    }}finally{sending=false;relatedDisabled();}
  });
  next.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&cursor&&!next.disabled){resetPreview();return loadGrants(cursor);}});
  refresh.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&!sending&&!refresh.disabled){resetPreview();return loadGrants("",true);}});
  section.append(refresh,next,status,preview,diff,prepare,prepareStatus,message);area.append(section);
  if(!snapshot?.snapshot_id||typeof value.revision!=="number"){status.textContent="缺少当前快照或本地版本，不能准备字段写回。";return;}
  resetValue();
  loadGrants();
}
function renderBugCommentComposer(value,epoch,area){
  const section=node("section",undefined,"note"),text=node("textarea"),grant=node("select"),next=node("button","读取更多进度评论授权"),preview=node("button","预览进度评论"),save=node("button","准备进度评论"),output=node("div"),status=node("p");
  text.setAttribute("aria-label","进度评论全文");text.maxLength=20000;grant.setAttribute("aria-label","进度评论授权");
  section.append(node("h3","添加进度评论"),node("p","可在调查开始前记录受理、进度或阻塞说明。预览和准备不会发送；保存后使用既有发送入口。"),text,grant,next,preview,output,save,status);area.append(section);
  let cursor="",loading=false,sending=false,pending=null,prepared=null;const grants=new Map();
  const current=()=>epoch===bugDetailEpoch;
  function eligible(item){const s=item.scope||{},expires=Date.parse(item.expires_at||"");return item.status==="active"&&expires>Date.now()&&s.host===value.host&&s.project_key===value.project_key&&s.type_key===value.type_key&&Array.isArray(s.bug_ids)&&s.bug_ids.includes(value.bug_id)&&Array.isArray(s.actions)&&s.actions.includes("bug.comment");}
  function controls(){const frozen=!!pending||sending;grant.disabled=text.disabled=frozen;next.disabled=frozen||loading||cursor===null;preview.disabled=frozen||loading||!grants.size;save.disabled=sending||loading||!prepared;}
  function invalidate(){if(!current()||pending)return;prepared=null;output.replaceChildren();controls();}
  text.addEventListener("input",invalidate);grant.addEventListener("change",invalidate);
  async function read(){
    if(!current()||loading||pending||cursor===null)return;loading=true;controls();
    try{const page=await api("/api/project-bugs/list-grants",{bug_id:value.bug_id,after_id:cursor});if(!current())return;
      const old=grant.value;for(const item of page.items||[])if(eligible(item))grants.set(item.grant_id,item);
      grant.replaceChildren();for(const item of grants.values()){const option=node("option",item.grant_id+" · 到期 "+item.expires_at);option.value=item.grant_id;grant.append(option);}grant.value=grants.has(old)?old:(grants.keys().next().value||"");
      cursor=page.next_cursor||null;status.textContent=grants.size?"选择授权后预览将发送的全文。":"没有符合当前 Bug 范围的评论授权，请先创建持续授权。";
    }catch(error){if(current())status.textContent="评论授权读取失败："+error.message;}
    finally{loading=false;if(current())controls();}
  }
  next.addEventListener("click",read);
  preview.addEventListener("click",()=>{
    if(!current()||pending||loading)return;const selected=grants.get(grant.value);
    if(!selected||!eligible(selected)||!text.value.trim()||text.value.length>20000){prepared=null;status.textContent="请填写不超过 20000 字符的评论并选择有效授权。";controls();return;}
    prepared={bug_id:value.bug_id,snapshot_id:value.snapshot.snapshot_id,expected_revision:value.revision,grant_id:grant.value,action:"bug.comment",change:{text:text.value}};
    output.replaceChildren(node("h4","拟发送评论全文"),node("pre",text.value));status.textContent="请核对全文；准备后仍需单独发送。";controls();
  });
  save.addEventListener("click",async()=>{
    if(!current()||sending||loading||!prepared||save.disabled)return;
    pending??={...prepared,request_id:crypto.randomUUID()};sending=true;controls();
    try{await api("/api/project-bugs/prepare-write",pending);if(current())await loadBugDetail(value.bug_id);}
    catch(error){if(current()){
      if([400,403,409].includes(error.http_status)){pending=null;prepared=null;output.replaceChildren();status.textContent="准备被拒绝或来源已变化，请重新读取详情后核对授权和评论。";}
      else{status.textContent="准备结果待核对："+error.message+"；重试沿用同一请求，不会发送评论。";save.textContent="重试同一进度评论请求";}
    }}finally{sending=false;if(current())controls();}
  });
  if(!value.snapshot?.snapshot_id||typeof value.revision!=="number"){text.disabled=grant.disabled=next.disabled=preview.disabled=save.disabled=true;status.textContent="缺少当前快照，先读取 Bug 详情。";return;}
  controls();read();
}
function renderBugWorkflow(value,epoch,area){
  const section=node("section",undefined,"note"),status=node("p"),grant=node("select"),optionSelect=node("select"),details=node("div"),requiredArea=node("div"),message=node("p");
  const read=node("button","读取当前可用流转"),next=node("button","读取下一页授权"),refresh=node("button","重新读取授权"),grantButton=node("button","授权此流转"),prepare=node("button","准备此流转"),approval=node("button","申请所选关闭审批");
  section.append(node("h3","官方流转选项"),node("p","只显示本次官方元数据读取返回的流转。准备请求不会发送到远端；关闭需单独申请审批。"));
  grant.setAttribute("aria-label","选择流转读取授权");optionSelect.setAttribute("aria-label","选择官方流转");
  const grantLabel=node("label","读取授权"),optionLabel=node("label","官方流转");grantLabel.append(grant);optionLabel.append(optionSelect);
  section.append(grantLabel,read,next,refresh,status,optionLabel,details,requiredArea,grantButton,prepare,approval,message);
  area.append(section);
  let grants=[],cursor=null,readEpoch=0,loadedAll=false,workflow=null,selected=null,pending=null,sending=false,unknown=false,readBusy=false,completed=false,requiredPreviewBody=null,requiredPending=null;
  next.disabled=refresh.disabled=read.disabled=grantButton.disabled=prepare.disabled=approval.disabled=true;
  function exactScope(item){const s=item.scope||{},expires=Date.parse(item.expires_at||"");return item.status==="active"&&Number.isFinite(expires)&&expires>Date.now()&&s.host===value.host&&s.project_key===value.project_key&&s.type_key===value.type_key&&Array.isArray(s.bug_ids)&&s.bug_ids.includes(value.bug_id)&&Array.isArray(s.actions);}
  function selectedGrant(){return grants.find(g=>g.grant_id===grant.value)||null;}
  function mutationGrant(option){return grants.find(g=>exactScope(g)&&g.scope.actions.includes(option.action)&&Array.isArray(g.scope.transitions)&&g.scope.transitions.includes(option.transition_id))||null;}
  function chosenOption(){return (workflow?.options||[]).find(item=>item.transition_id===optionSelect.value)||null;}
  function requiredGapText(option){
    const evidence=value.snapshot?.read_evidence||{},names=evidence.field_names&&typeof evidence.field_names==="object"?evidence.field_names:{},unobserved=new Set([...(Array.isArray(evidence.unobserved_field_keys)?evidence.unobserved_field_keys:[]),...(Array.isArray(evidence.omitted_value_field_keys)?evidence.omitted_value_field_keys:[])]);
    const missing=Array.isArray(option.missing_required)?option.missing_required:Array.isArray(option.missing_required_keys)?option.missing_required_keys:[];
    if(!missing.length)return "必需项尚未完整，但本次元数据未列出具体字段；可能受流程角色或元数据读取范围限制。请重新读取详情并确认流程角色。";
    return "缺少必需项："+missing.map(item=>{
      const key=typeof item==="string"?item:item?.key;
      if(typeof key!=="string"||!key)return "未识别字段";
      const label=names[key]||key,kind=typeof item==="object"&&typeof item.class==="string"&&item.class?`（${item.class}）`:"";
      return `${label}（${key}）${kind}${unobserved.has(key)?"：本次未读取到值，需重新读取，不能当作空值":""}`;
    }).join("；");
  }
  function missingItems(option){
    const raw=Array.isArray(option.missing_required)?option.missing_required:Array.isArray(option.missing_required_keys)?option.missing_required_keys:[];
    return raw.filter(item=>item&&typeof item==="object"&&typeof item.key==="string");
  }
  function renderRequiredForm(){
    requiredArea.replaceChildren();
    const fields=selected&&selected.required_complete!==true?missingItems(selected).filter(item=>item.class==="field"):[];
    if(!fields.length)return;
    const evidence=value.snapshot?.read_evidence||{},names=evidence.field_names&&typeof evidence.field_names==="object"?evidence.field_names:{},types=evidence.field_types&&typeof evidence.field_types==="object"?evidence.field_types:{},options=evidence.field_options&&typeof evidence.field_options==="object"?evidence.field_options:{};
    const field=node("select"),writeGrant=node("select"),preview=node("button","预览补齐必填字段"),submit=node("button","准备补齐必填字段"),form=node("div"),notice=node("p"),previewInfo=node("div");
    field.setAttribute("aria-label","待补齐必填字段");writeGrant.setAttribute("aria-label","补齐必填字段授权");
    for(const item of fields){const option=node("option",`${names[item.key]||item.key} · ${item.key}`);option.value=item.key;field.append(option);}
    field.value=fields[0].key;
    const render=()=>{
      requiredPreviewBody=null;previewInfo.replaceChildren();form.replaceChildren();writeGrant.replaceChildren();
      notice.textContent="";
      const key=field.value,kind=types[key],matching=grants.filter(item=>exactScope(item)&&item.scope.actions.includes("bug.fields")&&Array.isArray(item.scope.fields)&&item.scope.fields.includes(key));
      for(const item of matching){const option=node("option",`${item.grant_id} · 到期 ${item.expires_at}`);option.value=item.grant_id;writeGrant.append(option);}
      if(matching.length)writeGrant.value=matching[0].grant_id;
      writeGrant.disabled=sending||unknown||readBusy||completed||!matching.length;
      const valueLabel=node("label","拟写值"),old=node("p",`原值：官方未完成必填，值未观测（${names[key]||key}）。`);let input=null,supported=true;
      if(["text","multi-text","multi_text"].includes(kind)){input=node("textarea");input.value="";}
      else if(["select","tree-select"].includes(kind)&&Array.isArray(options[key])&&options[key].length){input=node("select");const blank=node("option","请选择官方选项");blank.value="";input.append(blank);for(const item of options[key]){if(item&&typeof item.value==="string"&&typeof item.label==="string"){const option=node("option",`${item.label} · ${item.value}`);option.value=item.value;input.append(option);}}if(input.children.length===1)supported=false;}
      else if(kind==="user"&&typeof workflow?.writer_user_key==="string"&&workflow.writer_user_key){input=node("select");const option=node("option",`当前配置操作者 · ${workflow.writer_user_key}`);option.value=workflow.writer_user_key;input.append(option);notice.textContent="人员字段只能填写本次官方读取已核验的当前配置操作者；不能猜测或手工输入其他人员 ID。";}
      else supported=false;
      if(!supported){notice.textContent=`字段类型 ${kind||"未知"} 暂不能安全补齐；请重新读取或由具备相应流程权限的人员处理。`;preview.disabled=submit.disabled=true;form.append(old,notice);return;}
      input.setAttribute("aria-label","补齐必填字段值");valueLabel.append(input);form.append(old,valueLabel);
      if(!matching.length)notice.textContent=`没有覆盖 ${names[key]||key} 的有效 bug.fields 授权；请在上方“持续授权”勾选此字段后创建授权，再重新读取官方流转。`;
      else notice.textContent=notice.textContent||"预览只固定本次准备内容；准备后不会自动派发。";
      for(const event of ["input","change"])input.addEventListener(event,()=>{requiredPreviewBody=null;previewInfo.replaceChildren();submit.disabled=true;});
      writeGrant.addEventListener("change",()=>{requiredPreviewBody=null;previewInfo.replaceChildren();submit.disabled=true;});
      preview.disabled=sending||unknown||readBusy||completed||!matching.length;
      submit.disabled=true;
      preview.addEventListener("click",()=>{
        const selectedGrant=matching.find(item=>item.grant_id===writeGrant.value),newValue=input.value;
        if(!selectedGrant){notice.textContent="所选授权不包含此字段，请重新读取授权。";return;}
        if((["text","multi-text","multi_text"].includes(kind)&&!newValue.trim())||(["select","tree-select"].includes(kind)&&!newValue)){notice.textContent="请填写非空值或选择官方选项。";return;}
        requiredPreviewBody={bug_id:value.bug_id,grant_id:selectedGrant.grant_id,snapshot_id:value.snapshot.snapshot_id,expected_revision:value.revision,action:"bug.fields",change:{fields:{[key]:newValue},required_target_status_id:selected.target_status_id,required_missing_fields:[key]}};
        previewInfo.replaceChildren(node("h4","补齐必填字段预览"),old,node("p",`拟写值：${bugFieldValue(key,newValue,true,value.snapshot)}`),node("p","此预览尚未向后台提交；准备后仍不会自动派发。"));submit.disabled=false;notice.textContent="请核对预览后准备写入。";
      });
      submit.addEventListener("click",()=>submitRequired());
      form.append(writeGrant,notice,preview,submit,previewInfo);
    };
    field.addEventListener("change",render);requiredArea.append(node("h4","补齐官方必填字段"),field,form);render();
  }
  async function submitRequired(){
    if(epoch!==bugDetailEpoch||sending||unknown||readBusy||completed||!requiredPreviewBody)return;
    requiredPending??={...requiredPreviewBody,request_id:crypto.randomUUID()};sending=true;refreshOptions();message.textContent="正在准备补齐官方必填字段；不会自动派发…";
    try{await api("/api/project-bugs/prepare-write",requiredPending);if(epoch!==bugDetailEpoch)return;completed=true;message.textContent="补齐必填字段已准备；未发送到远端。详情将重新读取以显示既有发送控制。";await loadBugDetail(value.bug_id);}
    catch(error){if(epoch!==bugDetailEpoch)return;if([400,403,409].includes(error.http_status)){requiredPending=null;requiredPreviewBody=null;workflow=null;selected=null;optionSelect.replaceChildren();message.textContent="补齐必填字段未成立："+error.message+"。请重新读取详情和官方当前流转。";}else{unknown=true;message.textContent="补齐必填字段准备结果待核对："+error.message+"。只能重试同一请求。";const retry=node("button","重试同一补齐必填字段请求");retry.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&unknown){unknown=false;submitRequired();}});message.append(retry);}refreshOptions();}
    finally{sending=false;refreshOptions();}
  }
  function refreshOptions(){
    const old=optionSelect.value;optionSelect.replaceChildren(...(workflow?.options||[]).map(item=>{const opt=node("option",`${item.target_status_label} · ${item.transition_id} · ${item.action}`);opt.value=item.transition_id;return opt;}));
    optionSelect.value=(workflow?.options||[]).some(item=>item.transition_id===old)?old:(workflow?.options?.[0]?.transition_id||"");
    selected=chosenOption();details.replaceChildren();
    if(workflow){details.append(node("p",`当前状态：${workflow.current_status.label} · ${workflow.current_status.id}`),node("p",`元数据时间：${workflow.observed_at} · 快照 ${workflow.snapshot_id} · 本地版本 ${workflow.revision}`));}
    if(selected){
      details.append(node("p",`目标状态：${selected.target_status_label} · ${selected.target_status_id}`),node("p",`动作：${selected.action} · 必需项完整：${selected.required_complete===true?"是":"否"}`));
      if(selected.required_complete!==true)details.append(node("p",requiredGapText(selected)),node("p","补齐并重新读取官方当前流转后，才能准备此流转。"));
      details.append(node("p",`授权范围预览：仅当前 Bug ${value.bug_id}；动作 bug.read + ${selected.action}；流转 ${selected.transition_id}；字段、仓库和设备均为空；有效期 1 小时。`));
    }
    renderRequiredForm();
    if(workflow&&workflow.source!=="official_current_metadata")details.append(node("p","来源不是官方当前元数据；不显示此流转选项。"));
    const active=selectedGrant(),mut=selected&&mutationGrant(selected),can=Boolean(workflow&&workflow.source==="official_current_metadata"&&selected);
    read.disabled=sending||unknown||completed||readBusy||!active||!loadedAll;
    grantButton.disabled=sending||unknown||completed||readBusy||!can||!active||Boolean(mut);
    prepare.disabled=sending||unknown||completed||readBusy||!can||!mut||selected.required_complete!==true;
    approval.disabled=sending||unknown||completed||readBusy||!can||selected.action!=="bug.close";
    next.disabled=sending||unknown||readBusy||!cursor;refresh.disabled=sending||unknown||readBusy;
    optionSelect.disabled=sending||unknown||readBusy||!workflow?.options?.length;grant.disabled=sending||unknown||readBusy||!grants.some(g=>exactScope(g)&&g.scope.actions.includes("bug.read"));
    if(unknown){read.disabled=next.disabled=refresh.disabled=grantButton.disabled=prepare.disabled=approval.disabled=optionSelect.disabled=grant.disabled=true;}
  }
  async function loadGrants(after="",restart=false){
    if(epoch!==bugDetailEpoch||sending||unknown)return;
    const r=++readEpoch;readBusy=true;workflow=null;selected=null;optionSelect.replaceChildren();next.disabled=refresh.disabled=true;refreshOptions();
    if(restart){grants=[];cursor=null;loadedAll=false;grant.replaceChildren();}
    status.textContent="正在读取当前操作者的授权…";
    try{const response=await api("/api/project-bugs/list-grants",{bug_id:value.bug_id,after_id:after});if(epoch!==bugDetailEpoch||r!==readEpoch)return;
      for(const item of response.items||[])if(exactScope(item)&&!grants.some(g=>g.grant_id===item.grant_id))grants.push(item);
      cursor=response.next_cursor||null;loadedAll=!cursor;readBusy=false;
      const readable=grants.filter(g=>g.scope.actions.includes("bug.read"));
      const old=grant.value;grant.replaceChildren(...readable.map(g=>{const opt=node("option",`${g.grant_id} · 到期 ${g.expires_at}`);opt.value=g.grant_id;return opt;}));grant.value=readable.some(g=>g.grant_id===old)?old:(readable[0]?.grant_id||"");
      status.textContent=loadedAll?`已读完授权；${readable.length} 份可读取授权`:`已读取 ${readable.length} 份可读取授权；仍有后续页面，请继续读取。`;
      refreshOptions();
    }catch(error){if(epoch===bugDetailEpoch&&r===readEpoch){readBusy=false;status.textContent="授权读取失败："+error.message;refreshOptions();}}
  }
  async function readWorkflow(){
    if(epoch!==bugDetailEpoch||sending||unknown||readBusy)return;
    const g=selectedGrant();if(!g||!loadedAll){status.textContent="请先读完授权分页并选择含 bug.read 的有效授权。";return;}
    const r=++readEpoch;readBusy=true;workflow=null;refreshOptions();status.textContent="正在读取官方当前流转…";
    try{const response=await api("/api/project-bugs/workflow-options",{bug_id:value.bug_id,grant_id:g.grant_id,snapshot_id:value.snapshot.snapshot_id,expected_revision:value.revision});if(epoch!==bugDetailEpoch||r!==readEpoch)return;
      readBusy=false;if(response.source==="official_current_metadata"&&response.snapshot_id===value.snapshot.snapshot_id&&response.revision===value.revision&&Array.isArray(response.options)){workflow={...response,options:response.options.filter(item=>item&&typeof item.transition_id==="string"&&typeof item.target_status_id==="string"&&typeof item.target_status_label==="string"&&["bug.transition","bug.close"].includes(item.action)&&typeof item.required_complete==="boolean")};status.textContent="已读取官方当前元数据中的流转选项。";}
      else{workflow=null;status.textContent="返回来源或快照版本不匹配；请重新读取详情后再试。";}refreshOptions();
    }catch(error){if(epoch===bugDetailEpoch&&r===readEpoch){readBusy=false;workflow=null;status.textContent="流转读取失败："+error.message;refreshOptions();}}
  }
  function freeze(){[grant,optionSelect,read,next,refresh,grantButton,prepare,approval].forEach(el=>el.disabled=true);}
  async function write(kind){
    if(epoch!==bugDetailEpoch||sending||unknown||readBusy||completed)return;
    if(!pending){const item=chosenOption();if(!workflow||workflow.source!=="official_current_metadata"||workflow.snapshot_id!==value.snapshot.snapshot_id||workflow.revision!==value.revision||!item)return;
      if(kind==="grant"){
        const expires_at=new Date(Date.now()+3600000).toISOString();pending={request_id:crypto.randomUUID(),expires_at,scope:{host:value.host,project_key:value.project_key,type_key:value.type_key,bug_ids:[value.bug_id],actions:["bug.read",item.action],fields:[],transitions:[item.transition_id],repositories:[],devices:[]}};
      } else if(kind==="prepare"){
        const g=mutationGrant(item);if(!g||item.required_complete!==true)return;
        pending={bug_id:value.bug_id,grant_id:g.grant_id,snapshot_id:value.snapshot.snapshot_id,expected_revision:value.revision,action:item.action,change:{transition_id:item.transition_id,target_status_id:item.target_status_id},request_id:crypto.randomUUID()};
      } else {
        if(item.action!=="bug.close")return;
        pending={bug_id:value.bug_id,request_id:crypto.randomUUID(),change:{transition_id:item.transition_id,target_status_id:item.target_status_id},expires_at:new Date(Date.now()+3600000).toISOString()};
      }
    }
    sending=true;freeze();message.textContent=kind==="grant"?"正在记录精确范围授权…":kind==="prepare"?"正在准备此流转写入；不会发送到远端…":"正在申请所选关闭审批…";
    const endpoint=kind==="grant"?"/api/project-bugs/issue-grant":kind==="prepare"?"/api/project-bugs/prepare-write":"/api/project-bugs/request-close-approval";
    try{const result=await api(endpoint,pending);if(epoch!==bugDetailEpoch)return;sending=false;
      if(kind==="grant"){message.textContent=`授权已记录：${result.grant_id||"请刷新授权核对"}；只包含所示流转范围，不会执行。`;pending=null;await loadGrants("",true);}
      else if(kind==="prepare"){completed=true;message.textContent="流转写入已准备；未发送到远端。详情将重新读取，以显示既有的发送控制。";freeze();await loadBugDetail(value.bug_id);}
      else{completed=true;message.textContent="所选关闭审批已申请；不会自动批准或关闭。详情将重新读取以显示审批记录。";freeze();await loadBugDetail(value.bug_id);}
    }catch(error){if(epoch!==bugDetailEpoch)return;sending=false;
      if([400,403,409].includes(error.http_status)){pending=null;workflow=null;selected=null;optionSelect.replaceChildren();message.textContent="请求被明确拒绝或来源已变化："+error.message+"。请重新读取授权和官方当前流转。";freeze();refreshOptions();}
      else{unknown=true;message.textContent="请求结果待核对："+error.message+"。意图和 ID 已冻结；只能重试同一请求。";const retry=node("button",kind==="grant"?"重试同一授权":kind==="prepare"?"重试同一准备请求":"重试同一关闭审批");retry.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&unknown){unknown=false;pending&&submitFrozen(kind);}});message.append(retry);freeze();}
    }
  }
  async function submitFrozen(kind){if(epoch!==bugDetailEpoch||sending||!pending)return;unknown=false;await write(kind);}
  grant.addEventListener("change",()=>{if(epoch===bugDetailEpoch&&!sending&&!unknown){workflow=null;optionSelect.replaceChildren();refreshOptions();}});
  optionSelect.addEventListener("change",()=>{if(epoch===bugDetailEpoch&&!sending&&!unknown)refreshOptions();});
  read.addEventListener("click",readWorkflow);next.addEventListener("click",()=>{if(cursor&&!next.disabled)return loadGrants(cursor);});refresh.addEventListener("click",()=>loadGrants("",true));
  grantButton.addEventListener("click",()=>write("grant"));prepare.addEventListener("click",()=>write("prepare"));approval.addEventListener("click",()=>write("approval"));
  area.addEventListener("bug-grants-changed",async()=>{if(epoch!==bugDetailEpoch||sending||unknown||completed)return;await loadGrants("",true);if(loadedAll)await readWorkflow();});
  if(!value.snapshot?.snapshot_id||typeof value.revision!=="number"){status.textContent="缺少当前快照或本地版本，不能读取流转。";return;}
  loadGrants();
}
let bugActivityWatches={}, bugActivityPageEpoch={history:0,comments:0,relations:0,attachments:0,attachment_download:0,comment_reconcile:0,write_reconcile:0};
const bugActivityErrors={authorization_or_reader_changed:"读取授权或运行配置已变化",login_required:"专用 Project 身份需要重新登录",remote_changed:"读取期间内容发生变化，请重新读取",activity_limit:"内容较多，本次未能完整读取",attachment_storage_full:"附件留存空间不足，请处理留存空间后重新请求",read_timeout:"读取超时，未确认完整文件",activity_read_failed:"官方读取失败或返回格式待核验",reader_unavailable:"后台读取不可用",interrupted_read_limit:"多次读取中断"};
function activityLabel(kind){return ({comments:"评论",history:"操作记录",relations:"关系与版本",attachments:"附件",attachment_download:"附件下载",comment_reconcile:"评论写入核对",write_reconcile:"字段与流转写入核对"})[kind]||"未识别内容";}
function showBugActivity(value,target){
  const kind=activityLabel(value.kind),labels={queued:`等待后台读取${kind}`,running:`正在读取${kind}`,succeeded:`${kind}已读取，可查看本地留存`,failed:`${kind}读取失败`,blocked:`${kind}读取已阻止`};
  target.textContent=(value.lease_expired?"读取租约已过期，等待后台恢复":labels[value.state]||"状态待核对")+(value.error_code?" · "+(bugActivityErrors[value.error_code]||"请检查后台记录"):"");
}
async function loadBugActivityPage(activityId,epoch,area,kind,offset=0){
  const pageEpoch=++bugActivityPageEpoch[kind],label=activityLabel(kind);area.replaceChildren(node("p",`正在读取留存的${label}…`));
  try{
    const value=await api("/api/project-bugs/activity-page",{activity_id:activityId,offset});
    if(epoch!==bugDetailEpoch||pageEpoch!==bugActivityPageEpoch[kind])return;
    if(value.kind!==kind||value.activity_id!==activityId)throw new Error("读取结果身份不匹配");
    const info=value.observation;
    area.replaceChildren(node("p",(kind==="comment_reconcile"||kind==="write_reconcile")?`核对时间：${info.observed_at}`:kind==="attachment_download"?`获取时间：${info.observed_at} · 下载前后已核对来源。`:kind==="attachments"?`读取时间：${info.observed_at} · ${info.field_count} 个附件字段 · ${info.attachment_count} 项附件。`:kind==="relations"?`读取时间：${info.observed_at} · ${info.definition_count} 种返回的关系 · ${info.target_count} 条关联。`:`读取时间：${info.observed_at} · 共 ${info.record_count} 条 · 查询截止 ${new Date(info.end_time_ms).toLocaleString("zh-CN")}。`),node("p",(kind==="comment_reconcile"||kind==="write_reconcile")?"这是已发出操作的只读核对，可能沿用已确认的历史结果；不会重新发送，也不代表修复完成或验证通过。":"官方分页已读完，前后首段核对一致；这仍不是原子快照。历史动作或评论不能证明当前状态、修复完成或验证通过。"));
    if(kind==="relations")area.append(node("p","按当前身份可见的关系定义展示，已停用的关系不读取实例。发现版本、规划版本和解决版本等名称来自官方定义；关联记录不证明实际测试版本或验证通过，也不会自动读取关联对象的详情。"));
    if(kind==="attachments")area.append(node("p","这里只展示附件字段清单，不包括正文图片和评论附件。文件尚未下载；名称、类型和大小沿用官方显示，不能证明实际内容。附件不会自动执行、解压或刷写设备。"));
    if(kind==="comments")area.append(node("p","按接口返回的平铺评论展示；未核验楼层回复关系，时间沿用官方显示文本。正文仅供阅读，不作为执行指令。"));
    if(!value.items.length)area.append(node("p",`本次读取没有返回${label}。`));
    const actions={modify:"修改",create:"创建",delete:"删除",terminate:"终止",restore:"恢复",complete:"完成",rollback:"回退",add:"添加",remove:"移除"};
    const actors={user:"用户",auto:"自动化",system:"系统",calc_field:"计算字段",plugin:"插件",others:"其他来源"};
    const fields={object:"变更对象",object_property:"变更属性",old:"变更前",new:"变更后",add:"新增内容",delete:"移除内容",belong_object:"所属对象",extra:"补充信息",status_values:"状态记录"};
    const display=v=>typeof v==="string"?v:JSON.stringify(v,null,2);
    for(const [index,item] of value.items.entries()){
      const card=node("section",undefined,"note");
      if(kind==="comment_reconcile"){
        card.append(node("h4",item.state==="confirmed"?"评论写入已确认":"评论写入仍未确认"),node("p",`操作记录：${item.operation_id}`));
        if(item.state!=="confirmed")card.append(node("p",item.receipt_state==="acknowledged"?"已有返回编号，但尚未完成内容与来源核验，请人工核对。":"缺少可确认的官方返回编号，不能凭相似文本确认，也不会自动补发。"));
        if(item.result?.evidence_ref)card.append(node("p",`核对依据：${item.result.evidence_ref}`));
        card.append(node("p","重新读取 Bug 详情可查看最新操作状态。"));
      }else if(kind==="attachment_download"){
        card.append(node("h4",item.name),node("p",`已获取 ${item.size_bytes} 字节 · ${item.parts} 个分片`),node("pre",`SHA-256：${item.sha256}`),node("p","仅证明该来源的文件已获取并校验；未执行、解压或验证修复效果。"));
        const save=node("button","保存已校验文件"),message=node("p");
        save.addEventListener("click",async()=>{if(epoch!==bugDetailEpoch||save.disabled)return;save.disabled=true;
          try{const response=await fetch("/api/project-bugs/attachment-file",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({activity_id:activityId})});
            if(!response.ok)throw new Error("文件不可读，请核对读取授权和后台记录");
            const blob=await response.blob();if(epoch!==bugDetailEpoch)return;
            const url=URL.createObjectURL(blob),link=document.createElement("a");link.href=url;link.download=`attachment-${item.sha256.slice(0,12)}.bin`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);message.textContent="文件已交给浏览器保存。";
          }catch(error){if(epoch===bugDetailEpoch)message.textContent=error.message;}finally{save.disabled=false;}});
        card.append(save,message);
      }else if(kind==="attachments"){
        card.append(node("h4",item.field_name));
        const states={unobserved:"接口未返回该字段的值，附件情况未知。",empty:"该字段返回为空。",unsupported_legacy_format:"该字段使用尚未核验的旧附件格式。"};
        if(item.state!=="listed")card.append(node("p",states[item.state]||"格式待核验"));
        else card.append(node("p",item.name),node("p",`${item.size_display} · ${item.media_type_display}`),node("p","尚未下载或校验文件内容。"));
        if(item.state==="listed"&&item.has_source_reference&&area.attachmentGrant){
          const download=node("button","获取并校验此附件"),message=node("p");let pending=null,sending=false;
          download.disabled=!area.attachmentGrant.value||area.attachmentAvailable!==true;
          download.addEventListener("click",async()=>{if(epoch!==bugDetailEpoch||sending||download.disabled)return;
            pending??={activity_id:activityId,field_key:item.field_key,source_digest:item.source_digest,grant_id:area.attachmentGrant.value,request_id:crypto.randomUUID()};sending=true;download.disabled=true;
            try{const result=await api("/api/project-bugs/attachment-download",pending);if(epoch!==bugDetailEpoch)return;showBugActivity(result,message);
              if(["queued","running"].includes(result.state))bugActivityWatches.attachment_download={epoch,kind:"attachment_download",id:result.activity_id,target:message,results:area,busy:false};
              else if(result.state==="succeeded")loadBugActivityPage(result.activity_id,epoch,area,"attachment_download");
            }catch(error){if(epoch===bugDetailEpoch){message.textContent="下载请求结果待核对："+error.message;if([400,403,409].includes(error.http_status))pending=null;download.disabled=false;}}
            finally{sending=false;}});card.append(download,message);
        }
      }else if(kind==="relations"){
        card.append(node("h4",item.relation_name));
        if(item.state==="disabled")card.append(node("p","关系已停用，未读取关联对象。"));
        else if(item.state==="empty")card.append(node("p","本次读取未发现该关系下的关联对象。"));
        else {const target=item.target;card.append(node("p",`${target.type_name} · ${target.name}`),node("p",`${target.project_name} · 对象标识 ${target.item_id}`));}
      }else if(kind==="comments"){
        card.append(node("h4",`${value.offset+index+1}. ${item.created_at_display} · ${item.creator}`),node("pre",item.content),node("p",`评论标识：${item.comment_id}`));
        if(item.attachment_reference)card.append(node("p","包含附件引用；此处尚未下载或核验附件。"));
      }else{
        card.append(node("h4",`${value.offset+index+1}. ${actions[item.action]||item.action} · ${new Date(item.operation_time_ms).toLocaleString("zh-CN")}`),node("p",`${actors[item.operator_type]||item.operator_type}标识：${item.operator_key}`));
        for(const change of item.contents){const part=node("details");part.append(node("summary","查看变更详情"));
          for(const [key,content] of Object.entries(change))if(content!==null)part.append(node("h5",fields[key]||`其他记录：${key}`),node("pre",display(content)));
          card.append(part);
        }
      }
      area.append(card);
    }
    const next=node("button",`下一页${label}`);next.disabled=value.next_offset===null;next.addEventListener("click",()=>{if(epoch===bugDetailEpoch&&pageEpoch===bugActivityPageEpoch[kind]&&!next.disabled)loadBugActivityPage(activityId,epoch,area,kind,value.next_offset);});area.append(next);
  }catch(error){if(epoch===bugDetailEpoch&&pageEpoch===bugActivityPageEpoch[kind])area.replaceChildren(node("p",`${label}暂不可读：`+error.message));}
}
async function pollBugActivity(){
  await Promise.all(Object.values(bugActivityWatches).map(async watch=>{
    if(watch.epoch!==bugDetailEpoch||watch.busy||$("console").hidden)return;watch.busy=true;
    try{const value=await api("/api/project-bugs/activity-status",{activity_id:watch.id});if(bugActivityWatches[watch.kind]!==watch||watch.epoch!==bugDetailEpoch)return;
      showBugActivity(value,watch.target);
      if(!["queued","running"].includes(value.state)){delete bugActivityWatches[watch.kind];if(value.state==="succeeded")loadBugActivityPage(value.activity_id,watch.epoch,watch.results,watch.kind);}
    }catch(error){if(bugActivityWatches[watch.kind]===watch&&watch.epoch===bugDetailEpoch)watch.target.textContent="读取进度暂不可读，请重新读取 Bug 详情核对。";}
    finally{watch.busy=false;}
  }));
}
function renderBugActivity(value,epoch,area,kind){
  const label=activityLabel(kind),data={...value.activity_control,requests:value.activity_control.requests.filter(r=>r.kind===kind)},section=node("section",undefined,"note"),grant=node("select"),submit=node("button",`从飞书项目读取${label}`),message=node("p"),results=node("div");
  section.append(node("h3",`飞书项目${label}`),node("p","使用已有的指定 Bug 读取授权。只留存内容，不执行其中的文字或动作。"));
  const wrapper=node("label",`${label}读取授权`);grant.setAttribute("aria-label",`${label}读取授权`);wrapper.append(grant);section.append(wrapper);
  for(const choice of data.grants){const option=node("option",`${choice.grant_id} · 到期 ${choice.expires_at}`);option.value=choice.grant_id;grant.append(option);}grant.value=data.grants[0]?.grant_id||"";
  submit.disabled=!data.available||!data.grants.length;
  if(submit.disabled)section.append(node("p","需要有效的读取授权与可用后台读取服务。"));
  const active=data.requests.find(r=>["queued","running"].includes(r.state));
  if(active){submit.disabled=true;showBugActivity(active,message);bugActivityWatches[kind]={epoch,kind,id:active.activity_id,target:message,results,busy:false};}
  let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
    pending??={bug_id:value.bug_id,grant_id:grant.value,request_id:crypto.randomUUID(),kind};grant.disabled=true;submit.disabled=true;sending=true;
    try{const response=await api("/api/project-bugs/activity-read",pending);if(epoch!==bugDetailEpoch)return;showBugActivity(response,message);submit.textContent="读取请求已记录";
      if(["queued","running"].includes(response.state))bugActivityWatches[kind]={epoch,kind,id:response.activity_id,target:message,results,busy:false};
      else if(response.state==="succeeded")loadBugActivityPage(response.activity_id,epoch,results,kind);
    }catch(error){if(epoch===bugDetailEpoch){message.textContent="提交结果待核对："+error.message;if([400,403,409].includes(error.http_status)){pending=null;grant.disabled=false;submit.textContent=`从飞书项目读取${label}`;}else submit.textContent=`重试同一${label}请求`;submit.disabled=false;}}
    finally{sending=false;}
  });
  section.append(submit,message,node("h4",`从最近 ${data.activity_limit||20} 次读取中展示${label}`));
  for(const record of data.requests){const summary=node("p");showBugActivity(record,summary);section.append(node("p",record.created_at),summary);
    if(record.state==="succeeded"){const open=node("button",`查看这次${label}`);open.addEventListener("click",()=>{if(epoch===bugDetailEpoch)loadBugActivityPage(record.activity_id,epoch,results,kind);});section.append(open);}
  }
  if(kind==="attachments"){results.attachmentGrant=grant;results.attachmentAvailable=data.available;}
  section.append(results);area.append(section);
}
function renderBugDownloads(value,epoch,area){
  const section=node("section",undefined,"note"),results=node("div");section.append(node("h3","已请求的附件下载"));
  for(const record of value.activity_control.requests.filter(r=>r.kind==="attachment_download")){
    const message=node("p");showBugActivity(record,message);section.append(message);
    if(["queued","running"].includes(record.state))bugActivityWatches.attachment_download={epoch,kind:"attachment_download",id:record.activity_id,target:message,results,busy:false};
    if(record.state==="succeeded"){const open=node("button","查看文件校验结果");open.addEventListener("click",()=>{if(epoch===bugDetailEpoch)loadBugActivityPage(record.activity_id,epoch,results,"attachment_download");});section.append(open);}
  }
  section.append(results);area.append(section);
}
function renderCommentReconcile(value,operation,epoch,card){
  const data=value.activity_control;if(!data||operation.action!=="bug.comment")return;
  const records=data.requests.filter(r=>r.kind==="comment_reconcile"&&r.source?.operation_id===operation.operation_id),results=node("div"),message=node("p");
  if(operation.can_reconcile){
    const grant=node("select"),submit=node("button","只读核对这次评论写入");grant.setAttribute("aria-label","评论核对读取授权");
    for(const choice of data.grants){const option=node("option",`${choice.grant_id} · 到期 ${choice.expires_at}`);option.value=choice.grant_id;grant.append(option);}grant.value=data.grants[0]?.grant_id||"";
    const active=records.find(r=>["queued","running"].includes(r.state));submit.disabled=!data.available||!grant.value||Boolean(active);
    if(active){showBugActivity(active,message);bugActivityWatches.comment_reconcile={epoch,kind:"comment_reconcile",id:active.activity_id,target:message,results,busy:false};}
    let pending=null,sending=false;
    submit.addEventListener("click",async()=>{if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
      pending??={operation_id:operation.operation_id,write_digest:operation.write_digest,grant_id:grant.value,request_id:crypto.randomUUID()};sending=true;submit.disabled=true;grant.disabled=true;
      try{const result=await api("/api/project-bugs/comment-reconcile",pending);if(epoch!==bugDetailEpoch)return;showBugActivity(result,message);
        if(["queued","running"].includes(result.state))bugActivityWatches.comment_reconcile={epoch,kind:"comment_reconcile",id:result.activity_id,target:message,results,busy:false};
        else if(result.state==="succeeded")loadBugActivityPage(result.activity_id,epoch,results,"comment_reconcile");
      }catch(error){if(epoch===bugDetailEpoch){message.textContent="核对请求结果待确认："+error.message;if([400,403,409].includes(error.http_status)){pending=null;grant.disabled=false;}submit.disabled=false;}}
      finally{sending=false;}});
    card.append(node("p","此操作只读取记录，不会补发评论。"),grant,submit,message);
  }
  for(const record of records){const summary=node("p");showBugActivity(record,summary);card.append(summary);
    if(record.state==="succeeded"){const open=node("button","查看这次核对结果");open.addEventListener("click",()=>{if(epoch===bugDetailEpoch)loadBugActivityPage(record.activity_id,epoch,results,"comment_reconcile");});card.append(open);}}
  card.append(results);
}
let bugCommentSendWatch=null;
const commentSendReasons={writer_not_configured:"尚未配置专用发送身份",native_contract_unverified:"官方评论创建接口尚未完成真实验收",reader_unavailable:"专用连接不可用",authority_or_source_changed:"授权、任务状态或来源已变化",dispatch_configuration_changed:"发送配置已变化",writer_identity_changed:"专用登录身份不匹配",dispatch_lease_lost:"后台处理权已变化",interrupted_dispatch_limit:"多次处理中断",comment_dispatch_unavailable:"发送处理未完成，请核对操作状态",comment_provider_unavailable:"官方连接不可用"};
function showCommentSend(record,target){
  const result=record.result;
  if(result){target.textContent=result.operation_state==="confirmed"?"评论写入已确认；不代表修复完成或验证通过。":result.reconcile_required?"发送结果尚未确认，请使用只读核对；不会自动补发。":`发送处理已结束，操作状态：${bugStates[result.operation_state]||result.operation_state}`;return;}
  target.textContent=({queued:"评论发送已排队",running:"正在核对并处理评论发送",blocked:"发送处理被阻止或中断，请重新读取操作状态",failed:"发送处理未完成，请重新读取操作状态"})[record.state]||"发送状态待核对";
  if(record.error_code)target.textContent+=" · "+(commentSendReasons[record.error_code]||"请核对后台记录");
}
async function pollCommentSend(){
  const watch=bugCommentSendWatch;if(!watch||watch.epoch!==bugDetailEpoch||watch.busy||$("console").hidden)return;watch.busy=true;
  try{const result=await api("/api/project-bugs/comment-send-status",{dispatch_id:watch.id});if(bugCommentSendWatch!==watch||watch.epoch!==bugDetailEpoch)return;showCommentSend(result,watch.target);if(!["queued","running"].includes(result.state))bugCommentSendWatch=null;}
  catch(error){if(bugCommentSendWatch===watch&&watch.epoch===bugDetailEpoch)watch.target.textContent="发送进度暂不可读，请重新读取 Bug 详情核对。";}finally{watch.busy=false;}
}
function renderCommentSend(operation,epoch,card){
  if(operation.action!=="bug.comment")return;
  const data=operation.send_control;if(!data)return;
  const message=node("p"),active=data.requests.find(r=>["queued","running"].includes(r.state));
  if(operation.state==="prepared"){
    const submit=node("button","按现有授权发送评论");submit.disabled=!data.available||Boolean(active);
    if(!data.available)card.append(node("p",commentSendReasons[data.reason]||"此评论当前不可发送"));
    let pending=null,sending=false;
    submit.addEventListener("click",async()=>{if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
      pending??={operation_id:operation.operation_id,expected_digest:operation.request_digest,request_id:crypto.randomUUID()};sending=true;submit.disabled=true;
      try{const result=await api("/api/project-bugs/send-comment",pending);if(epoch!==bugDetailEpoch)return;showCommentSend(result,message);
        if(["queued","running"].includes(result.state))bugCommentSendWatch={epoch,id:result.dispatch_id,target:message,busy:false};
      }catch(error){if(epoch===bugDetailEpoch){message.textContent="提交结果待核对："+error.message;if([400,403,409].includes(error.http_status))pending=null;submit.disabled=false;}}
      finally{sending=false;}});card.append(submit);
  }
  if(active){showCommentSend(active,message);bugCommentSendWatch={epoch,id:active.dispatch_id,target:message,busy:false};}
  card.append(message);
  for(const record of data.requests){const summary=node("p");showCommentSend(record,summary);card.append(summary);}
}
setInterval(pollCommentSend,5000);
let bugWriteSendWatches={};
const writeSendReasons={writer_not_configured:"尚未配置专用写入身份",native_contract_unverified:"官方写入接口尚未完成真实验收",reader_unavailable:"专用连接不可用",risk_policy_forbids_dispatch:"当前风险策略禁止自动写入",reader_destination_mismatch:"连接与目标空间不匹配",authority_or_source_changed:"授权、任务状态或来源已变化",dispatch_configuration_changed:"写入配置已变化",writer_identity_changed:"专用登录身份不匹配",dispatch_lease_lost:"后台处理权已变化",interrupted_dispatch_limit:"多次处理中断",write_provider_unavailable:"官方连接不可用",write_dispatch_unavailable:"写入处理未完成，请核对操作状态"};
const writeActionLabels={"bug.fields":{button:"按现有授权写入字段",queued:"字段写入已排队",running:"正在核对并处理字段写入",confirmed:"字段写入已确认；不代表修复完成或验证通过。"},"bug.transition":{button:"按现有授权执行流转",queued:"流程流转已排队",running:"正在核对并处理流程流转",confirmed:"流程流转已确认；不代表修复完成或验证通过。"},"bug.close":{button:"按现有授权关闭缺陷",queued:"关闭写入已排队",running:"正在核对并处理关闭写入",confirmed:"关闭写入已确认；不代表修复完成或验证通过。"}};
function showWriteSend(record,target,operation){
  const labels=writeActionLabels[operation.action]||writeActionLabels["bug.fields"],result=record.result;
  if(result){
    if(result.operation_state==="confirmed"){target.textContent=labels.confirmed;return;}
    if(result.reconcile_required){
      target.textContent=operation.state==="confirmed"&&operation.result?.settled_by_human===true
        ? "原始发送回执：结果尚未确认；该回执保持未知。操作已由人工结算确认生效，不代表新的远端读取。"
        : "结果尚未确认，请使用只读核对或人工结算；不会自动重发。";
      return;
    }
    target.textContent=`写入处理已结束，操作状态：${bugStates[result.operation_state]||result.operation_state}`;return;
  }
  target.textContent=({queued:labels.queued,running:labels.running,blocked:"写入处理被阻止或中断，请重新读取操作状态",failed:"写入处理未完成，请重新读取操作状态"})[record.state]||"写入状态待核对";
  if(record.error_code)target.textContent+=" · "+(writeSendReasons[record.error_code]||"请核对后台记录");
}
async function pollWriteSend(){
  await Promise.all(Object.values(bugWriteSendWatches).map(async watch=>{
    if(watch.epoch!==bugDetailEpoch||watch.busy||$("console").hidden)return;watch.busy=true;
    try{const result=await api("/api/project-bugs/write-send-status",{dispatch_id:watch.id});if(bugWriteSendWatches[watch.key]!==watch||watch.epoch!==bugDetailEpoch)return;
      showWriteSend(result,watch.target,watch.operation);if(!["queued","running"].includes(result.state))delete bugWriteSendWatches[watch.key];
    }catch(error){if(bugWriteSendWatches[watch.key]===watch&&watch.epoch===bugDetailEpoch)watch.target.textContent="写入进度暂不可读，请重新读取 Bug 详情核对。";}
    finally{watch.busy=false;}
  }));
}
function renderWriteSend(operation,epoch,card){
  if(!["bug.fields","bug.transition","bug.close"].includes(operation.action))return;
  const data=operation.send_control;if(!data)return;
  const message=node("p"),active=data.requests.find(r=>["queued","running"].includes(r.state)),labels=writeActionLabels[operation.action];
  if(operation.state==="prepared"){
    const submit=node("button",labels.button);submit.disabled=!data.available||Boolean(active);
    if(!data.available)card.append(node("p",writeSendReasons[data.reason]||"此写入当前不可发送"));
    let pending=null,sending=false;
    submit.addEventListener("click",async()=>{if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
      pending??={operation_id:operation.operation_id,expected_digest:operation.request_digest,request_id:crypto.randomUUID()};sending=true;submit.disabled=true;
      try{const result=await api("/api/project-bugs/send-write",pending);if(epoch!==bugDetailEpoch)return;showWriteSend(result,message,operation);
        if(["queued","running"].includes(result.state))bugWriteSendWatches[operation.operation_id]={epoch,key:operation.operation_id,id:result.dispatch_id,target:message,operation,busy:false};
      }catch(error){if(epoch===bugDetailEpoch){message.textContent="提交结果待核对："+error.message;if([400,403,409].includes(error.http_status))pending=null;submit.disabled=false;}}
      finally{sending=false;}});
    card.append(submit);
  }
  if(active){showWriteSend(active,message,operation);bugWriteSendWatches[operation.operation_id]={epoch,key:operation.operation_id,id:active.dispatch_id,target:message,operation,busy:false};}
  card.append(message);
  for(const record of data.requests){const summary=node("p");showWriteSend(record,summary,operation);card.append(summary);}
}
setInterval(pollWriteSend,5000);
function renderUnknownWriteSettlement(operation,epoch,card){
  if(!operation.can_settle)return;
  const section=node("section"),message=node("p"),evidence=node("textarea"),submit=node("button","提交人工结算");
  const name=`settle-${operation.operation_id}`,choices=[["confirmed_applied","核实已生效"],["confirmed_not_applied","核实未生效"]];
  section.append(node("h4","未知结果人工结算"),node("p","人工结算需要你已亲自核对远端结果，并留下可审计证据说明。"));
  for(const [value,labelText] of choices){const input=node("input"),label=node("label");input.type="radio";input.name=name;input.value=value;input.checked=false;input.setAttribute("aria-label",labelText);label.append(input,node("span",labelText));section.append(label);}
  evidence.value="";evidence.setAttribute("aria-label","人工结算证据说明");section.append(evidence,submit,message);let sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
    const verdict=choices.map(([value])=>value).find(value=>descendantsForSettle(section).some(el=>el.name===name&&el.value===value&&el.checked));
    if(!evidence.value.trim()){message.textContent="请填写可审计证据说明。人工结算需要你已亲自核对远端结果，并留下可审计证据说明。";return;}
    if(!verdict){message.textContent="请明确选择已核实生效或未生效；未知结果不能默认确认为成功。";return;}
    sending=true;submit.disabled=true;
    try{await api("/api/project-bugs/settle-unknown-write",{operation_id:operation.operation_id,verdict,evidence_text:evidence.value.trim()});
      if(epoch===bugDetailEpoch)message.textContent="人工结算已提交；请重新读取详情核对最新操作状态。";
    }catch(error){if(epoch===bugDetailEpoch){message.textContent="人工结算未确认："+error.message;submit.disabled=false;}}
    finally{sending=false;}
  });
  card.append(section);
}
function descendantsForSettle(element){return [element,...Array.from(element.children).flatMap(descendantsForSettle)];}
const closeApprovalStates={requested:"等待决定",approved:"已批准待消费",denied:"已拒绝",consumed:"已消费",expired:"已过期",revoked:"已失效"};
function renderCloseApprovals(value,epoch,area,allowRequest){
  const section=node("section",undefined,"note"),list=node("div"),message=node("p"),transition=node("input"),target=node("input"),duration=node("select"),submit=node("button","发起关闭审批");
  section.append(node("h3","关闭审批"),node("p","关闭审批消费后单次生效；有编码调查的轮次还需完成修复复核，修复或验证证据变化会使已批准的审批失效。"));
  transition.value="";target.value="";transition.setAttribute("aria-label","关闭流转 ID");target.setAttribute("aria-label","目标状态 ID");
  for(const [hours,label] of [[1,"1 小时"],[8,"8 小时"],[24,"24 小时"],[72,"72 小时"]]){const option=node("option",label);option.value=String(hours);duration.append(option);}duration.value="24";duration.setAttribute("aria-label","关闭审批有效期");
  let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending)return;
    if(!pending){if(!transition.value.trim()||!target.value.trim()){message.textContent="请填写关闭流转 ID 和目标状态 ID。";return;}
      pending={bug_id:value.bug_id,request_id:crypto.randomUUID(),change:{transition_id:transition.value.trim(),target_status_id:target.value.trim()},expires_at:new Date(Date.now()+Number(duration.value)*3600000).toISOString()};
      transition.disabled=target.disabled=duration.disabled=true;
    }
    sending=true;submit.disabled=true;
    try{await api("/api/project-bugs/request-close-approval",pending);if(epoch===bugDetailEpoch)message.textContent="关闭审批已发起；批准后仍需匹配的关闭写入消费。";}
    catch(error){if(epoch===bugDetailEpoch){message.textContent="关闭审批未提交："+error.message;if([400,403,409].includes(error.http_status)){pending=null;transition.disabled=target.disabled=duration.disabled=false;}submit.disabled=false;}}
    finally{sending=false;}
  });
  for(const approval of value.close_approvals||[]){
    const card=node("section",undefined,"note");
    card.append(node("h4",`${closeApprovalStates[approval.status]||approval.status} · ${approval.approval_id}`),node("p",`申请：${approval.actor} · 到期 ${approval.expires_at}`),node("p",`验证摘要：${approval.verification_digest}`));
    if(approval.action){
      const action=approval.action;
      card.append(node("p",`远端对象：${action.host} / ${action.project_key} / ${action.type_key} / ${action.item_id}`),node("p",`关闭流转：${action.transition_id} → 目标状态 ${action.target_status_id}`));
    }
    if(approval.status==="consumed"){
      card.append(node("p","审批已用于一次关闭写入；申请时验证摘要保留在上方。关闭是否生效以远端状态和写入回执为准。"));
    }else{
      card.append(node("p",`当前验证：${approval.current_verification?.verification_state||"不可用"}。审批允许匹配且已授权的关闭写入消费，不是远端关闭成功回执。`));
    }
    const blockers={approval_not_requested:"审批已处理",approval_expired:"审批已过期",verification_changed:"修复或验证证据已变化，需重新申请",verification_unavailable:"当前修复或验证证据不可用，请先检查复核状态"};
    if(approval.status!=="consumed")for(const blocker of approval.approval_blockers||[])card.append(node("p",blockers[blocker]||"审批条件待核对"));
    if(approval.decided_at)card.append(node("p",`决定：${approval.decided_by||"未知"} · ${approval.decided_at}`));
    if(approval.consumed_at)card.append(node("p",`消费：${approval.consumed_operation_id||"未知"} · ${approval.consumed_at}`));
    if(approval.status==="requested"){
      for(const [approve,labelText] of [[true,"批准关闭审批"],[false,"拒绝关闭审批"]]){
        const decide=node("button",labelText),status=node("p");let decidePending=null,deciding=false;
        decide.disabled=approve?approval.can_approve!==true:approval.can_deny!==true;
        decide.addEventListener("click",async()=>{
          if(epoch!==bugDetailEpoch||deciding||decide.disabled)return;
          decidePending??={approval_id:approval.approval_id,request_id:crypto.randomUUID(),approve,expected_digest:approval.action_digest};
          deciding=true;decide.disabled=true;
          try{await api("/api/project-bugs/decide-close-approval",decidePending);if(epoch===bugDetailEpoch)status.textContent=approve?"已记录批准；重新读取详情核对是否被消费。":"已记录拒绝；拒绝为终态。";}
          catch(error){if(epoch===bugDetailEpoch){status.textContent="决定未确认："+error.message;if([400,403,409].includes(error.http_status))decidePending=null;decide.disabled=false;}}
          finally{deciding=false;}
        });card.append(decide,status);
      }
    }
    list.append(card);
  }
  if(!(value.close_approvals||[]).length)list.append(node("p","暂无关闭审批。"));
  section.append(list);
  if(allowRequest)section.append(node("h4","发起关闭审批"),transition,target,duration,submit,message);
  else section.append(node("p",value.project_key==="synthetic-local-only"?"本地合成验收不对应飞书项目对象，不能发起远端关闭审批。":value.snapshot?"当前没有可用于新关闭审批的调查轮次；保留历史审批记录。若缺陷重新打开，先读取远端状态并开始新轮次。":"尚无飞书项目远端快照，不能发起远端关闭审批。"));
  area.append(section);
}
const createGrantStates={active:"有效",expired:"已到期",revoked:"已吊销"};
const createDraftStates={draft:"草稿",ready:"就绪",dispatched:"已派发",unknown:"结果未知",created:"已创建",rejected:"已拒绝",cancelled:"已取消"};
function renderCreateDraftConsole(area){
  const section=node("section",undefined,"note"),grantList=node("div"),draftList=node("div"),grantMessage=node("p"),draftMessage=node("p");
  section.append(node("h3","创建缺陷草稿"),node("p","填写并保存草稿，核对必填信息和可能重复的缺陷后，再确认创建。结果未知时先核对飞书项目，不会自动重试。"));
  const scopeRead=node("button","读取可创建范围"),scopeSelect=node("select"),host=node("input"),project=node("input"),type=node("input"),max=node("input"),grantHours=node("select"),grantSubmit=node("button","创建缺陷授权"),grantRead=node("button","读取创建授权"),grantSelect=node("select");
  scopeSelect.setAttribute("aria-label","创建空间和类型");scopeSelect.disabled=true;
  grantSelect.setAttribute("aria-label","已有创建授权");grantSelect.disabled=true;
  host.value="";project.value="";type.value="";host.setAttribute("aria-label","创建授权 Host");project.setAttribute("aria-label","创建授权项目 Key");type.setAttribute("aria-label","创建授权类型 Key");max.setAttribute("aria-label","最大创建数");max.type="number";max.min="1";max.max="20";max.value="1";
  for(const [hours,label] of [[1,"1 小时"],[8,"8 小时"],[24,"24 小时"]]){const option=node("option",label);option.value=String(hours);grantHours.append(option);}grantHours.value="24";grantHours.setAttribute("aria-label","创建授权有效期");
  let grantPending=null,grantSending=false,grantCursor="",applyCreateGrant=null;
  function applyScope(scope){
    if(!scope||grantPending)return;
    host.value=scope.host;project.value=scope.project_key;type.value=scope.type_key;
    if(applyCreateGrant)applyCreateGrant(scope);
  }
  scopeRead.addEventListener("click",async()=>{
    scopeRead.disabled=true;grantMessage.textContent="正在读取已配置的可创建范围…";
    try{const response=await api("/api/project-bugs/create-scope-options",{});scopeSelect.replaceChildren();
      for(const [index,scope] of (response.options||[]).entries()){const option=node("option",`${scope.simple_name} · ${scope.type_key}`);option.value=String(index);scopeSelect.append(option);}
      scopeSelect.disabled=!response.available||!(response.options||[]).length;
      scopeSelect._readerHost=response.reader_host||"";
      if(!scopeSelect.disabled){const scope=response.options[0];applyScope({host:response.reader_host,project_key:scope.project_key,type_key:scope.type_key});grantMessage.textContent="已载入可创建空间和类型；创建授权仍需你确认提交。";}
      else grantMessage.textContent="当前没有已配置的可创建范围。";
      scopeSelect._scopes=response.options||[];
    }catch(error){grantMessage.textContent="可创建范围暂不可读："+error.message;}
    finally{scopeRead.disabled=false;}
  });
  scopeSelect.addEventListener("change",()=>{const scope=scopeSelect._scopes?.[Number(scopeSelect.value)];if(scope)applyScope({host:scopeSelect._readerHost||host.value,project_key:scope.project_key,type_key:scope.type_key});});
  grantSubmit.addEventListener("click",async()=>{
    if(grantSending||grantSubmit.disabled)return;
    if(!grantPending){const count=Number(max.value);if(!host.value.trim()||!project.value.trim()||!type.value.trim()||!Number.isInteger(count)||count<1||count>20){grantMessage.textContent="请填写 Host、项目、类型和 1–20 的最大创建数。";return;}
      grantPending={request_id:crypto.randomUUID(),scope:{host:host.value.trim(),project_key:project.value.trim(),type_key:type.value.trim(),max_creations:count},expires_at:new Date(Date.now()+Number(grantHours.value)*3600000).toISOString()};
      [host,project,type,max,grantHours].forEach(input=>input.disabled=true);
    }
    grantSending=true;grantSubmit.disabled=true;
    try{const result=await api("/api/project-bugs/issue-create-grant",grantPending);applyCreateGrant?.({grant_id:result.grant_id,...result.scope});grantMessage.textContent=`创建授权已记录：${result.grant_id}。已填入草稿范围，不会创建缺陷。`;}
    catch(error){grantMessage.textContent="创建授权未确认："+error.message;if([400,403,409].includes(error.http_status)){grantPending=null;[host,project,type,max,grantHours].forEach(input=>input.disabled=false);}grantSubmit.disabled=false;}
    finally{grantSending=false;}
  });
  async function readGrants(after=""){grantRead.disabled=true;grantMessage.textContent="正在读取创建授权…";
    try{const page=await api("/api/project-bugs/list-create-grants",{after_id:after});grantCursor=page.next_cursor||"";grantList.replaceChildren();
      grantSelect.replaceChildren();
      for(const grant of page.items||[]){const card=node("section",undefined,"note"),scope=grant.scope;card.append(node("h4",`${createGrantStates[grant.status]||grant.status} · ${grant.grant_id}`),node("p",`${scope.host} · ${scope.project_key} · ${scope.type_key} · 上限 ${scope.max_creations} · 已用 ${grant.used} · 剩余 ${grant.remaining}`),node("p",`到期：${grant.expires_at}`));
        if(grant.can_revoke){const revoke=node("button","吊销创建授权");revoke.addEventListener("click",async()=>{revoke.disabled=true;try{await api("/api/project-bugs/revoke-create-grant",{grant_id:grant.grant_id});grantMessage.textContent="吊销已提交；请重新读取授权核对。";}catch(error){grantMessage.textContent="吊销未确认："+error.message;revoke.disabled=false;}});card.append(revoke);}
        grantList.append(card);
        if(grant.status==="active"&&grant.remaining>0){const option=node("option",`${grant.grant_id} · ${scope.project_key} · ${scope.type_key}`);option.value=grant.grant_id;option._grant=grant;grantSelect.append(option);}
      }
      grantSelect.disabled=!grantSelect.children.length;
      if(!grantSelect.disabled){const grant=grantSelect.children[0]._grant;applyCreateGrant?.({grant_id:grant.grant_id,...grant.scope});}
      if(!(page.items||[]).length)grantList.append(node("p","暂无创建授权。"));grantMessage.textContent="创建授权读取完成。";grantRead.textContent=grantCursor?"读取下一页创建授权":"读取创建授权";grantRead.disabled=false;
    }catch(error){grantMessage.textContent="创建授权暂不可读："+error.message;grantRead.disabled=false;}
  }
  grantRead.addEventListener("click",()=>readGrants(grantCursor));
  grantSelect.addEventListener("change",()=>{const grant=grantSelect.children[grantSelect.selectedIndex]?._grant;if(grant)applyCreateGrant?.({grant_id:grant.grant_id,...grant.scope});});
  const scopeLabel=node("label","创建空间和类型"),grantLabel=node("label","使用已有有效创建授权");scopeLabel.append(scopeSelect);grantLabel.append(grantSelect);
  const grantScopeDetails=node("details"),grantScopeSummary=node("summary","高级：核对创建空间标识");
  for(const [label,input] of [["Host",host],["项目 Key",project],["类型 Key",type]]){const row=node("label",label);row.append(input);grantScopeDetails.append(row);}
  grantScopeDetails.prepend(grantScopeSummary);
  const grantHistory=node("details"),grantHistorySummary=node("summary","查看历史创建授权");grantHistory.append(grantHistorySummary,grantList);
  section.append(node("h4","创建授权"),scopeLabel,scopeRead,grantScopeDetails,max,grantHours,grantSubmit,grantRead,grantLabel,grantMessage,grantHistory);
  const draftGrant=node("input"),draftHost=node("input"),draftProject=node("input"),draftType=node("input"),fieldValues=node("textarea"),required=node("textarea"),draftSubmit=node("button","准备创建草稿"),draftRead=node("button","读取创建草稿");
  for(const [label,input] of [["草稿授权 ID",draftGrant],["草稿 Host",draftHost],["草稿项目 Key",draftProject],["草稿类型 Key",draftType],["草稿字段 JSON",fieldValues],["必填字段，每行 field_key:label",required]])input.setAttribute("aria-label",label);
  draftGrant.value="";draftHost.value="";draftProject.value="";draftType.value="";required.value="";fieldValues.value="{}";let draftPending=null,draftSending=false,draftCursor="";
  const formLoad=node("button","读取飞书创建字段"),formArea=node("div");
  let officialForm=null,formStamp=null,formEditors=[],memberControls=[],formGeneration=0;
  const currentFormScope=()=>({grant_id:draftGrant.value.trim(),host:draftHost.value.trim(),project_key:draftProject.value.trim(),type_key:draftType.value.trim()});
  applyCreateGrant=(grant)=>{
    if(draftPending||!grant)return;
    formGeneration++;officialForm=null;formStamp=null;formEditors=[];memberControls=[];formArea.replaceChildren();
    draftGrant.value=grant.grant_id||"";
    if(grant.host)draftHost.value=grant.host;
    if(grant.project_key)draftProject.value=grant.project_key;
    if(grant.type_key)draftType.value=grant.type_key;
    formLoad.disabled=false;
  };
  function copyIncompleteDraft(draft){
    if(draftSending||draftPending){draftMessage.textContent="当前草稿保存请求已有固定内容，请先完成核对或刷新页面。";return;}
    if(draft.state!=="draft"||!(draft.missing_required||[]).length)return;
    formGeneration++;officialForm=null;formStamp=null;formEditors=[];memberControls=[];formArea.replaceChildren();
    draftGrant.value=draft.grant_id;draftHost.value=draft.scope.host;draftProject.value=draft.scope.project_key;draftType.value=draft.scope.type_key;
    fieldValues.value=JSON.stringify(draft.field_values||{},null,2);
    required.value=(draft.required_fields||[]).map(f=>f.field_key+":"+f.label).join("\n");
    fieldValues.disabled=false;required.disabled=false;formLoad.disabled=false;
    draftMessage.textContent="已载入待补充字段。补齐后保存为新草稿，原草稿保留；仍需查重、核对必填和确认创建。";
    fieldValues.focus();
  }

  formLoad.addEventListener("click",async()=>{
    if(formLoad.disabled||draftPending)return;formLoad.disabled=true;
    const scope=currentFormScope(),stamp=JSON.stringify(scope),generation=++formGeneration;
    try{const result=await api("/api/project-bugs/create-form",scope);
      if(JSON.stringify(currentFormScope())!==stamp){draftMessage.textContent="创建范围已变化，请重新读取字段。";return;}
      officialForm=result;formStamp=stamp;formEditors=[];memberControls=[];formArea.replaceChildren();
      for(const field of result.fields){const wrapper=node("label",field.label+(field.required?"（必填）":"")),input=node(field.editor==="select"?"select":"textarea");
        if(field.editor==="select"){const placeholder=node("option","请选择");placeholder.value="";input.append(placeholder);
          for(const choice of field.options||[]){const option=node("option",choice.label);option.value=choice.value;input.append(option);}}
        input.setAttribute("aria-label",field.label);input.value="";wrapper.append(input);
        if(["user","related"].includes(field.editor)){
          const related=field.editor==="related",single=field.type==="user"||(related&&!field.type.includes("multi_select")),noun=related?"关联项":"人员";
          input.hidden=true;input.readOnly=true;
          const query=node("input"),search=node("button","搜索"+noun),choices=node("div"),selected=node("div"),message=node("p");
          query.setAttribute("aria-label",field.label+(related?"搜索关联项名称":"搜索姓名或标识"));
          const members=new Map();let searchSerial=0;
          function showMembers(){selected.replaceChildren();input.value=members.size?JSON.stringify(single?[...members.keys()][0]:related?[...members.keys()].map(Number):[...members.keys()]):"";
            for(const [key,label] of members){const remove=node("button","移除 "+label);remove.disabled=!!draftPending;remove.addEventListener("click",()=>{if(draftPending)return;members.delete(key);showMembers();});selected.append(remove);memberControls.push(remove);}}
          search.addEventListener("click",async()=>{
            if(draftPending||search.disabled)return;
            if(formStamp!==stamp||JSON.stringify(currentFormScope())!==stamp){message.textContent="创建范围已变化，请重新读取字段。";return;}
            const term=query.value.trim(),serial=++searchSerial;if(!term){message.textContent=related?"请输入关联项名称。":"请输入姓名或标识。";return;}
            search.disabled=true;choices.replaceChildren();
            try{const found=await api("/api/project-bugs/"+(related?"search-create-related":"search-create-users"),{...scope,field_key:field.field_key,query:term});
              if(draftPending||generation!==formGeneration||serial!==searchSerial||formStamp!==stamp||JSON.stringify(currentFormScope())!==stamp||query.value.trim()!==term)return;
              for(const choice of found.options||[]){const choose=node("button",choice.label+" · "+choice.value);choose.addEventListener("click",()=>{
                if(draftPending||generation!==formGeneration||formStamp!==stamp||JSON.stringify(currentFormScope())!==stamp)return;
                if(related&&(!/^\d+$/.test(choice.value)||!Number.isSafeInteger(Number(choice.value))||Number(choice.value)<=0)){message.textContent="关联项标识无法安全提交。";return;}
                if(single)members.clear();
                if(members.size>=20&&!members.has(choice.value)){message.textContent="每个字段最多选择 20 项。";return;}
                members.set(choice.value,choice.label);showMembers();});choices.append(choose);memberControls.push(choose);}
              message.textContent=found.narrow_query?"结果较多，请缩小搜索范围。":(found.options||[]).length?"请选择"+noun+"；同名时请核对标识。":"没有找到可选"+noun+"。";
            }catch(error){message.textContent=noun+"查询失败："+error.message;}finally{search.disabled=!!draftPending;}
          });
          memberControls.push(query,search);wrapper.append(query,search,message,choices,selected);
        }else if(!["text","select"].includes(field.editor))wrapper.append(node("p","此字段的选项或对象格式尚未核验，请填写有效 JSON；不会自动套用默认值。"));
        formArea.append(wrapper);formEditors.push({field,input});}
      required.disabled=true;draftMessage.textContent="已读取官方字段；普通文本可直接填写，复杂字段暂需 JSON。";
    }catch(error){draftMessage.textContent="字段读取失败："+error.message;}
    finally{formLoad.disabled=false;}
  });
  function requiredFields(){return required.value.split("\n").map(line=>line.trim()).filter(Boolean).map(line=>{const [field_key,...rest]=line.split(":");return {field_key:field_key.trim(),label:rest.join(":").trim()||field_key.trim()};});}
  draftSubmit.addEventListener("click",async()=>{
    if(draftSending||draftSubmit.disabled)return;
    if(!draftPending){let parsed;try{parsed=JSON.parse(fieldValues.value||"{}");}catch{draftMessage.textContent="草稿字段 JSON 格式不正确。";return;}
      if(!draftGrant.value.trim()||!draftHost.value.trim()||!draftProject.value.trim()||!draftType.value.trim()||parsed===null||Array.isArray(parsed)||typeof parsed!=="object"){draftMessage.textContent="请填写草稿授权、范围和对象形式的字段 JSON。";return;}
      let requiredList=requiredFields();
      if(officialForm){
        if(formStamp!==JSON.stringify(currentFormScope())){draftMessage.textContent="创建范围已变化，请重新读取字段。";return;}
        requiredList=officialForm.fields.filter(f=>f.required).map(f=>({field_key:f.field_key,label:f.label}));
        for(const {field,input} of formEditors){if(!input.value.trim())continue;
          try{parsed[field.field_key]=["text","select"].includes(field.editor)?input.value:JSON.parse(input.value);}
          catch{draftMessage.textContent=field.label+"的 JSON 格式不正确。";return;}}
      }
      draftPending={request_id:crypto.randomUUID(),grant_id:draftGrant.value.trim(),host:draftHost.value.trim(),project_key:draftProject.value.trim(),type_key:draftType.value.trim(),field_values:parsed,required_fields:requiredList};
      [draftGrant,draftHost,draftProject,draftType,fieldValues,required,...formEditors.map(e=>e.input),...memberControls,formLoad].forEach(input=>input.disabled=true);
    }
    draftSending=true;draftSubmit.disabled=true;
    try{const draft=await api("/api/project-bugs/prepare-create-draft",draftPending);draftMessage.textContent=`草稿已保存：${draft.draft_id}。不会自动创建缺陷。`;draftList.prepend(renderCreateDraftItem(draft,draftMessage,copyIncompleteDraft));}
    catch(error){draftMessage.textContent="草稿未确认："+error.message;if([400,403,409].includes(error.http_status)){draftPending=null;[draftGrant,draftHost,draftProject,draftType,fieldValues,required,...formEditors.map(e=>e.input),...memberControls,formLoad].forEach(input=>input.disabled=false);}draftSubmit.disabled=false;}
    finally{draftSending=false;}
  });
  async function readDrafts(after=""){draftRead.disabled=true;draftMessage.textContent="正在读取创建草稿…";
    try{const page=await api("/api/project-bugs/create-drafts",{after_id:after});draftCursor=page.next_cursor||"";draftList.replaceChildren(...(page.items||[]).map(item=>renderCreateDraftItem(item,draftMessage,copyIncompleteDraft)));
      if(!(page.items||[]).length)draftList.append(node("p","暂无创建草稿。"));draftMessage.textContent="创建草稿读取完成。";draftRead.textContent=draftCursor?"读取下一页创建草稿":"读取创建草稿";draftRead.disabled=false;
    }catch(error){draftMessage.textContent="创建草稿暂不可读："+error.message;draftRead.disabled=false;}
  }
  draftRead.addEventListener("click",()=>readDrafts(draftCursor));
  const draftAdvanced=node("details"),draftAdvancedSummary=node("summary","高级：授权标识与字段 JSON");
  for(const [label,input] of [["创建授权 ID",draftGrant],["Host",draftHost],["项目 Key",draftProject],["类型 Key",draftType],["草稿字段 JSON",fieldValues],["必填字段",required]]){const row=node("label",label);row.append(input);draftAdvanced.append(row);}
  draftAdvanced.prepend(draftAdvancedSummary);
  const draftHistory=node("details"),draftHistorySummary=node("summary","查看历史创建草稿");draftHistory.append(draftHistorySummary,draftList);
  section.append(node("h4","草稿"),node("p","先读取飞书创建字段并填写表单。复杂字段可在高级输入中核对；草稿内容不可改，修改内容需新建草稿。"),formLoad,formArea,draftAdvanced,draftSubmit,draftRead,draftMessage,draftHistory);area.append(section);
}
function renderCreateDraftItem(draft,message,onCopyIncomplete){
  const card=node("section",undefined,"note"),scope=draft.scope||{},missing=draft.missing_required||[];
  card.append(node("h4",`${createDraftStates[draft.state]||draft.state} · ${draft.draft_id}`),node("p",`${scope.host} · ${scope.project_key} · ${scope.type_key}`),node("pre",JSON.stringify(draft.field_values||{},null,2)),node("p","原生创建通道已验收：就绪草稿仅在人工点击派发后创建一次，绝不自动重试。"));
  if(draft.state==="draft")card.append(node("p",`草稿（需补齐必填 missing_required 并完成查重确认）：${missing.join("、")||"必填已补齐"}；查重：${draft.duplicate_confirmed?"已确认非重复":"尚未确认非重复"}`));
  if(draft.state==="draft"&&missing.length&&onCopyIncomplete){const copy=node("button","载入并补充草稿");copy.addEventListener("click",()=>onCopyIncomplete(draft));card.append(copy);}
  if(draft.state==="ready")card.append(node("p","就绪：点击“创建并读取绑定”会消耗本草稿唯一一次创建派发；结果未知时转人工结算，不会自动重发。"));
  if(draft.state==="unknown"){card.append(node("p","创建结果未知：请先在飞书项目核对是否已创建，再选择下方人工结算；草稿不会自动重发。"));
    const itemInput=node("input");itemInput.setAttribute("aria-label","远端已创建对象 ID");itemInput.setAttribute("placeholder","远端核对到的工作项 ID");
    const foundBtn=node("button","人工结算：远端已创建"),missingBtn=node("button","人工结算：远端未创建");
    foundBtn.addEventListener("click",async()=>{const item=itemInput.value.trim();if(!item){message.textContent="请先填写远端核对到的工作项 ID。";return;}
      foundBtn.disabled=true;try{await api("/api/project-bugs/settle-create-verified-created",{draft_id:draft.draft_id,expected_digest:draft.request_digest,created_item_id:item});message.textContent="已按人工核对结算为已创建；请重新读取草稿核对。";}
      catch(error){message.textContent="人工结算未确认："+error.message;foundBtn.disabled=false;}});
    missingBtn.addEventListener("click",async()=>{missingBtn.disabled=true;
      try{await api("/api/project-bugs/settle-create-verified-missing",{draft_id:draft.draft_id,expected_digest:draft.request_digest});message.textContent="已按人工核对结算为未创建；可重新准备草稿。";}
      catch(error){message.textContent="人工结算未确认："+error.message;missingBtn.disabled=false;}});
    card.append(itemInput,foundBtn,missingBtn);}
  if(draft.error_code)card.append(node("p",writeSendReasons[draft.error_code]||draft.error_code));
  if(draft.created_item_id)card.append(node("p",`已创建对象：${draft.created_item_id}；可用下方 8 小时只读授权读取并建立本地 P2 绑定，不会再次创建远端缺陷。`));
  if(draft.state==="created"){
    addCreatedDraftBinding(draft,card);
  }
  if(!["draft","ready","unknown"].includes(draft.state))return card;
  const keyword=node("input"),search=node("button","搜索可能重复的缺陷"),read=node("button","读取查重结果"),results=node("div"),attach=node("button","采用以上查重结果");
  keyword.setAttribute("aria-label","查重关键词");keyword.value=String(draft.field_values?.name||"").slice(0,200);
  read.disabled=true;attach.disabled=true;
  let pendingSearch=null,searchId=null,displayedSearchId=null;
  search.addEventListener("click",async()=>{
    if(search.disabled)return;
    if(!pendingSearch){if(!keyword.value.trim()){message.textContent="请填写能描述问题的查重关键词。";return;}
      pendingSearch={draft_id:draft.draft_id,expected_digest:draft.request_digest,keyword:keyword.value.trim(),request_id:crypto.randomUUID()};}
    search.disabled=true;keyword.disabled=true;attach.disabled=true;displayedSearchId=null;read.disabled=true;searchId=null;
    try{const value=await api("/api/project-bugs/search-create-duplicates",pendingSearch);searchId=value.search_id;read.disabled=false;message.textContent="搜索已排队；读取结果后核对候选。";}
    catch(error){message.textContent="搜索未确认："+error.message;search.disabled=false;
      if([400,403,409].includes(error.http_status)){pendingSearch=null;keyword.disabled=false;}}
  });
  read.addEventListener("click",async()=>{
    if(read.disabled||!searchId)return;read.disabled=true;const observedSearchId=searchId;
    try{const value=await api("/api/project-bugs/search-status",{search_id:observedSearchId});
      if(searchId!==observedSearchId)return;
      if(value.state==="succeeded"){
        const items=value.result?.items||[];results.replaceChildren(...items.map(item=>node("p",`${item.item_id} · ${item.title}`)));
        if(!items.length)results.append(node("p","本次关键词搜索没有匹配项；仍需人工判断是否重复。"));
        const incomplete=value.result?.next_after_id!=null;
        attach.disabled=incomplete||value.authorization_expired;displayedSearchId=attach.disabled?null:observedSearchId;
        message.textContent=incomplete?"候选超过一页，请缩小关键词范围重新搜索。":"请核对候选后采用结果，再确认是否重复。";
        pendingSearch=null;search.disabled=false;keyword.disabled=false;
      }else{message.textContent=["queued","running"].includes(value.state)?"搜索尚未完成，请稍后读取。":"搜索失败或被阻止，请检查授权后重新搜索。";
        if(!["queued","running"].includes(value.state)){pendingSearch=null;search.disabled=false;keyword.disabled=false;}}
    }catch(error){message.textContent="查重结果暂不可读："+error.message;}finally{if(searchId===observedSearchId)read.disabled=false;}
  });
  attach.addEventListener("click",async()=>{
    if(attach.disabled||!displayedSearchId)return;attach.disabled=true;
    try{await api("/api/project-bugs/attach-create-search",{draft_id:draft.draft_id,expected_digest:draft.request_digest,search_id:displayedSearchId});message.textContent="实际搜索结果已记录；重新读取草稿后确认是否重复。";}
    catch(error){message.textContent="查重结果未采用："+error.message;attach.disabled=false;}
  });
  card.append(node("p","搜索仅覆盖当前授权空间和类型；关键词匹配不能证明绝无重复。"),keyword,search,read,results,attach);
  const actions=[["确认不是重复缺陷","confirm-create-not-duplicate"],["标记草稿就绪","mark-create-ready"],["创建并读取绑定","dispatch-create-draft"],["重新打开草稿","reopen-create-draft"],["取消草稿","cancel-create-draft"]];
  for(const [label,action] of actions){const button=node("button",label);
    button.disabled=(action==="mark-create-ready"&&(missing.length>0||!draft.duplicate_confirmed))||(action==="dispatch-create-draft"&&draft.state!=="ready");
    if(action==="dispatch-create-draft"&&draft.state==="ready")card.append(node("p","点击“创建并读取绑定”授权一次创建远端缺陷，并立即用 8 小时只读授权读取和建立本地 P2 绑定；创建、派发或响应结果未知时绝不绑定或自动重试。"));
    if(button.disabled&&action==="mark-create-ready")card.append(node("p","草稿需补齐必填 missing_required 并完成查重确认后才能标记就绪。"));
    button.addEventListener("click",async()=>{if(button.disabled)return;button.disabled=true;
      try{const result=await api(`/api/project-bugs/${action}`,{draft_id:draft.draft_id,expected_digest:draft.request_digest});
        if(action==="dispatch-create-draft"&&result.state==="created"){message.textContent=`远端缺陷 ${result.created_item_id} 已创建，正在提交 8 小时只读读取与本地 P2 绑定。`;card.append(node("p",`已创建对象：${result.created_item_id}；本地绑定仍需远端回读确认。`));await addCreatedDraftBinding({...draft,...result},card).submit(true);}
        else message.textContent=`${label}已提交；请重新读取草稿核对。`;}
      catch(error){message.textContent=`${label}未确认：`+error.message;if(action!=="dispatch-create-draft")button.disabled=false;}
    });card.append(button);
  }
  return card;
}
function addCreatedDraftBinding(draft,card){
    const bind=node("button","读取并绑定新缺陷"),status=node("div"),epoch=bugIntakeEpoch;
    let retryIntakeId=null;
    const terminal=result=>{if(["failed","blocked"].includes(result.state)){retryIntakeId=result.intake_id;bind.textContent="重新读取已创建缺陷";bind.disabled=false;}};
    card.append(node("p","读取此对象并建立 8 小时只读授权；新建本地任务优先级 P2，不会再次创建远端缺陷。"),bind,status);
    const submit=async(auto=false)=>{
      if(bind.disabled||epoch!==bugIntakeEpoch)return;
      bind.disabled=true;
      try{
        const payload={draft_id:draft.draft_id,expected_digest:draft.request_digest,read_hours:8,local_priority:"P2"};
        if(retryIntakeId)payload.retry_intake_id=retryIntakeId;
        const result=await api(retryIntakeId?"/api/project-bugs/retry-created-draft-read":"/api/project-bugs/bind-created-draft",payload);
        if(epoch!==bugIntakeEpoch)return;
        showBugIntake(result,status);
        if(["queued","running"].includes(result.state))bugIntakeWatch={epoch,id:result.intake_id,target:status,busy:false,onTerminal:terminal};
        else terminal(result);
      }catch(error){if(epoch===bugIntakeEpoch){status.replaceChildren(node("p",auto?"远端缺陷已创建，但绑定读取未确认；请用下方按钮重试同一只读读取，不要重复创建。":"绑定读取未确认："+error.message+"。重试只核对同一读取请求，不会再次创建缺陷。"));bind.disabled=false;}}
    };
    bind.addEventListener("click",()=>submit());
    return {submit};
}
setInterval(pollBugActivity,5000);
let bugRefreshWatch=null;
const bugRefreshStates={queued:"等待后台读取",running:"正在读取飞书项目",succeeded:"刷新完成，可重新读取详情查看",failed:"刷新失败，保留原快照",blocked:"刷新已阻止，请核对授权和运行模式"};
const bugRefreshErrors={login_required:"专用 Project 身份需要重新登录",read_timeout:"读取超时",remote_changed:"读取期间远端内容变化",project_read_failed:"接口读取失败",reader_unavailable:"后台读取不可用",newer_snapshot_or_conflict:"已有更新快照或请求冲突",authorization_or_reader_changed:"授权、配置或运行模式变化",interrupted_read_limit:"多次读取中断"};
function showBugRefresh(status,target){target.textContent=`${status.lease_expired?"读取租约已过期，等待后台核对":bugRefreshStates[status.state]||"刷新状态待核对"} · 尝试 ${status.attempt||0} 次${status.error_code?" · "+(bugRefreshErrors[status.error_code]||"请检查后台读取状态"):""}`;}
async function pollBugRefresh(){
  const watch=bugRefreshWatch;
  if(!watch||watch.epoch!==bugDetailEpoch||watch.busy||$("console").hidden)return;
  watch.busy=true;
  try {
    const value=await api("/api/project-bugs/refresh-status",{refresh_id:watch.id});
    if(bugRefreshWatch!==watch||watch.epoch!==bugDetailEpoch)return;
    showBugRefresh(value,watch.target);
    if(!["queued","running"].includes(value.state))bugRefreshWatch=null;
  } catch(error){if(bugRefreshWatch===watch&&watch.epoch===bugDetailEpoch)watch.target.textContent="刷新进度暂不可读，请重新读取详情核对。";}
  finally{watch.busy=false;}
}
function renderBugRefresh(value,epoch,area){
  const control=value.refresh_control,section=node("section",undefined,"note"),message=node("p"),select=node("select"),submit=node("button","从飞书项目刷新");
  section.append(node("h3","飞书项目同步"));
  for(const grant of control.grants){const option=node("option",`读取授权 · 到期 ${grant.expires_at}`);option.value=grant.grant_id;select.append(option);}
  select.value=control.grants[0]?.grant_id||"";select.setAttribute("aria-label","刷新使用的持续授权");
  submit.disabled=!control.available||!control.grants.length;
  if(!control.available)section.append(node("p","后台 Project 读取未启用，或当前运行模式禁止读取。"));
  else if(!control.grants.length)section.append(node("p","请先创建当前 Bug 的读取授权，再重新读取详情。"));
  const active=control.requests.find(r=>["queued","running"].includes(r.state));
  if(active){submit.disabled=true;bugRefreshWatch={epoch,id:active.refresh_id,target:message,busy:false};showBugRefresh(active,message);}
  if(control.requests.length){
    const latest=node("p");showBugRefresh(control.requests[0],latest);section.append(latest);
    if(control.requests.length>1){
      const history=node("details");history.append(node("summary",`查看较早的刷新记录（${control.requests.length-1} 条）`));
      for(const request of control.requests.slice(1)){const row=node("p");showBugRefresh(request,row);history.append(row);}
      section.append(history);
    }
  }
  section.append(select,submit,message);area.append(section);
  let pending=null,sending=false;
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending||submit.disabled)return;
    if(!pending){if(!select.value)return;pending={bug_id:value.bug_id,grant_id:select.value,request_id:crypto.randomUUID()};select.disabled=true;}
    sending=true;submit.disabled=true;message.textContent="正在提交只读刷新…";
    try{
      const result=await api("/api/project-bugs/refresh",pending);
      if(epoch!==bugDetailEpoch)return;
      showBugRefresh(result,message);submit.textContent="刷新请求已记录";
      if(["queued","running"].includes(result.state))bugRefreshWatch={epoch,id:result.refresh_id,target:message,busy:false};
    }catch(error){
      if(epoch===bugDetailEpoch){
        if(error.http_status===409){pending=null;select.disabled=false;message.textContent="刷新未提交："+error.message+"。请重新读取详情核对。";submit.textContent="从飞书项目刷新";}
        else {message.textContent="提交结果待核对："+error.message;submit.textContent="重试同一刷新请求";}
        submit.disabled=false;
      }
    }finally{sending=false;}
  });
}
setInterval(pollBugRefresh,5000);
function renderBugRoundStarter(value,current,epoch,area) {
  const section=node("details",undefined,"note");
  section.append(node("summary",current?"开始新的调查轮次":"建立调查轮次"));
  const message=node("p");
    const label=node("label","调查目的"), reason=node("textarea"), start=node("button","建立调查轮次");
    reason.value="";reason.setAttribute("aria-label","调查目的");label.append(reason);
    if(current)start.textContent="建立下一轮调查";
    if(current?.settlement?.ready===false){start.disabled=true;message.textContent="旧执行或资源尚未核对完毕，请先处理详情中的阻塞事项。";}
    if(current)section.append(node("p","补充本轮验证可使用下方验证计划；新一轮调查会归档本轮记录，不会修改远端 Bug 状态。"));
    section.append(label,start,message);area.append(section);
    let pending=null,sending=false;
    start.addEventListener("click",async()=>{
      if(epoch!==bugDetailEpoch||sending||current?.settlement?.ready===false)return;
      if(!pending) {
        if(!reason.value.trim()){message.textContent="请填写调查目的。";return;}
        pending={bug_id:value.bug_id,request_id:crypto.randomUUID(),reason:reason.value.trim(),expected_revision:value.revision};
        reason.disabled=true;
      }
      sending=true;start.disabled=true;
      try {
        await api("/api/project-bugs/start-round",pending);
        if(epoch===bugDetailEpoch)await loadBugDetail(value.bug_id);
      } catch(error) {
        if(epoch===bugDetailEpoch){
          if(error.http_status===409){pending=null;reason.disabled=false;message.textContent="轮次未建立："+error.message+"。请核对阻塞事项，页面过期时重新读取详情。";start.textContent=current?"建立下一轮调查":"建立调查轮次";}
          else {message.textContent="建立结果待核对："+error.message;start.textContent="重试同一轮次请求";}
          start.disabled=false;
        }
      } finally {sending=false;}
    });
}
function renderBugPlanEditor(value,epoch,area) {
  const current=value.rounds.find(r=>!r.archived_at), section=node("details",undefined,"note");
  if(!current){renderBugRoundStarter(value,current,epoch,area);return;}
  const unstarted=current.execution_state==="planned"&&!(value.investigation_jobs||[]).some(job=>job.round_id===current.round_id);
  if(unstarted||["succeeded","failed","cancelled"].includes(current.execution_state))renderBugRoundStarter(value,current,epoch,area);
  section.append(node("summary","准备 / 调整验证计划"));
  const message=node("p");
  if(!["planned","paused","human","blocked","succeeded","failed","cancelled"].includes(current.execution_state)||current.settlement?.ready===false) {
    section.append(node("p","本轮执行或资源尚未核对完毕，暂不能修改计划。请先处理阻塞事项。"));area.append(section);return;
  }
  const prior=(value.verification_plans||[]).find(p=>p.round_id===current.round_id)?.definition;
  section.append(node("p",`第 ${current.number} 轮。保存产生新的计划版本；不会启动任务、授权设备操作或标记验证通过。`));
  const controls=[], groups={repositories:[],artifacts:[],devices:[],steps:[]}, boxes={};
  let locked=false,pending=null,sending=false;
  function field(parent,label,initial="",type="text") {
    const input=node(type==="textarea"?"textarea":"input"),wrapper=node("label",label);
    if(type!=="textarea")input.type=type;
    input.value=String(initial??"");input.setAttribute("aria-label",label);wrapper.append(input);parent.append(wrapper);controls.push(input);return input;
  }
  function select(parent,label,options,initial="") {
    const input=node("select"),wrapper=node("label",label);
    for(const [key,name] of options){const option=node("option",name);option.value=key;input.append(option);}
    input.value=initial;input.setAttribute("aria-label",label);wrapper.append(input);parent.append(wrapper);controls.push(input);return input;
  }
  const title=field(section,"计划名称",prior?.title||"");
  const names={repositories:"仓库与版本",artifacts:"产物",devices:"设备",steps:"验证步骤"};
  const layers=[["","请选择验证层级"],["static","静态检查"],["build","构建"],["software_test","软件测试"],["ram_boot","RAM 启动"],["persistent_flash","持久刷写回读"],["device_function","实机功能"],["stability","稳定性"]];
  function description(item) {
    return item.kind==="repositories"?item.fields.repository.value : item.kind==="steps"?item.fields.title.value : item.fields.id.value;
  }
  function references() {
    for(const step of groups.steps.filter(x=>x.active)) {
      step.references.replaceChildren();
      for(const kind of ["repositories","artifacts","devices","depends_on"]) {
        const list=kind==="depends_on"?groups.steps:groups[kind], chosen=step.selected[kind];
        const block=node("div"),caption=kind==="depends_on"?"前置步骤":names[kind];block.append(node("p",caption));
        for(const item of list.filter(item=>((item.active&&(kind!=="depends_on"||groups.steps.indexOf(item)<groups.steps.indexOf(step)))||chosen.has(item)))) {
          const input=node("input"),label=node("label");input.type="checkbox";input.checked=chosen.has(item);input.disabled=locked;
          const text=(description(item)||"尚未命名")+(item.active?"":"（已移除，请取消选择）");
          input.setAttribute("aria-label",`${step.fields.title.value||"验证步骤"} · ${caption} · ${text}`);
          label.append(input,node("span",text));block.append(label);
          input.addEventListener("change",()=>{if(locked||epoch!==bugDetailEpoch)return;if(input.checked)chosen.add(item);else chosen.delete(item);});
        }
        step.references.append(block);
      }
    }
  }
  function add(kind,seed={}) {
    if(locked||groups[kind].filter(x=>x.active).length>=100)return;
    const card=node("section",undefined,"note"),item={kind,active:true,key:seed.id||crypto.randomUUID(),fields:{}};
    groups[kind].push(item);boxes[kind].append(card);
    const f=(key,label,type="text",fallback="")=>item.fields[key]=field(card,label,seed[key]??fallback,type);
    if(kind==="repositories") {
      const configured=value.verification_catalog?.repositories||[];
      const choices=[...new Set([...configured,...(seed.repository?[seed.repository]:[])])];
      item.fields.repository=select(card,"仓库",[["","请选择仓库"],...choices.map(name=>[name,configured.includes(name)?name:`${name}（当前未配置）`])],seed.repository||"");
      f("node","代码所在节点","text",value.verification_catalog?.node||"");f("branch","目标分支");f("base_commit","基线完整提交号");f("candidate_commit","候选完整提交号");
    } else if(kind==="artifacts") {f("id","产物名称");f("sha256","产物 SHA-256");}
    else if(kind==="devices") {f("id","设备标识");f("node","设备所在节点");f("identity","设备身份依据");}
    else {
      f("title","步骤名称");item.fields.layer=select(card,"验证层级",layers,seed.layer||"");
      item.fields.required=field(card,"本步骤必须通过","","checkbox");item.fields.required.checked=seed.required!==false;
      f("node","执行节点","text",value.verification_catalog?.node||"");f("environment","验证环境","textarea");
      f("procedure","复现 / 验证步骤","textarea");f("oracle","通过判据","textarea");f("timeout_seconds","超时秒数","number",300);
      item.selected={};
      for(const key of ["repositories","artifacts","devices","depends_on"]) {
        const candidates=key==="depends_on"?groups.steps:groups[key];
        item.selected[key]=new Set(candidates.filter(x=>(seed[key]||[]).includes(x.kind==="repositories"||x.kind==="steps"?x.key:x.fields.id.value)));
      }
      item.references=node("div");card.append(item.references);
    }
    for(const input of Object.values(item.fields))input.addEventListener("input",()=>{if(!locked)references();});
    const remove=node("button","移除"+names[kind]);controls.push(remove);card.append(remove);
    remove.addEventListener("click",()=>{if(locked||epoch!==bugDetailEpoch)return;item.active=false;card.hidden=true;references();});
    references();return item;
  }
  for(const kind of Object.keys(groups)) {
    section.append(node("h4",names[kind]));boxes[kind]=node("div");section.append(boxes[kind]);
    const button=node("button","添加"+names[kind]);controls.push(button);section.append(button);
    button.addEventListener("click",()=>{if(epoch===bugDetailEpoch)add(kind);});
    for(const seed of prior?.[kind]||[])add(kind,seed);
  }
  if(!prior){add("repositories");add("steps");}
  const submit=node("button","保存验证计划"),preview=node("div");
  section.append(node("p","保存前请核对完整提交号和通过判据。硬件验证步骤还需选择设备及产物；长时稳定性验证需结合现有作业时限拆分。"),submit,message,preview);area.append(section);
  function build() {
    const plan={title:title.value.trim()};
    for(const kind of Object.keys(groups))plan[kind]=groups[kind].filter(x=>x.active).map(item=>{
      const result={};
      for(const [key,input] of Object.entries(item.fields))result[key]=key==="required"?input.checked:key==="timeout_seconds"?Number(input.value):input.value.trim();
      if(kind==="repositories"||kind==="steps")result.id=item.key;
      if(kind==="steps")for(const [key,selected] of Object.entries(item.selected)) {
        if([...selected].some(x=>!x.active))throw new Error("步骤仍引用已移除的资源，请取消对应选择。");
        result[key]=[...selected].map(x=>x.kind==="repositories"||x.kind==="steps"?x.key:x.fields.id.value.trim());
      }
      return result;
    });
    if(!plan.title||!plan.repositories.length||!plan.steps.length)throw new Error("请填写计划名称，并添加仓库和验证步骤。");
    for(const repo of plan.repositories)if(!repo.repository||!repo.node||!repo.branch||![repo.base_commit,repo.candidate_commit].every(s=>/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(s)))throw new Error("请补齐仓库、节点、分支和完整提交号。");
    for(const step of plan.steps)if(!step.title||!step.layer||!step.node||!step.environment||!step.procedure||!step.oracle||!step.repositories.length)throw new Error("请补齐步骤说明、环境、通过判据，并选择仓库。");
    return plan;
  }
  submit.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending)return;
    if(!pending) {
      try {pending={bug_id:value.bug_id,round_id:current.round_id,request_id:crypto.randomUUID(),expected_revision:value.revision,plan:build()};}
      catch(error){message.textContent=error.message;return;}
      locked=true;controls.forEach(input=>input.disabled=true);references();
      preview.append(node("p",`本次保存内容已锁定：${pending.plan.repositories.length} 个仓库、${pending.plan.steps.length} 个步骤；修改内容需先重新读取详情。`));
    }
    sending=true;submit.disabled=true;message.textContent="正在保存计划…";
    try {
      const saved=await api("/api/project-bugs/publish-verification-plan",pending);
      if(epoch!==bugDetailEpoch)return;
      message.textContent=`已保存版本 ${saved.version}。未启动执行，也未标记验证通过；重新读取详情可继续调整。`;submit.textContent="计划已保存";
    } catch(error) {
      if(epoch===bugDetailEpoch){
        if(error.http_status===409){pending=null;locked=false;controls.forEach(input=>input.disabled=false);references();message.textContent="计划未保存："+error.message+"。可修改后再保存；页面已过期时请先重新读取详情。";submit.textContent="保存验证计划";}
        else {message.textContent="保存结果待核对："+error.message;submit.textContent="重试同一计划请求";}
        submit.disabled=false;
      }
    } finally {sending=false;}
  });
}
const bugGrantActions={"bug.read":"读取缺陷","bug.fields":"更新指定字段","bug.comment":"添加评论","bug.transition":"指定流程流转","bug.close":"关闭缺陷","bug.create":"创建缺陷","code.read":"读取代码","code.edit":"修改代码","code.build":"构建","code.test":"测试","code.commit":"本地提交","code.push":"推送","code.merge":"合并","device.read":"读取设备","device.reset":"复位","device.ram_boot":"RAM 启动","device.flash":"持久刷写"};
function verificationReviewButton(runId,epoch,card) {
  const open=node("button",`核验执行证据 · ${runId}`),panel=node("section");let generation=0;
  open.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch)return;
    const version=++generation,current=()=>epoch===bugDetailEpoch&&version===generation;
    open.disabled=true;panel.replaceChildren(node("p","正在读取核验证据…"));
    try {
      const view=await api("/api/project-bugs/verification-review-detail",{run_id:runId});
      if(!current())return;
      panel.replaceChildren(node("h4",view.step.title),node("p",`步骤类型：${view.step.layer} · 节点 ${view.step.node}`),node("p",`环境：${view.step.environment}`),node("p",`执行步骤：${view.step.procedure}`),node("p",`通过判据：${view.step.oracle}`),node("pre",view.remote.command));
      for(const repo of view.bindings.repositories)panel.append(node("p",`候选提交：${repo.repository} · ${repo.candidate_commit}`));
      panel.append(node("p",`证据摘要：${view.evidence_digest}。源码采样不覆盖未跟踪文件、构建环境或功能判据，需单独核对。`));
      const labels={obsolete_binding:"计划、执行尝试或调查已变化",execution_not_successful:"缺少成功执行回执",source_not_verified:"缺少匹配的执行前后源码证据",device_evidence_unavailable:"缺少独立设备证据",artifact_evidence_unavailable:"缺少独立产物证据",dependency_not_passed:"依赖步骤尚未通过",dependency_changed:"依赖步骤的执行或核验记录已变化"};
      for(const reason of view.pass_blockers)panel.append(node("p",`不能记录通过：${labels[reason]||reason}`));
      if(view.review)panel.append(node("p",`已有人工核验：${view.review.verdict} · ${view.review.actor} · ${view.state}`),node("pre",view.review.rationale));
      for(const channel of ["stdout","stderr"]) {
        const read=node("button",`读取 ${channel} 证据`),log=node("pre");let offset=0,loading=false;
        read.disabled=!view.execution.receipt;
        read.addEventListener("click",async()=>{
          if(!current()||loading||offset===null)return;
          loading=true;read.disabled=true;
          try {
            const page=await api("/api/project-bugs/verification-review-output",{run_id:runId,evidence_digest:view.evidence_digest,channel,offset});
            if(!current())return;
            log.textContent+=page.text;offset=page.next_offset;read.textContent=offset===null?`${channel} 已读完`:`继续读取 ${channel}`;read.disabled=offset===null;
          } catch(error){if(current()){panel.append(node("p","证据读取失败："+error.message));read.disabled=false;}}
          finally {loading=false;}
        });panel.append(read,log);
      }
      if(!view.review_available){panel.append(node("p","当前执行仍在进行或已过期，不能记录新的核验结论。"));return;}
      const verdict=node("select"),rationale=node("textarea"),attest=node("input"),label=node("label"),save=node("button","记录人工核验"),status=node("p");
      verdict.setAttribute("aria-label","核验结论");rationale.setAttribute("aria-label","核验依据与差异");attest.type="checkbox";attest.checked=false;
      for(const [value,text] of [["inconclusive","证据不足"],["failed","不通过"],["passed","通过本步骤"]]) {const option=node("option",text);option.value=value;option.disabled=value==="passed"&&view.pass_blockers.length>0||value==="failed"&&!view.execution.receipt;verdict.append(option);}
      verdict.value="inconclusive";label.append(attest,node("span","我已核对本步骤的实际环境、执行记录和判据；此结论只适用于所示版本与证据。"));
      let pending=null,sending=false;
      save.addEventListener("click",async()=>{
        if(!current()||sending)return;
        if(!pending) {
          if(!attest.checked||!rationale.value?.trim()){status.textContent="请填写核验依据并确认已经核对证据。";return;}
          pending={run_id:runId,evidence_digest:view.evidence_digest,expected_review_id:view.review?.review_id||null,request_id:crypto.randomUUID(),verdict:verdict.value,rationale:rationale.value.trim(),attested:true};
          verdict.disabled=rationale.disabled=attest.disabled=true;
        }
        sending=true;save.disabled=true;
        try {const result=await api("/api/project-bugs/record-verification-review",pending);if(current())status.textContent=`人工核验已记录：${result.review_id}。重新读取 Bug 详情查看汇总；不会自动关闭缺陷。`;}
        catch(error){if(current()){status.textContent="记录未确认："+error.message;save.disabled=false;save.textContent="重试同一核验请求";}}
        finally {sending=false;}
      });panel.append(verdict,rationale,label,save,status);
    } catch(error){if(current())panel.replaceChildren(node("p","读取核验失败："+error.message));}
    finally {if(current())open.disabled=false;}
  });card.append(open,panel);
}
function resultCommentEditor(draft,epoch,panel) {
  const section=node("section"),text=node("textarea"),grant=node("select"),more=node("button","读取评论授权"),save=node("button","保存待写回评论"),message=node("p");
  text.setAttribute("aria-label","拟写回评论全文");text.maxLength=20000;
  grant.setAttribute("aria-label","评论持续授权");
  text.value=`调查记录（待核验）\n任务：${draft.job_id}\n报告摘要：${draft.report.digest}\n\n`+
    [["root_cause","模型分析"],["changes","修改说明"],["verification","模型自报验证"],["risks","风险"]].map(([key,label])=>`${label}：\n${draft.report.sections[key]||"未提供"}${draft.report.truncated_sections.includes(key)?"\n【此节已截断，提交前请补齐或删除】":""}`).join("\n\n")+
    "\n\n以上为调查说明；命令完成、修复完成、功能验证通过和缺陷关闭需分别核验。";
  let cursor="",loading=false,pending=null,sending=false;
  const known=new Set();save.disabled=true;
  more.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||loading||pending)return;
    loading=true;more.disabled=true;
    try {
      const page=await api("/api/project-bugs/list-grants",{bug_id:draft.bug_id,after_id:cursor});
      if(epoch!==bugDetailEpoch||pending)return;
      for(const item of page.items)if(item.status==="active"&&item.scope.actions.includes("bug.comment")&&!known.has(item.grant_id)) {
        known.add(item.grant_id);const option=node("option",`评论授权 · 到期 ${item.expires_at} · ${item.grant_id}`);option.value=item.grant_id;grant.append(option);
        if(!grant.value)grant.value=item.grant_id;
      }
      cursor=page.next_cursor;more.textContent=cursor?"读取更多评论授权":"评论授权已读完";more.disabled=!cursor;
      save.disabled=known.size===0;message.textContent=known.size?"请审阅全文并选择授权；保存仅创建待写回记录。":"尚无有效评论授权，请先在本 Bug 的持续授权中添加。";
    } catch(error) {if(epoch===bugDetailEpoch){message.textContent="读取授权失败："+error.message;more.disabled=false;}}
    finally {loading=false;}
  });
  save.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||sending)return;
    if(!pending) {
      if(!text.value.trim()||text.value.length>20000||!known.has(grant.value)){message.textContent="请填写不超过 20000 字符的评论并选择有效授权。";return;}
      pending={bug_id:draft.bug_id,job_id:draft.job_id,result_digest:draft.report.digest,
        snapshot_id:draft.snapshot_id,expected_revision:draft.bug_revision,grant_id:grant.value,
        request_id:crypto.randomUUID(),text:text.value};
      text.disabled=grant.disabled=more.disabled=true;
    }
    sending=true;save.disabled=true;
    try {
      const operation=await api("/api/project-bugs/prepare-result-comment",pending);
      if(epoch!==bugDetailEpoch)return;
      message.textContent=`已保存 ${operation.operation_id} · ${operation.state}。尚未发送；重新读取 Bug 详情可审阅或取消待写回记录。`;save.textContent="待写回评论已保存";
    } catch(error) {
      if(epoch===bugDetailEpoch){message.textContent="保存未确认："+error.message+"。可重试同一请求；内容需修改时请重新读取详情。";save.textContent="重试同一评论请求";save.disabled=false;}
    } finally {sending=false;}
  });
  section.append(node("h4","审阅并准备评论回填"),node("p","请核对下面将保存的完整文字。保存会检查持续授权和报告版本，不会立即发送。"),text,grant,more,save,message);panel.append(section);
}
function investigationResultButton(bugId,jobId,epoch,area) {
  const button=node("button",`查看结果草稿 · ${jobId}`), panel=node("section");
  let pending=false;
  button.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||pending)return;
    pending=true;button.disabled=true;panel.replaceChildren(node("p","正在整理结果草稿…"));
    try {
      const draft=await api("/api/project-bugs/investigation-result",{bug_id:bugId,job_id:jobId});
      if(epoch!==bugDetailEpoch)return;
      panel.replaceChildren(node("h3","调查结果草稿 · 仅供审阅"));
      panel.append(node("p",`任务 ${draft.job_id} · 轮次 ${draft.round_id} · 报告对应执行尝试 ${draft.attempt_no}${draft.archived?" · 已归档，请勿作为当前修复结果":""}`));
      panel.append(node("p",`执行：${bugStates[draft.execution_state]||draft.execution_state} · 修复：${bugStates[draft.repair_state]||draft.repair_state} · 验证：${bugStates[draft.verification_state]||draft.verification_state}`));
      panel.append(node("p","模型报告是待核验的说明。以下草稿不写回飞书项目，也不证明功能验证通过或缺陷可以关闭。"));
      if(draft.report.state==="available") {
        panel.append(node("p",`模型报告摘要标识：${draft.report.digest}`));
        if(draft.report.artifact_format==="invalid")panel.append(node("p","产物清单格式不合规，不能用这份报告确认修复。保留原报告与补丁，可创建只读调查补充合规证据；不必重复修改代码或运行无变化的测试。"));
        else if(draft.report.artifact_format==="valid")panel.append(node("p","产物清单格式有效；尚不代表提交、测试或修复已通过独立核验。"));
        const labels={status:"模型自报状态",root_cause:"根因分析",changes:"修改说明",verification:"模型自报验证",board_state:"设备说明",push_state:"推送说明",artifacts:"模型自报产物",risks:"风险",next_action:"建议下一步",reply_draft:"拟回填文字"};
        for(const [key,label] of Object.entries(labels)) {
          panel.append(node("h4",label),node("pre",draft.report.sections[key]||"未提供"));
          if(draft.report.truncated_sections.includes(key))panel.append(node("p","此节过长，当前仅显示前 4000 字符，不能视为完整报告。"));
        }
      } else panel.append(node("p",draft.report.state==="not_received"?"尚未收到当前执行尝试的模型报告。":"报告绑定或完整性核验失败，当前不展示报告内容。"));
      if(draft.review)panel.append(node("p",`已有审核记录：${draft.review.review_id} · ${draft.review.state}。审核记录状态不等于本 Bug 的功能验证结论。`));
      panel.append(node("h4","独立记录与证据缺口"),node("p",`每类最多展示最近 ${draft.evidence_limit} 条。代码采样不覆盖未跟踪文件、构建环境或实机行为。`));
      for(const command of draft.commands)panel.append(node("p",`命令 ${command.request_id} · 执行尝试 ${command.attempt_no} · ${bugStates[command.state]||command.state} · 退出码 ${command.exit_code??"未知"}。命令结果需结合退出码和测试证据判断。`));
      for(const observation of draft.checkout_after) {
        panel.append(node("p",`命令结束后采样 ${observation.request_id} · ${observation.state}${observation.source?" · 实际提交 "+observation.source.head:""}`));
        const change=observation.changeset;
        if(change?.state==="observed") {
          const details=node("details");
          details.append(node("summary",`独立变更集 · ${change.commits.length} 个提交 · ${(change.paths||[]).length} 个文件`));
          details.append(node("p",`仓库基线 ${change.base_commit} → 实际提交 ${change.head_commit}`),node("pre",change.commits.join("\n")),node("pre",(change.paths||[]).join("\n")));
          details.append(node("p",`补丁 SHA-256：${change.patch_sha256}`),node("pre",change.patch_text||"基线到目标无文件变化"));
          details.append(node("p",change.tracked_content_matches?"采样时已跟踪内容与提交一致。":"采样时存在未提交的已跟踪内容；以下补丁不覆盖这些修改。"));
          if((change.untracked_paths||[]).length)details.append(node("p","以下未跟踪文件（包括忽略项）不在提交补丁中："),node("pre",change.untracked_paths.join("\n")));
          details.append(node("p","这是单仓库、单次代码观测，仍需审查修复范围；不代表修复完成、构建或设备验证通过。"));
          panel.append(details);
        } else panel.append(node("p","本次缺少完整变更集（历史记录、采集失败或超出大小限制）；不能仅凭提交摘要确认修复完成。"));
      }
      if(!draft.checkout_after.length)panel.append(node("p","缺少命令结束后的独立代码采样。"));
      panel.append(node("p","下一步：核对报告、提交和独立采样，再按验证计划取得功能及所需实机证据。"));
      if(draft.report.state==="available"&&!draft.archived&&draft.snapshot_id)resultCommentEditor(draft,epoch,panel);
    } catch(error) {if(epoch===bugDetailEpoch)panel.replaceChildren(node("p","读取结果草稿失败："+error.message));}
    finally {pending=false;if(epoch===bugDetailEpoch)button.disabled=false;}
  });
  area.append(button,panel);
}
function repairReviewButton(bugId,roundId,epoch,area) {
  const open=node("button","复核本轮修复范围"),panel=node("section");let generation=0;
  open.addEventListener("click",async()=>{
    if(epoch!==bugDetailEpoch||open.disabled)return;
    open.disabled=true;const currentGeneration=++generation,current=()=>epoch===bugDetailEpoch&&generation===currentGeneration;
    panel.replaceChildren(node("p","正在读取本轮仓库与变更证据…"));
    try {
      const view=await api("/api/project-bugs/repair-review-detail",{bug_id:bugId,round_id:roundId});
      if(!current())return;
      panel.replaceChildren(node("h3","修复范围复核"),node("p","逐一审查本轮已声明仓库的完整变更。此结论不代表构建、功能或实机验证通过，也不会关闭缺陷。"));
      const labels={round_archived:"调查已归档",execution_unsettled:"执行或资源尚未收尾",scope_limit:"仓库调查记录超出当前完整读取上限",input_unavailable:"任务输入绑定不可核验",no_repositories:"尚未声明调查仓库",repository_not_investigated:"缺少该仓库的调查证据",current_changeset_unavailable:"缺少当前执行对应的完整变更集",untracked_content:"尚有未纳入提交的文件（含忽略项）",uncommitted_tracked_content:"已跟踪内容尚未提交",verification_candidate_mismatch:"验证计划版本与实际变更不一致",round_changeset_chain_incomplete:"缺少从本轮原始基线开始的完整变更链",repository_evidence_incomplete:"仓库证据尚不完整",no_committed_change:"尚无已提交的代码变更"};
      for(const reason of view.ready_blockers)panel.append(node("p",labels[reason]||reason));
      for(const repo of view.repositories) {
        const section=node("details");section.append(node("summary",repo.repository),node("p",`${repo.base_commit||"未知基线"} → ${repo.head_commit||"未知提交"}`));
        if(repo.segments) {
          section.append(node("p",`本轮审查基线：${repo.review_base_commit}。按顺序审查以下全部变更段：`));
          for(const segment of repo.segments)section.append(node("p",`${segment.base_commit} → ${segment.head_commit}`),node("pre",segment.commits.join("\n")),node("pre",segment.patch_text||"无文件差异"),node("p",`补丁摘要：${segment.patch_sha256}`));
        } else if(repo.commits)section.append(node("pre",repo.commits.join("\n")),node("pre",repo.patch_text||"无文件差异"),node("p",`补丁摘要：${repo.patch_sha256}`));
        for(const reason of repo.blockers)section.append(node("p",labels[reason]||reason));
        panel.append(section);
      }
      if(view.review)panel.append(node("p",`已有结论：${bugStates[view.review.verdict]||view.review.verdict} · ${view.review_state==="current"?"仍适用":"证据已变化，需重新复核"}`),node("pre",view.review.rationale));
      if(!view.review_available){panel.append(node("p","当前不能记录新结论，请先处理阻塞。"));return;}
      const verdict=node("select"),rationale=node("textarea"),attest=node("input"),label=node("label"),save=node("button","记录修复复核"),status=node("p");
      verdict.setAttribute("aria-label","修复复核结论");rationale.setAttribute("aria-label","修复范围与复核依据");attest.type="checkbox";
      for(const [key,text] of [["in_progress","仍需修复"],["ready","确认修复范围已完成"],["not_applicable","无需代码修复（须说明原因）"]]){const option=node("option",text);option.value=key;option.disabled=key==="ready"&&!view.can_mark_ready;verdict.append(option);}
      verdict.value="in_progress";label.append(attest,node("span","我已审查全部声明仓库、变更与证据缺口"));let pending=null,sending=false;
      save.addEventListener("click",async()=>{
        if(!current()||sending||save.disabled)return;
        if(!pending){
          if(!attest.checked||!rationale.value.trim()){status.textContent="请填写复核依据并确认已审查范围。";return;}
          if(verdict.value==="ready"&&!view.can_mark_ready){status.textContent="当前证据不足，不能确认修复完成。";return;}
          pending={bug_id:bugId,round_id:roundId,request_id:crypto.randomUUID(),evidence_digest:view.evidence_digest,expected_review_id:view.review?.review_id||null,verdict:verdict.value,rationale:rationale.value.trim(),attested:true};
          verdict.disabled=rationale.disabled=attest.disabled=true;
        }
        sending=true;save.disabled=true;open.disabled=true;
        try{const result=await api("/api/project-bugs/record-repair-review",pending);if(current())status.textContent=`修复复核已记录：${result.review_id}。重新读取详情查看状态；验证和关闭结果保持独立。`;}
        catch(error){if(current()){status.textContent="复核结果待核对："+error.message;save.disabled=false;save.textContent="重试同一修复复核请求";}}
        finally{sending=false;if(current())open.disabled=false;}
      });panel.append(verdict,rationale,label,save,status);
    }catch(error){if(current())panel.replaceChildren(node("p","读取修复范围失败："+error.message));}
    finally{if(current())open.disabled=false;}
  });area.append(open,panel);
}
function bugLifecycleNotice(round) {
  if(!round)return "";
  if(round.archived_at)return "此轮已归档；以下结果仅作历史记录，不代表当前 Bug 已修复或验证通过。";
  const state=round.lifecycle?.state;
  return ({reopened:"已观测到 Bug 重新打开。本轮修复与验证仅作历史记录，须开始新轮次重新调查和验证。",closed_once:"本轮已用于关闭缺陷。再次处理须开始新轮次，不能复用本轮关闭证据。",unavailable:"轮次与缺陷流程的对应证据暂不可用，当前修复和验证结论须核对。",archived:"此轮已归档；以下结果仅作历史记录。"})[state]||"";
}
function renderBugOutcome(value,area) {
  const rounds=value.rounds||[],active=rounds.find(round=>!round.archived_at),latest=active||rounds[rounds.length-1];
  const notice=bugLifecycleNotice(latest);
  if(notice)area.append(node("p",notice,"note"));
  const prefix=notice?(latest?.lifecycle?.state==="unavailable"?"适用性待核对的轮次记录 · ":"历史轮次记录 · "):"";
  area.append(node("p",`${prefix}执行：${latest?bugStates[latest.execution_state]||latest.execution_state:"尚无调查轮次"} · 修复：${latest?bugStates[latest.repair_state]||latest.repair_state:"未开始"} · 验证：${latest?bugStates[latest.verification_state]||latest.verification_state:"未验证"}`));
}
async function loadBugDetail(bugId) {
  bugRefreshWatch=null;
  bugActivityWatches={};
  bugWriteSendWatches={};
  const epoch=++bugDetailEpoch, area=$("bug-detail");
  area.replaceChildren(node("p","正在读取 Bug 详情…"));
  try {
    const value=await api("/api/project-bugs/detail",{bug_id:bugId});
    if(epoch!==bugDetailEpoch)return;
    const snapshot=value.snapshot;
    const localOnly=value.project_key==="synthetic-local-only";
    area.replaceChildren(node("h2",`Bug ${value.item_id}`));
    area.append(node("p",localOnly?"本地合成验收：没有飞书项目远端对象或状态。":snapshot?`远端状态标识：${snapshot.status_id} · 最近同步：${snapshot.observed_at}`:"尚未取得远端状态"));
    if(snapshot?.read_evidence){
      const evidence=snapshot.read_evidence;
      area.append(node("p",`远端显示状态：${evidence.status_name} · 字段分页已读完；${evidence.unobserved_field_keys.length} 个定义字段未返回值，不视为空值。前后读取一致仍不代表原子快照。`));
    }
    renderBugOutcome(value,area);
    renderBugRoles(snapshot,area);
    const refresh=node("button","重新读取详情"), control=node("button","接管 / 查看事项控制");
    refresh.addEventListener("click",()=>loadBugDetail(bugId));
    control.addEventListener("click",()=>{openDetailDialog();loadDetail(value.case_id);});
    area.append(refresh,control,node("h3","处理轮次"));
    if(!localOnly&&value.refresh_control)renderBugRefresh(value,epoch,area);
    if(!localOnly&&value.activity_control){for(const kind of ["comments","history","relations","attachments"])renderBugActivity(value,epoch,area,kind);renderBugDownloads(value,epoch,area);}
    for(const round of value.rounds)area.append(node("p",`第 ${round.number} 轮：${round.reason} · ${bugStates[round.execution_state]||round.execution_state}${round.archived_at?" · 已归档":""}`));
    for(const round of value.rounds)if(round.settlement&&!round.settlement.ready) {
      const labels={job_active:"编码任务尚未结束",launch_unsettled:"执行进程启动或退出待核对",resources_unsettled:"执行资源尚未确认释放",command_unsettled:"命令结果或清理待核对",verification_unsettled:"验证执行尚未核对完毕"};
      area.append(node("p",`本轮有 ${round.settlement.blocker_count} 项执行或资源阻塞；任务状态本身不能证明资源已经释放。`));
      for(const blocker of round.settlement.blockers)area.append(node("p",`${labels[blocker.kind]||"执行记录待核对"} · ${blocker.identity}`));
    }
    if(!value.rounds.length)area.append(node("p","尚未建立调查计划。"));
    const activeRound=value.rounds.find(r=>!r.archived_at);
    if(activeRound)repairReviewButton(value.bug_id,activeRound.round_id,epoch,area);
    const currentLifecycle=!activeRound?.lifecycle||activeRound.lifecycle.state==="current";
    if(activeRound&&currentLifecycle&&["planned","running"].includes(activeRound.execution_state)) {
      const launch=node("button","发起本轮编码调查");
      launch.addEventListener("click",()=>{if(epoch!==bugDetailEpoch)return;openDetailDialog();openCodingTask(value.case_id,{bug_id:value.bug_id,round_id:activeRound.round_id,expected_revision:value.revision});});
      area.append(launch);
    }
    if(activeRound&&currentLifecycle&&["succeeded","failed","cancelled"].includes(activeRound.execution_state)&&activeRound.settlement?.ready&&!(value.verification_plans||[]).some(plan=>plan.round_id===activeRound.round_id)) {
      const predecessors=(value.investigation_jobs||[]).filter(job=>job.round_id===activeRound.round_id&&["succeeded","failed","cancelled"].includes(job.state));
      if(predecessors.length){
        const label=node("label","接续的前序任务"),select=node("select"),launch=node("button","接续本轮调查");
        select.setAttribute("aria-label","接续的前序任务");
        for(const job of predecessors){const option=node("option",`${job.repositories?.join("、")||"仓库未记录"} · ${job.job_id} · ${bugStates[job.state]||job.state}`);option.value=job.job_id;select.append(option);}
        select.value=predecessors[0].job_id;label.append(select);
        launch.addEventListener("click",()=>{if(epoch!==bugDetailEpoch||!predecessors.some(job=>job.job_id===select.value))return;openDetailDialog();openCodingTask(value.case_id,{bug_id:value.bug_id,round_id:activeRound.round_id,expected_revision:value.revision,predecessor_job_id:select.value});});
        area.append(label,launch,node("p","可继续选择本仓库或另一个仓库；前序任务结束不代表修复通过。已有验证计划时须建立新轮，保留旧证据。"));
      }
    }
    for(const job of value.investigation_jobs||[]) {
      const verifying=job.purpose==="verification";
      area.append(node("p",`${verifying?"独立验证任务":"编码调查"} ${job.job_id} · ${job.agent} · ${bugStates[job.state]||job.state}${job.error_class?" · "+job.error_class:""}。${verifying?"命令执行成功仍须复核本步骤证据。":"执行成功仍需核验修复和测试证据。"}`));
      investigationResultButton(bugId,job.job_id,epoch,area);
      if(job.predecessor_job_id)area.append(node("p",`接续前序任务：${job.predecessor_job_id}。来源提交仍按本任务的基线与补丁来源单独核验。`));
    }
    for(const job of value.investigation_jobs||[]) {
      const evidence=node("details"), observations=(job.source_observations||[]).length+(job.checkout_observations||[]).length+(job.checkout_after_observations||[]).length;
      evidence.append(node("summary",`代码与工作副本采样 · ${job.job_id} · ${observations} 条记录`));
      if(job.source)evidence.append(node("p",`计划基线：${job.source.branch} · ${job.source.base_commit} · 节点 ${job.source.node} · 版本 ${job.source.version||"未指定"}。${job.checkout_observations?.length?"工作副本见命令开始前采样。":"实际工作副本尚待独立核验。"}`));
      for(const observation of job.source_observations||[])evidence.append(node("p",`${job.source?.candidate_request_id?"前序补丁基线核验":"源仓库基线核验"}：${({matched:"匹配",mismatch:"不匹配",unavailable:"无法核验"})[observation.state]||"未知"} · ${observation.recorded_at}。此证据不证明实际工作副本或修复结果。`));
      for(const observation of job.checkout_observations||[])evidence.append(node("p",`命令开始前工作副本：${observation.source.path} · ${observation.source.head} · ${observation.source.tracked_content_matches?"已跟踪文件与提交一致":"包含未提交修改"}。这是开始前采样，不是命令使用来源或修复通过证明。`));
      for(const observation of job.checkout_after_observations||[])evidence.append(node("p",`命令结束后工作副本：${({clean:"已跟踪代码与当前提交一致",dirty:"检测到未提交修改",mismatch:"提交来源或采样一致性异常",unavailable:"无法完成核验"})[observation.state]||"未知"}${observation.source?" · "+observation.source.head:""}。命令退出状态、代码状态和功能验证结论分别判断。`));
      area.append(evidence);
    }
    area.append(node("h3","验证计划"));
    if(!(value.verification_plans||[]).length)area.append(node("p","尚无验证计划，不能据构建结果判定缺陷已解决。"));
    for(const plan of value.verification_plans||[]) {
      const card=node("section",undefined,"note");
      card.append(node("h3",`${plan.definition.title} · 版本 ${plan.version}`));
      const lifecycleNotice=bugLifecycleNotice(value.rounds.find(round=>round.round_id===plan.round_id));
      if(lifecycleNotice)card.append(node("p",lifecycleNotice,"note"));
      card.append(node("p",`验证：${bugStates[plan.verification_state]||"未验证"}。通过结论只适用于本计划版本；人工核验与执行回执分开记录，不会自动关闭缺陷。`));
      if(!(plan.operator_verification?.steps||[]).some(step=>step.review))card.append(node("p","尚无可信执行证据和核验结论证明本计划功能通过。"));
      if(plan.operator_verification&&!plan.operator_verification.functional_step_required)card.append(node("p","本计划缺少必需的功能验证步骤，静态检查或构建通过不能形成整体验证通过结论。"));
      for(const step of plan.operator_verification?.steps||[])card.append(node("p",`步骤核验 ${step.step_id}：${({passed:"人工核验通过",failed:"人工核验不通过",inconclusive:"证据不足",stale:"旧结论已失效"})[step.state]||bugStates[step.state]||step.state}`));
      for(const repo of plan.definition.repositories)card.append(node("p",`${repo.repository} · ${repo.branch} · ${repo.node} · 候选提交 ${repo.candidate_commit}`));
      for(const step of plan.definition.steps) {
        card.append(node("h4",`${step.title} · ${step.required?"必需":"可选"}`));
        card.append(node("p",`节点：${step.node}；环境：${step.environment}`),node("p",`步骤：${step.procedure}`),node("p",`通过判据：${step.oracle}`));
        if(activeRound&&currentLifecycle&&activeRound.round_id===plan.round_id&&["planned","succeeded","failed","cancelled"].includes(activeRound.execution_state)&&activeRound.settlement?.ready&&["static","build","software_test"].includes(step.layer)&&step.repositories.length>=1&&!step.artifacts.length&&!step.devices.length) {
          const selectedRepositories=step.repositories.map(id=>plan.definition.repositories.find(repo=>repo.id===id));
          const repo=selectedRepositories[0];
          const launch=node("button",`独立验证：${step.title}`);
          launch.disabled=(step.depends_on||[]).some(id=>!(plan.operator_verification?.steps||[]).some(item=>item.step_id===id&&item.state==="passed"));
          if(launch.disabled)card.append(node("p","前置步骤尚未完成证据核验，暂不能启动本步骤。"));
          launch.addEventListener("click",()=>{if(launch.disabled||epoch!==bugDetailEpoch)return;openDetailDialog();openCodingTask(value.case_id,{bug_id:value.bug_id,round_id:activeRound.round_id,expected_revision:value.revision},{plan_id:plan.plan_id,step_id:step.id,...repo,repositories:selectedRepositories,oracle:step.oracle,procedure:step.procedure,environment:step.environment});});
          card.append(launch);
        }
      }
      for(const run of plan.runs||[]) {
        verificationReviewButton(run.run_id,epoch,card);
        const assessment=(plan.operator_verification?.steps||[]).find(item=>item.step_id===run.step_id&&item.run_id===run.run_id);
        const reviewed=assessment?.review?({passed:"独立核验通过（仅限本次运行及其证据）",failed:"独立核验不通过",inconclusive:"独立核验证据不足",stale:"旧核验结论已失效，须重新核对证据"})[assessment.state]:null;
        card.append(node("p",`步骤 ${run.step_id} · 执行：${({prepared:"待派发",queued:"已排队",succeeded:"命令执行成功"})[run.execution_state]||bugStates[run.execution_state]||run.execution_state} · ${reviewed||"版本、环境和通过判据尚待独立核验"}`));
        if(run.receipt)card.append(node("p",`实际退出码：${run.receipt.exit_code} · 回执时间：${run.receipt.finished_at}`));
        if(run.workspace_preparation)card.append(node("p",`验证副本准备：${({queued:"等待准备",running:"准备中",succeeded:"已准备",failed:"准备失败",cancelled:"已取消",unknown:"结果待核对"})[run.workspace_preparation.state]||"结果待核对"}。副本准备完成不代表测试已执行或通过。`));
        for(const observation of run.source_observations||[]) {
          card.append(node("p",`${observation.phase==="before"?"执行前":"执行后"}源码核对：${({matched:"与候选提交一致",mismatch:"不一致",unavailable:"无法核对"})[observation.state]||observation.state}（时点检查，不代表功能通过）`));
          for(const source of observation.sources||[])card.append(node("p",`${source.repository} · 实际提交 ${source.head} · 已跟踪文件 ${source.tracked_count} 个；未覆盖忽略文件、未跟踪文件和构建环境`));
        }
      }
      area.append(card);
    }
    if(value.plan_controls_available)renderBugPlanEditor(value,epoch,area);
    if(!localOnly&&value.grant_controls_available)renderBugGrants(value,epoch,area);
    if(!localOnly&&value.snapshot&&value.grant_controls_available){renderBugCommentComposer(value,epoch,area);renderBugFieldEditor(value,epoch,area);renderBugWorkflow(value,epoch,area);}
    renderCloseApprovals(value,epoch,area,Boolean(!localOnly&&value.snapshot&&activeRound&&currentLifecycle));
    area.append(node("h3","拟写回与结果"),node("p","以下差异基于最近同步快照；实际写入前仍需重新核对。","muted"));
    if(!value.remote_dispatch_available)area.append(node("p","此处不直接执行远端写入；请使用各操作下方的发送控制，发送前会重新核验权限与远端状态。"));
    for(const operation of value.operations) {
      const card=node("section",undefined,"note");
      card.append(node("h3",`${bugStates[operation.state]||operation.state} · ${bugGrantActions[operation.action]||operation.action}`));
      const historical=operation.state==="confirmed";
      const history=historical?node("details"):card;
      if(historical){
        const fields=operation.preview?.differences?.map(item=>value.snapshot?.read_evidence?.field_names?.[item.field]||item.field)||[];
        card.append(node("p",fields.length?`已确认字段：${fields.join("、")}`:"本次操作已确认；原始回执可展开核对。"));
        history.append(node("summary","查看历史差异与原始回执"));
      }
      if(operation.preview) {
        for(const field of operation.preview.differences) {
          const labels={change:"拟修改",already_matches:"已符合",conflict:historical?"当时读取值与原值不同":"同事修改冲突",unavailable:"字段未读取完整"};
          const fieldName=value.snapshot?.read_evidence?.field_names?.[field.field]||field.field;
          history.append(node("p",`${fieldName} · 历史预览：${labels[field.state]||field.state}`),node("p",`原值：${bugFieldValue(field.field,field.base,field.base_present,value.snapshot)}；当时值：${bugFieldValue(field.field,field.current,field.current_present,value.snapshot)}；拟写值：${bugFieldValue(field.field,field.proposed,true,value.snapshot)}`));
          const observed=value.snapshot?.fields||{},unobserved=new Set([...(value.snapshot?.read_evidence?.unobserved_field_keys||[]),...(value.snapshot?.read_evidence?.omitted_value_field_keys||[])]),latestPresent=Object.prototype.hasOwnProperty.call(observed,field.field)&&!unobserved.has(field.field),latest=observed[field.field];
          if(!latestPresent)history.append(node("p","最新留存快照：该字段未读取到，不能据此判断。"));
          else if(JSON.stringify(latest)===JSON.stringify(field.proposed))history.append(node("p",historical&&operation.result?.settled_by_human===true?"最新留存快照已符合拟写值；操作已由人工结算确认。该快照不是新的远端读取。":"最新留存快照已符合拟写值；这不代表新的远端读取。"));
          else if(!historical&&field.base_present&&JSON.stringify(latest)===JSON.stringify(field.base))history.append(node("p","最新留存快照仍为原值；派发前会重新读取远端并检查同事修改。"));
          else if(!historical)history.append(node("p","最新留存快照与原值和拟写值均不同，可能已有同事修改；派发前须重新读取远端核对。"));
          else history.append(node("p","最新留存快照与历史拟写值不同；本次确认依据请查看原始回执和操作记录。"));
          const raw=node("details");raw.append(node("summary","查看历史预览原始字段值"),node("pre",JSON.stringify(field,null,2)));history.append(raw);
          const latestRaw=node("details");latestRaw.append(node("summary","查看最新留存快照字段值"),node("pre",JSON.stringify({field:field.field,present:latestPresent,value:latest},null,2)));history.append(latestRaw);
        }
        if(!operation.preview.differences.length)history.append(node("pre",JSON.stringify(operation.preview.change,null,2)));
      }
      if(operation.result)history.append(node("pre",JSON.stringify(operation.result,null,2)));
      if(historical)card.append(history);
      const operationControls=historical?history:card;
      renderCommentSend(operation,epoch,operationControls);
      renderWriteSend(operation,epoch,operationControls);
      renderUnknownWriteSettlement(operation,epoch,operationControls);
      renderCommentReconcile(value,operation,epoch,operationControls);
      if(operation.can_cancel) {
        const cancel=node("button","取消这次待写回");
        cancel.addEventListener("click",async()=>{
          cancel.disabled=true;
          try {
            await api("/api/project-bugs/cancel-write",{operation_id:operation.operation_id,expected_digest:operation.request_digest});
            if(epoch===bugDetailEpoch)await loadBugDetail(bugId);
          } catch(error) {
            if(epoch===bugDetailEpoch)card.append(node("p","取消结果待核对："+error.message));
          }
        });card.append(cancel);
      }
      area.append(card);
    }
    if(!value.operations.length)area.append(node("p","暂无写回操作。"));
  } catch(error) {
    if(epoch===bugDetailEpoch)area.replaceChildren(node("p","读取失败："+error.message));
  }
}
let csrf = "", busy = false;
const $ = id => document.getElementById(id);
let appearance = "light";
function setAppearance(value, persist = false) {
  appearance = value === "dark" ? "dark" : "light";
  document.documentElement?.setAttribute("data-theme", appearance);
  $("theme-toggle").textContent = appearance === "dark" ? "日间" : "夜间";
  $("theme-toggle").setAttribute("aria-label", appearance === "dark" ? "切换浅色主题" : "切换深色主题");
  $("theme-toggle").setAttribute("aria-pressed", String(appearance === "dark"));
  if(persist){try{localStorage.setItem("feishu-console-theme",appearance);}catch{/* Storage may be disabled. */}}
}
try {
  const saved = localStorage.getItem("feishu-console-theme");
  appearance = ["light","dark"].includes(saved) ? saved : typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
} catch { appearance = "light"; }
setAppearance(appearance);
$("theme-toggle").addEventListener("click",()=>setAppearance(appearance === "dark" ? "light" : "dark",true));
const names = {observe:"观察",collaborate:"协作",auto_60:"自动 60 分钟",auto:"自动",paused:"已暂停",stopped:"完全停止"};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el; }
function notice(text,error=false){$("notice").hidden=false;$("notice").textContent=text;$("notice").className=error?"error":"";}
async function api(path, body){const options={credentials:"same-origin",headers:{}};if(body!==undefined){if(path==="/api/action"&&!body.request_id)body={...body,request_id:crypto.randomUUID()};options.method="POST";options.headers={"Content-Type":"application/json","X-CSRF-Token":csrf};options.body=JSON.stringify(body);}const response=await fetch(path,options);const data=await response.json();if(!response.ok){const error=new Error(data.error||"请求未确认成功");error.http_status=response.status;throw error;}return data;}
function row(title,detail,badge,style=""){const el=node("article",undefined,"item"),content=node("div");content.append(node("h3",title),node("div",detail,"metadata"));el.append(content,node("span",badge,"badge "+style));return el;}
let knowledgeBrowsing=false;
function list(id,rows,empty,force=false){if(id==="knowledge-list"&&knowledgeBrowsing&&!force)return;$(id).replaceChildren(...(rows.length?rows:[node("div",empty,"empty")]));}
let detailRequest = 0, currentDetail = null, detailCase = null;
let detailKind = "case";
let detailOpener = null, detailOpenerLabel = null;
function openDetailDialog() {
  if ($("case-dialog").open) return;
  detailOpener = document.activeElement;
  detailOpenerLabel = detailOpener?.getAttribute?.("aria-label");
  $("case-dialog").showModal();
}
let queueSnapshot = null, queueRequest = 0, queueView = "all";
async function loadQueue(cursor = null) {
  const request = ++queueRequest;
  try {
    const value = await api("/api/workbench", {view:queueView, cursor});
    if (request !== queueRequest) return;
    queueSnapshot = value;
    list("queue", value.items.map(queueItem), "当前筛选没有事项，可重新读取或切换筛选。");
    $("queue-prev").disabled = !value.previous_cursor;
    $("queue-next").disabled = !value.next_cursor;
    $("queue-count").textContent = `本页 ${value.items.length} 项 · 本次范围内 ${value.total_items} 项`;
  } catch(error) {
    if (request !== queueRequest) return;
    queueSnapshot = null;
    list("queue", [], "读取失败：" + error.message);
    $("queue-prev").disabled = $("queue-next").disabled = true;
    $("queue-count").textContent = "请重新读取";
  }
}
function detailControls(loading = false) {
  $("case-prev").disabled = loading || !currentDetail || currentDetail.page <= 1;
  $("case-next").disabled = loading || !currentDetail || currentDetail.page >= currentDetail.page_count;
  $("case-refresh").disabled = loading;
}
function detailActions(value) {
  let area = $("case-actions");
  if (!area) {
    area = node("div", undefined, "detail-controls");
    area.id = "case-actions";
    $("case-dialog").append(area);
  }
  area.replaceChildren(...(value.actions || []).map(action => {
    const button = node("button", action.label);
    button.addEventListener("click", async () => {
      if (action.label === "交给 AI" && !confirm("将此任务的沟通交回 AI？后续回复仍受运行模式和审批规则限制。")) return;
      const request = detailRequest, caseId = detailCase;
      area.querySelectorAll("button").forEach(el => el.disabled = true);
      detailControls(true);
      try {
        const kind = detailKind;
        const result = await api(kind === "approval" ? "/api/approval-action" : "/api/case-action", {token: action.token, request_id: crypto.randomUUID()});
        if (result.requires_confirmation) {
          if (request === detailRequest && $("case-dialog").open) {
            $("case-status").textContent = result.message + " 取消请关闭窗口或重新读取。";
            detailActions({actions:[result.confirmation]});
            detailControls();
          }
          return;
        }
        notice(result.message);
        if (request === detailRequest && $("case-dialog").open) {
          if (kind === "approval") await loadApproval(caseId); else await loadDetail(caseId);
        }
        await refresh();
      } catch (error) {
        if (request === detailRequest && $("case-dialog").open) {
          currentDetail = null;
          area.replaceChildren();
          $("case-status").textContent = error.message + "；请重新读取核对。";
          detailControls();
        }
      }
    });
    return button;
  }));
  if (value.linked_approvals?.length) {
    area.append(node("span", "最近 20 项关联审批："));
    value.linked_approvals.forEach(approval => {
      const button=node("button", `${approval.approval_type} · ${approval.status}`);
      button.addEventListener("click",()=>loadApproval(approval.approval_id));
      area.append(button);
    });
  }
}
async function openCodingTask(caseId, investigation=null, verification=null) {
  const request = ++detailRequest;
  currentDetail = null;
  detailActions({});
  detailControls(true);
  $("case-content").textContent = "";
  $("case-page").textContent = "";
  $("case-status").textContent = "正在读取可用编码工具…";
  try {
    const options = await api("/api/coding-task-options", {case_id:caseId});
    if (request !== detailRequest || !$("case-dialog").open) return;
    const area = $("case-actions"), form = node("form", undefined, "coding-task-form");
    const repo = node("select"), executor = node("select"), instructions = node("textarea"), acceptance = node("textarea");
    const available = options.items.filter(item => item.status === "configured");
    const fields = [repo, executor, instructions, acceptance];
    const repositories=verification?options.repositories.filter(name=>name===verification.repository):options.repositories;
    repositories.forEach(name => {const option=node("option",name);option.value=name;repo.append(option);});
    available.forEach(item => {
      const option=node("option",`${item.label} · ${item.agent} · ${item.model} · ${item.reasoning}`);
      option.value=item.id;executor.append(option);
    });
    instructions.maxLength=10000;acceptance.maxLength=4000;
    instructions.placeholder="希望修改什么，以及需要遵守的范围";
    acceptance.placeholder="如何确认完成，例如需要通过的构建或测试";
    if(verification){instructions.placeholder="例如：python -m unittest discover -v；这里只填可执行命令，不填任务说明";acceptance.value=`环境：${verification.environment}\n步骤：${verification.procedure}\n通过判据：${verification.oracle}`;acceptance.readOnly=true;}
    ["代码仓库","编码工具",verification?"测试命令（原样执行）":"任务说明","验收要求"].forEach((text,index)=>{
      const label=node("label",text);fields[index].required=true;label.append(fields[index]);form.append(label);
    });
    let sourceFields=null;
    if(investigation) {
      sourceFields={branch:node("input"),base_commit:node("input"),version:node("input")};
      for(const [key,labelText] of [["branch","基线分支"],["base_commit","基线完整提交"],["version","版本名称（可选）"]]) {
        const input=sourceFields[key],label=node("label",labelText);input.setAttribute("aria-label",labelText);
        input.value="";input.maxLength=key==="base_commit"?64:256;input.required=key!=="version";
        label.append(input);form.append(label);fields.push(input);
      }
      const candidate=node("select"), manual=node("option","从配置仓库的基线开始");manual.value="";candidate.append(manual);
      candidate.setAttribute("aria-label","补丁来源");
      const seeds=(options.candidate_choices||[]).filter(item=>!verification||(item.repository===verification.repository&&item.source.base_commit===verification.candidate_commit&&item.source.branch===verification.branch));
      const resetSeeds=()=>{candidate.replaceChildren(manual);candidate.value="";
        for(const item of seeds.filter(item=>item.repository===repo.value)) {
          const option=node("option",`继承任务 ${item.job_id} · ${item.source.base_commit}`);option.value=item.request_id;candidate.append(option);
        }
        ["branch","base_commit","version"].forEach(key=>{sourceFields[key].value=verification?({branch:verification.branch,base_commit:verification.candidate_commit,version:""}[key]):"";sourceFields[key].readOnly=!!verification;});
      };
      resetSeeds();repo.addEventListener("change",resetSeeds);
      candidate.addEventListener("change",()=>{
        const item=seeds.find(item=>item.repository===repo.value&&item.request_id===candidate.value);
        for(const key of ["branch","base_commit","version"]){sourceFields[key].value=item?.source[key]||(verification?({branch:verification.branch,base_commit:verification.candidate_commit,version:""}[key]):"");sourceFields[key].readOnly=!!item||!!verification;}
      });
      sourceFields.candidate_request_id=candidate;
      const candidateLabel=node("label","补丁来源");candidateLabel.append(candidate);form.append(candidateLabel);fields.push(candidate);
      if(verification&&seeds.filter(item=>item.repository===repo.value).length===1){candidate.value=seeds.find(item=>item.repository===repo.value).request_id;candidate.dispatchEvent(new Event("change"));}
      form.append(node("p","继承只复制已记录的提交到新目录；不会继承旧任务权限，也不代表功能验证通过。启动前会再次核验来源。"));
      const location=node("p");
      const updateLocation=()=>{location.textContent=`执行节点：${options.source_choices?.[repo.value]?.node||"未配置"}。基线须为完整提交；分支和版本名称尚未独立核验。`;};
      repo.addEventListener("change",updateLocation);updateLocation();form.append(location);
    }
    const jointRepositories=verification?.repositories?.length>1?verification.repositories:[];
    const jointSelections=new Map();
    const jointConfigured=jointRepositories.every(item=>options.repositories.includes(item.repository)&&options.source_choices?.[item.repository]?.node===item.node);
    if(jointRepositories.length){
      form.append(node("p","本次联合验证绑定全部候选提交，只执行一次测试命令。命令在主仓库运行，可从 K3_VERIFICATION_SOURCES 环境变量读取仓库名称到独立副本路径的 JSON 映射。"));
      for(const item of jointRepositories.slice(1)){
        const select=node("select"),label=node("label",`联合仓库 ${item.repository} 的补丁来源`),base=node("option","从配置仓库读取候选提交");
        base.value="";select.append(base);select.setAttribute("aria-label",`联合仓库 ${item.repository} 的补丁来源`);
        const seeds=(options.candidate_choices||[]).filter(seed=>seed.repository===item.repository&&seed.source.base_commit===item.candidate_commit&&seed.source.branch===item.branch&&seed.source.node===item.node);
        for(const seed of seeds){const option=node("option",`继承任务 ${seed.job_id} · ${seed.source.base_commit}`);option.value=seed.request_id;select.append(option);}
        label.append(select);form.append(node("p",`${item.repository} · ${item.branch} · ${item.node} · ${item.candidate_commit}`),label);fields.push(select);jointSelections.set(item.repository,select);
      }
    }
    const status=node("p","已配置工具不代表执行节点在线；创建后可在执行记录查看进展。","muted");
    status.setAttribute("role","status");
    const submit=node("button",verification?"创建独立验证任务":"创建编码任务"), back=node("button","返回事项");
    submit.type="submit";back.type="button";
    const submissionAllowed=investigation?options.investigation_submission_allowed:options.submission_allowed;
    submit.disabled=!available.length || !repositories.length || submissionAllowed===false || !jointConfigured;
    if (submit.disabled) status.textContent="尚未配置可用的编码工具或仓库，请完成节点配置后重新读取。";
    if(submissionAllowed===false)status.textContent="当前事项状态不允许执行编码任务，请先分流或恢复事项，再重新读取。暂停、接管和已结束的事项不会因创建任务而自动恢复。";
    if(!jointConfigured)status.textContent="联合验证缺少仓库配置或执行节点不一致，不能提交部分仓库。";
    back.addEventListener("click",()=>loadDetail(caseId));
    if(investigation)form.append(node("p",`绑定 Bug ${investigation.bug_id} 的当前调查轮次；执行结果不会自动关闭缺陷。`));
    form.append(node("p",verification?"授权范围：在候选代码的独立副本中执行本步骤绑定的测试命令，随后核对证据。":"授权范围：所选仓库的读代码、修改、构建、测试和本地提交。"),status,submit,back);
    area.replaceChildren(form);
    $("case-status").textContent="创建编码任务 · 请填写具体目标和验收要求。";
    let pending=null, submitting=false;
    form.addEventListener("submit",async event=>{
      event.preventDefault();
      if(submissionAllowed===false||!jointConfigured)return;
      if (submitting || request !== detailRequest || !$("case-dialog").open) return;
      if (!pending) {
        const choice=available.find(item=>item.id===executor.value);
        if (!choice || !repositories.includes(repo.value) || !instructions.value.trim() || !acceptance.value.trim()) return;
        let source=null;
        if(investigation) {
          const selected=options.source_choices?.[repo.value];
          if(!selected||!sourceFields.branch.value.trim()||!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(sourceFields.base_commit.value.trim())) {
            status.textContent="请填写基线分支和完整的小写提交哈希，并确认执行节点已配置。";return;
          }
          source={...selected,branch:sourceFields.branch.value.trim(),base_commit:sourceFields.base_commit.value.trim(),version:sourceFields.version.value.trim()};
          if(sourceFields.candidate_request_id.value)source.candidate_request_id=sourceFields.candidate_request_id.value;
        }
        let jointSources=null;
        if(jointRepositories.length){
          jointSources={[repo.value]:source};
          for(const item of jointRepositories.slice(1)){
            const selection=jointSelections.get(item.repository),configured=options.source_choices[item.repository];
            const extra={...configured,branch:item.branch,base_commit:item.candidate_commit,version:""};
            if(selection.value)extra.candidate_request_id=selection.value;
            jointSources[item.repository]=extra;
          }
        }
        pending={case_id:caseId,case_version:options.case_version,repository:repo.value,
          executor_id:choice.id,contract_fingerprint:choice.contract_fingerprint,
          instructions:instructions.value,acceptance:acceptance.value,request_id:crypto.randomUUID(),...(source?{source}:{}),...(verification?{verification:{plan_id:verification.plan_id,step_id:verification.step_id,command:instructions.value.trim(),...(jointSources?{sources:jointSources}:{})}}:{})};
      }
      submitting=true;submit.disabled=true;fields.forEach(field=>field.disabled=true);
      status.textContent="正在确认任务创建结果…";
      try {
        const body=investigation?{...pending,...investigation}:pending;
        if(investigation)delete body.case_id;
        const result=await api(verification?"/api/project-bugs/create-verification-job":investigation?.predecessor_job_id?"/api/project-bugs/continue-investigation-job":investigation?"/api/project-bugs/create-investigation-job":"/api/coding-task",body);
        if (request !== detailRequest || !$("case-dialog").open) return;
        status.textContent=`${result.created ? "已创建任务" : "已找到原任务"} ${result.job_id} · 提交时状态：${{queued:"等待执行",running:"执行中",finished:"已结束",failed:"失败",cancelled:"已取消"}[result.state] || result.state}。点击“返回事项”重新读取最新结果。`;
        submit.textContent="已确认";
      } catch(error) {
        if (request !== detailRequest || !$("case-dialog").open) return;
        status.textContent=`未确认成功：${error.message}。重试会使用同一请求，不会重复创建；更改任务前请先核对执行记录。`;
        submit.textContent="重试同一请求";submit.disabled=false;
      } finally {submitting=false;}
    });
    instructions.focus();
  } catch(error) {
    if (request === detailRequest && $("case-dialog").open) $("case-status").textContent="读取工具失败："+error.message;
  } finally {if (request === detailRequest) detailControls();}
}
async function loadDetail(caseId, page = 1, snapshot = null) {
  detailKind = "case";
  const request = ++detailRequest;
  detailCase = caseId;
  $("case-title").textContent = caseId;
  $("case-status").textContent = "正在读取…";
  $("case-content").textContent = "";
  $("case-page").textContent = "";
  detailActions({});
  detailControls(true);
  try {
    const body = {case_id: caseId, page};
    if (snapshot) { body.content_digest = snapshot.content_digest; body.origin_cursor = snapshot.origin_cursor; }
    const result = await api("/api/case-detail", body);
    if (request !== detailRequest || !$("case-dialog").open) return;
    currentDetail = result;
    detailActions(result);
    const codingButton = node("button", "创建编码任务");
    codingButton.addEventListener("click", () => openCodingTask(caseId));
    $("case-actions").append(codingButton);
    const inventoryButton = node("button", "查看数据保留范围");
    inventoryButton.addEventListener("click", () => loadContentInventory(caseId));
    $("case-actions").append(inventoryButton);
    (result.sent_knowledge || []).forEach(sent => {
      const button = node("button", `评价已发送回复 · ${sent.created_at}`);
      button.addEventListener("click", () => loadSentKnowledge(caseId, sent.use_id));
      $("case-actions").append(button);
    });
    $("case-content").textContent = result.text;
    $("case-page").textContent = `${result.page} / ${result.page_count} 页`;
    $("case-status").textContent = "已读取快照；查看期间状态可能继续变化。";
  } catch (error) {
    if (request !== detailRequest || !$("case-dialog").open) return;
    currentDetail = null;
    $("case-status").textContent = error.message + "；请重新读取。";
  } finally {
    if (request === detailRequest) detailControls();
  }
}
async function loadContentInventory(caseId) {
  const request = ++detailRequest;
  currentDetail = null;
  detailActions({});
  detailControls(true);
  $("case-content").textContent = "";
  $("case-page").textContent = "";
  $("case-status").textContent = "正在统计数据保留范围…";
  try {
    const value = await api("/api/case-content-inventory", {case_id: caseId});
    if (request !== detailRequest || !$("case-dialog").open) return;
    const names = {cases:"任务描述",case_events:"任务事件",case_sources:"资料来源",evidence:"验证证据",
      case_suggestions:"回复建议",approvals:"审批内容",outbox:"发送记录",action_ledger:"操作结果",
      inbound_events:"原始消息",job_attempts:"执行尝试",operator_activities:"接管记录",
      case_handoffs:"诊断交接",case_lifecycle_actions:"任务状态操作",case_rounds:"任务轮次说明",
      codex_reviews:"编码代理审查",conversation_contexts:"对话上下文",delivery_blocks:"发送阻断说明",
      diagnostic_snapshots:"诊断快照",incident_cluster_members:"相似问题说明",jobs:"执行上下文",
      knowledge_feedback:"知识反馈",locks:"设备占用说明",mail_items:"关联邮件",
      meeting_create_attempts:"会议创建记录",meeting_previews:"会议草稿",
      model_budget_attempts:"模型费用回执",professional_validation_runs:"知识验证环境",
      route_decisions:"问题分流依据",mail_summary_membership:"邮件摘要成员快照",
      mail_digest_runs:"关联邮件摘要",mail_digest_links:"邮件摘要分享记录"};
    const holdNames = {case_not_closed:"案件尚未关闭",worker_exit_unverified:"编码执行退出尚未确认",remote_execution_unsettled:"远端执行收尾尚未确认",board_cleanup_unsettled:"板卡收尾尚未确认",resource_lock_present:"资源占用仍有记录，过期不代表已收尾",jobs_unsettled:"执行任务尚未收尾",approvals_open:"审批仍待决或有效",
      delivery_unsettled:"发送结果尚未确认",actions_unsettled:"操作尚未核对完成",
      conversation_open:"对话尚未关闭",linked_case_review_required:"需核对关联任务",
      shared_source_messages:"原始消息仍被其他任务或发送记录引用",
      knowledge_source_reference:"知识条目仍引用本任务或资料来源",
      mail_summary_reference:"邮件摘要仍引用关联邮件，需单独处理保留期限"};
    const holds = (value.observed_holds || []).map(row => `${holdNames[row.reason] || row.reason}：${row.count} 项`);
    const group = value.canonical_group_holds;
    const groupLines = group ? group.members.map(member => {
      const reasons = [...member.observed_holds,...(member.dependency_holds || [])].map(row => `${holdNames[row.reason] || row.reason} ${row.count} 项`);
      return `${member.case_id} · ${member.case_state}：${reasons.length ? reasons.join("；") : "未发现已检查的未完成事项（不代表可清理）"}`;
    }) : [];
    const graph = value.declared_reference_graph;
    const transforms = value.outbox_json_fields;
    $("case-content").textContent = (holds.length ? "当前保留原因\n" + holds.join("\n") + "\n\n" : "未发现已检查的活动项，但不代表允许清理。\n\n") + value.surfaces.map(row =>
      `${names[row.table] || row.table}：${row.rows_counted} 条 · ${row.bytes_counted} 字节${row.truncated ? "（达到统计上限，非完整数量）" : ""}`
    ).join("\n") + (transforms ? `\n\n消息正文转换预览：${transforms.candidate_transform_rows || 0} 条已识别，预计移除 ${transforms.candidate_removed_bytes || 0} 字节。${transforms.unclassified_rows} 条格式尚未覆盖${transforms.truncated ? "；统计达到上限" : ""}。仅预览，不代表允许清理。` : "") +
      (group ? "\n\n关联组保留检查\n" + groupLines.join("\n") +
      "\n按案件分别统计，同一条记录可能被多个案件引用，不能相加。" +
      (group.resolved_group_complete ? "" : "\n关联组未完整解析，不能据此批准清理。") : "");
    $("case-status").textContent = "仅统计已登记字段，不显示正文、不执行删除。文件、备份及其他未登记副本未完整覆盖。" +
      (graph ? ` 外键追踪：${graph.tables.length} 张表、${graph.rows} 条记录${graph.truncated ? "（达到上限）" : ""}；不含未声明引用，不能据此批准清理。` : "") +
      (Number.isInteger(value.unclassified_columns) ? ` 数据库另有 ${value.unclassified_columns} 个字段尚未分类（其中可能包含普通元数据，不等于同等数量的正文副本）。` : "");
  } catch (error) {
    if (request !== detailRequest || !$("case-dialog").open) return;
    $("case-status").textContent = error.message + "；未执行清理。";
  } finally {
    if (request === detailRequest && $("case-dialog").open) {
      const back = node("button", "返回任务详情");
      back.addEventListener("click", () => loadDetail(caseId));
      $("case-actions").append(back);
      detailControls();
    }
  }
}
async function loadSentKnowledge(caseId, useId) {
  const request = ++detailRequest;
  currentDetail = null;
  detailActions({});
  detailControls(true);
  $("case-content").textContent = "";
  $("case-page").textContent = "";
  $("case-status").textContent = "正在读取实际发送内容…";
  try {
    const value = await api("/api/sent-knowledge-preview", {case_id:caseId, use_id:useId});
    if (request !== detailRequest || !$("case-dialog").open) return;
    $("case-content").textContent = value.sent_text;
    $("case-status").textContent = value.note;
    let submitted = false;
    if (value.version_bound) for (const [verdict,label] of [["helpful","有用"],["incorrect","有误"],["incomplete","不足"]]) {
      const button = node("button", label);
      button.addEventListener("click", async () => {
        if (submitted || request !== detailRequest || !$("case-dialog").open) return;
        submitted = true;
        $("case-actions").querySelectorAll("button").forEach(el => el.disabled = true);
        try {
          await api("/api/sent-knowledge-feedback", {case_id:caseId,use_id:useId,
            content_digest:value.content_digest,verdict,request_id:crypto.randomUUID()});
          if (request === detailRequest && $("case-dialog").open)
            $("case-status").textContent = verdict === "helpful" ? "已记录有用反馈，不代表现场已解决。" : "已记录，等待复核；未修改当前知识版本。";
        } catch(error) {
          if (request === detailRequest && $("case-dialog").open)
            $("case-status").textContent = error.message + "；请重新读取核对，不自动重试。";
        }
      });
      $("case-actions").append(button);
    }
  } catch(error) {
    if (request === detailRequest && $("case-dialog").open) $("case-status").textContent = error.message;
  } finally {
    if (request === detailRequest) detailControls();
  }
}
async function loadApproval(approvalId) {
  const request = ++detailRequest;
  detailKind = "approval";
  detailCase = approvalId;
  currentDetail = null;
  $("case-title").textContent = "审批详情";
  $("case-status").textContent = "正在读取完整申请…";
  $("case-content").textContent = "";
  $("case-page").textContent = "";
  detailActions({});
  detailControls(true);
  try {
    const value = await api("/api/approval-detail", {approval_id: approvalId});
    if (request !== detailRequest || !$("case-dialog").open) return;
    $("case-content").textContent = `${value.case_id}\n${value.text}`;
    $("case-status").textContent = value.note;
    detailActions(value);
  } catch(error) {
    if (request === detailRequest && $("case-dialog").open) $("case-status").textContent = error.message + "；请重新读取核对。";
  } finally {
    if (request === detailRequest) detailControls();
  }
}
function queueItem(item) {
  const categories = {owner_decision:"待你判断",approval:"待审批",ai_processing:"AI 队列",human_hold:"人工负责",waiting:"等待",case_error:"事项异常",knowledge_review:"知识复审",delivery_failure:"投递失败",mail_link_failure:"邮件链接失败",job_failure:"任务失败",closed_case:"已结束",answered_faq:"资料咨询已答"};
  const states = {intake:"待分流",triage:"分流中",answering:"准备回复",investigating:"调查中",waiting_board:"等待设备",board_testing:"设备测试中",waiting_push:"等待推送审批",monitoring:"跟踪中",escalated:"待人工判断",takeover:"已接管",paused:"已暂停",stopped:"已停止",resolved:"已解决",cancelled:"已取消",error:"发生异常",queued:"排队中",running:"执行中",waiting:"等待中",succeeded:"任务已完成",failed:"任务失败",orphaned:"执行状态待核对",requested:"待审批",candidate:"待审核",stale:"待复审",permanent_failure:"投递失败"};
  const el = row(item.title || item.case_id, [item.case_id,categories[item.kind] || "其他事项",item.state ? states[item.state] || "状态待核对" : null].filter(Boolean).join(" · "), item.severity || "待处理");
  if (item.case_id) {
    const button = node("button", item.kind === "approval" ? "查看审批" : "查看详情");
    button.setAttribute("aria-label", "查看任务详情 " + item.case_id);
    button.addEventListener("click", () => {
      currentDetail = null;
      openDetailDialog();
      if (item.kind === "approval") loadApproval(item.target_id); else loadDetail(item.case_id);
    });
    el.append(button);
  }
  return el;
}
function render(data){$("mode").textContent=names[data.mode.mode]||"状态未知";$("gate").textContent="系统策略："+({shadow:"仅观察",active:"启用",drain:"停止接收新任务"}[data.static_mode]||"状态待核对");$("connection").textContent="已同步 "+new Date().toLocaleTimeString();const counts=data.workbench.counts||{};$("metrics").replaceChildren(...[["待你判断","owner_decision"],["待审批","approval"],["AI 队列","ai_processing"],["人工负责","human_hold"]].map(([label,key])=>{const el=node("div",undefined,"metric");el.append(node("span",label),node("strong",String(counts[key]||0)));return el;}));list("knowledge-list",data.knowledge.map(knowledgeItem),"尚无知识条目");const featureNames={shadow_reply:"观察模式建议",auto_faq:"知识自动回复",codex:"编码代理",board:"开发板测试",wip_push:"WIP 推送",mail:"邮箱",calendar:"日历会议",base_sync:"多维表格同步"};$("features").replaceChildren(...Object.entries(data.features).map(([key,value])=>row(featureNames[key]||key,"是否执行还取决于运行模式及独立审批",value?"配置开启":"配置关闭",value?"ok":"")));$("hours").textContent=data.work_hours.start+" — "+data.work_hours.end;list("services",data.services.map(service=>{const fresh=Date.now()-Date.parse(service.heartbeat_at)<180000;return row(service.component,"最近心跳："+service.heartbeat_at,fresh?service.status:"心跳过期",fresh&&service.status==="ready"?"ok":"warn");}),"尚未收到服务心跳");}
let knowledgeEpoch=0,knowledgeNext=null,knowledgeFilters=null;
const knowledgeQuery=node("input"),knowledgeStatus=node("select"),knowledgeSearch=node("button","搜索 / 重新读取"),knowledgeMore=node("button","下一页"),knowledgeCount=node("p");
knowledgeQuery.setAttribute("aria-label","知识关键词");knowledgeQuery.placeholder="标题、正文、项目或模块关键词";
knowledgeStatus.setAttribute("aria-label","知识状态");
for(const [value,label] of Object.entries({all:"全部状态",candidate:"待审核",approved:"已有审核记录",stale:"需复审",retired:"已停用"})){
  const option=node("option",label);option.value=value;knowledgeStatus.append(option);
}
knowledgeStatus.value="all";knowledgeMore.disabled=true;
const knowledgeFiltersArea=node("div",undefined,"detail-controls");
knowledgeFiltersArea.append(knowledgeQuery,knowledgeStatus,knowledgeSearch,knowledgeMore,knowledgeCount);
$("knowledge-filters").append(knowledgeFiltersArea);
const attachmentArea=node("details"),attachmentFile=node("input"),attachmentRead=node("button","预览解析结果"),attachmentNext=node("button","下一页"),attachmentStatus=node("p"),attachmentItems=node("div");
attachmentFile.type="file";attachmentFile.accept=".json,application/json";attachmentFile.setAttribute("aria-label","附件解析结果 JSON");
attachmentNext.disabled=true;
attachmentArea.append(node("summary","附件解析结果预览"),node("p","选择转换后的 JSON，查看正文、表格和来源位置。此处不保存或发布知识。"),attachmentFile,attachmentRead,attachmentNext,attachmentStatus,attachmentItems);
$("knowledge-filters").append(attachmentArea);
let attachmentEpoch=0,attachmentEvidence=null,attachmentPage=0;
attachmentFile.addEventListener("change",()=>{++attachmentEpoch;attachmentEvidence=null;attachmentPage=0;attachmentNext.disabled=true;attachmentItems.replaceChildren();invalidateAttachmentDraft();attachmentStatus.textContent="文件已变更，请重新预览。";});
async function loadAttachmentEvidence(page=1){
  invalidateAttachmentDraft();
  const epoch=++attachmentEpoch;attachmentNext.disabled=true;attachmentItems.replaceChildren();attachmentStatus.textContent="正在读取…";
  let evidence=attachmentEvidence;attachmentEvidence=null;
  try{
    if(page===1){
      attachmentEvidence=null;
      const file=attachmentFile.files?.[0];
      if(!file||file.size>2000000)throw new Error("请选择不超过 2 MB 的解析结果 JSON。");
      evidence=JSON.parse(await file.text());
      if(epoch!==attachmentEpoch)return;
    }
    if(!evidence)throw new Error("请先选择解析结果。");
    const result=await api("/api/attachment-evidence-preview",{evidence,page});
    if(epoch!==attachmentEpoch)return;
    attachmentEvidence=evidence;
    attachmentPage=result.page;attachmentNext.disabled=result.page>=result.page_count;
    attachmentStatus.textContent=`${result.notice} 来源：${result.source.id} · 版本 ${result.source.version} · 第 ${result.page}/${result.page_count} 页`;
    for(const item of result.items){
      const block=node("article",undefined,"card");
      block.append(node("h3",item.reference),node("pre",item.text),node("p",`来源位置：${item.provenance}`));
      if(item.text_truncated)block.append(node("p","正文较长，此处仅展示前 16000 字符，请查看原始解析文件。"));
      if(item.provenance_truncated)block.append(node("p","来源位置未完整展示，请查看原始解析文件后再核对引用。"));
      if(item.cells.length){
        const table=node("table");
        for(const cell of item.cells){const tr=node("tr");tr.append(node("td",`行 ${cell.row??"?"} / 列 ${cell.column??"?"} · 跨 ${cell.row_span??1} 行 ${cell.col_span??1} 列`),node("td",cell.text+(cell.text_truncated?"\n［内容已截断，请核对原始解析文件］":"")));table.append(tr);}
        if(item.cells_truncated)block.append(node("p","表格还有未展示的单元格，不能仅依据本页确认整张表。"));
        block.append(node("p",`单元格：显示 ${item.cells.length}/${item.cell_count}，按原始坐标列出；单格最多显示 4000 字符。`),table);
      }
      attachmentItems.append(block);
    }
  }catch(error){if(epoch===attachmentEpoch)attachmentStatus.textContent=error.message;}
}
attachmentRead.addEventListener("click",()=>loadAttachmentEvidence());
attachmentNext.addEventListener("click",()=>{if(!attachmentNext.disabled)loadAttachmentEvidence(attachmentPage+1);});
const attachmentDraftForm=node("details"),attachmentDraftTitle=node("input"),attachmentDraftQuestion=node("input"),attachmentDraftAnswer=node("textarea"),attachmentDraftRefs=node("input"),attachmentDraftRisk=node("select"),attachmentDraftRollback=node("textarea"),attachmentDraftBuild=node("button","生成草稿，不发布"),attachmentDraftOutput=node("textarea");
attachmentDraftForm.append(node("summary","整理为专业知识草稿"));
for(const [label,field] of [["标题",attachmentDraftTitle],["同事会怎么问",attachmentDraftQuestion],["候选答案",attachmentDraftAnswer],["来源位置，以逗号分隔，例如 #/texts/0",attachmentDraftRefs],["操作风险",attachmentDraftRisk],["回退说明（持久化或破坏性操作必填）",attachmentDraftRollback]]){
  const wrapper=node("label",label);field.setAttribute("aria-label",label);wrapper.append(field);attachmentDraftForm.append(wrapper);
}
for(const [value,label] of [["","请选择风险"],["read_only","只读"],["transient","临时变更"],["persistent","持久化变更"],["destructive","破坏性操作"]]){const option=node("option",label);option.value=value;attachmentDraftRisk.append(option);}
attachmentDraftOutput.readOnly=true;attachmentDraftOutput.setAttribute("aria-label","专业知识草稿 Markdown");
const attachmentDraftSave=node("button","保存为待审核草稿");attachmentDraftSave.disabled=true;
const attachmentScopeProduct=node("input"),attachmentScopeComponent=node("input"),attachmentScopeVersion=node("input");
for(const [label,field] of [["产品（未知可留空）",attachmentScopeProduct],["组件（如 u-boot，未知可留空）",attachmentScopeComponent],["软件版本（明确版本或提交，未知可留空）",attachmentScopeVersion]]){
  const wrapper=node("label",label);field.setAttribute("aria-label",label);field.value="";wrapper.append(field);attachmentDraftForm.append(wrapper);field.addEventListener("input",invalidateAttachmentDraft);
}
attachmentDraftForm.append(node("p","填写范围只是待审核声明，不代表来源已证实；不要猜测板型或版本。"));
attachmentDraftForm.append(attachmentDraftBuild,attachmentDraftOutput,attachmentDraftSave);attachmentArea.append(attachmentDraftForm);
let attachmentDraftEpoch=0,attachmentPrepared=null;
function invalidateAttachmentDraft(){++attachmentDraftEpoch;attachmentDraftOutput.value="";attachmentPrepared=null;attachmentDraftSave.disabled=true;}
for(const field of [attachmentDraftTitle,attachmentDraftQuestion,attachmentDraftAnswer,attachmentDraftRefs,attachmentDraftRisk,attachmentDraftRollback])field.addEventListener("input",invalidateAttachmentDraft);
attachmentDraftBuild.addEventListener("click",async()=>{
  invalidateAttachmentDraft();const epoch=attachmentDraftEpoch,sourceEpoch=attachmentEpoch;
  if(!attachmentEvidence){attachmentStatus.textContent="请先选择并预览解析材料。";return;}
  try{
    const fields=JSON.parse(JSON.stringify({evidence:attachmentEvidence,title:attachmentDraftTitle.value,question:attachmentDraftQuestion.value,answer:attachmentDraftAnswer.value,references:attachmentDraftRefs.value.split(",").map(x=>x.trim()).filter(Boolean),risk_class:attachmentDraftRisk.value,rollback:attachmentDraftRollback.value}));
    fields.authored_scope={product:attachmentScopeProduct.value,component:attachmentScopeComponent.value,software_version:attachmentScopeVersion.value};
    const result=await api("/api/attachment-draft",fields);
    if(epoch!==attachmentDraftEpoch||sourceEpoch!==attachmentEpoch)return;
    attachmentDraftOutput.value=result.markdown;attachmentStatus.textContent=result.notice+" 建议文件名："+result.filename;
    attachmentPrepared={fields,digest:result.revision_digest,epoch,sourceEpoch};attachmentDraftSave.disabled=false;
  }catch(error){if(epoch===attachmentDraftEpoch&&sourceEpoch===attachmentEpoch)attachmentStatus.textContent=error.message;}
});
attachmentDraftSave.addEventListener("click",async()=>{
  const prepared=attachmentPrepared;
  if(attachmentDraftSave.disabled||!prepared||prepared.epoch!==attachmentDraftEpoch||prepared.sourceEpoch!==attachmentEpoch)return;
  attachmentDraftSave.disabled=true;
  try{
    const result=await api("/api/attachment-draft-save",{fields:prepared.fields,expected_digest:prepared.digest});
    if(prepared!==attachmentPrepared||prepared.sourceEpoch!==attachmentEpoch)return;
    attachmentStatus.textContent=`已保存为待审核草稿：${result.title}。未审核、未发布，不会用于自动回复。`;
  }catch(error){if(prepared===attachmentPrepared&&prepared.sourceEpoch===attachmentEpoch)attachmentStatus.textContent="保存结果未确认，请先在已保存草稿中核对；不会自动重试。";}
});
const authoringArea=node("details"),authoringRead=node("button","读取已保存草稿"),authoringNext=node("button","下一页"),authoringStatus=node("p"),authoringItems=node("div"),authoringOutput=node("textarea");
authoringNext.disabled=true;authoringOutput.readOnly=true;authoringOutput.setAttribute("aria-label","已保存的待审核草稿 Markdown");
authoringArea.append(node("summary","已保存的待审核草稿"),authoringRead,authoringNext,authoringStatus,authoringItems,authoringOutput);attachmentArea.append(authoringArea);
let authoringEpoch=0,authoringCursor=null;
const authoringProvenance=node("div");authoringArea.append(authoringProvenance);
async function loadAuthoringDetail(candidate_id){
  const current=++authoringEpoch;invalidateDraftClear();authoringOutput.value="";authoringProvenance.replaceChildren();
  authoringStatus.textContent="正在读取草稿及来源…";
  try{const detail=await api("/api/knowledge-authoring-detail",{candidate_id});if(current!==authoringEpoch)return;
    authoringOutput.value=detail.markdown;
    authoringStatus.textContent="待审核采集草稿 · 未发布 · 不参与自动回复。";
    const source=detail.material?.source||{},parser=detail.material?.parser||{};
    const scope=detail.metadata?.scope||{};
    const risks={read_only:"只读",transient:"临时变更",persistent:"持久化变更",destructive:"破坏性操作"};
    authoringProvenance.append(node("h3",detail.title),
      node("p",`保存：${detail.saved_at||"未知"} · ${detail.saved_by||"未知"}`),
      node("p",`来源：${source.id||"未提供"} · 版本 ${source.version||"未提供"}`),
      node("p",`内容摘要：${source.sha256||"未提供"}`),
      node("p",`解析器：${parser.name||"未知"} · ${parser.version||"未知"}`),
      node("p",`声明适用范围（待核验）：${scope.product||"未知"} / ${scope.component||"未知"} · ${(scope.software_versions||[]).join("、")||"版本未知"}`),
      node("p","来源与解析结果仍需核对；保存成功不代表内容正确。"));
    for(const claim of detail.metadata?.claims||[])authoringProvenance.append(node("p",`操作风险：${risks[claim.risk_class]||"未分类"}`));
    const taskLabels={classify_knowledge:"选择知识类型（文档入口、命令参考或操作流程）",assign_owner:"指定维护人",verify_applicability:"核验适用范围和软硬件版本",verify_original_sources:"核对原始来源、内容与坐标",validate_claims:"补齐各项声明要求的验证证据",review_disclosure:"确认可分享内容与权限范围",human_review:"完成内容审核并记录复审期限",evaluate_before_auto_reply:"自动回复前完成独立评测与发布检查"};
    authoringProvenance.append(node("h3","下一步审核清单"));
    for(const task of detail.review_tasks?.items||[])authoringProvenance.append(node("p",`待办：${taskLabels[task]||task}`));
    const refs=node("details");refs.append(node("summary","查看来源坐标与审核材料"),node("pre",JSON.stringify(detail.metadata?.sources||[],null,2)));authoringProvenance.append(refs);
    const resume=node("button","继续整理，另存草稿");authoringProvenance.append(resume);
    resume.addEventListener("click",()=>{
      if(current!==authoringEpoch)return;
      if(!confirm("将替换当前未保存的编辑内容。原草稿保留；另存的新草稿仍需审核。继续吗？"))return;
      const metadata=detail.metadata,material=detail.material;
      if(!material?.document||!metadata?.claims?.length){authoringStatus.textContent="缺少完整来源或声明，不能恢复编辑。";return;}
      ++attachmentEpoch;invalidateAttachmentDraft();attachmentNext.disabled=true;attachmentItems.replaceChildren();
      attachmentEvidence=JSON.parse(JSON.stringify({document:material.document,source:material.source,parser:material.parser}));
      attachmentDraftTitle.value=metadata.title;
      attachmentDraftQuestion.value=metadata.intent?.question_examples?.[0]||"";
      attachmentDraftAnswer.value=metadata.content?.summary||"";
      attachmentDraftRefs.value=(metadata.sources||[]).map(source=>source.locator?.json_pointer).filter(Boolean).join(", ");
      attachmentDraftRisk.value=metadata.claims[0].risk_class;
      attachmentDraftRollback.value=(metadata.content?.rollback||[]).join("\n");
      attachmentScopeProduct.value=scope.product==="unresolved"?"":scope.product||"";
      attachmentScopeComponent.value=scope.component==="unclassified"?"":scope.component||"";
      attachmentScopeVersion.value=(scope.software_versions||[]).filter(value=>value!=="unresolved").join(", ");
      attachmentArea.open=true;attachmentDraftForm.open=true;
      attachmentStatus.textContent="已恢复草稿编辑。修改后请重新生成并保存；原稿不会覆盖，新稿仍未审核。";
      attachmentDraftTitle.focus();
    });
  }catch(error){if(current===authoringEpoch)authoringStatus.textContent=error.message;}
}
const draftDays=node("input"),draftInfo=node("p"),draftActions=node("div");
draftDays.type="number";draftDays.min="1";draftDays.max="3650";draftDays.value="180";draftDays.setAttribute("aria-label","草稿保留天数");
authoringArea.append(node("p","草稿清理：天数仅用于本次检查，不修改自动策略；原文件和备份不删除。"),draftDays,draftInfo,draftActions);
let draftEpoch=0;
function invalidateDraftClear(){draftEpoch++;draftActions.replaceChildren();draftInfo.textContent="";}
draftDays.addEventListener("input",invalidateDraftClear);
async function previewDraftClear(candidate_id){
  invalidateDraftClear();const epoch=draftEpoch,days=Number(draftDays.value);
  if(!Number.isInteger(days)||days<1||days>3650){draftInfo.textContent="请输入 1–3650 的整数天数。";return;}
  try{const value=await api("/api/draft-retention-preview",{candidate_id,days});if(epoch!==draftEpoch)return;
    const reasons={not_expired:"未过保留期限",not_unreviewed_capture:"不是未审核采集草稿",professional_revision_exists:"已有专业知识版本",foreign_key_reference:"仍被引用",referenced_or_scan_incomplete:"仍被引用或扫描不完整",derived_draft_reference:"被其他草稿引用"};
    draftInfo.textContent=value.eligible?`可清理草稿数据 ${value.logical_bytes} 字节；不可直接撤销，原文件和备份仍保留。`:`保留：${(value.blockers||[]).map(key=>reasons[key]||key).join("、")}`;
    if(!value.eligible)return;
    const clear=node("button","确认清理这份草稿");draftActions.append(clear);
    clear.addEventListener("click",async()=>{
      if(clear.disabled||epoch!==draftEpoch)return;
      if(!confirm("仅清理这份未审核草稿，不可直接撤销。原文件和备份不会删除。继续？"))return;
      clear.disabled=true;
      try{await api("/api/draft-retention-clear",{candidate_id,days,preview_digest:value.row_digest,confirm_logical_delete:true});
        if(epoch!==draftEpoch)return;
        await loadAuthoring();draftInfo.textContent="草稿已清理；原文件和备份未删除。";
      }catch(error){if(epoch===draftEpoch)draftInfo.textContent=`清理结果未确认：${error.message}。请重新读取核对，不会自动重试。`;}
    });
  }catch(error){if(epoch===draftEpoch)draftInfo.textContent=`无法预览：${error.message}`;}
}
async function loadAuthoring(after_id=""){
  invalidateDraftClear();
  const epoch=++authoringEpoch;authoringNext.disabled=true;authoringOutput.value="";authoringProvenance.replaceChildren();authoringItems.replaceChildren();
  try{
    const result=await api("/api/knowledge-authoring-list",{after_id});if(epoch!==authoringEpoch)return;
    authoringCursor=result.next_cursor;authoringNext.disabled=!authoringCursor;authoringStatus.textContent=result.items.length?"这些草稿均未审核，不参与自动回复。":"暂无已保存草稿。";
    for(const item of result.items){const row=node("div"),open=node("button","查看草稿"),preview=node("button","预览清理");row.append(node("span",item.title),node("small",item.saved_at),open,preview);authoringItems.append(row);
      preview.addEventListener("click",()=>previewDraftClear(item.candidate_id));
      open.addEventListener("click",()=>loadAuthoringDetail(item.candidate_id));
    }
  }catch(error){if(epoch===authoringEpoch)authoringStatus.textContent=error.message;}
}
authoringRead.addEventListener("click",()=>loadAuthoring());authoringNext.addEventListener("click",()=>{if(!authoringNext.disabled)loadAuthoring(authoringCursor);});
const feedbackQueue = node("div", undefined, "queue");
const feedbackRead = node("button", "读取待复核反馈");
let feedbackEpoch = 0;
async function loadFeedbackQueue(cursor = "", state = "pending") {
  const epoch = ++feedbackEpoch;
  feedbackRead.disabled = true;
  feedbackQueue.replaceChildren(node("p", "正在读取…"));
  try {
    const value = await api("/api/sent-knowledge-pending", {after_id:cursor,state});
    if (epoch !== feedbackEpoch) return;
    feedbackQueue.replaceChildren(...value.items.map(item => {
      const el = row(item.case_id, `发送版本 ${item.entry_fingerprint} · ${item.created_at}`,
        item.decision === "needs_revision" ? "待修订（尚未修复）" : item.decision === "dismissed" ? "反馈不成立" : item.verdict === "incorrect" ? "有误待复核" : "不足待复核");
      const open = node("button", "核对原回复");
      open.addEventListener("click", () => {
        detailCase = item.case_id; detailKind = "case";
        $("case-title").textContent = item.case_id;
        openDetailDialog();
        loadSentKnowledge(item.case_id, item.use_id);
      });
      if (item.decision) {
        el.append(node("p", `复核理由：${item.reason} · ${item.reviewed_at}`), open);
        const knowledge = node("button", "查看当前知识");
        knowledge.addEventListener("click", () => {
          openDetailDialog();
          loadKnowledgeDetail(item.knowledge_id);
        });
        el.append(knowledge);
        if (item.decision === "needs_revision") {
          const material = node("button", "查看修订材料");
          material.addEventListener("click", async () => {
            const request = ++detailRequest;
            detailCase = item.case_id; detailKind = "case"; currentDetail = null;
            openDetailDialog();
            $("case-title").textContent = "修订候选材料";
            $("case-content").textContent = "";
            $("case-page").textContent = "";
            $("case-status").textContent = "正在读取；不会调用代理或发布知识。";
            detailActions({}); detailControls(true);
            try {
              const value = await api("/api/sent-knowledge-material", {feedback_id:item.request_id});
              if (request !== detailRequest || !$("case-dialog").open) return;
              $("case-content").textContent = JSON.stringify(value,null,2);
              $("case-status").textContent = "只读修订输入，非标准答案；需核对来源后修订。";
              const candidate = value.regression_candidate;
              if (candidate && candidate.status === "candidate") {
                const text = JSON.stringify(candidate) + "\n";
                const field = node("textarea");
                field.value = text; field.readOnly = true; field.rows = 5;
                field.setAttribute("aria-label", "待审核回归候选 JSONL");
                const copy = node("button", "复制回归候选");
                copy.addEventListener("click", async () => {
                  if (request !== detailRequest || !$("case-dialog").open) return;
                  copy.disabled = true;
                  try {
                    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
                    await navigator.clipboard.writeText(text);
                    if (request === detailRequest && $("case-dialog").open) $("case-status").textContent = "候选已复制；尚未审核，不可作为标准答案。";
                  } catch (_) {
                    if (request === detailRequest && $("case-dialog").open) $("case-status").textContent = "无法复制，请手动选取下方候选文本。";
                  } finally { copy.disabled = false; }
                });
                $("case-actions").append(node("p", "候选可能包含内部问题，请仅保存到私有审核目录。"), field, copy);
              }
            } catch(error) {
              if (request === detailRequest && $("case-dialog").open) $("case-status").textContent = error.message;
            } finally { if (request === detailRequest) detailControls(); }
          });
          el.append(material);
        }
        return el;
      }
      const reason = node("textarea");
      reason.placeholder = "核对原回复后填写复核理由；需要修订不等于已经修好。";
      reason.maxLength = 2000;
      reason.setAttribute("aria-label", "反馈复核理由");
      let submitted = false;
      el.append(open, reason);
      for (const [decision,label] of [["needs_revision","确认需要修订"],["dismissed","确认反馈不成立"]]) {
        const action = node("button", label);
        action.addEventListener("click", async () => {
          if (submitted || epoch !== feedbackEpoch) return;
          const text = (reason.value || "").trim();
          if (!text) { notice("请先填写复核理由。", true); return; }
          submitted = true;
          try {
            await api("/api/sent-knowledge-review", {feedback_id:item.request_id, decision,
              reason:text, content_digest:item.review_digest, request_id:crypto.randomUUID()});
            if (epoch === feedbackEpoch) {
              el.replaceChildren(node("p", label + "：已记录。未修改或发布知识，也未解决 Case。"));
            }
          } catch(error) {
            if (epoch === feedbackEpoch) el.append(node("p", error.message + "；请重新读取核对，不自动重试。"));
          }
        });
        el.append(action);
      }
      return el;
    }));
    if (!value.items.length) feedbackQueue.append(node("p", "此筛选下暂无反馈。"));
    if (value.next_cursor) {
      const next = node("button", "下一页反馈");
      next.addEventListener("click", () => loadFeedbackQueue(value.next_cursor,state));
      feedbackQueue.append(next);
    }
  } catch(error) {
    if (epoch === feedbackEpoch) feedbackQueue.replaceChildren(node("p", error.message));
  } finally { if (epoch === feedbackEpoch) feedbackRead.disabled = false; }
}
feedbackRead.addEventListener("click", () => loadFeedbackQueue());
$("knowledge-filters").append(feedbackRead, feedbackQueue);
for (const [state,label] of [["needs_revision","查看待修订反馈"],["dismissed","查看不成立记录"]]) {
  const button = node("button", label);
  button.addEventListener("click", () => loadFeedbackQueue("",state));
  $("knowledge-filters").append(button);
}
knowledgeSearch.addEventListener("click",()=>loadKnowledge());
knowledgeMore.addEventListener("click",()=>{if(knowledgeNext)loadKnowledge(knowledgeNext);});
function invalidateKnowledge(){++knowledgeEpoch;knowledgeNext=null;knowledgeMore.disabled=true;knowledgeCount.textContent="筛选已修改，请点击搜索。";list("knowledge-list",[],"请重新搜索以应用筛选",true);}
knowledgeQuery.addEventListener("input",invalidateKnowledge);
knowledgeStatus.addEventListener("change",invalidateKnowledge);
async function loadKnowledge(afterId=""){
  knowledgeBrowsing=true;const epoch=++knowledgeEpoch;knowledgeMore.disabled=true;
  if(!afterId)knowledgeFilters={query:knowledgeQuery.value||"",status:knowledgeStatus.value||"all"};
  list("knowledge-list",[],"正在读取",true);
  try{
    const result=await api("/api/knowledge-list",{...knowledgeFilters,after_id:afterId});
    if(epoch!==knowledgeEpoch)return;
    knowledgeNext=result.next_cursor;knowledgeMore.disabled=!knowledgeNext;
    knowledgeCount.textContent=`匹配 ${result.total_matching} 条 · 本页 ${result.items.length} 条 · 关键词筛选，不是 AI 语义匹配`;
    list("knowledge-list",result.items.map(knowledgeItem),"没有匹配条目",true);
  }catch(error){if(epoch===knowledgeEpoch){knowledgeNext=null;knowledgeCount.textContent=error.message;list("knowledge-list",[],"读取失败，请重新搜索",true);}}
}
function knowledgeItem(item){
  const card=row(item.title,[item.project,item.module].filter(Boolean).join(" · "),item.status);
  const open=node("button","查看完整条目与来源");
  open.addEventListener("click",()=>{if(!$("case-dialog").open)openDetailDialog();loadKnowledgeDetail(item.knowledge_id);});
  card.append(open);return card;
}
async function loadKnowledgeDetail(id,page=1,digest=null){
  const request=++detailRequest;detailKind="knowledge";detailCase=id;currentDetail=null;
  $("case-title").textContent="知识条目详情";$("case-content").textContent="";
  $("case-status").textContent="查看不改变状态；退回待审核或停用需另行确认，此处不能批准发布。";$("case-page").textContent="";
  detailActions({});detailControls(true);
  try{
    const value=await api("/api/knowledge-detail",{knowledge_id:id,page,content_digest:digest});
    if(request!==detailRequest||!$("case-dialog").open)return;
    currentDetail=value;$("case-content").textContent=value.plain_text;
    knowledgeLifecycleButtons(id,value.content_digest);
    const sources=node("div");
    for(const source of value.source_links||[]){
      if(typeof source.url!=="string"||!source.url.startsWith("https://"))continue;
      const link=node("a",source.title||"来源文档");
      link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";
      const entry=node("p");entry.append(link);sources.append(entry);
    }
    if(sources.children.length){sources.prepend(node("p","来源文档 · 无权限时可向文档所有者申请；查看不会发送给同事。","muted"));$("case-actions").append(sources);}
    $("case-page").textContent=`${value.page} / ${value.page_count}`;
  }catch(error){if(request===detailRequest&&$("case-dialog").open)$("case-status").textContent=error.message+"；请重新读取。";}
  finally{if(request===detailRequest)detailControls();}
}
function panel(value){if(value.buttons.some(b=>["A","S"].includes(b.callback_data.split(":")[1]))){const details=$("mode-info").closest?.("details");if(details)details.open=true;}$("mode-info").textContent=value.text;$("mode-buttons").replaceChildren(...value.buttons.filter(b=>!['i','r','w'].includes(b.callback_data.split(':')[1])).map(b=>{const button=node("button",b.text);if(b.text.includes("暂停")||b.text.includes("停止"))button.classList.add("danger");if(b.text.startsWith("✅"))button.classList.add("active");button.addEventListener("click",async()=>{if(busy)return;busy=true;$("mode-buttons").querySelectorAll("button").forEach(el=>el.disabled=true);try{panel(await api("/api/action",{callback:b.callback_data}));await refresh();notice("已收到控制面确认，请查看当前模式或二次确认提示。");}catch(error){notice(error.message+"；请刷新核对状态。",true);$("connection").textContent="操作状态待核对";}finally{busy=false;$("mode-buttons").querySelectorAll("button").forEach(el=>el.disabled=false);}});return button;}));}
function knowledgeLifecycleButtons(id,digest){
  const area=$("case-actions");
  area.replaceChildren(...[["candidate","退回待审核"],["retired","停用条目"]].map(([decision,label])=>{
    const button=node("button",label);
    button.addEventListener("click",async()=>{
      if(button.disabled||!confirm(`${label}？这会改变条目可用性，但不会撤回已发送消息，也不会批准发布。`))return;
      const request=detailRequest;
      for(const item of area.children)item.disabled=true;
      detailControls(true);
      try{
        await api("/api/knowledge-lifecycle",{knowledge_id:id,content_digest:digest,decision,request_id:crypto.randomUUID()});
        if(request!==detailRequest||!$("case-dialog").open)return;
        $("case-status").textContent="操作已确认，请重新读取当前条目状态。";
        currentDetail=null;area.replaceChildren();
      }catch(error){if(request===detailRequest&&$("case-dialog").open){currentDetail=null;area.replaceChildren();$("case-status").textContent=error.message+"；结果待核对，请重新读取，不自动重试。";}}
      finally{if(request===detailRequest)detailControls();}
    });return button;
  }));
}
let notificationSnapshot = null, notificationBusy = false;
function renderNotifications(value) {
  notificationSnapshot=value||null;
  $("notification-status").textContent=(!value?"提醒状态尚未核实":value.active?`普通待判断提醒暂缓至 ${value.until_at}`:"普通提醒未静音")+(value?.night_enabled?" · 夜间汇总已开启":"");
  $("notification-quiet").disabled=notificationBusy||!value;
  $("notification-resume").disabled=notificationBusy||!value||!value.manual_active;
  $("notification-night").disabled=notificationBusy||!value;
  $("notification-night").textContent=value?.night_enabled?"关闭夜间汇总":"开启夜间汇总";
}
async function setNotificationSnooze(minutes, night = null) {
  if(!notificationSnapshot||notificationBusy)return;
  const revision=notificationSnapshot.revision;
  notificationBusy=true;renderNotifications(notificationSnapshot);
  try {
    const body={minutes,expected_revision:revision,request_id:crypto.randomUUID()};
    if(night!==null)body.night_enabled=night;
    await api("/api/notification-snooze",body);
    notice(night!==null?"夜间汇总设置已记录；按当前配置时区和工作时间执行。":minutes?"普通提醒静音设置已记录；严重故障、审批和任务不受影响。":"手动静音已结束；夜间规则及当前模式仍然有效。");
    notificationBusy=false;await refresh();
  } catch(error) {
    notificationBusy=false;renderNotifications(null);
    notice(error.message+"；请刷新核对提醒状态，不要重复提交。",true);
  }
}
$("notification-quiet").addEventListener("click",()=>setNotificationSnooze(60));
$("notification-resume").addEventListener("click",()=>setNotificationSnooze(0));
$("notification-night").addEventListener("click",()=>{if(notificationSnapshot)setNotificationSnooze(null,!notificationSnapshot.night_enabled);});
async function refresh(){const data=await api("/api/status");render(data);renderLaunches(data.broker_launches);renderNotifications(data.notification_snooze);await loadQueue(queueSnapshot?.cursor || null);}
function renderLaunches(value){
  let area=$("broker-launch-status");
  if(!area){area=node("section");area.id="broker-launch-status";$("system").append(area);}
  area.replaceChildren(node("h2","编码调度状态（只读）"));
  if(!value){area.append(node("p","无法核实调度记录。"));return;}
  area.append(node("p",value.warning));
  if(!value.unresolved.length){area.append(node("p","没有未核对启动记录；不代表服务已部署或运行正常。"));return;}
  value.unresolved.forEach(item=>area.append(row(item.label,"任务编号："+item.claim_request_id+" · 更新："+item.updated_at,"待核对","warn")));
}
let featureSnapshot = null, featureDraft = null, featureEpoch = 0;
const featureInputs = new Map();
const featureLabels = {shadow_reply:"观察模式建议",auto_faq:"知识自动回复",codex:"编码代理",board:"开发板测试",wip_push:"WIP 推送",mail:"邮箱",calendar:"日历会议",base_sync:"多维表格同步"};
function cancelFeatures() {
  ++featureEpoch; featureSnapshot = featureDraft = null; featureInputs.clear();
  $("features-editor").replaceChildren(); $("features-diff").textContent="";
  $("features-preview").disabled = $("features-apply").disabled = true;
  $("features-status").textContent="编辑已关闭；已应用的配置不会因此撤销。";
}
async function editFeatures() {
  cancelFeatures(); const epoch = featureEpoch;
  try {
    const result = await api("/api/features");
    if (epoch !== featureEpoch) return;
    featureSnapshot = result;
    $("features-status").textContent=result.requires_rebase ? "基础配置已变化，功能暂不放行。请逐项选择迁移后要启用的功能；默认不继承旧开关。" : `编辑版本 ${result.revision}；勾选仅修改草稿。`;
    $("features-preview").textContent=result.requires_rebase ? "预览基础配置迁移" : "预览变更";
    $("features-editor").replaceChildren(...Object.entries(result.values).map(([key,value])=>{
      const label=node("label",undefined,"item"), input=node("input");
      input.type="checkbox";input.checked=value;featureInputs.set(key,input);
      input.addEventListener("change",()=>{++featureEpoch;featureDraft=null;$("features-apply").disabled=true;$("features-preview").disabled=false;$("features-diff").textContent="内容已修改，请重新预览。";});
      const labelText=(featureLabels[key]||key)+(result.requires_rebase ? `（旧配置：${result.historical_values[key]?"开":"关"}；当前停用）` : "");
      label.append(input,node("span",labelText));return label;
    }));
    $("features-preview").disabled=false;
    $("features-history").replaceChildren(...result.history.map(item=>{
      const card=row(`版本 ${item.revision}`,item.updated_at,"已应用"),button=node("button","预览恢复到此版本应用前");
      button.disabled=Boolean(result.requires_rebase);
      button.addEventListener("click",()=>previewFeatures(item.revision));card.append(button);return card;
    }));
  } catch(error) {if(epoch===featureEpoch)$("features-status").textContent=error.message;}
}
async function previewFeatures(rollbackRevision = null) {
  if (!featureSnapshot) return;
  const epoch=++featureEpoch;featureDraft=null;$("features-apply").disabled=true;$("features-preview").disabled=true;
  const body={expected_revision:featureSnapshot.revision};
  if(featureSnapshot.requires_rebase)body.rebase=true;
  if(rollbackRevision!==null)body.rollback_revision=rollbackRevision;
  else body.values=Object.fromEntries([...featureInputs].map(([key,input])=>[key,input.checked]));
  try {
    const result=await api("/api/features-preview",body);
    if(epoch!==featureEpoch)return;
    featureDraft=result;
    $("features-diff").textContent=(result.rebase ? `基础配置版本：${result.base_change.from} → ${result.base_change.to}\n` : "")+result.changes.map(change=>`${featureLabels[change.feature]||change.feature}：${change.before?"开启":"关闭"} → ${change.after?"开启":"关闭"}`).join("\n")+"\n\n"+result.warning;
    $("features-status").textContent="草稿未应用；有效期至 "+result.expires_at;
    $("features-apply").disabled=false;
  } catch(error) {if(epoch===featureEpoch)$("features-status").textContent=error.message;}
  finally {if(epoch===featureEpoch)$("features-preview").disabled=false;}
}
async function applyFeatures() {
  if(!featureDraft || !confirm("确认应用上方精确变更？开启可能允许已有待办继续，关闭不撤销在途操作。"))return;
  const epoch=++featureEpoch, draft=featureDraft;featureDraft=null;$("features-apply").disabled=true;
  try {
    const result=await api("/api/features-apply",{draft_id:draft.draft_id});
    notice(`配置版本 ${result.revision} 已记录；请核对当前配置，具体执行仍受模式和审批约束。`);
    if(epoch===featureEpoch)await editFeatures();
  } catch(error) {
    if(epoch===featureEpoch){cancelFeatures();$("features-status").textContent=error.message+"；结果可能未确认，请重新读取配置和历史，不要直接重复提交。";}
  }
}
$("features-edit").addEventListener("click",editFeatures);
$("features-preview").addEventListener("click",()=>previewFeatures());
$("features-apply").addEventListener("click",applyFeatures);
$("features-cancel").addEventListener("click",cancelFeatures);
const queueControls = node("div", undefined, "detail-controls");
const queueSelect = node("select");
queueSelect.setAttribute("aria-label", "任务筛选");
Object.entries({all:"全部待办",needs_me:"需要我",ai:"AI 队列",human:"人工负责",waiting:"等待",errors:"异常",approvals:"审批",knowledge:"知识",closed:"已结束"}).forEach(([value,label])=>{const option=node("option",label);option.value=value;queueSelect.append(option);});
queueSelect.addEventListener("change",()=>{queueView=queueSelect.value;queueSnapshot=null;loadQueue();});
queueControls.append(queueSelect);
for (const [id,label] of [["queue-prev","上一页"],["queue-next","下一页"],["queue-reload","重新读取（含新事项）"]]) {
  const button=node("button",label);button.id=id;
  button.addEventListener("click",()=>loadQueue(id==="queue-prev"?queueSnapshot?.previous_cursor:id==="queue-next"?queueSnapshot?.next_cursor:null));
  queueControls.append(button);
}
const queueCount=node("span");queueCount.id="queue-count";queueControls.append(queueCount);
$("home").append(queueControls);
$("case-close").addEventListener("click", () => $("case-dialog").close());
$("case-dialog").addEventListener("close", () => {
  ++detailRequest; currentDetail = null; detailCase = null;
  // Polling may have replaced the opener while the dialog was open.
  const replacement = detailOpenerLabel ? Array.from(document.querySelectorAll("button[aria-label]")).find(button => button.getAttribute("aria-label") === detailOpenerLabel && !button.disabled) : null;
  const target = detailOpener?.isConnected && !detailOpener.disabled ? detailOpener : replacement || $("workspace-content");
  target?.focus();
  detailOpener = null; detailOpenerLabel = null;
});
$("case-prev").addEventListener("click", () => { if(currentDetail){if(detailKind==="knowledge")loadKnowledgeDetail(detailCase,currentDetail.page-1,currentDetail.content_digest);else loadDetail(detailCase, currentDetail.page - 1, currentDetail);} });
$("case-next").addEventListener("click", () => { if(currentDetail){if(detailKind==="knowledge")loadKnowledgeDetail(detailCase,currentDetail.page+1,currentDetail.content_digest);else loadDetail(detailCase, currentDetail.page + 1, currentDetail);} });
$("case-refresh").addEventListener("click", () => { if(detailCase){if(detailKind === "approval")loadApproval(detailCase);else if(detailKind==="knowledge")loadKnowledgeDetail(detailCase);else loadDetail(detailCase);} });
async function enter(){const session=await api("/api/session");csrf=session.csrf;$("login").hidden=true;$("console").hidden=false;await refresh();panel(await api("/api/panel",{}));}
$("login-form").addEventListener("submit",async event=>{event.preventDefault();try{const result=await api("/api/login",{key:$("key").value});csrf=result.csrf;$("key").value="";await enter();}catch(error){$("login-error").textContent=error.message;}});
$("refresh").addEventListener("click",async()=>{try{await refresh();panel(await api("/api/panel",{}));$("notice").hidden=true;}catch(error){notice(error.message,true);}});
$("logout").addEventListener("click",async()=>{await api("/api/logout",{});location.reload();});
let mailEpoch=0, mailNext=null, mailBusy=false;
const mailCategories={all:"全部类别",build_ci:"构建 / CI",code_review:"代码评审",upstream:"Upstream",company:"公司事务",project_release:"项目 / 发布",support_bug:"技术支持 / Bug",meeting:"会议",security_account:"安全 / 账号",external:"外部往来",other:"其他",unclassified:"尚未分类"};
for(const [id,label,choices] of [["mail-category","邮件类别",mailCategories],["mail-state","处理状态",{all:"全部状态",todo:"待处理",snoozed:"稍后看",done:"已处理"}]]) {
  const select=node("select");select.id=id;select.setAttribute("aria-label",label);
  for(const [value,text] of Object.entries(choices)){const option=node("option",text);option.value=value;select.append(option);}
  select.value="all";
  select.addEventListener("change",()=>loadMail());
  $("mail-filters").append(node("span",label),select);
}
async function loadMail(afterId="") {
  if(mailBusy)return;
  const epoch=++mailEpoch;
  $("mail-next").disabled=true;
  list("mail-list",[],"正在读取");
  try {
    const value=await api("/api/mail-list",{after_id:afterId,category:$("mail-category").value||"all",state:$("mail-state").value||"all"});
    if(epoch!==mailEpoch)return;
    mailNext=value.next_cursor;
    list("mail-list",value.items.map(mailItem),"暂无缓存邮件");
    $("mail-next").disabled=!mailNext;
    $("mail-status").textContent=`本页 ${value.items.length} 封 · 本地处理状态`;
  } catch(error) {
    if(epoch!==mailEpoch)return;
    mailNext=null;
    list("mail-list",[],"读取失败，请重新读取");
    $("mail-status").textContent=error.message;
  }
}
function displayInstant(value,timeZone=Intl.DateTimeFormat().resolvedOptions().timeZone){
  if(value===null||value===undefined||value==="")return "无";
  if(typeof value!=="string"||!/(Z|[+-]\d{2}:\d{2})$/i.test(value))return `时间未确认：${String(value)}`;
  const instant=new Date(value);if(!Number.isFinite(instant.getTime()))return `时间未确认：${value}`;
  try{return new Intl.DateTimeFormat("zh-CN",{timeZone,year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hourCycle:"h23"}).format(instant)+`（浏览器时区 ${timeZone}）`;}
  catch{return `时间未确认：${value}`;}
}
function mailItem(value,afterChange) {
  const labels={todo:"待处理",done:"已处理",snoozed:"稍后看"};
  const card=row(value.header.subject||"无主题",`${value.header.sender_name||value.header.sender_address||"未知发件人"} · ${mailCategories[value.header.category]||"尚未分类"} · ${value.message_id}`,labels[value.effective_state]);
  const controls=node("div",undefined,"detail-controls"),caseInput=node("input");
  caseInput.setAttribute("aria-label","关联 Case 编号");
  caseInput.placeholder="Case 编号";
  caseInput.value=value.linked_case_id||"";
  controls.append(caseInput);
  const epoch=mailEpoch;
  const meeting=node("button","准备会议草稿");
  meeting.addEventListener("click",()=>openMailMeeting(value.message_id));
  for(const [action,label,extra] of [["done","已处理",{}],["reopen","恢复待办",{}],["snooze","1 小时后看",{minutes:60}],["link_case","关联 Case",{}],["unlink_case","取消关联",{}]]) {
    const button=node("button",label);
    button.addEventListener("click",async()=>{
      if(mailBusy||epoch!==mailEpoch)return;
      const args={...extra};
      if(action==="link_case") {
        args.case_id=caseInput.value.trim();
        if(!args.case_id){notice("请填写 Case 编号",true);return;}
      }
      mailBusy=true;
      ++mailEpoch;
      $("mail-reload").disabled=$("mail-next").disabled=true;
      $("mail-category").disabled=$("mail-state").disabled=true;
      controls.querySelectorAll("button").forEach(el=>el.disabled=true);
      try {
        await api("/api/mail-action",{message_id:value.message_id,action,expected_revision:value.revision,content_digest:value.content_digest,request_id:crypto.randomUUID(),...args});
        $("mail-status").textContent="操作已记录；请重新读取查看最新状态。";
      } catch(error) {
        $("mail-status").textContent="操作未确认："+error.message+"。请重新读取核对，不自动重试。";
      } finally {
        mailBusy=false;
        list("mail-list",[],"请重新读取最新状态");
        $("mail-reload").disabled=false;
        $("mail-category").disabled=$("mail-state").disabled=false;
        if(typeof afterChange==="function")await afterChange();
      }
    });
    controls.append(button);
  }
  controls.append(meeting);
  card.append(node("p",`关联：${value.linked_case_id||"无"} · 稍后截止：${displayInstant(value.snooze_until)}`),controls);
  return card;
}
$("mail-reload").addEventListener("click",()=>loadMail());
let meetingDraftEpoch=0, meetingDraft=null, meetingDraftBusy=false;
const meetingEditor=node("section");meetingEditor.id="mail-meeting-editor";meetingEditor.hidden=true;
meetingEditor.append(node("h3","邮件会议草稿"),node("p","仅保存本地草稿，不创建日程、不邀请参会人。时间请填带时区的 ISO 格式；参会人必须是已确认的飞书 ID。"));
for(const [key,label] of [["summary","主题"],["description","议程"],["start","开始时间"],["end","结束时间"],["timezone","时区"],["attendee_ids","参会人 ID（逗号分隔）"]]) {
  const field=node("input");field.id="mail-meeting-"+key;field.setAttribute("aria-label",label);
  meetingEditor.append(node("p",label),field);
}
const meetingStatus=node("p");meetingStatus.id="mail-meeting-status";meetingStatus.setAttribute("role","status");
const meetingSave=node("button","保存本地草稿");meetingSave.id="mail-meeting-save";
const meetingPrepare=node("button","准备正式审批预览"),meetingReview=node("button","查看已有审批");
const meetingCancel=node("button","撤销预览准备");meetingCancel.disabled=true;
meetingEditor.append(meetingCancel);
meetingCancel.addEventListener("click",async()=>{
  if(meetingDraftBusy||!meetingDraft?.preparation)return;
  if(!confirm("撤销这次预览准备及其审批效力？不会删除会议或撤回邀请；已经开始创建时必须使用会议恢复流程。"))return;
  const observed=meetingDraft.preparation;meetingDraft=null;meetingDraftBusy=true;
  meetingCancel.disabled=meetingPrepare.disabled=meetingReview.disabled=meetingSave.disabled=true;
  try {
    await api("/api/mail-meeting-cancel",{prepare_request_id:observed.request_id,binding_digest:observed.binding_digest,request_id:crypto.randomUUID()});
    meetingStatus.textContent="预览准备已撤销，迟到预览不能获批。重新打开后保存新草稿版本，可重新准备。未操作远端日程。";
  } catch(error){meetingStatus.textContent="撤销未确认："+error.message+"。请重新打开核对。";}
  finally {meetingDraftBusy=false;}
});
meetingPrepare.disabled=meetingReview.disabled=true;
meetingEditor.append(meetingPrepare,meetingReview);
meetingPrepare.addEventListener("click",async()=>{
  if(!meetingDraft||meetingDraftBusy)return;
  for(const [key,value] of Object.entries(meetingDraft.draft)) {
    const shown=Array.isArray(value)?value.join(","):value;
    if($("mail-meeting-"+key).value!==shown){meetingStatus.textContent="内容已修改，请先保存并重新打开。";return;}
  }
  if(!confirm("将已保存草稿交给后台查询忙闲和原日历，并生成待审批预览？不会创建会议。请先关联一个有效 Case。"))return;
  const observed=meetingDraft;meetingDraft=null;meetingDraftBusy=true;
  meetingPrepare.disabled=meetingSave.disabled=meetingReview.disabled=true;
  try {
    const result=await api("/api/mail-meeting-prepare",{message_id:observed.message_id,expected_revision:observed.revision,source_digest:observed.source_digest,request_id:crypto.randomUUID()});
    meetingStatus.textContent="预览准备状态："+result.state+"。稍后重新打开查看审批；未创建会议。";
  } catch(error){meetingStatus.textContent="准备未确认："+error.message+"。请重新打开核对。";}
  finally {meetingDraftBusy=false;}
});
meetingReview.addEventListener("click",()=>{if(meetingDraft?.preparation?.approval_id){if(!$("case-dialog").open)openDetailDialog();loadApproval(meetingDraft.preparation.approval_id);}});
const meetingClose=node("button","关闭");
meetingClose.addEventListener("click",()=>{if(meetingDraftBusy)return;++meetingDraftEpoch;meetingDraft=null;meetingEditor.hidden=true;});
meetingEditor.append(meetingStatus,meetingSave,meetingClose);$("mail").append(meetingEditor);
async function openMailMeeting(messageId) {
  if(meetingDraftBusy)return;
  const epoch=++meetingDraftEpoch;meetingDraft=null;meetingEditor.hidden=false;meetingSave.disabled=meetingPrepare.disabled=meetingReview.disabled=meetingCancel.disabled=true;
  meetingStatus.textContent="正在读取草稿";
  try {
    const value=await api("/api/mail-meeting-draft",{message_id:messageId});
    if(epoch!==meetingDraftEpoch)return;
    meetingDraft=value;
    for(const [key,fieldValue] of Object.entries(value.draft))$("mail-meeting-"+key).value=Array.isArray(fieldValue)?fieldValue.join(","):fieldValue;
    meetingStatus.textContent=value.source_changed?"来源邮件已变化，请核对后保存新版本。":"尚未创建日程；保存后仍须完整预览与审批。";
    meetingSave.disabled=false;
    meetingPrepare.disabled=value.revision===0||value.source_changed;
    meetingReview.disabled=!value.preparation?.approval_id;
    meetingCancel.disabled=!value.preparation||value.preparation.state==="cancelled";
    if(value.preparation)meetingStatus.textContent+=" 上次准备："+value.preparation.state+"（草稿版本 "+value.preparation.draft_revision+"）。";
  } catch(error) {if(epoch===meetingDraftEpoch)meetingStatus.textContent="读取失败："+error.message;}
}
meetingSave.addEventListener("click",async()=>{
  if(!meetingDraft||meetingDraftBusy)return;
  const observed=meetingDraft,draft={};
  for(const key of Object.keys(observed.draft))draft[key]=$("mail-meeting-"+key).value;
  draft.attendee_ids=draft.attendee_ids.split(",").map(v=>v.trim()).filter(Boolean);
  meetingDraftBusy=true;meetingSave.disabled=meetingPrepare.disabled=meetingReview.disabled=true;meetingDraft=null;
  try {
    await api("/api/mail-meeting-draft-save",{message_id:observed.message_id,draft,expected_revision:observed.revision,source_digest:observed.source_digest,request_id:crypto.randomUUID()});
    meetingStatus.textContent="本地草稿已保存，未创建日程。重新打开可继续编辑。";
  } catch(error) {meetingStatus.textContent="保存未确认："+error.message+"。请重新打开核对，不自动重试。";}
  finally {meetingDraftBusy=false;}
});
$("mail-next").addEventListener("click",()=>{if(mailNext)loadMail(mailNext);});
const budgetArea=node("section"),budgetText=node("pre");
budgetArea.append(node("h2","模型预算账本（只读）"),node("p","语义入口及编码会话启动已接预算；编码内部每次调用尚未逐次控制。预留占用不是供应商实际账单，未知费用不按零计算。"),budgetText);
$("system").append(budgetArea);
let budgetSnapshot=null,budgetDraft=null,budgetEditEpoch=0;
const budgetFields={},budgetEdit=node("button","编辑预算"),budgetPreview=node("button","预览预算差异"),budgetApply=node("button","确认应用预算"),budgetDiff=node("pre");
budgetEdit.disabled=budgetPreview.disabled=budgetApply.disabled=true;
budgetArea.append(node("p","金额以所填币种为单位，最多六位小数。留空不会设置默认额度。更改预算不清除已用额度。"),budgetEdit);
for(const [key,label] of [["currency","币种（如 USD）"],["daily_limit","每日总限额（UTC）"],["case_limit","单 Case 累计限额"],["attempt_limit","单次调用 / 编码会话预留上限"]]){
  const field=node("input");field.disabled=true;field.setAttribute("aria-label",label);budgetFields[key]=field;
  field.addEventListener("input",()=>{++budgetEditEpoch;budgetDraft=null;budgetApply.disabled=true;budgetDiff.textContent="内容已变更，请重新预览。";});
  budgetArea.append(node("p",label),field);
}
budgetArea.append(budgetPreview,budgetApply,budgetDiff);
budgetEdit.addEventListener("click",()=>{
  if(!budgetSnapshot)return;++budgetEditEpoch;budgetDraft=null;budgetApply.disabled=true;
  const policy=budgetSnapshot.policy;
  for(const [key,field] of Object.entries(budgetFields)){
    field.disabled=false;
    field.value=!policy?"":key==="currency"?policy[key]:`${Math.floor(policy[key]/1000000)}.${String(policy[key]%1000000).padStart(6,"0")}`;
  }
  budgetPreview.disabled=false;budgetDiff.textContent="编辑尚未生效。";
});
budgetPreview.addEventListener("click",async()=>{
  if(!budgetSnapshot)return;
  const epoch=++budgetEditEpoch;budgetDraft=null;budgetApply.disabled=true;
  try{
    const result=await api("/api/budget-preview",{values:Object.fromEntries(Object.entries(budgetFields).map(([key,field])=>[key,field.value])),expected_revision:budgetSnapshot.policy?.revision||0});
    if(epoch!==budgetEditEpoch)return;budgetDraft=result.draft_id;budgetApply.disabled=false;
    budgetDiff.textContent=result.warning+"\n差异中的金额单位为币种的百万分之一：\n"+JSON.stringify({previous:result.previous,proposed:result.proposed},null,2);
  }catch(error){if(epoch===budgetEditEpoch)budgetDiff.textContent="预览失败："+error.message;}
});
budgetApply.addEventListener("click",async()=>{
  if(!budgetDraft||!confirm("确认应用上述预算差异？首次配置会启用已接入调用的预算阻断。已有占用不会清除。"))return;
  const draft=budgetDraft;budgetDraft=null;++budgetEditEpoch;budgetApply.disabled=budgetPreview.disabled=budgetEdit.disabled=true;
  Object.values(budgetFields).forEach(field=>field.disabled=true);
  try{await api("/api/budget-apply",{draft_id:draft});budgetDiff.textContent="预算已应用，历史占用保留。";await loadBudget();}
  catch(error){budgetDiff.textContent="应用未确认："+error.message+"。请重新进入系统页核对，不自动重试。";}
});
let budgetEpoch=0;
async function loadBudget(){
  ++budgetEditEpoch;budgetDraft=null;budgetSnapshot=null;budgetEdit.disabled=budgetPreview.disabled=budgetApply.disabled=true;
  Object.values(budgetFields).forEach(field=>field.disabled=true);
  const epoch=++budgetEpoch;budgetText.textContent="正在读取";
  try {
    const value=await api("/api/model-budget");
    if(epoch!==budgetEpoch)return;
    budgetSnapshot=value;budgetEdit.disabled=false;
    budgetText.textContent=(value.configured?"已配置局部调用预算":"未配置预算，不保证调用费用受控")+"\n金额单位：币种的百万分之一；日期按 UTC。\n"+JSON.stringify(value,null,2);
  } catch(error){if(epoch===budgetEpoch)budgetText.textContent="无法核实预算："+error.message;}
}
let hoursEpoch=0,hoursSnapshot=null,hoursDraft=null;
const hoursArea=node("section",undefined,"note"),hoursStart=node("input"),hoursEnd=node("input"),hoursRead=node("button","读取工作时间"),hoursPreview=node("button","预览工作时间变更"),hoursApply=node("button","确认应用工作时间"),hoursInfo=node("pre");
hoursStart.type=hoursEnd.type="time";hoursStart.disabled=hoursEnd.disabled=hoursPreview.disabled=hoursApply.disabled=true;
const hoursStartLabel=node("label","开始 "),hoursEndLabel=node("label","结束 ");hoursStartLabel.append(hoursStart);hoursEndLabel.append(hoursEnd);
const hoursControls=node("div",undefined,"detail-controls");hoursControls.append(hoursStartLabel,hoursEndLabel,hoursRead,hoursPreview,hoursApply);
hoursArea.append(node("h2","调整工作时间"),node("p","按配置时区每日执行，支持跨午夜，不推断节假日。会影响消息兜底、夜间汇总及会议工作时段；不改已创建会议或已排队提醒的固定时间。","muted"),hoursControls,hoursInfo);$("settings").append(hoursArea);
function invalidateHours(){++hoursEpoch;hoursDraft=null;hoursApply.disabled=true;hoursInfo.textContent="有未应用修改，请重新预览。";}
const hoursHistory=node("div");hoursArea.append(node("h3","最近 20 次工作时间变更"),hoursHistory);
function renderHoursHistory(rows){
  hoursHistory.replaceChildren(...rows.map(item=>{
    const card=row(`版本 ${item.revision}：${item.previous.start}–${item.previous.end} → ${item.values.start}–${item.values.end}`,`${item.actor_id} · ${item.updated_at}`);
    const restore=node("button","预览恢复到此变更前");
    restore.disabled=Boolean(hoursSnapshot?.needs_migration);
    restore.addEventListener("click",async()=>{
      if(!hoursSnapshot)return;const epoch=++hoursEpoch;hoursDraft=null;hoursApply.disabled=true;
      try{const value=await api("/api/work-hours-preview",{expected_revision:hoursSnapshot.revision,rollback_revision:item.revision});if(epoch!==hoursEpoch)return;hoursDraft=value.draft_id;hoursStart.value=value.proposed.start;hoursEnd.value=value.proposed.end;hoursStart.disabled=hoursEnd.disabled=false;hoursInfo.textContent=JSON.stringify(value,null,2);hoursApply.disabled=false;}
      catch(error){if(epoch===hoursEpoch)hoursInfo.textContent="回滚预览失败："+error.message;}
    });card.append(restore);return card;
  }));
}
hoursStart.addEventListener("input",invalidateHours);hoursEnd.addEventListener("input",invalidateHours);
hoursRead.addEventListener("click",async()=>{
  const epoch=++hoursEpoch;hoursSnapshot=hoursDraft=null;hoursHistory.replaceChildren();hoursStart.disabled=hoursEnd.disabled=hoursPreview.disabled=hoursApply.disabled=true;
  try{const value=await api("/api/work-hours",{});if(epoch!==hoursEpoch)return;hoursSnapshot=value;hoursStart.value=value.values.start;hoursEnd.value=value.values.end;hoursStart.disabled=hoursEnd.disabled=hoursPreview.disabled=false;hoursPreview.textContent=value.needs_migration?"预览迁移至当前配置":"预览工作时间变更";hoursInfo.textContent=value.needs_migration?`基础配置已变化，旧覆盖暂停生效。请核对时段，迁移后按 ${value.timezone} 执行。旧时区未记录，不自动换算。`:`生效版本 ${value.revision} · 时区 ${value.timezone}`;renderHoursHistory(value.history||[]);}
  catch(error){if(epoch===hoursEpoch)hoursInfo.textContent="工作时间无法核实："+error.message;}
});
hoursPreview.addEventListener("click",async()=>{
  if(!hoursSnapshot)return;const epoch=++hoursEpoch;hoursDraft=null;hoursApply.disabled=true;
  try{const value=await api("/api/work-hours-preview",{values:{start:hoursStart.value,end:hoursEnd.value},expected_revision:hoursSnapshot.revision,...(hoursSnapshot.needs_migration?{migrate:true}:{})});if(epoch!==hoursEpoch)return;hoursDraft=value.draft_id;hoursInfo.textContent=JSON.stringify(value,null,2);hoursApply.disabled=false;}
  catch(error){if(epoch===hoursEpoch)hoursInfo.textContent="预览失败："+error.message;}
});
hoursApply.addEventListener("click",async()=>{
  if(!hoursDraft||hoursApply.disabled||!confirm("应用已预览的工作时间？影响后续时间策略，不撤回已发送消息或更改已有会议。"))return;
  const draft=hoursDraft,epoch=++hoursEpoch;hoursDraft=null;hoursStart.disabled=hoursEnd.disabled=hoursPreview.disabled=hoursApply.disabled=true;
  try{const value=await api("/api/work-hours-apply",{draft_id:draft});if(epoch===hoursEpoch)hoursInfo.textContent=`已应用版本 ${value.revision}，请重新读取核对。`;}
  catch(error){if(epoch===hoursEpoch)hoursInfo.textContent="应用结果待核对："+error.message+"。请重新读取，不自动重试。";}
});
let auditEpoch=0,auditNext=null;
const auditArea=node("section",undefined,"note"),auditKind=node("select"),auditReload=node("button","读取操作记录"),auditMore=node("button","下一页"),auditInfo=node("p"),auditList=node("div");
auditKind.setAttribute("aria-label","审计类别");
for(const [value,label] of Object.entries({all:"全部已接入类别",mode:"运行模式",features:"功能开关",budget:"预算",work_hours:"工作时间",notifications:"提醒策略",knowledge:"知识退回 / 停用",knowledge_import:"知识包导入",execution:"执行停止",case:"事项流转",approval:"审批当前快照",approval_history:"审批变更历史",delivery:"对外发送记录"})){const option=node("option",label);option.value=value;auditKind.append(option);}
auditKind.value="all";auditMore.disabled=true;
const profileAuditOption=node("option","同事角色修正");profileAuditOption.value="profiles";auditKind.append(profileAuditOption);
const subscriptionAuditOption=node("option","个人关注订阅");subscriptionAuditOption.value="subscriptions";auditKind.append(subscriptionAuditOption);
const auditControls=node("div",undefined,"detail-controls");auditControls.append(auditKind,auditReload,auditMore);
auditArea.append(node("h2","操作审计"),node("p","汇总已接入记录，包括全部工作时间修订；审批快照与升级后逐次变更历史分开，升级前历史不补造。发送记录区分快照、派发与结果事件，不证明对方已读，也不提供盲目重试。配置详细差异仍在功能与策略页查看。","muted"),auditControls,auditInfo,auditList);$("system").append(auditArea);
function auditItem(item){
  const card=row(item.summary,`${item.occurred_at} · ${item.actor_id} · ${item.target}`,item.kind);
  if(["case","approval","approval_history"].includes(item.kind)){
    const open=node("button",item.kind==="case"?"查看事项详情":"查看审批详情");
    open.addEventListener("click",()=>{if(!$("case-dialog").open)openDetailDialog();if(item.kind==="case")loadDetail(item.target);else loadApproval(item.target);});
    card.append(open);
  }return card;
}
async function loadAudit(cursor=null){
  const epoch=++auditEpoch;auditNext=null;auditMore.disabled=true;auditList.replaceChildren();auditInfo.textContent="正在读取";
  try{const value=await api("/api/audit",{kind:auditKind.value,cursor});if(epoch!==auditEpoch)return;
    auditNext=value.next_cursor;auditMore.disabled=!auditNext;auditInfo.textContent=`匹配 ${value.total_matching} 条 · 本页 ${value.items.length} 条 · 实时分页`;
    auditList.replaceChildren(...value.items.map(auditItem));
    if(!value.items.length)auditList.append(node("p","没有匹配记录"));
  }catch(error){if(epoch===auditEpoch)auditInfo.textContent="审计读取失败："+error.message;}
}
auditKind.addEventListener("change",()=>loadAudit());auditReload.addEventListener("click",()=>loadAudit());auditMore.addEventListener("click",()=>{if(auditNext)loadAudit(auditNext);});
const releaseArea=node("section"),releaseRepo=node("input"),releaseRead=node("button","读取发布评估"),releaseMore=node("button","下一页"),releaseInfo=node("p"),releaseList=node("div");
releaseRepo.setAttribute("aria-label","发布评估仓库（精确名称，留空为全部）");
releaseMore.disabled=true;
releaseArea.append(node("h2","发布影响"),node("p","只查看已有本地记录，不查询仓库、不调用 AI、不发送提醒。按记录编号分页，不是按发布时间排序。","muted"),releaseRepo,releaseRead,releaseMore,releaseInfo,releaseList);
$("system").append(releaseArea);
let releaseEpoch=0,releaseNext=null;
async function loadReleases(after=""){
  const epoch=++releaseEpoch;releaseNext=null;releaseMore.disabled=true;releaseList.replaceChildren();
  releaseInfo.textContent="正在读取…";
  try{
    const value=await api("/api/release-list",{repository:releaseRepo.value||"",after_id:after});
    if(epoch!==releaseEpoch)return;
    releaseInfo.textContent=value.note;
    for(const item of value.items){
      const card=node("article",undefined,"queue-item");
      card.append(node("h3",item.subject),node("p",`${item.repository} · ${item.change_id} · ${item.revision}`),node("p",`评估影响：${({low:"低",medium:"中",high:"高"})[item.impact_level]||"未知"} · 记录时间：${displayInstant(item.created_at)}`),node("p",item.summary));
      releaseList.append(card);
    }
    if(!value.items.length)releaseList.append(node("p","没有匹配的本地发布评估"));
    releaseNext=value.next_after_id;releaseMore.disabled=!releaseNext;
  }catch(error){if(epoch===releaseEpoch)releaseInfo.textContent=error.message+"；请重新读取。";}
}
releaseRepo.addEventListener("input",()=>{++releaseEpoch;releaseNext=null;releaseMore.disabled=true;releaseList.replaceChildren();releaseInfo.textContent="筛选已更改，请重新读取。";});
releaseRead.addEventListener("click",()=>loadReleases());
releaseMore.addEventListener("click",()=>{if(releaseNext)loadReleases(releaseNext);});
const watchArea=node("section"),watchRead=node("button","读取关注动态"),watchMore=node("button","下一页"),watchInfo=node("p"),watchList=node("div");
watchMore.disabled=true;
watchArea.append(node("h2","仓库与事项关注"),node("p","只显示已收集的本地动态。读取不会触发收集或发送通知。","muted"),watchRead,watchMore,watchInfo,watchList);
$("system").append(watchArea);
const watchKind=node("select"),watchKey=node("input"),watchSettingsRead=node("button","读取订阅设置"),watchPreview=node("button","预览订阅 / 退订"),watchApply=node("button","确认应用订阅"),watchSettingsInfo=node("p");
for(const [value,label] of [["release","仓库"],["case","Case 编号"]]){const option=node("option",label);option.value=value;watchKind.append(option);}
watchKind.value="release";watchKey.setAttribute("aria-label","精确仓库名称或 Case 编号");
watchKind.setAttribute("aria-label","关注对象类型");
watchSettingsInfo.setAttribute("role","status");watchInfo.setAttribute("role","status");
watchPreview.disabled=true;watchApply.disabled=true;
watchArea.prepend(watchKind,watchKey,watchSettingsRead,watchPreview,watchApply,watchSettingsInfo);
let watchSettingsEpoch=0,watchObserved=null,watchDraft=null;
function invalidateWatchSettings(){++watchSettingsEpoch;watchObserved=null;watchDraft=null;watchPreview.disabled=true;watchApply.disabled=true;watchSettingsInfo.textContent="请重新读取订阅设置。";}
watchKind.addEventListener("change",invalidateWatchSettings);watchKey.addEventListener("input",invalidateWatchSettings);
watchSettingsRead.addEventListener("click",async()=>{
  invalidateWatchSettings();const epoch=watchSettingsEpoch;
  try{const value=await api("/api/watch-settings",{source_kind:watchKind.value,source_key:watchKey.value});
    if(epoch!==watchSettingsEpoch)return;watchObserved=value;watchPreview.disabled=false;watchSettingsInfo.textContent=`版本 ${value.revision} · ${value.enabled?"已关注":"未关注"}`;
  }catch(error){if(epoch===watchSettingsEpoch)watchSettingsInfo.textContent=error.message;}
});
watchPreview.addEventListener("click",()=>{
  if(!watchObserved)return;
  watchDraft={source_kind:watchObserved.source_kind,source_key:watchObserved.source_key,enabled:!watchObserved.enabled,expected_revision:watchObserved.revision,request_id:crypto.randomUUID()};
  watchApply.disabled=false;watchSettingsInfo.textContent=`预览：${watchDraft.enabled?"关注":"退订"} ${watchDraft.source_kind}/${watchDraft.source_key}；不发送通知。重新关注可能补收退订期间动态。`;
});
watchApply.addEventListener("click",async()=>{
  if(!watchDraft)return;const draft=watchDraft;invalidateWatchSettings();const epoch=watchSettingsEpoch;
  try{await api("/api/watch-save",draft);if(epoch===watchSettingsEpoch)watchSettingsInfo.textContent="设置已记录，请重新读取核对。";}
  catch(error){if(epoch===watchSettingsEpoch)watchSettingsInfo.textContent=error.message+"；结果未知时不要重复提交，请重新读取。";}
});
let watchEpoch=0,watchNext=null;
function watchEventTitle(item){
  if(item.source_kind!=="case")return item.title;
  const labels={case_created:"事项已建立",state_transition:"事项状态流转",state_changed:"事项状态变更"};
  return Object.hasOwn(labels,item.title)?`${labels[item.title]}（${item.title}）`:item.title;
}
async function loadWatches(after=""){
  const epoch=++watchEpoch;watchNext=null;watchMore.disabled=true;watchList.replaceChildren();
  watchInfo.textContent="正在读取…";
  try{
    const value=await api("/api/watch-list",{after_id:after});
    if(epoch!==watchEpoch)return;
    watchInfo.textContent=value.note;
    for(const item of value.items){
      const card=node("article",undefined,"queue-item");
      card.append(node("h3",watchEventTitle(item)),node("p",`${item.source_kind==="case"?"事项":"仓库"}：${item.source_key} · ${displayInstant(item.occurred_at)}`));
      if(item.source_kind==="case")card.append(executionCaseButton(item.source_key));
      else card.append(node("p",`修订：${item.revision}`));
      const seen=node("button","已看过（仅本地）");
      seen.addEventListener("click",async()=>{
        if(epoch!==watchEpoch||seen.disabled)return;seen.disabled=true;
        try{await api("/api/watch-seen",{action_id:item.action_id});if(epoch===watchEpoch)await loadWatches();}
        catch(error){if(epoch===watchEpoch)watchInfo.textContent=error.message+"；结果未知，请重新读取，不自动重试。";}
      });
      card.append(seen);
      watchList.append(card);
    }
    if(!value.items.length)watchList.append(node("p","暂无已收集的关注动态"));
    watchNext=value.next_after_id;watchMore.disabled=!watchNext;
  }catch(error){if(epoch===watchEpoch)watchInfo.textContent=error.message+"；请重新读取。";}
}
watchRead.addEventListener("click",()=>loadWatches());
watchMore.addEventListener("click",()=>{if(watchNext)loadWatches(watchNext);});
let executionEpoch=0,executionNext=null;
const executionArea=node("section",undefined,"page");executionArea.id="executions";executionArea.hidden=true;
const executionState=node("select"),executionReload=node("button","重新读取"),executionMore=node("button","下一页"),executionInfo=node("p"),executionBoard=node("div"),executionList=node("div");
executionState.setAttribute("aria-label","执行任务状态");
for(const [value,label] of Object.entries({active:"待处理 / 进行中 / 失联",all:"全部状态",failed:"失败",succeeded:"成功",cancelled:"已取消"})){const option=node("option",label);option.value=value;executionState.append(option);}
executionState.value="active";executionMore.disabled=true;
const executionControls=node("div",undefined,"detail-controls");executionControls.append(executionState,executionReload,executionMore);
executionInfo.setAttribute("role","status");
executionArea.append(node("h2","设备与代理"),node("p","读取控制面记录；停止或恢复任务需单独确认，不直接操作设备。没有租约不代表设备空闲；任务成功不等于现场问题已解决。","muted"),executionBoard,executionControls,executionInfo,executionList);
const executionParent=$("system").parentElement;
if(executionParent)executionParent.insertBefore(executionArea,executionParent.querySelector("footer"));
function executionCaseButton(id){const button=node("button","查看事项 / 审批");button.addEventListener("click",()=>{if(!$("case-dialog").open)openDetailDialog();loadDetail(id);});return button;}
function executionStopProgress(value){
  const process={not_started:"排队时已取消，未启动",main_process_exited:"主进程退出已有回执",unverified:"进程退出待核验"};
  const cleanup={not_applicable:"无关联上板会话",verified:"本会话 BROM 收尾和租约释放已有证据",unverified:"BROM 收尾和租约释放待核验"};
  const service={service_main_exited:"独立服务主进程退出已有管理器证据",unverified:"独立服务退出待核验"};
  const serviceText=service[value.service_process];
  return node("p",`${process[value.process]||"进程状态未知"}${serviceText?"；"+serviceText:""}；${cleanup[value.board_cleanup]||"收尾状态未知"}。不据此保证所有逃逸子进程已停止。`);
}
function executionStopButton(id){
  const button=node("button","停止本次编码任务");
  button.addEventListener("click",async()=>{
    if(button.disabled)return;button.disabled=true;const epoch=executionEpoch;
    try{
      const shown=await api("/api/execution-stop-preview",{job_id:id});
      if(epoch!==executionEpoch)return;
      if(!confirm(`${shown.job_id} · ${shown.case_id}\n${shown.message}`)){button.disabled=false;return;}
      const result=await api("/api/execution-stop",{job_id:id,binding_digest:shown.binding_digest,request_id:crypto.randomUUID()});
      if(epoch!==executionEpoch)return;
      executionInfo.textContent=result.accepted?"停止请求已接受；进程退出、BROM 收尾和租约释放仍待核验。沟通负责人未改变。":"停止未确认，请重新读取。";
    }catch(error){if(epoch===executionEpoch)executionInfo.textContent="停止结果待核对："+error.message+"。请重新读取，不自动重试。";}
  });return button;
}
function executionRecoveryButton(id){
  const button=node("button","校验并恢复排队");
  button.addEventListener("click",async()=>{
    if(button.disabled)return;button.disabled=true;const epoch=executionEpoch;
    try{
      const shown=await api("/api/execution-recovery-preview",{job_id:id});
      if(epoch!==executionEpoch)return;
      if(!confirm(`${shown.job_id} · ${shown.case_id}\n${shown.message}`)){button.disabled=false;return;}
      const result=await api("/api/execution-recovery",{job_id:id,binding_digest:shown.binding_digest,request_id:crypto.randomUUID()});
      if(epoch!==executionEpoch)return;
      executionInfo.textContent=result.accepted?"恢复请求已接受，请重新读取任务状态；后续执行仍遵循当前模式、上板和推送审批。":"恢复未确认，请重新读取。";
      if(result.accepted)await loadExecutions();
    }catch(error){if(epoch===executionEpoch)executionInfo.textContent="未确认恢复："+error.message+"。请重新读取，不自动重试。";}
  });return button;
}
function remoteCleanupButton(id){
  const button=node("button","核对远端占用");
  button.addEventListener("click",async()=>{
    if(button.disabled)return;button.disabled=true;const epoch=executionEpoch;
    const observationId=crypto.randomUUID();
    try{
      const observed=await api("/api/remote-observe",{request_id:id,observation_id:observationId});
      if(epoch!==executionEpoch)return;
      if(observed.state!=="observed"){
        executionInfo.textContent=`远端核对尚未确认（${observed.state}）。保留观察编号 ${observationId}，不自动重查或重跑。`;return;
      }
      const shown=await api("/api/remote-cleanup-preview",{request_id:id,observation_id:observationId});
      if(epoch!==executionEpoch)return;
      if(!confirm(`${id}\n${shown.summary}`)){button.disabled=false;return;}
      await api("/api/remote-cleanup-apply",{request_id:id,observation_id:observationId,preview_digest:shown.preview_digest});
      if(epoch!==executionEpoch)return;
      await loadExecutions();
    }catch(error){if(epoch===executionEpoch)executionInfo.textContent=`未确认解除：${error.message}。观察编号 ${observationId}；不自动重试。`;}
  });return button;
}
async function loadExecutions(afterId=""){
  const epoch=++executionEpoch;executionNext=null;executionMore.disabled=true;executionInfo.textContent="正在读取";executionList.replaceChildren();executionBoard.replaceChildren();
  try{
    const result=await api("/api/executions",{state:executionState.value,after_id:afterId});
    if(epoch!==executionEpoch)return;
    executionNext=result.next_cursor;executionMore.disabled=!executionNext;
    executionInfo.textContent=`匹配 ${result.total_matching} 项 · 本页 ${result.items.length} 项 · 读取于 ${result.observed_at}`;
    const lease=result.board.lease;
    executionBoard.append(node("h3",result.board.name),node("p",lease?`租约记录：${lease.expiry_state} · 到期 ${lease.expires_at} · 心跳 ${lease.heartbeat_at}`:"没有控制面租约记录，实际占用未知。"));
    if(lease?.case_id)executionBoard.append(executionCaseButton(lease.case_id));
    if(result.board.cleanup_pending?.total){
      executionBoard.append(node("h3",`${result.board.cleanup_pending.total} 项板卡收尾尚未确认（不受任务筛选影响）`));
      for(const cleanup of result.board.cleanup_pending.items){
        executionBoard.append(node("p",`${cleanup.label} · 会话 ${cleanup.session_id} · 更新 ${cleanup.updated_at}`));
        if(cleanup.case_id)executionBoard.append(executionCaseButton(cleanup.case_id));
      }
    }
    executionBoard.append(node("p","上板仍需确认当前是否可用。接管回复不会停止测试；此页不提供未经验证的强制释放或复位按钮。","muted"));
    executionList.replaceChildren(...result.items.map(item=>{
      const identity=item.coding_identity;
      const title=item.job_type==="codex"?(identity?.label||"编码任务"):item.job_type;
      const card=row(`${title} · ${item.job_id}`,`事项 ${item.case_id||"未关联"} · 心跳 ${item.heartbeat_at||"未知"} · 退出码 ${item.exit_code??"未知"}`,item.state);
      if(item.job_type==="codex"){
        const binding=identity?.binding;
        card.append(node("p",binding==="deployment_contract"||binding==="legacy"
          ?`任务模型：${identity.model} · 推理：${identity.reasoning} · ${binding==="deployment_contract"?"已绑定部署契约":"旧任务配置"}`
          :binding==="retired"?"任务内容已退役，执行器信息不再展示。":"执行器身份未确认，请核对任务记录。","muted"));
        if(binding==="deployment_contract"||binding==="legacy")card.append(node("p","以上为任务绑定配置，不代表工具已启动或任务已完成。","muted"));
      }
      if(item.stop?.requested)card.append(executionStopProgress(item.stop));
      if(item.job_type==="codex"&&["queued","running"].includes(item.state))card.append(executionStopButton(item.job_id));
      if(item.input_recovery_available){card.append(node("p",item.error_class==="broker_budget_blocked"?"预算阻断：调整额度后可预览恢复；不清空已有费用，不重试已授权执行。":"输入快照异常：需先修复原输入，再校验恢复；此处不修改快照。"),executionRecoveryButton(item.job_id));}
      for(const remote of item.remote_pending||[]){
        card.append(node("p",`远端待核对 · ${remote.request_id}`,"muted"));
        if(remote.observation_available)card.append(remoteCleanupButton(remote.request_id));
        else card.append(node("p","旧任务没有可核对的回执，需人工排查；不会强行解除占用。","muted"));
      }
      for(const cleanup of item.board_cleanup||[]){
        card.append(node("h3",cleanup.label),node("p",`第 ${cleanup.lifecycle_round} 轮 / 第 ${cleanup.attempt_no} 次执行 · 会话 ${cleanup.session_id} · 更新 ${cleanup.updated_at}`,"muted"));
        card.append(node("p","收尾完成记录不代表板卡当前空闲，也不代表故障已修复。","muted"));
      }
      if(item.case_id)card.append(executionCaseButton(item.case_id));return card;
    }));
    if(!result.items.length)executionList.append(node("p","没有匹配任务"));
  }catch(error){if(epoch===executionEpoch)executionInfo.textContent="无法核实执行状态："+error.message;}
}
executionReload.addEventListener("click",()=>loadExecutions());executionState.addEventListener("change",()=>loadExecutions());executionMore.addEventListener("click",()=>{if(executionNext)loadExecutions(executionNext);});
document.querySelectorAll("nav [data-view]").forEach(button=>button.addEventListener("click",()=>{document.querySelectorAll(".page").forEach(page=>page.hidden=page.id!==button.dataset.view);document.querySelectorAll("nav button").forEach(el=>{el.classList.remove("selected");el.setAttribute("aria-current","false");});button.classList.add("selected");button.setAttribute("aria-current","page");$("page-title").textContent=button.textContent;if(button.dataset.view==="bugs")loadBugs();if(button.dataset.view==="mail")loadMail();if(button.dataset.view==="executions")loadExecutions();if(button.dataset.view==="system")loadBudget();if(button.dataset.view==="knowledge")loadKnowledge();}));
const importArea=node("section",undefined,"note"),importInput=node("textarea"),importPreview=node("button","校验并预览知识包"),importApply=node("button","确认导入已预览知识包"),importInfo=node("pre");
const retentionArea=node("section",undefined,"note"),retentionFilter=node("select"),retentionRead=node("button","读取资料保留状态"),retentionNext=node("button","下一页保留记录"),retentionInfo=node("p"),retentionList=node("div");
const bodyRetentionArea=node("section",undefined,"note"),bodyRetentionDays=node("input"),bodyRetentionRead=node("button","预览过期正文"),bodyRetentionNext=node("button","下一页正文"),bodyRetentionInfo=node("p"),bodyRetentionList=node("div");
bodyRetentionDays.type="number";bodyRetentionDays.min="1";bodyRetentionDays.max="3650";bodyRetentionDays.value="180";bodyRetentionDays.setAttribute("aria-label","正文保留天数");
bodyRetentionNext.disabled=true;
bodyRetentionArea.append(node("h2","正文保留"),node("p","预览不展示消息正文。清理须逐条确认且不可撤销；天数仅用于本次操作，不会修改自动保留策略。","muted"),bodyRetentionDays,bodyRetentionRead,bodyRetentionNext,bodyRetentionInfo,bodyRetentionList);$("system").append(bodyRetentionArea);
let bodyRetentionEpoch=0,bodyRetentionCursor=null;
const bodyRetentionBlockers={already_cleared:"已清理",referenced:"仍被引用",dependency_scan_incomplete:"引用扫描不完整",nonterminal:"尚未处理完成",lease_present:"仍有处理租约",raw_artifact_present:"另有原始文件"};
async function loadBodyRetention(after=""){
  const epoch=++bodyRetentionEpoch,days=Number(bodyRetentionDays.value);bodyRetentionCursor=null;bodyRetentionNext.disabled=true;bodyRetentionList.replaceChildren();
  if(!Number.isInteger(days)||days<1||days>3650){bodyRetentionInfo.textContent="请输入 1–3650 的整数天数。";return;}
  bodyRetentionInfo.textContent="正在核对正文保留状态…";
  try{const value=await api("/api/body-retention-preview",{days,after_id:after,limit:30});if(epoch!==bodyRetentionEpoch)return;
    bodyRetentionCursor=value.next_cursor;bodyRetentionNext.disabled=!bodyRetentionCursor;
    bodyRetentionInfo.textContent=value.json_scan_issues?.length?"引用扫描不完整，本页不能作为清理依据。":"本页仅显示已检查范围内的状态，不代表清理授权。";
    for(const item of value.items){const cleared=item.body_state==="cleared_by_retention";
      const card=row(item.event_pk,`${item.source} · ${item.received_at}`,cleared?"正文已清理":"正文仍保留");
      card.append(node("p",`消息正文：${item.body_bytes} 字节 · 错误详情：${item.last_error_bytes ?? '未知'} 字节`));
      if(item.clear_receipt)card.append(node('p',item.clear_receipt.last_error_digest?'本次回执包含错误详情清理。':'旧回执仅证明消息正文清理，不证明错误详情已清理。','muted'));
      if(item.clear_receipt)card.append(node("p",`清理时间：${item.clear_receipt.cleared_at} · 执行者：${item.clear_receipt.actor}`));
      card.append(node("p",(item.clear_blockers||[]).map(key=>bodyRetentionBlockers[key]||key).join("、")||"已检查范围内无阻挡；仍需单独确认清理。","muted"));
      if(!cleared&&Array.isArray(item.clear_blockers)&&!item.clear_blockers.length&&!value.json_scan_issues?.length){
        const clear=node("button","清理此条正文…");card.append(clear);
        clear.addEventListener("click",async()=>{if(clear.disabled||epoch!==bodyRetentionEpoch)return;
          if(!confirm(`消息 ${item.event_pk}\n保留期限：${days} 天\n将不可撤销地清空此条数据库消息正文和错误详情，保留去重身份、状态、重试次数和审计凭据。原始文件、派生副本和备份不会随之清理。\n确认继续？`))return;
          clear.disabled=true;
          try{await api("/api/body-retention-clear",{days,after_id:after,limit:30,expected:{[item.event_pk]:item.snapshot_digest},confirm_database_body_only:true,confirm_error_details:true});
            if(epoch===bodyRetentionEpoch)await loadBodyRetention(after);
          }catch(error){if(epoch===bodyRetentionEpoch)bodyRetentionInfo.textContent=`清理未确认：${error.message}。请重新预览核对，不会自动重试。`;}
        });
      }
      bodyRetentionList.append(card);
    }
    if(!value.items.length)bodyRetentionList.append(node("p","没有符合本次保留天数的记录。"));
  }catch(error){if(epoch===bodyRetentionEpoch)bodyRetentionInfo.textContent=`无法读取：${error.message}`;}
}
bodyRetentionRead.addEventListener("click",()=>loadBodyRetention());
bodyRetentionNext.addEventListener("click",()=>{if(bodyRetentionCursor)loadBodyRetention(bodyRetentionCursor);});
bodyRetentionDays.addEventListener("input",()=>{bodyRetentionEpoch++;bodyRetentionCursor=null;bodyRetentionNext.disabled=true;bodyRetentionList.replaceChildren();bodyRetentionInfo.textContent="天数已修改，请重新预览。";});
retentionFilter.setAttribute("aria-label","资料保留状态筛选");
for(const [value,label] of [["all","全部"],["prepared","等待隔离或核对"],["quarantined","已隔离"],["restored","已恢复"],["failed","处理失败"],["cancelled","已取消"]]){const option=node("option",label);option.value=value;retentionFilter.append(option);}
retentionFilter.value="all";retentionNext.disabled=true;
const retentionPurgeDays=node("input");retentionPurgeDays.type="number";retentionPurgeDays.value="30";retentionPurgeDays.min="1";retentionPurgeDays.max="3650";retentionPurgeDays.setAttribute("aria-label","隔离后保留天数");
retentionArea.append(node("h2","资料保留状态"),retentionFilter,node("p","永久清理仅针对已隔离原件，不清理备份；隔离后保留天数：","muted"),retentionPurgeDays,retentionRead,retentionNext,retentionInfo,retentionList);$("system").append(retentionArea);
let retentionEpoch=0,retentionCursor=null;
retentionPurgeDays.addEventListener("input",()=>{++retentionEpoch;retentionCursor=null;retentionNext.disabled=true;retentionList.replaceChildren();retentionInfo.textContent="隔离保留天数已改变，请重新读取。";});
function appendPurgeControls(card,item,epoch){
  const active=["prepared","running","unknown","purged"].includes(item.purge_state);
  if(active){
    card.append(node("p",`永久清理请求：${item.purge_state} · ${item.purge_request_id}`,"muted"));
    const check=node("button","核对永久清理（只读）");card.append(check);
    check.addEventListener("click",async()=>{if(check.disabled||epoch!==retentionEpoch)return;check.disabled=true;
      try{const value=await api("/api/retention-purge-inspect",{request_id:item.purge_request_id});if(epoch!==retentionEpoch)return;
        const labels={present:"文件仍在",absent:"未发现文件",unavailable:"无法核实"},observations=value.observations||{};
        card.append(node("p",`原位置：${labels[observations.original_path?.state]||"未知"}；隔离位置：${labels[observations.quarantine_path?.state]||"未知"}`));
        retentionInfo.textContent="仅核对当前观察，不重试删除；文件缺失不等于已成功执行删除。";
        if(["running","unknown"].includes(value.request_state)&&value.source_binding==="quarantine"&&observations.original_path?.state==="absent"){
          const state=observations.quarantine_path?.state;
          const decision=state==="present"?"keep_file":state==="absent"?"confirm_absence":null;
          if(decision&&value.observation_digest){
            const label=decision==="keep_file"?"保留原件，撤销旧清理":"确认文件缺失，修正记录";
            const finish=node("button",label);card.append(finish);
            finish.addEventListener("click",async()=>{if(finish.disabled||epoch!==retentionEpoch)return;
              if(!confirm(`${label}\n请求 ${item.purge_request_id}\n本操作只修正记录，不删除文件、不重试清理。服务器会重新核对文件与执行进程。确认？`))return;
              finish.disabled=true;
              try{const result=await api("/api/retention-purge-reconcile",{request_id:item.purge_request_id,decision,observation_digest:value.observation_digest,confirm_observation:true});
                if(epoch!==retentionEpoch)return;
                if(result.request_id!==item.purge_request_id||result.decision!==decision||result.files_deleted!==0)throw new Error("核对收尾状态未确认");
                await loadRetention();
              }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`核对收尾未确认，请重新读取；不会自动重试：${error.message}`;}
            });
          }
        }
      }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`核对失败：${error.message}`;}
    });
    if(item.purge_state==="prepared"){
      const cancel=node("button","撤销未执行清理");card.append(cancel);
      cancel.addEventListener("click",async()=>{if(cancel.disabled||epoch!==retentionEpoch)return;cancel.disabled=true;
        try{await api("/api/retention-purge-cancel",{request_id:item.purge_request_id});if(epoch===retentionEpoch)await loadRetention();}
        catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`撤销未确认，请重新核对：${error.message}`;}
      });
    }
    return;
  }
  if(item.state!=="quarantined")return;
  const purge=node("button","预览永久清理…");card.append(purge);
  purge.addEventListener("click",async()=>{if(purge.disabled||epoch!==retentionEpoch)return;
    const days=Number(retentionPurgeDays.value);if(!Number.isInteger(days)||days<1||days>3650){retentionInfo.textContent="请输入 1–3650 的整数天数。";return;}
    purge.disabled=true;
    try{const shown=await api("/api/retention-purge-preview",{attempt_id:item.attempt_id,days});if(epoch!==retentionEpoch)return;
      if(!Array.isArray(shown.blockers)||shown.blockers.length){retentionInfo.textContent=`不能清理：${(shown.blockers||["检查不完整"]).join("、")}`;return;}
      if(!confirm(`永久删除隔离原件 ${item.attempt_id}\n隔离后保留 ${days} 天；${shown.logical_bytes} 字节\n删除不可恢复，备份不会一并清理。确认继续？`)){purge.disabled=false;return;}
      const request_id=crypto.randomUUID();
      const prepared=await api("/api/retention-purge-prepare",{attempt_id:item.attempt_id,days,binding_digest:shown.binding_digest,request_id,confirm_permanent_delete:true});if(epoch!==retentionEpoch)return;
      if(prepared.state!=="prepared"||prepared.request_id!==request_id)throw new Error("清理准备状态未确认");
      const result=await api("/api/retention-purge-execute",{request_id});if(epoch!==retentionEpoch)return;
      if(result.state!=="purged")throw new Error("清理结果尚未确认");
      await loadRetention();
    }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`永久清理未确认：${error.message}。请重新读取并核对，不自动重试。`;}
  });
}
async function loadRetention(after=""){
  const epoch=++retentionEpoch;retentionCursor=null;retentionNext.disabled=true;retentionList.replaceChildren();retentionInfo.textContent="正在读取…";
  try{const value=await api("/api/retention-inventory",{state:retentionFilter.value,after_id:after});if(epoch!==retentionEpoch)return;
    retentionCursor=value.next_cursor;retentionNext.disabled=!retentionCursor;retentionInfo.textContent=`匹配 ${value.total_matching} 项。${value.note}`;
    for(const item of value.items){const card=row(item.label,`记录 ${item.attempt_id} · 更新 ${item.updated_at}`,item.state);
      appendPurgeControls(card,item,epoch);
      if(item.recovery_state)card.append(node("p",`最近恢复请求：${item.recovery_state==="restored"?"已完成":"结果待核对"} · 操作员 ${item.recovery_actor}`,"muted"));
      if(item.recovery_request_id&&["running","unknown"].includes(item.recovery_state)){
        const check=node("button","核对恢复结果（不移动文件）");card.append(check);
        check.addEventListener("click",async()=>{if(check.disabled)return;check.disabled=true;try{
          const checked=await api("/api/retention-recovery-check",{request_id:item.recovery_request_id});if(epoch!==retentionEpoch)return;
          if(checked.state==="restored")await loadRetention(after);else retentionInfo.textContent="证据仍不足，保留待核对；没有移动文件，请管理员检查。";
        }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`核对未完成：${error.message}`;}});
      }else if(["prepared","quarantined","failed"].includes(item.state)&&!["prepared","running","unknown","purged"].includes(item.purge_state)){
        const restore=node("button","预览恢复原件");card.append(restore);
        restore.addEventListener("click",async()=>{if(restore.disabled)return;restore.disabled=true;
          try{const shown=await api("/api/retention-recovery-preview",{attempt_id:item.attempt_id});if(epoch!==retentionEpoch)return;
            if(!confirm(`记录 ${shown.attempt_id}\n来源 ${shown.event_pk}\n${shown.summary}`)){restore.disabled=false;return;}
            const done=await api("/api/retention-recovery-apply",{attempt_id:shown.attempt_id,binding_digest:shown.binding_digest,request_id:crypto.randomUUID()});
            if(epoch!==retentionEpoch)return;
            if(done.state!=="restored"&&done.state!=="cancelled")throw new Error("恢复状态尚未确认");
            await loadRetention(after);
          }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`恢复结果待核对，不自动重试：${error.message}`;}
        });
      }retentionList.append(card);}
    if(!value.items.length)retentionList.append(node("p","没有匹配的保留记录"));
  }catch(error){if(epoch===retentionEpoch)retentionInfo.textContent=`无法读取保留记录：${error.message}`;}
}
retentionRead.addEventListener("click",()=>loadRetention());retentionFilter.addEventListener("change",()=>loadRetention());retentionNext.addEventListener("click",()=>{if(retentionCursor)loadRetention(retentionCursor);});
const replayArchiveArea=node('details'),replayArchiveRead=node('button','读取归档列表'),replayArchiveInfo=node('p'),replayArchiveItems=node('div');
replayArchiveInfo.setAttribute('role','status');
replayArchiveArea.append(node('summary','历史回放归档'),node('p','只查看归档信息，不读取聊天正文。列出不代表可信；执行回放仍需独立保存的校验摘要。','muted'),replayArchiveRead,replayArchiveInfo,replayArchiveItems);
$('system').append(replayArchiveArea);
let replayArchiveEpoch=0;
const archiveCaptureArea=node('details'),archiveCaptureName=node('input'),archiveCaptureEvent=node('textarea'),archiveCaptureProposal=node('textarea'),archiveCaptureStart=node('button','确认保存私有归档'),archiveCaptureInfo=node('p');
archiveCaptureName.setAttribute('aria-label','新归档名称');archiveCaptureName.placeholder='例如 acceptance-20260909';
archiveCaptureEvent.setAttribute('aria-label','回放事件 JSON');archiveCaptureProposal.setAttribute('aria-label','路由提案 JSON');
archiveCaptureArea.append(node('summary','保存当前实例回放归档'),node('p','先选择完全停止，并暂停外部写入。此操作复制私有数据库、当前代码及依赖，最多运行 30 秒；不会自动回放，不包含完整模型与系统环境。','muted'),node('label','新归档名称'),archiveCaptureName,node('label','回放事件 JSON'),archiveCaptureEvent,node('label','路由提案 JSON'),archiveCaptureProposal,archiveCaptureStart,archiveCaptureInfo);
replayArchiveArea.append(archiveCaptureArea);
archiveCaptureStart.addEventListener('click',async()=>{
  if(archiveCaptureStart.disabled)return;
  const name=archiveCaptureName.value.trim();let event,proposal;
  try{
    if(!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/.test(name))throw Error();
    event=JSON.parse(archiveCaptureEvent.value);proposal=JSON.parse(archiveCaptureProposal.value);
    if(!event||Array.isArray(event)||typeof event!=='object'||!proposal||Array.isArray(proposal)||typeof proposal!=='object')throw Error();
  }catch(error){archiveCaptureInfo.textContent='请填写合法归档名称和两个 JSON 对象。';return;}
  if(!confirm('确认已完全停止并暂停外部写入，且允许保存含私有内容的归档？不会自动恢复服务或执行回放。'))return;
  archiveCaptureStart.disabled=true;archiveCaptureInfo.textContent=`正在保存 ${name}…请勿重复提交。`;
  try{
    const value=await api('/api/replay-archive-capture',{name,event,proposal,confirmed:true,writers_quiesced:true});
    archiveCaptureInfo.textContent=`已保存 ${value.name}。请独立保存清单 SHA-256：${value.manifest_digest}。不代表完整历史环境已复原或允许发布。`;
  }catch(error){archiveCaptureInfo.textContent=`归档 ${name} 未确认完成，请手动核对归档列表；不自动重试或恢复服务。`;}
  finally{archiveCaptureStart.disabled=false;}
});
function archiveRunControls(name,epoch){
  const area=node('div'),label=node('label','独立保存的清单 SHA-256'),input=node('input'),run=node('button','确认并隔离回放'),info=node('p'),output=node('pre');
  input.type='text';input.maxLength=64;input.autocomplete='off';input.setAttribute('aria-label','独立保存的清单 SHA-256');
  label.append(input);area.append(label,run,info,output);
  run.addEventListener('click',async()=>{
    if(run.disabled||epoch!==replayArchiveEpoch)return;
    const manifest_digest=input.value.trim();
    if(!/^[a-f0-9]{64}$/.test(manifest_digest)){info.textContent='请填写独立保存的 64 位小写 SHA-256；不能使用列表中的版本摘要。';return;}
    if(!confirm(`隔离回放归档 ${name}？将执行归档中的代码，结果可能包含历史内容；不连接生产服务、不批准发布。`))return;
    run.disabled=true;input.disabled=true;output.textContent='';info.textContent='正在校验并隔离回放…';
    try{
      const value=await api('/api/replay-archive-run',{name,manifest_digest,confirmed:true});
      if(epoch!==replayArchiveEpoch)return;
      output.textContent=JSON.stringify(value,null,2);info.textContent='回放已返回结果；这不代表历史模型已复原、问题已修复或允许发布。';
    }catch(error){if(epoch===replayArchiveEpoch)info.textContent='回放未确认完成。请核对归档和运行状态，不要自动重试。';}
    finally{if(epoch===replayArchiveEpoch){run.disabled=false;input.disabled=false;}}
  });
  return area;
}
async function loadReplayArchives(){
  const epoch=++replayArchiveEpoch;replayArchiveItems.replaceChildren();replayArchiveInfo.textContent='正在读取归档…';
  try{
    const result=await api('/api/replay-archives',{limit:50});if(epoch!==replayArchiveEpoch)return;
    for(const item of result.items){
      const state={manifest_present_unverified:'清单存在 · 尚未验证',incomplete:'归档未完成',unreadable_or_invalid:'无法读取或格式无效'}[item.status]||'状态未知';
      const card=node('div');card.append(node('p',`${item.name} · ${state}`));
      if(item.recorded_at)card.append(node('p',`记录时间：${item.recorded_at}`,'muted'));
      if(item.runtime_digest)card.append(node('p',`版本摘要：${item.runtime_digest.slice(0,12)}…`,'muted'));
      if(item.status==='manifest_present_unverified')card.append(archiveRunControls(item.name,epoch));
      replayArchiveItems.append(card);
    }
    replayArchiveInfo.textContent=result.truncated?'仅显示部分归档；这不是完整列表。':result.items.length?'仅供查阅，未执行回放。':'暂无历史回放归档。';
  }catch(error){if(epoch===replayArchiveEpoch)replayArchiveInfo.textContent='归档读取失败，请稍后手动重试。';}
}
replayArchiveRead.addEventListener('click',()=>loadReplayArchives());
const backupAuditArea=node("details"),backupAuditRead=node("button","读取清理记录"),backupAuditNext=node("button","下一页"),backupAuditInfo=node("p"),backupAuditItems=node("div");
backupAuditNext.disabled=true;backupAuditArea.append(node("summary","备份清理记录 · 只读核对"),backupAuditRead,backupAuditNext,backupAuditInfo,backupAuditItems);$("system").append(backupAuditArea);
let backupAuditEpoch=0,backupCheckEpoch=0,backupAuditCursor=null;
async function loadBackupAudit(after_id=""){
  const epoch=++backupAuditEpoch;++backupCheckEpoch;backupAuditNext.disabled=true;backupAuditItems.replaceChildren();backupAuditInfo.textContent="正在读取…";
  try{
    const result=await api("/api/backup-retention-history",{after_id});if(epoch!==backupAuditEpoch)return;
    backupAuditCursor=result.next_cursor;backupAuditNext.disabled=!backupAuditCursor;backupAuditInfo.textContent=result.items.length?"核对只观察当前文件，不执行删除或重试。":"暂无清理记录。";
    for(const item of result.items){
      const row=node("div"),state={prepared:"结果未确认",deleted:"有删除完成记录",unreadable:"记录不可读"}[item.state]||"未知状态";
      row.append(node("p",`${item.name||"备份记录"} · ${state}`));
      if(item.state!=="unreadable"){
        const inspect=node("button","只读核对");row.append(inspect);
        inspect.addEventListener("click",async()=>{
          const check=++backupCheckEpoch;inspect.disabled=true;
          try{
            const value=await api("/api/backup-retention-inspect",{receipt_id:item.receipt_id});
            if(epoch!==backupAuditEpoch||check!==backupCheckEpoch)return;
            const description={missing:"当前文件已不在，但不能据此确认删除原因。",same_version:"原版本文件仍在。",different_version:"同名文件已变更，不应作为原清理目标。",not_regular:"目标已不是普通文件，未跟随链接。",changed_during_check:"核对期间目标发生变化，本次不下结论。"}[value.target_state]||"当前状态无法确认。";
            backupAuditInfo.textContent=`${value.name}：${description} 核对时间：${value.observed_at||"未提供"}。未修改记录，未授权删除或重试。`;
          }catch(error){if(epoch===backupAuditEpoch&&check===backupCheckEpoch)backupAuditInfo.textContent="无法完成核对，保留原记录；没有执行清理。";}
          finally{inspect.disabled=false;}
        });
      }
      backupAuditItems.append(row);
    }
  }catch(error){if(epoch===backupAuditEpoch)backupAuditInfo.textContent=error.message;}
}
backupAuditRead.addEventListener("click",()=>loadBackupAudit());backupAuditNext.addEventListener("click",()=>{if(!backupAuditNext.disabled)loadBackupAudit(backupAuditCursor);});
importInput.setAttribute("aria-label","已审核专业知识包 JSON");importInput.rows=8;importApply.disabled=true;
let importEpoch=0,importDraft=null;
const importReview=node("div");
importArea.append(node("h2","导入专业知识包"),node("p","粘贴已按专业知识规范编译的 JSON 包。预览展示完整内容；导入会更新知识库，不签发自动回复授权，不替代独立的人类审核。单包最多 500 KB。"),importInput,importPreview,importApply,importInfo);$("knowledge").append(importArea);
importArea.append(importReview);
function renderImportReview(result){
  const labels={create:"新增",update:"更新",unchanged:"内容未变化（不会恢复已停用条目）"};
  importReview.replaceChildren(...(result.changes||[]).map(change=>{
    const proposed=(result.entries||[]).find(entry=>entry.metadata.id===change.id);
    const card=node("section",undefined,"note");card.append(node("h3",`${labels[change.action]||change.action} · ${change.id}`));
    card.append(node("h4","当前版本（正文、来源、适用范围及状态）"),node("pre",change.previous?JSON.stringify(change.previous,null,2):"无，将新增"));
    card.append(node("h4",`拟导入版本 ${change.proposed_revision}`),node("pre",JSON.stringify(proposed,null,2)));
    return card;
  }));
}
importInput.addEventListener("input",()=>{++importEpoch;importDraft=null;importApply.disabled=true;importReview.replaceChildren();importInfo.textContent="内容已变化，请重新预览。";});
importPreview.addEventListener("click",async()=>{
  const epoch=++importEpoch;importDraft=null;importApply.disabled=true;importReview.replaceChildren();
  try{const bundle=JSON.parse(importInput.value);const result=await api("/api/knowledge-import-preview",{bundle});if(epoch!==importEpoch)return;importDraft=result.draft_id;renderImportReview(result);importInfo.textContent=JSON.stringify({draft_id:result.draft_id,actions:result.actions,expires_at:result.expires_at},null,2);importApply.disabled=false;}
  catch(error){if(epoch===importEpoch)importInfo.textContent="预览失败："+error.message;}
});
importApply.addEventListener("click",async()=>{
  if(!importDraft||importApply.disabled||!confirm("确认已核对上方全部知识、来源、适用范围及审核记录，并导入此包？不会签发自动回复授权。"))return;
  const epoch=++importEpoch,draft=importDraft;importDraft=null;importApply.disabled=true;
  try{const result=await api("/api/knowledge-import-apply",{draft_id:draft});if(epoch===importEpoch)importInfo.textContent="导入已提交，请重新查询知识列表核对。\n"+JSON.stringify(result,null,2);}
  catch(error){if(epoch===importEpoch)importInfo.textContent=`导入结果待核对。请在系统页「知识包导入」审计中查找 ${draft}，并核对知识列表；没有记录也不证明请求未在途。不自动重试：`+error.message;}
});
const modelPreviewArea=node("section",undefined,"note"),modelPreviewQuery=node("textarea"),modelPreviewSender=node("input"),modelPreviewChat=node("input"),modelPreviewStart=node("button","确认并运行 AI 路由预演"),modelPreviewPoll=node("button","刷新预演结果"),modelPreviewInfo=node("pre");
modelPreviewSender.value="ou_preview";modelPreviewChat.value="oc_preview";modelPreviewPoll.disabled=true;
modelPreviewArea.append(node("h2","AI 流程预演"),node("p","使用当前数据库的隔离快照，可选择路由、资料选择、追问审核或已保存 Debug 结果复核。确认后才调用配置模型，可能产生费用；不发送消息、不运行编码任务、不操作板卡。不是完整现场验证或历史时点重建。默认虚拟联系人按未知身份处理。"));
for(const [label,field] of [["预演问题",modelPreviewQuery],["提问者 ID",modelPreviewSender],["会话 ID",modelPreviewChat]]){field.setAttribute("aria-label",label);const wrapper=node("label",label);wrapper.append(field);modelPreviewArea.append(wrapper);}
const modelPreviewAssumptions={};
const retentionPolicyArea=node("section",undefined,"note"),retentionPolicyRead=node("button","读取正文保留策略"),retentionPolicyEnabled=node("input"),retentionPolicyDays=node("input"),retentionPolicyPreview=node("button","预览策略变更"),retentionPolicyApply=node("button","确认应用保留策略"),retentionPolicyInfo=node("p");
const retentionPolicyScope=node("select");retentionPolicyScope.setAttribute("aria-label","保留策略对象");
for(const [value,title] of [["body","消息正文"],["draft","未审核附件草稿"]]){const option=node("option",title);option.value=value;retentionPolicyScope.append(option);}retentionPolicyScope.value="body";
function retentionPolicyEndpoint(){return retentionPolicyScope.value==="draft"?"/api/draft-retention-policy":"/api/body-retention-policy";}
retentionPolicyArea.append(retentionPolicyScope);
retentionPolicyEnabled.type="checkbox";retentionPolicyEnabled.checked=false;retentionPolicyDays.type="number";retentionPolicyDays.min="1";retentionPolicyDays.max="3650";retentionPolicyDays.value="30";retentionPolicyApply.disabled=true;
const retentionPolicyToggle=node("label","启用所选内容自动清理 "),retentionPolicyDayLabel=node("label","保留天数 ");retentionPolicyToggle.append(retentionPolicyEnabled);retentionPolicyDayLabel.append(retentionPolicyDays);retentionPolicyDays.setAttribute("aria-label","所选内容保留天数");retentionPolicyEnabled.setAttribute("aria-label","启用所选内容自动清理");
retentionPolicyArea.append(node("h2","资料保留期"),node("p","两种策略独立且默认关闭：消息正文只清理终态且无引用内容；草稿只清理未审核且无引用内容。不删除原始文件、已发布知识或备份。关闭和延长不能恢复已清理内容。"),retentionPolicyRead,retentionPolicyToggle,retentionPolicyDayLabel,retentionPolicyPreview,retentionPolicyApply,retentionPolicyInfo);$("settings").append(retentionPolicyArea);
let retentionPolicyEpoch=0,retentionPolicySnapshot=null,retentionPolicyDraft=null;
retentionPolicyScope.addEventListener("change",()=>{invalidateRetentionPolicy();retentionPolicySnapshot=null;retentionPolicyEnabled.checked=false;retentionPolicyRead.textContent="读取所选保留策略";retentionPolicyInfo.textContent="对象已切换，请读取当前策略。两种期限独立，未提交任何变更。";});
function invalidateRetentionPolicy(){++retentionPolicyEpoch;retentionPolicyDraft=null;retentionPolicyApply.disabled=true;}
for(const field of [retentionPolicyEnabled,retentionPolicyDays])field.addEventListener("input",invalidateRetentionPolicy);
retentionPolicyRead.addEventListener("click",async()=>{invalidateRetentionPolicy();retentionPolicySnapshot=null;const epoch=retentionPolicyEpoch;try{const value=await api(retentionPolicyEndpoint(),{});if(epoch!==retentionPolicyEpoch)return;retentionPolicySnapshot=value;retentionPolicyEnabled.checked=value.days!==null;retentionPolicyDays.value=String(value.days||30);retentionPolicyInfo.textContent=value.needs_migration?"基础配置已变化，当前停止清理。请先关闭并应用，再重新设置。":`当前：${value.days===null?"关闭":value.days+" 天"} · 版本 ${value.revision}`;}catch(error){if(epoch===retentionPolicyEpoch)retentionPolicyInfo.textContent=error.message;}});
retentionPolicyPreview.addEventListener("click",async()=>{
  invalidateRetentionPolicy();const epoch=retentionPolicyEpoch;if(!retentionPolicySnapshot){retentionPolicyInfo.textContent="请先读取当前策略。";return;}
  const days=retentionPolicyEnabled.checked?Number(retentionPolicyDays.value):null;
  if(days!==null&&(!Number.isInteger(days)||days<1||days>3650)){retentionPolicyInfo.textContent="请输入 1–3650 天整数。";return;}
  try{const value=await api(retentionPolicyEndpoint()+"-preview",{days,expected_revision:retentionPolicySnapshot.revision});if(epoch!==retentionPolicyEpoch)return;retentionPolicyDraft=value;retentionPolicyApply.disabled=false;retentionPolicyInfo.textContent=`${value.previous_days===null?"关闭":value.previous_days+" 天"} → ${value.days===null?"关闭":value.days+" 天"}。${value.warning}`;}catch(error){if(epoch===retentionPolicyEpoch)retentionPolicyInfo.textContent=error.message;}
});
retentionPolicyApply.addEventListener("click",async()=>{
  const draft=retentionPolicyDraft;if(!draft||retentionPolicyApply.disabled||!confirm(retentionPolicyInfo.textContent))return;
  retentionPolicyApply.disabled=true;retentionPolicyDraft=null;const epoch=++retentionPolicyEpoch;
  try{await api(retentionPolicyEndpoint()+"-apply",{draft_id:draft.draft_id,confirm_policy_change:true});if(epoch===retentionPolicyEpoch){retentionPolicySnapshot=null;retentionPolicyInfo.textContent="策略已应用。后台下一次检查会读取新值；此请求没有直接清理内容。请重新读取核对。";}}
  catch(error){if(epoch===retentionPolicyEpoch){retentionPolicySnapshot=null;retentionPolicyInfo.textContent="应用结果待核对，请重新读取策略；不会自动重试。";}}
});
const modelPreviewKind=node("select"),modelPreviewDocuments=node("textarea");
const modelPreviewFollowups=node("textarea");modelPreviewFollowups.rows=3;
modelPreviewFollowups.setAttribute("aria-label","后续来信（每行一条）");
modelPreviewFollowups.placeholder="可选：每行一条后续来信，最多 9 条。例如：不是 Pico，是 EVB。";
modelPreviewArea.append(node("p","连续来信预演：同一提问者、同一会话和时间假设；选择资料预演后生效，不模拟真人接管或发送。","muted"),modelPreviewFollowups);
for(const [value,title] of [["routing","只预演路由"],["research","路由＋资料选择（模拟检索结果）"],["research_review","路由＋资料选择＋必要时追问审核"],["debug","已保存 Debug 结果复核（重放验证记录）"]]){const option=node("option",title);option.value=value;modelPreviewKind.append(option);}modelPreviewKind.value="routing";modelPreviewKind.setAttribute("aria-label","预演范围");
const debugPreviewJob=node("input"),debugPreviewTranscript=node("textarea"),debugPreviewArea=node("details");
const debugExecutionOption=node("option","排队 Debug 模拟执行＋复核");debugExecutionOption.value="debug_execution";modelPreviewKind.append(debugExecutionOption);
const debugPreviewReport=node("textarea"),debugPreviewExit=node("select");debugPreviewReport.rows=8;debugPreviewReport.maxLength=50000;debugPreviewReport.setAttribute("aria-label","模拟 Debug 完整报告");debugPreviewExit.setAttribute("aria-label","模拟执行退出状态");
for(const [key,label] of [["unknown","无退出证据"],["0","成功退出（不代表问题解决）"],["1","失败退出"]]){const option=node("option",label);option.value=key;debugPreviewExit.append(option);}debugPreviewExit.value="unknown";
debugPreviewJob.setAttribute("aria-label","已完成的 Debug 任务 ID");debugPreviewTranscript.setAttribute("aria-label","Debug 验证记录 JSON");debugPreviewTranscript.value="[]";
debugPreviewArea.append(node("summary","Debug 结果预演输入"),node("p","仅支持已完成且保存了 broker 结果的任务。提供验证记录 JSON；仅重放记录，不执行其中命令、不上板，不证明记录真实。此模式不使用上方问题、角色和时间假设。"),debugPreviewJob,debugPreviewTranscript);modelPreviewArea.append(debugPreviewArea);
debugPreviewArea.append(node("p","模拟执行模式：填写下一条可领取的排队任务 ID，并在下方提供完整报告和模拟退出状态。仅用于隔离预演，不会运行报告中的命令或批准上板。"),debugPreviewReport,debugPreviewExit);
const debugPreviewFile=node("input"),debugPreviewImportInfo=node("p");debugPreviewFile.type="file";debugPreviewFile.accept=".json,application/json";debugPreviewFile.setAttribute("aria-label","导入 Debug 验证记录文件");
debugPreviewArea.append(node("p",'可导入 JSON 文件（最多 60 KiB），格式：{"job_id":"任务 ID","transcript":[验证记录]}。文件仅在当前页面读取，不会因导入而上传或调用模型。'),debugPreviewFile,debugPreviewImportInfo);
let debugImportEpoch=0;
for(const field of [debugPreviewJob,debugPreviewTranscript])field.addEventListener("input",()=>{++debugImportEpoch;});
debugPreviewFile.addEventListener("change",async()=>{
  const epoch=++debugImportEpoch,file=debugPreviewFile.files?.[0],oldJob=debugPreviewJob.value,oldText=debugPreviewTranscript.value;
  if(!file)return;
  if(file.size>61440){debugPreviewImportInfo.textContent="文件超过 60 KiB，未读取或上传。";return;}
  try{
    const value=JSON.parse(await file.text());
    if(epoch!==debugImportEpoch||oldJob!==debugPreviewJob.value||oldText!==debugPreviewTranscript.value)return;
    if(!value||Array.isArray(value)||Object.keys(value).sort().join(",")!=="job_id,transcript"||typeof value.job_id!=="string"||!value.job_id.trim()||value.job_id.length>256||!Array.isArray(value.transcript)||value.transcript.length>100)throw new Error("文件需要任务 ID 和最多 100 条验证记录。");
    if((oldJob?.trim()||oldText!=="[]")&&!confirm("导入会替换当前 Debug 任务 ID 和验证记录，继续吗？"))return;
    debugPreviewJob.value=value.job_id;debugPreviewTranscript.value=JSON.stringify(value.transcript,null,2);modelPreviewKind.value="debug";
    debugPreviewImportInfo.textContent=`已在本页载入 ${value.transcript.length} 条记录，尚未上传或调用模型；完整字段由运行前校验。`;
  }catch(error){if(epoch===debugImportEpoch)debugPreviewImportInfo.textContent="导入失败，原输入未修改。请检查 JSON 格式和字段。";}
});
modelPreviewDocuments.value="[]";modelPreviewDocuments.setAttribute("aria-label","候选文档 JSON");modelPreviewDocuments.placeholder='[{"title":"文档标题","url":"https://…","content":"提供的摘要或测试内容"}]';
const modelPreviewDocArea=node("details"),modelPreviewDocAdvanced=node("details");modelPreviewDocAdvanced.append(node("summary","高级：编辑候选文档 JSON"),modelPreviewDocuments);
modelPreviewDocArea.append(node("summary","提供候选资料（资料选择与追问审核）"),node("p","最多 20 篇，模拟检索结果，不抓取链接、不批准知识。请只提供允许发送给配置模型的资料。"));modelPreviewArea.append(modelPreviewKind,modelPreviewDocArea);
const previewDocTitle=node("input"),previewDocUrl=node("input"),previewDocContent=node("textarea"),previewDocAdd=node("button","添加候选文档"),previewDocList=node("div"),previewDocStatus=node("p");
for(const [label,field,limit] of [["候选文档标题",previewDocTitle,300],["候选文档 HTTPS 链接",previewDocUrl,2000],["候选文档摘要（可留空）",previewDocContent,12000]]){field.value="";field.maxLength=limit;field.setAttribute("aria-label",label);const wrapper=node("label",label);wrapper.append(field);modelPreviewDocArea.append(wrapper);}
modelPreviewDocArea.append(previewDocAdd,previewDocStatus,previewDocList,modelPreviewDocAdvanced);
function previewDocumentValues(){let value;try{value=JSON.parse(modelPreviewDocuments.value);}catch(error){throw new Error("请先修正高级 JSON：内容不是有效 JSON。");}if(!Array.isArray(value)||value.length>20||value.some(doc=>!doc||typeof doc!=="object"||Array.isArray(doc)||Object.keys(doc).sort().join(",")!=="content,title,url"||typeof doc.title!=="string"||typeof doc.url!=="string"||typeof doc.content!=="string"))throw new Error("请先修正高级 JSON：最多 20 条 title/url/content 文档。");return value;}
function renderPreviewDocuments(){
  previewDocList.replaceChildren();
  try{const docs=previewDocumentValues(),snapshot=modelPreviewDocuments.value;previewDocStatus.textContent=`已添加 ${docs.length}/20 篇候选文档，仅用于本次预演。`;
    docs.forEach((doc,index)=>{const row=node("div"),remove=node("button","移除");row.append(node("strong",doc.title||"未命名文档"),node("p",doc.url),remove);previewDocList.append(row);
      remove.addEventListener("click",()=>{if(snapshot!==modelPreviewDocuments.value){renderPreviewDocuments();return;}modelPreviewDocuments.value=JSON.stringify(docs.filter((_,i)=>i!==index),null,2);renderPreviewDocuments();});});
  }catch(error){previewDocStatus.textContent=error.message;}
}
previewDocAdd.addEventListener("click",()=>{
  try{const docs=previewDocumentValues(),title=previewDocTitle.value.trim(),url=previewDocUrl.value.trim(),content=previewDocContent.value;
    if(!title||title.length>300||!url.startsWith("https://")||url.length>2000||content.length>12000)throw new Error("请填写有效标题、HTTPS 链接，并遵守长度限制。");
    if(docs.length>=20)throw new Error("最多 20 篇，请先移除不需要的文档。");
    if(docs.some(doc=>doc.url===url))throw new Error("这个链接已添加，不重复加入。");
    modelPreviewDocuments.value=JSON.stringify([...docs,{title,url,content}],null,2);previewDocTitle.value="";previewDocUrl.value="";previewDocContent.value="";renderPreviewDocuments();
  }catch(error){previewDocStatus.textContent=error.message;}
});
modelPreviewDocuments.addEventListener("input",renderPreviewDocuments);renderPreviewDocuments();
for(const [key,label,options] of [
  ["relationship","预演关系",{supervisor:"直属上级",dotted_supervisor:"虚线上级",peer:"平级",direct_report:"下属",cross_function:"跨部门",external:"外部",unknown:"未知"}],
  ["function_role","预演职责",{engineering:"研发",project_manager:"项目经理",product_manager:"产品经理",qa:"测试",operations:"运维",management:"管理",other:"其他",unknown:"未知"}],
  ["mode","预演运行模式",{observe:"观察",collaborate:"协作",auto_60:"自动 60 分钟",auto:"自动",paused:"立即暂停",stopped:"完全停止"}]
]){const field=node("select"),wrapper=node("label",label);for(const [value,title] of Object.entries({"":"沿用当前快照",...options})){const option=node("option",title);option.value=value;field.append(option);}field.value="";field.setAttribute("aria-label",label);wrapper.append(field);modelPreviewArea.append(wrapper);modelPreviewAssumptions[key]=field;}
modelPreviewArea.append(node("p","关系与职责需同时选择，或均沿用快照；所选假设仅在隔离快照生效，不纠正真实联系人或切换实际模式。"));
const modelPreviewTime=node("input"),modelPreviewTimeLabel=node("label","预演时间（带时区，留空使用当前时间）");modelPreviewTime.value="";modelPreviewTime.placeholder="2026-09-09T22:00:00+08:00";modelPreviewTime.setAttribute("aria-label","预演时间（带时区，留空使用当前时间）");modelPreviewTimeLabel.append(modelPreviewTime);modelPreviewArea.append(modelPreviewTimeLabel,node("p","时间只影响使用统一业务时钟的逻辑；知识和联系人仍为当前快照，不还原历史资料，也不改变真实任务超时。"));
modelPreviewArea.append(modelPreviewStart,modelPreviewPoll,modelPreviewInfo);$("settings").append(modelPreviewArea);
modelPreviewInfo.setAttribute("role","status");modelPreviewInfo.setAttribute("aria-live","polite");
const modelPreviewDetails=node("details"),modelPreviewRaw=node("pre");modelPreviewDetails.append(node("summary","技术详情与证据边界"),modelPreviewRaw);modelPreviewArea.append(modelPreviewDetails);
const modelPreviewIncludeDrafts=node("input"),modelPreviewDrafts=node("div");modelPreviewIncludeDrafts.type="checkbox";modelPreviewIncludeDrafts.checked=false;
const modelPreviewDraftLabel=node("label","显示本次拟回复（包含私有内容，仅用于审核）");modelPreviewDraftLabel.prepend(modelPreviewIncludeDrafts);modelPreviewArea.append(modelPreviewDraftLabel,modelPreviewDrafts);
const modelPreviewLeave=node("button","结束查看（不取消模型调用）");modelPreviewArea.append(modelPreviewLeave);
modelPreviewLeave.addEventListener("click",()=>{
  if(!confirm("结束查看不会取消已经开始的模型请求，也不能证明没有产生费用。下次运行是新请求。继续吗？"))return;
  modelPreviewRequest=null;modelPreviewStart.disabled=false;modelPreviewPoll.disabled=true;modelPreviewRaw.textContent="";modelPreviewDetails.open=false;modelPreviewInfo.textContent="已结束查看，后台调用如已开始仍会完成；不会自动重试。";
  modelPreviewDrafts.replaceChildren();
});
let modelPreviewRequest=null;
modelPreviewStart.addEventListener("click",async()=>{
  if(modelPreviewStart.disabled)return;
  modelPreviewDrafts.replaceChildren();
  if(["debug","debug_execution"].includes(modelPreviewKind.value)){
    let transcript;try{transcript=JSON.parse(debugPreviewTranscript.value);if(!Array.isArray(transcript)||!debugPreviewJob.value?.trim())throw new Error();}catch(error){modelPreviewInfo.textContent="请填写已完成任务 ID 和验证记录 JSON 数组。";return;}
    const debug={job_id:debugPreviewJob.value.trim(),transcript};
    if(modelPreviewKind.value==="debug_execution"){
      if(!debugPreviewReport.value?.trim()||debugPreviewReport.value.length>50000){modelPreviewInfo.textContent="请提供完整的模拟 Debug 报告，最多 50000 字。";return;}
      debug.execution={report:debugPreviewReport.value,exit_status:debugPreviewExit.value==="unknown"?null:Number(debugPreviewExit.value)};
    }
    if(!confirm("确认将 Debug 报告及验证记录交给模型复核？模拟执行仅使用你提供的报告和退出状态。最多 1 次调用，可能产生费用；不执行命令、不上板、不发送消息。"))return;
    modelPreviewRequest=crypto.randomUUID();const identifier=modelPreviewRequest;modelPreviewStart.disabled=true;modelPreviewPoll.disabled=false;
    modelPreviewRaw.textContent="";modelPreviewInfo.textContent="正在提交 Debug 结果预演。";
    try{const result=await api("/api/model-preview-start",{request_id:identifier,event:{},debug,confirm_model_call:true});if(identifier===modelPreviewRequest)renderModelPreview(result);}
    catch(error){if(identifier===modelPreviewRequest)modelPreviewInfo.textContent="启动结果未确认，请刷新查询；不会自动重试。";}return;
  }
  if(!modelPreviewQuery.value?.trim()||!modelPreviewSender.value?.trim()||!modelPreviewChat.value?.trim()){modelPreviewInfo.textContent="请填写问题、提问者和会话 ID。";return;}
  const assumptions=Object.fromEntries(Object.entries(modelPreviewAssumptions).filter(([,field])=>field.value).map(([key,field])=>[key,field.value]));
  if(modelPreviewTime.value.trim())assumptions.observed_at=modelPreviewTime.value.trim();
  if(Boolean(assumptions.relationship)!==Boolean(assumptions.function_role)){modelPreviewInfo.textContent="关系与职责请同时选择，或均沿用当前快照。";return;}
  const extra={};if(["research","research_review"].includes(modelPreviewKind.value)){try{extra.documents=JSON.parse(modelPreviewDocuments.value);if(!Array.isArray(extra.documents))throw new Error();}catch(error){modelPreviewInfo.textContent="候选资料必须是 JSON 数组，请检查后再运行。";return;}}
  if(modelPreviewKind.value==="research_review")extra.review_clarification=true;
  if(modelPreviewIncludeDrafts.checked){if(!extra.documents){modelPreviewInfo.textContent="查看拟回复请先选择资料预演。";return;}extra.include_drafts=true;}
  const followups=(modelPreviewFollowups.value||"").split("\n").map(text=>text.trim()).filter(Boolean);
  if(followups.length){if(!extra.documents||followups.length>9||followups.some(text=>text.length>2000)){modelPreviewInfo.textContent="后续来信需选择资料预演，最多 9 条，每条最多 2000 字。";return;}extra.followups=followups;}
  const callLimit=(extra.review_clarification?3:extra.documents?2:1)*(followups.length+1);
  if(!confirm(`确认将该问题、匹配上下文及提供的候选资料发送给配置的 AI？最多 ${callLimit} 次模型调用，可能产生费用；仅预演，不对外回复或执行 Debug。`))return;
  modelPreviewRequest=crypto.randomUUID();modelPreviewStart.disabled=true;modelPreviewPoll.disabled=false;
  modelPreviewRaw.textContent="";modelPreviewDetails.open=false;modelPreviewInfo.textContent="正在提交预演。可继续使用其他控制功能；请刷新查看结果。";
  const event={source:"feishu_user_poll",identity:"user",external_id:"preview_"+modelPreviewRequest,payload:{content:modelPreviewQuery.value,chat_type:"p2p"},occurred_at:assumptions.observed_at||new Date().toISOString(),sender_id:modelPreviewSender.value.trim(),chat_id:modelPreviewChat.value.trim()};
  const identifier=modelPreviewRequest;
  try{const result=await api("/api/model-preview-start",{request_id:identifier,event,assumptions,...extra,confirm_model_call:true});if(identifier===modelPreviewRequest)renderModelPreview(result);}
  catch(error){if(identifier===modelPreviewRequest)modelPreviewInfo.textContent="启动结果未确认，请刷新查询；不会自动重试。";}
});
function renderModelPreview(result){
  modelPreviewRaw.textContent=JSON.stringify(result,null,2);
  modelPreviewDrafts.replaceChildren();
  if(result.state==="completed"&&result.result?.drafts){
    const drafts=result.result.drafts;modelPreviewDrafts.append(node("p","各轮当时的拟回复，均未发送；后续来信可能使旧内容失效，不是发送许可。","muted"));
    for(const draft of drafts.items||[]){const item=node("article");item.append(node("h3",`第 ${draft.turn} 轮 · ${draft.kind==="clarify"?"拟追问":"拟回复"}`),node("pre",draft.text));if(draft.truncated)item.append(node("p","正文超过显示上限，当前内容不完整。"));modelPreviewDrafts.append(item);}
    if(!drafts.items?.length)modelPreviewDrafts.append(node("p","本次未生成拟回复或拟追问。"));
    if(drafts.truncated)modelPreviewDrafts.append(node("p","仅显示前 20 条拟回复。"));
  }
  if(result.state==="rejected"&&result.execution_state==="not_started"){
    modelPreviewInfo.textContent="本次预演未启动："+(result.error||"请检查输入后重新提交。");
    modelPreviewStart.disabled=false;modelPreviewPoll.disabled=true;return;
  }
  if(result.state==="running"){modelPreviewInfo.textContent="预演进行中。不会发送消息或启动 Debug；请稍后刷新结果。";return;}
  if(result.result_evicted){modelPreviewInfo.textContent=result.execution_state==="unknown"?"历史预演结果未知，正文已清理；去重收据仍保留，不会再次调用模型。":"历史预演已结束，完整结果已清理；去重收据仍保留，不会再次调用模型。";modelPreviewStart.disabled=false;modelPreviewPoll.disabled=true;return;}
  if(result.state==="failed"&&result.execution_state==="not_started"){modelPreviewInfo.textContent="预演未启动，请核对原因后另行操作。";modelPreviewStart.disabled=false;modelPreviewPoll.disabled=true;return;}
  if(result.state==="failed"){modelPreviewInfo.textContent="预演未完成。模型调用结果或费用可能未知，不会自动重试。请查看技术详情或结束查看。";}
  else if(result.state==="completed"){
    const value=result.result||{},steps=value.steps||[];
    if(["captured_debug_review","simulated_debug_execution"].includes(value.preview_kind)){
      const completionLabels={unverified:"缺少退出证据",execution_failed:"模拟执行失败",board_cleanup_required:"板卡清理尚未确认",remote_cleanup_required:"远端执行清理尚未确认",review_pending:"模拟执行结束，已进入复核",stale:"任务权限或轮次已变化"};
      modelPreviewInfo.textContent=["Debug 结果预演完成（未执行命令、上板或发送消息）",...(value.preview_kind==="simulated_debug_execution"?[`模拟状态：${completionLabels[value.completion_state]||"未进入完成复核"}。`]:[]),value.review_ok?"模拟复核通过；不代表真实验证或现场问题已解决。":"复核未通过或被当前状态阻止，请查看技术详情。",`隔离库中的 Case 状态：${value.case_state||"未进入复核"}；通知意图：${value.outbox_intentions}。`,`验证记录：已匹配 ${value.transcript?.consumed} 条，剩余 ${value.transcript?.remaining} 条。`,value.model_callback_invoked?"模型回调已执行一次；提供方与费用另行核验。":"未调用模型复核。"].join("\n");
      modelPreviewStart.disabled=false;return;
    }
    const routes={ignore:"忽略",direct_answer:"直接回答",clarify:"追问",research:"查资料",codex_debug:"交给 Codex 排查",owner_decision:"找你决策",urgent_notify:"紧急通知"};
    if(value.preview_kind==="conversation"){
      const lines=["连续来信预演完成（同一快照，未发送或执行任务）"];
      for(const turn of value.turns||[]){
        const inbound=(turn.steps||[]).find(step=>step.kind==="inbound");
        lines.push(`第 ${turn.turn} 轮：${inbound?.ignored?"忽略":routes[inbound?.route]||"未确定或受当前模式限制"}`);
        if(inbound?.reason_labels?.length)lines.push("原因："+inbound.reason_labels.join("；"));
      }
      lines.push(`已调用 ${value.calls?.length||0} 次模型回调；提供方与费用仍需核验。`,"不代表故障已解决；资料选择和追问细节见技术详情。");
      modelPreviewInfo.textContent=lines.join("\n");modelPreviewStart.disabled=false;return;
    }
    const modes={observe:"观察",collaborate:"协作",auto_60:"自动 60 分钟",auto:"自动",paused:"立即暂停",stopped:"完全停止"};
    const reviewed=value.preview_kind==="routing_research_clarification";
    const lines=[reviewed?"路由、资料选择与必要时追问审核预演完成（未实际发送或执行）":value.preview_kind==="routing_and_document_selection"?"路由与资料选择预演完成（使用提供的候选资料，未实际发送或执行）":"首轮路由预演完成（未实际发送或执行）"];
    if(typeof value.business_window?.active==="boolean")lines.push(value.business_window.active?"所选时间：非工作时间（按当前工作时段配置）。":"所选时间：工作时间（按当前工作时段配置）。");
    if(value.assumptions&&Object.keys(value.assumptions).length)lines.push("本次使用了显式角色、模式或时间假设，真实资料与全局设置未修改；具体假设见技术详情。");
    for(const step of steps){
      if(step.kind==="research"){
        const states={needs_owner_review:"等待你审核",clarification_queued:"追问审核通过，仅生成待发送意图，未发送",disabled:"当前配置未启用此阶段",stale:"上下文已失效，停止处理",awaiting_info:"等待补充信息",investigating:"排查中"};
        lines.push(`资料处理：${states[step.state]||"请查看技术详情"}。${step.codex_queued?"仅生成调试任务意图，未启动 Codex。":""}`);continue;
      }
      if(step.kind!=="inbound")continue;
      if(step.blocked_by_mode)lines.push(`当前「${modes[step.blocked_by_mode]||"未知"}」模式阻止处理，没有生成发送许可。`);
      else if(step.ignored)lines.push("本条消息被忽略，没有继续处理。");
      else lines.push(`处理路径：${routes[step.route]||"未确定，请查看详情"}`);
      if(step.reason_labels?.length)lines.push("原因："+step.reason_labels.join("；"));
      const count=value=>Number.isSafeInteger(value)&&value>=0?String(value):"未知";
      lines.push(`隔离快照内的待发送意图：${count(step.outbox_intentions)}；待执行任务：${count(step.job_intentions)}。均未实际消费。`);
    }
    if(!steps.length)lines.push("没有可展示的流程步骤，请查看技术详情。");
    if(value.intention_counts)lines.push(`完整预演的意图总数：通知 ${value.intention_counts.outbox}，任务 ${value.intention_counts.jobs}；均未发送或执行。`);
    lines.push(value.model_callback_invoked===true?"路由模型回调已执行；提供方身份与费用不能仅由回调成功证明。":"此结果未确认调用了路由模型，不能据此评价 AI 回答质量。");
    lines.push(reviewed?"仅基于快照及候选资料审核；不含真实发送、实时资料抓取或 Debug 执行。":value.preview_kind==="routing_and_document_selection"?"不含实时资料抓取、完整追问审核或 Debug 执行，不代表现场问题已解决。":"仅首轮路由：不代表资料已查完、故障已修复或现场已解决。");
    modelPreviewInfo.textContent=lines.join("\n");
  }else modelPreviewInfo.textContent="预演状态未确认，请刷新查询；不会自动重试。";
  if(result.state==="completed"||result.state==="failed")modelPreviewStart.disabled=false;
}
modelPreviewPoll.addEventListener("click",async()=>{
  if(!modelPreviewRequest||modelPreviewPoll.disabled)return;
  const identifier=modelPreviewRequest;
  try{const result=await api("/api/model-preview-status",{request_id:identifier});if(identifier===modelPreviewRequest)renderModelPreview(result);}
  catch(error){if(identifier===modelPreviewRequest)modelPreviewInfo.textContent="结果暂不可确认。控制台重启会丢失预演记录；不要把记录缺失当作未调用模型。";}
});

const simulationArea=node("section",undefined,"note"),simulationInput=node("textarea"),simulationRun=node("button","离线路由规则试算"),simulationInfo=node("pre");
simulationInput.rows=12;simulationInput.setAttribute("aria-label","路由假设 JSON");simulationInput.value=JSON.stringify({query:"启动失败，需要排查",relationship:"peer",function_role:"engineering",severity:"P2",proposal:{route:"codex_debug",confidence:0.96,issue_type:"bug",severity:"P2",domain:"bootloader",repository_hints:[],reason_codes:["technical_investigation"],clarification_question:null,fallback_route:null,requires_owner_judgment:false,conversation_relation:"standalone"}},null,2);
simulationArea.append(node("h2","路由规则试算（高级）"),node("p","编辑假设角色和 AI 建议 JSON。复用当前路由规则；不调用 AI、不检索知识、不获取联系人、不发送消息或启动代理。结果不是完整工作流模拟或发送许可。"),simulationInput,simulationRun,simulationInfo);$("settings").append(simulationArea);
let simulationEpoch=0;
const scenarioForm=node("section",undefined,"note"),scenarioFields={},scenarioBuild=node("button","将表单生成到下方试算 JSON");
function scenarioField(key,label,options,initial){
  const input=node(options?"select":"input"),wrapper=node("label",label+" ");
  if(options)for(const [value,title] of Object.entries(options)){const option=node("option",title);option.value=value;input.append(option);}
  input.value=initial;input.setAttribute("aria-label",label);wrapper.append(input);scenarioForm.append(wrapper);scenarioFields[key]=input;
  input.addEventListener("input",()=>{++simulationEpoch;simulationInfo.textContent="表单已修改，请先生成 JSON；试算使用 JSON，不自动覆盖高级编辑。";});
}
scenarioField("query","问题",null,"启动失败，需要排查");
scenarioField("relationship","假设关系",{unknown:"未知",supervisor:"直属上级",dotted_supervisor:"虚线上级",peer:"平级",direct_report:"下属",cross_function:"跨部门",external:"外部"},"unknown");
scenarioField("function_role","假设职责",{unknown:"未知",engineering:"研发",project_manager:"项目经理",product_manager:"产品经理",qa:"测试",operations:"运维",management:"管理",other:"其他"},"unknown");
scenarioField("severity","假设严重程度",{P0:"P0",P1:"P1",P2:"P2",P3:"P3"},"P2");
scenarioField("route","假设 AI 建议",{ignore:"忽略",direct_answer:"直接答",clarify:"追问",research:"查资料",codex_debug:"Codex Debug",owner_decision:"找我决策",urgent_notify:"紧急通知"},"research");
scenarioField("confidence","假设建议置信度",null,"0.96");scenarioFields.confidence.type="number";scenarioFields.confidence.min="0";scenarioFields.confidence.max="1";scenarioFields.confidence.step="0.01";
scenarioField("question","追问内容（仅追问时必填）",null,"");
scenarioForm.append(node("p","以下理由是所选路线的测试假设，不是 AI 的判断或已核实证据。追问回退默认为查资料；直接答不假设已找到知识。生成后可在 JSON 中修改。"),scenarioBuild);
simulationArea.prepend(scenarioForm);
scenarioBuild.addEventListener("click",()=>{
  const f=scenarioFields,route=f.route.value,confidence=Number(f.confidence.value);
  if(!f.query.value.trim()||!f.confidence.value.trim()||!Number.isFinite(confidence)||confidence<0||confidence>1||(route==="clarify"&&!f.question.value.trim())){simulationInfo.textContent="请填写问题、有效置信度和必要的追问内容。";return;}
  const reason={ignore:"non_work_noise",direct_answer:"approved_knowledge_match",clarify:"missing_logs",research:"source_lookup_needed",codex_debug:"technical_investigation",owner_decision:"requires_policy_decision",urgent_notify:"severe_outage"}[route];
  simulationInput.value=JSON.stringify({query:f.query.value,relationship:f.relationship.value,function_role:f.function_role.value,severity:f.severity.value,proposal:{route,confidence,issue_type:route==="urgent_notify"?"incident":"investigation",severity:f.severity.value,domain:"bootloader",repository_hints:[],reason_codes:[reason],clarification_question:route==="clarify"?f.question.value:null,fallback_route:route==="clarify"?"research":null,requires_owner_judgment:route==="owner_decision",conversation_relation:"standalone"}},null,2);
  ++simulationEpoch;simulationInfo.textContent="已生成假设 JSON，尚未试算或更改配置。";
});
const simulationThreshold=node("input"),simulationCompare=node("button","比较当前与拟调整阈值");
simulationThreshold.type="number";simulationThreshold.min="0";simulationThreshold.max="1";simulationThreshold.step="0.01";simulationThreshold.setAttribute("aria-label","拟调整的路由置信度阈值");
simulationArea.append(node("p","可选：输入拟调整阈值，比较同一场景。只比较，不保存配置。"),simulationThreshold,simulationCompare);
simulationThreshold.addEventListener("input",()=>{++simulationEpoch;simulationInfo.textContent="阈值已变化，请重新比较。";});
simulationCompare.addEventListener("click",async()=>{const epoch=++simulationEpoch;try{if(!simulationThreshold.value.trim())throw new Error("请填写拟调整阈值");const result=await api("/api/policy-comparison",{scenario:JSON.parse(simulationInput.value),proposed_minimum_confidence:Number(simulationThreshold.value)});if(epoch===simulationEpoch)simulationInfo.textContent=JSON.stringify(result,null,2);}catch(error){if(epoch===simulationEpoch)simulationInfo.textContent="比较失败："+error.message;}});
simulationInput.addEventListener("input",()=>{++simulationEpoch;simulationInfo.textContent="假设已变化，请重新试算。";});
simulationRun.addEventListener("click",async()=>{const epoch=++simulationEpoch;try{const result=await api("/api/policy-simulation",JSON.parse(simulationInput.value));if(epoch===simulationEpoch)simulationInfo.textContent=JSON.stringify(result,null,2);}catch(error){if(epoch===simulationEpoch)simulationInfo.textContent="试算失败："+error.message;}});
const profileArea=node("section",undefined,"note"),profileQuery=node("input"),profileRead=node("button","查询缓存角色"),profileMore=node("button","下一页"),profileInfo=node("p"),profileList=node("div");
profileQuery.setAttribute("aria-label","同事姓名、部门或 ID");profileQuery.value="";profileMore.disabled=true;
profileArea.append(node("h2","同事角色依据与人工修正"),node("p","查询只读取已有缓存，不访问通讯录。来源、核实时间及过期状态可见；缓存可能过时，不保证对方当前职位。人工修正需填写依据、预览并确认；试算表单不会修改这些记录。"),profileQuery,profileRead,profileMore,profileInfo,profileList);$("settings").append(profileArea);
let profileEpoch=0,profileAfter=null;
const attentionArea=node("section",undefined,"note"),attentionRead=node("button","读取关注订阅"),attentionCollect=node("button","从已有邮件目录收集关注事项"),attentionNext=node("button","下一页关注事项"),attentionInfo=node("p"),attentionSettings=node("div"),attentionList=node("div");
let attentionEpoch=0,attentionAfter=null;
attentionNext.disabled=true;
attentionArea.append(node("h2","个人关注"),node("p","订阅本地邮件分类，仅关注订阅后进入目录的邮件。不会读取正文、标记已读或发送提醒。退订不删除历史；处理事项请使用邮件待办。"),attentionRead,attentionCollect,attentionNext,attentionInfo,attentionSettings,attentionList);$("mail").prepend(attentionArea);
async function loadAttention(after=""){
  const epoch=++attentionEpoch;attentionNext.disabled=true;
  try{const result=await api("/api/attention-list",{after_id:after});if(epoch!==attentionEpoch)return;attentionAfter=result.next_cursor;attentionNext.disabled=!attentionAfter;attentionList.replaceChildren(...result.items.map(item=>{const card=node("section",undefined,"note"),open=node("button","打开邮件待办");card.append(node("p",`${mailCategories[item.category]||item.category} · ${item.subject||"无主题"} · ${item.message_id}`),open);open.addEventListener("click",()=>openAttention(item.action_id));return card;}));if(!result.items.length)attentionList.append(node("p","暂无匹配的关注事项"));}
  catch(error){if(epoch===attentionEpoch)attentionInfo.textContent="读取失败："+error.message;}
}
async function openAttention(actionId){
  if(mailBusy)return;
  const epoch=++mailEpoch;mailNext=null;$("mail-next").disabled=true;list("mail-list",[],"正在读取关注邮件");
  try{const value=await api("/api/attention-detail",{action_id:actionId});if(epoch!==mailEpoch)return;list("mail-list",[mailItem(value,()=>loadAttention())],"暂无邮件");$("mail-status").textContent="已打开精确关注邮件；下方操作沿用邮件待办规则。";}
  catch(error){if(epoch===mailEpoch)$("mail-status").textContent="读取失败："+error.message;}
}
attentionNext.addEventListener("click",()=>{if(attentionAfter)loadAttention(attentionAfter);});
attentionRead.addEventListener("click",async()=>{
  attentionRead.disabled=true;attentionSettings.replaceChildren();
  try{const result=await api("/api/attention-subscriptions",{});for(const category of result.categories){const current=result.items.find(item=>item.category===category)||{revision:0,enabled:0},card=node("section",undefined,"note"),toggle=node("button",current.enabled?"预览退订":"预览订阅"),snooze=node("button","预览稍后 1 小时"),apply=node("button","确认变更"),info=node("p");let pending=null;
    apply.disabled=true;snooze.disabled=!current.enabled;card.append(node("h3",mailCategories[category]||category),node("p",`${current.enabled?"已订阅":"未订阅"} · 稍后至 ${displayInstant(current.snooze_until)}`),toggle,snooze,apply,info);
    function preview(enabled,minutes){pending={category,enabled,expected_revision:current.revision,request_id:crypto.randomUUID(),snooze_minutes:minutes};info.textContent=minutes?"将暂停显示此分类 1 小时，不停止 AI。":enabled?"将关注此分类的新入库邮件，不发送额外通知。":"将隐藏此分类关注事项，不删除邮件或历史。";apply.disabled=false;}
    toggle.addEventListener("click",()=>preview(!current.enabled,null));snooze.addEventListener("click",()=>preview(true,60));
    const resume=node("button","预览立即恢复关注");resume.disabled=!current.enabled||!current.snooze_until;card.append(resume);
    resume.addEventListener("click",()=>{preview(true,null);info.textContent="将清除本分类的稍后时间，恢复显示未完成关注事项；不改变邮件自己的稍后状态，不发送提醒。";});
    apply.addEventListener("click",async()=>{if(!pending||apply.disabled)return;const payload=pending;pending=null;apply.disabled=true;toggle.disabled=true;snooze.disabled=true;resume.disabled=true;try{await api("/api/attention-subscription-save",payload);info.textContent="已保存，请重新读取订阅核对。";loadAttention();}catch(error){info.textContent="结果待核对，不自动重试："+error.message;}});
    attentionSettings.append(card);}
    await loadAttention();
  }catch(error){attentionInfo.textContent="读取失败："+error.message;}finally{attentionRead.disabled=false;}
});
attentionCollect.addEventListener("click",async()=>{attentionCollect.disabled=true;try{const result=await api("/api/attention-collect",{});attentionInfo.textContent=`新增 ${result.created} 项；单次最多 100 项，可再次收集，已有事项不会重复建立。`;await loadAttention();}catch(error){attentionInfo.textContent="收集结果待核对："+error.message;}finally{attentionCollect.disabled=false;}});

function profileCorrection(item){
  const box=node("section",undefined,"note"),relation=node("select"),role=node("select"),reason=node("input"),preview=node("button","预览角色修正"),apply=node("button","确认人工修正"),info=node("pre");
  const relationships={unknown:"未知",supervisor:"直属上级",dotted_supervisor:"虚线上级",peer:"平级",direct_report:"下属",cross_function:"跨部门",external:"外部"},roles={unknown:"未知",engineering:"研发",project_manager:"项目经理",product_manager:"产品经理",qa:"测试",operations:"运维",management:"管理",other:"其他"};
  for(const [select,options] of [[relation,relationships],[role,roles]])for(const [value,label] of Object.entries(options)){const option=node("option",label);option.value=value;select.append(option);}
  relation.value=item.relationship;role.value=item.function_role;reason.value="";reason.maxLength=1000;
  relation.setAttribute("aria-label","修正后的关系");role.setAttribute("aria-label","修正后的职责");reason.setAttribute("aria-label","人工核实依据");
  apply.disabled=true;let pending=null,epoch=0;
  function invalidate(){++epoch;pending=null;apply.disabled=true;info.textContent="修改未提交，请重新预览。";}
  for(const field of [relation,role,reason])field.addEventListener("input",invalidate);
  preview.addEventListener("click",()=>{invalidate();if(!reason.value.trim()){info.textContent="请填写人工核实依据。";return;}pending={requester_id:item.requester_id,content_digest:item.content_digest,relationship:relation.value,function_role:role.value,reason:reason.value.trim(),request_id:crypto.randomUUID()};info.textContent=`${item.display_name||item.requester_id}\n关系：${relationships[item.relationship]} → ${relationships[relation.value]}\n职责：${roles[item.function_role]} → ${roles[role.value]}\n依据：${pending.reason}\n将记为人工确认，优先于自动通讯录刷新；影响后续路由，不撤回已发送消息。`;apply.disabled=false;});
  apply.addEventListener("click",async()=>{if(!pending||apply.disabled||!confirm(pending.reset_auto?"确认撤销人工覆盖？关系、职责及职务信息将清空，等待正常资料刷新；不会立即查询通讯录。":"确认已人工核实上述角色？此修改会影响回复与追问策略，并优先于自动资料刷新。"))return;const payload=pending,current=++epoch;pending=null;apply.disabled=true;try{await api("/api/requester-profile-correct",payload);if(current===epoch)info.textContent="修正已提交，请重新查询核对。";}catch(error){if(current===epoch)info.textContent="结果待核对，请重新查询，不自动重试："+error.message;}});
  const reset=node("button","预览撤销人工覆盖");reset.disabled=item.source!=="operator";
  reset.addEventListener("click",()=>{invalidate();if(!reason.value.trim()){info.textContent="请填写撤销依据。";return;}pending={requester_id:item.requester_id,content_digest:item.content_digest,relationship:"unknown",function_role:"unknown",reason:reason.value.trim(),reset_auto:true,request_id:crypto.randomUUID()};info.textContent="将撤销人工覆盖，清空关系、职责、部门及职务，标为未知。后续正常流程按配置刷新资料，不会立即访问通讯录。";apply.disabled=false;});
  box.append(node("p","人工修正（不修改飞书通讯录）"),relation,role,reason,preview,apply,info,reset);return box;
}
async function loadProfiles(after=""){
  const epoch=++profileEpoch;profileMore.disabled=true;
  try{const result=await api("/api/requester-profiles",{query:profileQuery.value,after_id:after});if(epoch!==profileEpoch)return;profileAfter=result.next_after_id;profileMore.disabled=!profileAfter;profileInfo.textContent=result.scope;profileList.replaceChildren(...result.items.map(item=>{const card=node("section",undefined,"note"),status={operator_override:"人工覆盖生效：不受缓存期限影响，直到你撤销。",untrusted:"自动资料不可信：当前按未知角色处理，不使用缓存身份自动追问。",valid_cache:"自动资料在有效期内：仍需结合问题与权限决定处理方式。"};card.append(node("h3",item.display_name||item.requester_id),node("p",status[item.authority_status]||"角色有效性未确认，请重新查询。"),node("pre",JSON.stringify(item,null,2)),profileCorrection(item));return card;}));if(!result.items.length)profileList.append(node("p","没有匹配的缓存资料"));}
  catch(error){if(epoch===profileEpoch)profileInfo.textContent="查询失败："+error.message;}
}
profileQuery.addEventListener("input",()=>{++profileEpoch;profileAfter=null;profileMore.disabled=true;profileList.replaceChildren();profileInfo.textContent="筛选已修改，请重新查询。";});profileRead.addEventListener("click",()=>loadProfiles());profileMore.addEventListener("click",()=>{if(profileAfter)loadProfiles(profileAfter);});
enter().catch(()=>{$("login").hidden=false;$("console").hidden=true;});
setInterval(()=>{if(!$("console").hidden&&!busy)refresh().catch(()=>{$("connection").textContent="连接异常 · 显示可能已过期";});},15000);

// Persisted assessments are operator-only read views, never publication actions.
let impactEpoch = 0, impactNext = null;
async function loadImpacts(afterId = "") {
  const epoch = ++impactEpoch;
  const more = $("release-impact-more");
  more.disabled = true;
  $("release-impact-status").textContent = "正在读取影响评估…";
  $("release-impact-list").replaceChildren();
  try {
    const result = await api("/api/release-impact-list", {after_id: afterId});
    if (epoch !== impactEpoch) return;
    impactNext = result.next_cursor;
    more.disabled = !impactNext;
    $("release-impact-status").textContent = result.items.length ? `本页 ${result.items.length} 条评估` : "尚无影响评估";
    $("release-impact-list").replaceChildren(...result.items.map(item => {
      const card = node("details", undefined, "item");
      card.append(node("summary", item.subject));
      card.append(node("p", `${item.repository} · ${item.change_id} · ${new Date(item.created_at).toLocaleString("zh-CN")}`, "muted"));
      const content = node("div"), retry = node("button", "读取详情");
      retry.type = "button";
      card.append(retry, content);
      let loaded = false, loading = false;
      async function read() {
        if (loaded || loading) return;
        loading = true; retry.disabled = true;
        content.textContent = "正在读取…";
        try {
          const value = await api("/api/release-impact-detail", {impact_id: item.impact_id});
          if (epoch !== impactEpoch || !card.isConnected) return;
          const assessment = value.assessment;
          const levels = {low: "低", medium: "中", high: "高"};
          content.replaceChildren(node("p", `影响程度：${levels[assessment.impact_level] || "未标注"}`),
            node("p", assessment.summary), node("p", `版本：${value.revision}`));
          for (const [label, values] of [["风险", assessment.risks], ["建议验证", assessment.recommended_validation],
            ["受影响知识", assessment.affected_knowledge_ids], ["变更文件", value.changed_paths]]) {
            content.append(node("h4", label));
            const entries = node("ul");
            for (const text of values || []) entries.append(node("li", text));
            content.append(entries.childNodes.length ? entries : node("p", "无记录", "muted"));
          }
          loaded = true; retry.hidden = true;
        } catch (error) {
          if (epoch === impactEpoch && card.isConnected) content.textContent = error.message;
        } finally { loading = false; retry.disabled = false; }
      }
      retry.addEventListener("click", read);
      card.addEventListener("toggle", () => { if (card.open) read(); });
      return card;
    }));
  } catch (error) {
    if (epoch === impactEpoch) { impactNext = null; $("release-impact-status").textContent = error.message; }
  }
}
$("release-impact-refresh").addEventListener("click", () => loadImpacts());
$("release-impact-more").addEventListener("click", () => { if (impactNext) loadImpacts(impactNext); });
$("release-impact-panel").addEventListener("toggle", () => {
  if ($("release-impact-panel").open && impactEpoch === 0) loadImpacts();
});

let mailDigestEpoch = 0, mailDigestNext = null;
async function loadMailDigests(afterId = "") {
  const epoch = ++mailDigestEpoch;
  $("mail-digest-more").disabled = true;
  $("mail-digest-status").textContent = "正在读取摘要…";
  $("mail-digest-list").replaceChildren();
  try {
    const result = await api("/api/mail-digest-list", {after_id: afterId});
    if (epoch !== mailDigestEpoch) return;
    mailDigestNext = result.next_cursor;
    $("mail-digest-more").disabled = !mailDigestNext;
    $("mail-digest-status").textContent = result.items.length ? `本页 ${result.items.length} 份摘要` : "尚无摘要";
    $("mail-digest-list").replaceChildren(...result.items.map(item => {
      const card = node("details", undefined, "item");
      const states = {linking: "准备中", prepared: "已生成", delivered: "通知已送达", failed: "处理失败"};
      card.append(node("summary", `${new Date(item.range_end).toLocaleString("zh-CN")} · ${item.item_count} 封 · ${states[item.state] || item.state}`));
      const content = node("div"), controls = node("div", undefined, "detail-controls");
      const retry = node("button", "读取摘要"), previous = node("button", "上一页邮件"), next = node("button", "下一页邮件");
      for (const button of [retry, previous, next]) button.type = "button";
      previous.disabled = next.disabled = true;
      controls.append(retry, previous, next);card.append(controls, content);
      let loaded = false, loading = false, current = 1, snapshot = null;
      async function read(page = current) {
        if (loading) return;
        loading = true; retry.disabled = previous.disabled = next.disabled = true;
        content.textContent = "正在读取…";
        try {
          const value = await api("/api/mail-digest-detail", {digest_id: item.digest_id, page, expected_digest: snapshot});
          if (epoch !== mailDigestEpoch || !card.isConnected) return;
          snapshot = value.snapshot_digest; current = value.page || 1; loaded = true;
          content.replaceChildren(node("p", value.assessment.overview));
          for (const important of value.assessment.important || []) {
            const entry = node("div", undefined, "item");
            entry.append(node("strong", important.summary), node("p", important.why_important));
            if (important.action) entry.append(node("p", `建议行动：${important.action}`));
            if (important.deadline) entry.append(node("p", `截止：${important.deadline}`));
            content.append(entry);
          }
          content.append(node("h4", "生成摘要时的邮件成员"));
          if (value.reason) content.append(node("p", value.reason));
          for (const mail of value.items) {
            content.append(row(mail.subject || "无主题", mail.sender || "未知发件人",
              ({information: "知会", action_required: "需行动", blocked: "阻塞 / 失败", waiting: "等待跟进"})[mail.attention] || "未标注"));
          }
          content.append(node("p", `第 ${current} / ${value.page_count || 1} 页`, "muted"));
          previous.disabled = current <= 1;
          next.disabled = current >= (value.page_count || 1);
          retry.textContent = "重新读取本页";
        } catch (error) {
          if (epoch === mailDigestEpoch && card.isConnected) content.textContent = error.message;
        } finally { loading = false; retry.disabled = false; }
      }
      retry.addEventListener("click", () => read());
      previous.addEventListener("click", () => read(current - 1));
      next.addEventListener("click", () => read(current + 1));
      card.addEventListener("toggle", () => { if (card.open && !loaded) read(); });
      return card;
    }));
  } catch (error) {
    if (epoch === mailDigestEpoch) { mailDigestNext = null; $("mail-digest-status").textContent = error.message; }
  }
}
$("mail-digest-refresh").addEventListener("click", () => loadMailDigests());
$("mail-digest-more").addEventListener("click", () => { if (mailDigestNext) loadMailDigests(mailDigestNext); });
$("mail-digest-panel").addEventListener("toggle", () => {
  if ($("mail-digest-panel").open && mailDigestEpoch === 0) loadMailDigests();
});

$("bugs-refresh").addEventListener("click",()=>loadBugs());
$("bugs-intake-open").addEventListener("click",()=>loadBugIntakes());
$("bugs-search-open").addEventListener("click",()=>loadBugSearch());
$("bugs-next").addEventListener("click",()=>{if(bugsNext)loadBugs(bugsNext);});

$("bugs-create-open").addEventListener("click",()=>{++bugIntakeEpoch;bugIntakeWatch=null;const area=$("bugs-intake");area.replaceChildren();renderCreateDraftConsole(area);});
