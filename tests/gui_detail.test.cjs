// Unit-test the shipped controller with a small DOM adapter, not browser QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function harness({navigation=false}={}) {
  const elements = new Map(), calls = [];
  function element() {
    return {
      textContent:'', open:false, children:[], handlers:{},
      classList:{add(){},remove(){}},
      append(...children){this.children.push(...children);},
      prepend(...children){this.children.unshift(...children);},
      replaceChildren(...children){this.children=children;},
      attributes:{}, setAttribute(key,value){this.attributes[key]=value;}, querySelectorAll(){return [];},
      addEventListener(name, callback){this.handlers[name]=callback;},
      dispatchEvent(event){this.handlers[event.type]?.(event);return true;},
      showModal(){this.open=true;},
      focus(){this.focused=true;},
      close(){this.open=false;this.handlers.close?.();},
    };
  }
  const get = id => {if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const views=['home','mail','executions','knowledge','settings','system'];
  const nav=navigation ? views.map(view=>{const button=get('nav-'+view);button.dataset={view};button.textContent=view;return button;}) : [];
  const context = vm.createContext({
    document:{getElementById:get,createElement:element,querySelectorAll:selector=>
      selector==='nav [data-view]' || selector==='nav button' ? nav :
      navigation && selector==='.page' ? views.map(view=>{const page=get(view);page.id=view;return page;}) : []},
    setInterval(){}, location:{reload(){}},
    crypto:require('node:crypto').webcrypto, confirm:()=>true,
    Event:class Event {constructor(type){this.type=type;}},
    fetch(url, options){
      if(url === '/api/session')return Promise.reject(new Error('logged out'));
      return new Promise(resolve=>calls.push({url,body:options.body ? JSON.parse(options.body) : null,resolve}));
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../src/k3_support/gui_assets/app.js'),'utf8'),context);
  const respond = (call, value, ok=true) => call.resolve({ok,json:async()=>value});
  return {get,calls,context,respond};
}
const snapshot = (text, page=1) => ({text,page,page_count:2,content_digest:'1234567890abcdef',origin_cursor:'bound-origin'});
const settle = () => new Promise(resolve=>setImmediate(resolve));

test('closing a detail restores keyboard focus after polling replaced its opener',()=>{
  const h=harness();
  const opener={isConnected:true,getAttribute:()=>"查看任务详情 case-1",focus(){this.focused=true;}};
  const replacement={getAttribute:()=>"查看任务详情 case-1",focus(){this.focused=true;}};
  h.context.document.activeElement=opener;
  h.context.openDetailDialog();
  opener.isConnected=false;
  h.context.document.querySelectorAll=selector=>selector==='button[aria-label]'?[replacement]:[];
  h.get('case-dialog').close();
  assert.equal(replacement.focused,true);
  assert.equal(opener.focused,undefined);
});

test('closing a removed detail returns keyboard focus to the workspace',()=>{
  const h=harness();
  h.context.document.activeElement={isConnected:false,getAttribute:()=>"查看任务详情 removed"};
  h.context.openDetailDialog();
  h.get('case-dialog').close();
  assert.equal(h.get('workspace-content').focused,true);
});

test('navigation exposes exactly one current page without performing actions',()=>{
  const h=harness({navigation:true});
  h.get('nav-settings').handlers.click();
  assert.equal(h.get('nav-settings').attributes['aria-current'],'page');
  assert.equal(h.get('settings').hidden,false);
  assert.equal(h.get('home').hidden,true);
  assert.equal(h.calls.length,0);
  h.get('nav-home').handlers.click();
  assert.equal(h.get('nav-settings').attributes['aria-current'],'false');
  assert.equal(h.get('nav-home').attributes['aria-current'],'page');
  assert.equal(h.get('home').hidden,false);
  assert.equal(h.get('settings').hidden,true);
  assert.equal(h.calls.length,0);
});

test('opt-in draft display is literal and cleared on a new result',()=>{
  const h=harness();
  assert.equal(vm.runInContext('modelPreviewIncludeDrafts.checked',h.context),false);
  h.context.renderModelPreview({state:'completed',result:{drafts:{items:[
    {turn:1,kind:'reply',text:'<script>literal</script>',truncated:true}]}}});
  const drafts=vm.runInContext('modelPreviewDrafts',h.context);
  assert.match(drafts.children[0].textContent,/均未发送.*不是发送许可/);
  assert.equal(drafts.children[1].children[1].textContent,'<script>literal</script>');
  assert.match(drafts.children[1].children[2].textContent,/不完整/);
  h.context.renderModelPreview({state:'running'});
  assert.equal(drafts.children.length,0);
});

test('conversation preview renders per-turn paths without claiming delivery',()=>{
  const h=harness();
  h.context.renderModelPreview({state:'completed',result:{preview_kind:'conversation',
    turns:[{turn:1,steps:[{kind:'inbound',route:'research',reason_labels:['需要进一步查阅资料']}]},
           {turn:2,steps:[{kind:'inbound',route:'owner_decision'}]}],calls:[{},{}]}});
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/第 1 轮：查资料/);assert.match(text,/第 2 轮：找你决策/);
  assert.match(text,/原因：需要进一步查阅资料/);
  assert.match(text,/未发送或执行任务/);assert.match(text,/不代表故障已解决/);
});

test('body retention policy needs read preview and explicit apply with stale drafts invalidated',async()=>{
  const h=harness();
  const read=vm.runInContext('retentionPolicyRead.handlers.click()',h.context);
  h.respond(h.calls[0],{revision:0,days:null,needs_migration:false});await read;
  vm.runInContext('retentionPolicyEnabled.checked=true;retentionPolicyDays.value="30"',h.context);
  const preview=vm.runInContext('retentionPolicyPreview.handlers.click()',h.context);
  assert.deepEqual(h.calls[1].body,{days:30,expected_revision:0});
  h.respond(h.calls[1],{draft_id:'d1',previous_days:null,days:30,warning:'cannot undo'});await preview;
  vm.runInContext('retentionPolicyDays.handlers.input()',h.context);
  assert.equal(vm.runInContext('retentionPolicyApply.disabled',h.context),true);
  const fresh=vm.runInContext('retentionPolicyPreview.handlers.click()',h.context);
  h.respond(h.calls[2],{draft_id:'d2',previous_days:null,days:30,warning:'cannot undo'});await fresh;
  const apply=vm.runInContext('retentionPolicyApply.handlers.click()',h.context);
  assert.deepEqual(h.calls[3].body,{draft_id:'d2',confirm_policy_change:true});
  h.respond(h.calls[3],{error:'unknown'},false);await apply;
  assert.match(vm.runInContext('retentionPolicyInfo.textContent',h.context),/不会自动重试/);
  assert.equal(h.calls.length,4);
});

test('explicit preview rejection unlocks correction without automatic retry',()=>{
  const h=harness();vm.runInContext('modelPreviewStart.disabled=true;modelPreviewPoll.disabled=false',h.context);
  h.context.renderModelPreview({state:'rejected',execution_state:'not_started',error:'时间缺少时区'});
  assert.equal(vm.runInContext('modelPreviewStart.disabled',h.context),false);
  assert.equal(vm.runInContext('modelPreviewPoll.disabled',h.context),true);
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/未启动.*时间缺少时区/);
  assert.equal(h.calls.length,0);
});

test('retention scope switch invalidates old body response and uses draft endpoint',async()=>{
  const h=harness();
  const body=vm.runInContext('retentionPolicyRead.handlers.click()',h.context);
  vm.runInContext('retentionPolicyScope.value="draft";retentionPolicyScope.handlers.change()',h.context);
  h.respond(h.calls[0],{revision:9,days:10});await body;
  assert.equal(vm.runInContext('retentionPolicySnapshot',h.context),null);
  const draft=vm.runInContext('retentionPolicyRead.handlers.click()',h.context);
  assert.equal(h.calls[1].url,'/api/draft-retention-policy');
  h.respond(h.calls[1],{revision:0,days:null});await draft;
  const preview=vm.runInContext('retentionPolicyPreview.handlers.click()',h.context);
  assert.equal(h.calls[2].url,'/api/draft-retention-policy-preview');
  h.respond(h.calls[2],{draft_id:'draft-only',previous_days:null,days:null,warning:'draft warning'});await preview;
  vm.runInContext('retentionPolicyScope.value="body";retentionPolicyScope.handlers.change()',h.context);
  assert.equal(vm.runInContext('retentionPolicyApply.disabled',h.context),true);
  assert.equal(h.calls.length,3);
});

test('candidate document form adds and removes without requesting a model',()=>{
  const h=harness();vm.runInContext('previewDocTitle.value="Fan guide";previewDocUrl.value="https://example.com/fan";previewDocContent.value="summary";previewDocAdd.handlers.click()',h.context);
  let docs=JSON.parse(vm.runInContext('modelPreviewDocuments.value',h.context));
  assert.deepEqual(docs,[{title:'Fan guide',url:'https://example.com/fan',content:'summary'}]);
  assert.equal(h.calls.length,0);
  vm.runInContext('previewDocTitle.value="Duplicate";previewDocUrl.value="https://example.com/fan";previewDocAdd.handlers.click()',h.context);
  assert.match(vm.runInContext('previewDocStatus.textContent',h.context),/不重复/);
  assert.equal(JSON.parse(vm.runInContext('modelPreviewDocuments.value',h.context)).length,1);
  vm.runInContext('previewDocList.children[0].children[2].handlers.click()',h.context);
  assert.deepEqual(JSON.parse(vm.runInContext('modelPreviewDocuments.value',h.context)),[]);
});

test('advanced document edits invalidate stale removal and malformed JSON is preserved',()=>{
  const h=harness();vm.runInContext('previewDocTitle.value="One";previewDocUrl.value="https://example.com/one";previewDocAdd.handlers.click()',h.context);
  const remove=vm.runInContext('previewDocList.children[0].children[2]',h.context);
  vm.runInContext('modelPreviewDocuments.value=JSON.stringify([{title:"Two",url:"https://example.com/two",content:""}])',h.context);
  remove.handlers.click();
  assert.equal(JSON.parse(vm.runInContext('modelPreviewDocuments.value',h.context))[0].title,'Two');
  vm.runInContext('modelPreviewDocuments.value="malformed";previewDocTitle.value="Three";previewDocUrl.value="https://example.com/three";previewDocAdd.handlers.click()',h.context);
  assert.equal(vm.runInContext('modelPreviewDocuments.value',h.context),'malformed');
  assert.match(vm.runInContext('previewDocStatus.textContent',h.context),/修正高级 JSON/);
});

test('two-stage GUI preview includes fixture documents and labels unexecuted work',async()=>{
  const h=harness();vm.runInContext('modelPreviewQuery.value="fan";modelPreviewKind.value="research";modelPreviewDocuments.value="not json"',h.context);
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  vm.runInContext('modelPreviewDocuments.value=JSON.stringify([{title:"fan",url:"https://example.com/fan",content:"fixture"}])',h.context);
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls[0].body.documents[0].title,'fan');
  h.respond(h.calls[0],{state:'completed',result:{preview_kind:'routing_and_document_selection',steps:[{kind:'research',state:'needs_owner_review',codex_queued:true}],intention_counts:{outbox:2,jobs:2}}});await pending;
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/等待你审核/);
  assert.match(text,/未启动 Codex/);
  assert.match(text,/完整预演的意图总数：通知 2/);
  assert.match(text,/使用提供的候选资料/);
});

test('three-stage preview requires explicit confirmation and explains queued question',async()=>{
  const h=harness();let confirmation='';
  h.context.confirm=text=>{confirmation=text;return false;};
  vm.runInContext('modelPreviewQuery.value="boot";modelPreviewKind.value="research_review";modelPreviewDocuments.value="[]"',h.context);
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.match(confirmation,/最多 3 次模型调用/);
  assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls[0].body.review_clarification,true);
  assert.deepEqual(h.calls[0].body.documents,[]);
  h.respond(h.calls[0],{state:'completed',result:{preview_kind:'routing_research_clarification',steps:[{kind:'research',state:'clarification_queued'}]}});await pending;
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/追问审核通过.*未发送/);
  assert.match(text,/不含真实发送/);
  assert.doesNotMatch(text,/不含.*完整追问审核/);
});

test('conversation confirmation counts all turns and sends only confirmed followups',async()=>{
  const h=harness();let confirmation='';
  vm.runInContext('modelPreviewQuery.value="boot";modelPreviewKind.value="research_review";modelPreviewDocuments.value="[]";modelPreviewFollowups.value="不是Pico，是EVB\\n串口停在SPL"',h.context);
  h.context.confirm=text=>{confirmation=text;return false;};
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.match(confirmation,/最多 9 次模型调用/);assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.deepEqual(h.calls[0].body.followups,['不是Pico，是EVB','串口停在SPL']);
  assert.equal(h.calls[0].body.confirm_model_call,true);
  h.respond(h.calls[0],{state:'running'});await pending;
  assert.equal(h.calls.length,1);
});

test('AI preview submits timezone observation as business and event time',async()=>{
  const h=harness();vm.runInContext('modelPreviewQuery.value="question";modelPreviewTime.value="2026-09-09T22:00:00+08:00"',h.context);
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls[0].body.assumptions.observed_at,'2026-09-09T22:00:00+08:00');
  assert.equal(h.calls[0].body.event.occurred_at,h.calls[0].body.assumptions.observed_at);
  h.respond(h.calls[0],{state:'completed',result:{business_window:{active:true},steps:[]}});await pending;
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/非工作时间/);
});

test('Debug preview separately confirms recorded checks and does not submit route assumptions',async()=>{
  const h=harness();let confirmation='';
  h.context.confirm=text=>{confirmation=text;return false;};
  vm.runInContext('modelPreviewKind.value="debug";debugPreviewJob.value="job-1";debugPreviewTranscript.value="[]";modelPreviewAssumptions.mode.value="auto"',h.context);
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.match(confirmation,/最多 1 次调用/);assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.deepEqual(h.calls[0].body.event,{});
  assert.deepEqual(h.calls[0].body.debug,{job_id:'job-1',transcript:[]});
  assert.equal(h.calls[0].body.assumptions,undefined);
  h.respond(h.calls[0],{state:'completed',result:{preview_kind:'captured_debug_review',review_ok:true,case_state:'monitoring',outbox_intentions:1,transcript:{consumed:4,remaining:0},model_callback_invoked:true}});await pending;
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/不代表真实验证或现场问题已解决/);
  assert.match(text,/未执行命令、上板或发送消息/);
});

test('queued Debug simulation defaults to absent exit and requires confirmation',async()=>{
  const h=harness();
  vm.runInContext('modelPreviewKind.value="debug_execution";debugPreviewJob.value="job-1";debugPreviewTranscript.value="[]";debugPreviewReport.value="synthetic report"',h.context);
  h.context.confirm=()=>false;
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.deepEqual(h.calls[0].body.debug.execution,{report:'synthetic report',exit_status:null});
  assert.equal(h.calls[0].body.assumptions,undefined);
  h.respond(h.calls[0],{state:'completed',result:{preview_kind:'simulated_debug_execution',completion_state:'unverified',review_ok:false,case_state:null,outbox_intentions:0,transcript:{consumed:0,remaining:0},model_callback_invoked:false}});await pending;
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/缺少退出证据/);assert.match(text,/未调用模型复核/);assert.doesNotMatch(text,/null/);
});

test('Debug file import fills only the form and ignores late reads after manual editing',async()=>{
  const h=harness();
  h.context.fileFixture={size:100,text:async()=>JSON.stringify({job_id:'job-import',transcript:[]})};
  vm.runInContext('debugPreviewFile.files=[fileFixture]',h.context);
  await vm.runInContext('debugPreviewFile.handlers.change()',h.context);
  assert.equal(vm.runInContext('debugPreviewJob.value',h.context),'job-import');
  assert.equal(h.calls.length,0);
  let resolve;h.context.fileFixture={size:100,text:()=>new Promise(r=>{resolve=r;})};
  vm.runInContext('debugPreviewFile.files=[fileFixture]',h.context);
  const pending=vm.runInContext('debugPreviewFile.handlers.change()',h.context);
  vm.runInContext('debugPreviewJob.value="manual";debugPreviewJob.handlers.input()',h.context);
  resolve(JSON.stringify({job_id:'late',transcript:[]}));await pending;
  assert.equal(vm.runInContext('debugPreviewJob.value',h.context),'manual');
  assert.equal(h.calls.length,0);
});

test('Debug oversized and malformed imports preserve current input',async()=>{
  const h=harness();
  h.context.fileFixture={size:61441,text:()=>assert.fail('oversized file read')};
  vm.runInContext('debugPreviewJob.value="keep";debugPreviewFile.files=[fileFixture]',h.context);
  await vm.runInContext('debugPreviewFile.handlers.change()',h.context);
  assert.match(vm.runInContext('debugPreviewImportInfo.textContent',h.context),/超过/);
  h.context.fileFixture={size:4,text:async()=>'null'};
  vm.runInContext('debugPreviewFile.files=[fileFixture]',h.context);
  await vm.runInContext('debugPreviewFile.handlers.change()',h.context);
  assert.equal(vm.runInContext('debugPreviewJob.value',h.context),'keep');
  assert.equal(h.calls.length,0);
});

test('AI preview submits explicit assumptions and rejects incomplete role selection',async()=>{
  const h=harness();vm.runInContext('modelPreviewQuery.value="boot failed";modelPreviewAssumptions.relationship.value="supervisor"',h.context);
  await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/同时选择/);
  vm.runInContext('modelPreviewAssumptions.function_role.value="project_manager";modelPreviewAssumptions.mode.value="observe"',h.context);
  const pending=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.deepEqual(h.calls[0].body.assumptions,{relationship:'supervisor',function_role:'project_manager',mode:'observe'});
  h.respond(h.calls[0],{state:'completed',result:{assumptions:h.calls[0].body.assumptions,steps:[]}});await pending;
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/真实资料与全局设置未修改/);
});

for(const [route,label] of Object.entries({ignore:'忽略',direct_answer:'直接回答',clarify:'追问',research:'查资料',codex_debug:'交给 Codex 排查',owner_decision:'找你决策',urgent_notify:'紧急通知'}))test(`model preview explains ${route} without claiming execution`,()=>{
  const h=harness();
  h.context.renderModelPreview({state:'completed',result:{model_callback_invoked:true,steps:[{kind:'inbound',route,outbox_intentions:1,job_intentions:2}]}});
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.ok(text.includes(label));
  assert.match(text,/未实际发送或执行/);
  assert.match(text,/待发送意图：1；待执行任务：2/);
  assert.match(text,/不代表资料已查完、故障已修复或现场已解决/);
  assert.equal(vm.runInContext('modelPreviewStart.disabled',h.context),false);
  assert.equal(JSON.parse(vm.runInContext('modelPreviewRaw.textContent',h.context)).state,'completed');
});

test('model preview distinguishes blocked mode from successful inference',()=>{
  const h=harness();
  h.context.renderModelPreview({state:'completed',result:{model_callback_invoked:false,steps:[{kind:'inbound',blocked_by_mode:'paused',outbox_intentions:0,job_intentions:0}]}});
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/立即暂停.*阻止处理/);
  assert.match(text,/未确认调用了路由模型/);
  assert.doesNotMatch(text,/处理路径：/);
  h.context.renderModelPreview({state:'failed',error:'<script>raw</script>'});
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/费用可能未知/);
  assert.doesNotMatch(vm.runInContext('modelPreviewInfo.textContent',h.context),/<script>/);
});

for(const execution_state of ['completed','unknown'])test(`evicted preview keeps ${execution_state} receipt without retry`,()=>{
  const h=harness();
  h.context.renderModelPreview({state:execution_state==='unknown'?'failed':'completed',execution_state,result_evicted:true});
  const text=vm.runInContext('modelPreviewInfo.textContent',h.context);
  assert.match(text,/去重收据仍保留，不会再次调用模型/);
  if(execution_state==='unknown')assert.match(text,/结果未知/);
  assert.equal(vm.runInContext('modelPreviewStart.disabled',h.context),false);
  assert.equal(vm.runInContext('modelPreviewPoll.disabled',h.context),true);
  assert.equal(h.calls.length,0);
});

test('preview thread start failure is not reported as unknown execution',()=>{
  const h=harness();
  h.context.renderModelPreview({state:'failed',execution_state:'not_started'});
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/预演未启动/);
  assert.doesNotMatch(vm.runInContext('modelPreviewInfo.textContent',h.context),/费用可能未知/);
  assert.equal(vm.runInContext('modelPreviewPoll.disabled',h.context),true);
  assert.equal(h.calls.length,0);
});

test('model preview requires confirmation, polls same request and ignores abandoned results',async()=>{
  const h=harness();vm.runInContext('modelPreviewQuery.value="why boot fails"',h.context);
  h.context.confirm=()=>false;await vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const starting=vm.runInContext('modelPreviewStart.handlers.click()',h.context);
  assert.equal(h.calls[0].url,'/api/model-preview-start');
  assert.equal(h.calls[0].body.confirm_model_call,true);
  assert.equal(h.calls[0].body.event.payload.content,'why boot fails');
  const identifier=h.calls[0].body.request_id;
  assert.equal(vm.runInContext('modelPreviewStart.disabled',h.context),true);
  h.respond(h.calls[0],{error:'unknown'},false);await starting;
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/不会自动重试/);
  const polling=vm.runInContext('modelPreviewPoll.handlers.click()',h.context);
  assert.equal(h.calls[1].url,'/api/model-preview-status');
  assert.equal(h.calls[1].body.request_id,identifier);
  vm.runInContext('modelPreviewLeave.handlers.click()',h.context);
  h.respond(h.calls[1],{state:'completed',result:{route:'stale'}});await polling;
  assert.match(vm.runInContext('modelPreviewInfo.textContent',h.context),/已结束查看/);
  assert.equal(vm.runInContext('modelPreviewStart.disabled',h.context),false);
  assert.equal(h.calls.length,2);
});

test('archive list renders literal names and never starts replay',async()=>{
  const h=harness();const loading=h.context.loadReplayArchives();
  assert.equal(h.calls[0].url,'/api/replay-archives');
  assert.deepEqual(h.calls[0].body,{limit:50});
  h.respond(h.calls[0],{items:[{name:'<script>fixture</script>',status:'manifest_present_unverified',recorded_at:'2026-09-09',runtime_digest:'a'.repeat(64)}],truncated:true});await loading;
  assert.match(vm.runInContext('replayArchiveItems.children[0].children[0].textContent',h.context),/<script>fixture<\/script>.*尚未验证/);
  assert.match(vm.runInContext('replayArchiveInfo.textContent',h.context),/不是完整列表/);
  assert.equal(h.calls.length,1);
});

test('archive capture requires valid input and confirmation without automatic replay or retry',async()=>{
  const h=harness();
  vm.runInContext('archiveCaptureName.value="one";archiveCaptureEvent.value="{}";archiveCaptureProposal.value="{}"',h.context);
  h.context.confirm=()=>false;await vm.runInContext('archiveCaptureStart.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=vm.runInContext('archiveCaptureStart.handlers.click()',h.context);
  assert.equal(h.calls[0].url,'/api/replay-archive-capture');
  assert.deepEqual(h.calls[0].body,{name:'one',event:{},proposal:{},confirmed:true,writers_quiesced:true});
  await vm.runInContext('archiveCaptureStart.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
  h.respond(h.calls[0],{error:'PRIVATE'},false);await pending;
  assert.match(vm.runInContext('archiveCaptureInfo.textContent',h.context),/不自动重试/);
  assert.doesNotMatch(vm.runInContext('archiveCaptureInfo.textContent',h.context),/PRIVATE/);
  vm.runInContext('archiveCaptureEvent.value="[]"',h.context);
  await vm.runInContext('archiveCaptureStart.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
});

test('archive refresh ignores stale response and permits manual error recovery',async()=>{
  const h=harness();const old=h.context.loadReplayArchives();const current=h.context.loadReplayArchives();
  h.respond(h.calls[1],{items:[],truncated:false});await current;
  h.respond(h.calls[0],{items:[{name:'stale',status:'incomplete'}],truncated:false});await old;
  assert.equal(vm.runInContext('replayArchiveItems.children.length',h.context),0);
  assert.match(vm.runInContext('replayArchiveInfo.textContent',h.context),/暂无/);
  const failure=h.context.loadReplayArchives();h.respond(h.calls[2],{error:'private error'},false);await failure;
  assert.match(vm.runInContext('replayArchiveInfo.textContent',h.context),/手动重试/);
  assert.equal(h.calls.length,3);
});

test('backup inspection uses read-only endpoint and preserves uncertainty',async()=>{
  const h=harness();const loading=h.context.loadBackupAudit();
  h.respond(h.calls[0],{items:[{receipt_id:'bound',name:'backup.db',state:'prepared'}],next_cursor:null});await loading;
  const pending=vm.runInContext('backupAuditItems.children[0].children[1].handlers.click()',h.context);
  assert.equal(h.calls[1].url,'/api/backup-retention-inspect');
  assert.equal(h.calls[1].body.receipt_id,'bound');
  h.respond(h.calls[1],{name:'backup.db',target_state:'missing',observed_at:'2026-09-08'});await pending;
  assert.match(vm.runInContext('backupAuditInfo.textContent',h.context),/不能据此确认删除原因/);
  assert.equal(h.calls.length,2);
});

test('late backup inspection cannot overwrite a refreshed list',async()=>{
  const h=harness();const loading=h.context.loadBackupAudit();
  h.respond(h.calls[0],{items:[{receipt_id:'bound',name:'backup.db',state:'prepared'}],next_cursor:null});await loading;
  const pending=vm.runInContext('backupAuditItems.children[0].children[1].handlers.click()',h.context);
  const refresh=h.context.loadBackupAudit();h.respond(h.calls[2],{items:[],next_cursor:null});await refresh;
  h.respond(h.calls[1],{name:'stale',target_state:'same_version'});await pending;
  assert.equal(vm.runInContext('backupAuditInfo.textContent',h.context),'暂无清理记录。');
});

test('attachment save binds prepared digest and never retries uncertain writes',async()=>{
  const h=harness();
  vm.runInContext('attachmentEvidence={};attachmentDraftTitle.value="title";attachmentDraftQuestion.value="question";attachmentDraftAnswer.value="answer";attachmentDraftRefs.value="#/texts/0";attachmentDraftRisk.value="read_only";attachmentDraftRollback.value=""',h.context);
  const generating=vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  h.respond(h.calls[0],{markdown:'draft',notice:'unreviewed',filename:'draft.md',revision_digest:'bound-digest'});await generating;
  const saving=vm.runInContext('attachmentDraftSave.handlers.click()',h.context);
  assert.equal(h.calls[1].url,'/api/attachment-draft-save');
  assert.equal(h.calls[1].body.expected_digest,'bound-digest');
  assert.equal(h.calls[1].body.fields.answer,'answer');
  h.respond(h.calls[1],{error:'unknown'},false);await saving;
  await vm.runInContext('attachmentDraftSave.handlers.click()',h.context);
  assert.equal(h.calls.length,2);
  assert.match(vm.runInContext('attachmentStatus.textContent',h.context),/未确认/);
});

test('attachment edits invalidate prepared save',async()=>{
  const h=harness();
  vm.runInContext('attachmentEvidence={};attachmentDraftTitle.value="title";attachmentDraftQuestion.value="question";attachmentDraftAnswer.value="answer";attachmentDraftRefs.value="#/texts/0";attachmentDraftRisk.value="read_only";attachmentDraftRollback.value=""',h.context);
  const generating=vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  h.respond(h.calls[0],{markdown:'draft',notice:'unreviewed',filename:'draft.md',revision_digest:'bound-digest'});await generating;
  vm.runInContext('attachmentDraftAnswer.handlers.input()',h.context);
  await vm.runInContext('attachmentDraftSave.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
});

test('declared scope is included in draft and editing it invalidates save',async()=>{
  const h=harness();
  vm.runInContext('attachmentEvidence={};attachmentDraftTitle.value="title";attachmentDraftQuestion.value="question";attachmentDraftAnswer.value="answer";attachmentDraftRefs.value="#/texts/0";attachmentDraftRisk.value="read_only";attachmentDraftRollback.value="";attachmentScopeProduct.value="K3";attachmentScopeComponent.value="u-boot";attachmentScopeVersion.value="v1"',h.context);
  const pending=vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.deepEqual(h.calls[0].body.authored_scope,{product:'K3',component:'u-boot',software_version:'v1'});
  h.respond(h.calls[0],{markdown:'draft',notice:'unreviewed',filename:'draft.md',revision_digest:'bound'});await pending;
  vm.runInContext('attachmentScopeVersion.value="v2";attachmentScopeVersion.handlers.input()',h.context);
  await vm.runInContext('attachmentDraftSave.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
  assert.equal(vm.runInContext('attachmentDraftSave.disabled',h.context),true);
});

test('attachment re-preview clears prepared save and cannot build from unchecked material',async()=>{
  const h=harness();
  vm.runInContext('attachmentEvidence={};attachmentPrepared={};attachmentDraftSave.disabled=false;attachmentFile.files=[{size:10,text:async()=>"{}"}]',h.context);
  const pending=h.context.loadAttachmentEvidence();await settle();
  assert.equal(vm.runInContext('attachmentDraftSave.disabled',h.context),true);
  assert.equal(vm.runInContext('attachmentPrepared',h.context),null);
  await vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
  h.respond(h.calls[0],{error:'invalid evidence'},false);await pending;
  assert.equal(vm.runInContext('attachmentEvidence',h.context),null);
  await vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.equal(h.calls.length,1);
});

test('draft cleanup invalidates preview on policy edits and never retries uncertain writes',async()=>{
  const h=harness();let pending=h.context.previewDraftClear('candidate');
  vm.runInContext('draftDays.handlers.input()',h.context);
  h.respond(h.calls[0],{eligible:true,row_digest:'stale'});await pending;
  assert.equal(vm.runInContext('draftActions.children.length',h.context),0);
  pending=h.context.previewDraftClear('candidate');
  h.respond(h.calls[1],{eligible:true,row_digest:'bound',logical_bytes:12});await pending;
  pending=vm.runInContext('draftActions.children[0].handlers.click()',h.context);
  assert.equal(h.calls[2].url,'/api/draft-retention-clear');
  assert.equal(h.calls[2].body.preview_digest,'bound');
  assert.equal(h.calls[2].body.confirm_logical_delete,true);
  h.respond(h.calls[2],{error:'unknown'},false);await pending;
  await vm.runInContext('draftActions.children[0].handlers.click()',h.context);
  assert.equal(h.calls.length,3);
  assert.match(vm.runInContext('draftInfo.textContent',h.context),/不会自动重试/);
});

test('draft cleanup does not offer delete for retained drafts',async()=>{
  const h=harness();const pending=h.context.previewDraftClear('candidate');
  h.respond(h.calls[0],{eligible:false,blockers:['not_expired']});await pending;
  assert.equal(vm.runInContext('draftActions.children.length',h.context),0);
  assert.match(vm.runInContext('draftInfo.textContent',h.context),/未过保留期限/);
});

test('saved draft list rejects stale page response',async()=>{
  const h=harness();const first=h.context.loadAuthoring();const second=h.context.loadAuthoring();
  h.respond(h.calls[1],{items:[],next_cursor:null});await second;
  h.respond(h.calls[0],{items:[{candidate_id:'old',title:'stale',saved_at:'old'}],next_cursor:'old'});await first;
  assert.equal(vm.runInContext('authoringItems.children.length',h.context),0);
  assert.equal(vm.runInContext('authoringNext.disabled',h.context),true);
});

test('draft detail shows source and risk as text and discards stale detail',async()=>{
  const h=harness();const old=h.context.loadAuthoringDetail('old');const fresh=h.context.loadAuthoringDetail('new');
  h.respond(h.calls[1],{title:'new',markdown:'new draft',material:{source:{id:'<script>source</script>',version:'7',sha256:'hash'},parser:{name:'Docling',version:'1'}},metadata:{claims:[{risk_class:'persistent'}],sources:[]}});await fresh;
  h.respond(h.calls[0],{title:'old',markdown:'old draft'});await old;
  assert.equal(vm.runInContext('authoringOutput.value',h.context),'new draft');
  const text=vm.runInContext('authoringProvenance.children.map(x=>x.textContent).join("\\n")',h.context);
  assert.match(text,/<script>source<\/script>/);
  assert.match(text,/持久化变更/);
  assert.match(vm.runInContext('authoringStatus.textContent',h.context),/不参与自动回复/);
});

test('saved draft resumes editing only after confirmation and regenerates before save',async()=>{
  const h=harness();const loading=h.context.loadAuthoringDetail('saved');
  h.respond(h.calls[0],{title:'Draft',markdown:'original',material:{document:{schema_name:'fixture'},source:{id:'source'},parser:{version:'1'}},metadata:{title:'Draft',intent:{question_examples:['Question']},content:{summary:'Answer',rollback:['Restore']},scope:{product:'K3',component:'u-boot',software_versions:['commit-1']},claims:[{risk_class:'persistent'}],sources:[{locator:{json_pointer:'#/texts/0'}}]}});await loading;
  const resume=vm.runInContext('authoringProvenance.children.find(x=>x.textContent==="继续整理，另存草稿")',h.context);
  vm.runInContext('attachmentDraftTitle.value="unsaved"',h.context);
  h.context.confirm=()=>false;resume.handlers.click();
  assert.equal(vm.runInContext('attachmentDraftTitle.value',h.context),'unsaved');
  h.context.confirm=()=>true;resume.handlers.click();
  assert.equal(vm.runInContext('attachmentDraftTitle.value',h.context),'Draft');
  assert.equal(vm.runInContext('attachmentDraftTitle.focused',h.context),true);
  assert.equal(vm.runInContext('attachmentDraftSave.disabled',h.context),true);
  assert.equal(vm.runInContext('authoringOutput.value',h.context),'original');
  const build=vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.equal(h.calls[1].url,'/api/attachment-draft');
  assert.equal(h.calls[1].body.answer,'Answer');
  assert.equal(h.calls[1].body.references[0],'#/texts/0');
  assert.equal(h.calls[1].body.authored_scope.software_version,'commit-1');
  assert.equal(h.calls[1].body.evidence.source.id,'source');
  h.respond(h.calls[1],{markdown:'regenerated',revision_digest:'new',notice:'unreviewed'});await build;
  assert.equal(vm.runInContext('attachmentDraftSave.disabled',h.context),false);
  const refresh=h.context.loadAuthoring();
  vm.runInContext('attachmentDraftTitle.value="keep"',h.context);resume.handlers.click();
  assert.equal(vm.runInContext('attachmentDraftTitle.value',h.context),'keep');
  h.respond(h.calls[2],{items:[],next_cursor:null});await refresh;
});

test('attachment preview requires selection and ignores stale file response',async()=>{
  const h=harness();await h.context.loadAttachmentEvidence();assert.equal(h.calls.length,0);
  vm.runInContext('attachmentFile.files=[{size:10,text:async()=>"{}"}]',h.context);
  const pending=h.context.loadAttachmentEvidence();await settle();
  assert.equal(h.calls[0].url,'/api/attachment-evidence-preview');
  vm.runInContext('attachmentFile.handlers.change()',h.context);
  h.respond(h.calls[0],{notice:'stale',source:{id:'old',version:'1'},page:1,page_count:2,items:[]});await pending;
  assert.equal(vm.runInContext('attachmentNext.disabled',h.context),true);
  assert.equal(vm.runInContext('attachmentItems.children.length',h.context),0);
});

test('attachment draft requires material and discards edits during generation',async()=>{
  const h=harness();
  await vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.equal(h.calls.length,0);
  vm.runInContext('attachmentEvidence={};attachmentDraftTitle.value="title";attachmentDraftQuestion.value="question";attachmentDraftAnswer.value="answer";attachmentDraftRefs.value="#/texts/0";attachmentDraftRisk.value="read_only";attachmentDraftRollback.value=""',h.context);
  const generating=vm.runInContext('attachmentDraftBuild.handlers.click()',h.context);
  assert.equal(h.calls[0].url,'/api/attachment-draft');
  vm.runInContext('attachmentDraftAnswer.handlers.input()',h.context);
  h.respond(h.calls[0],{markdown:'old draft',notice:'old',filename:'old.md'});await generating;
  assert.equal(vm.runInContext('attachmentDraftOutput.value',h.context),'');
});

test('body retention preview stays read-only and discards stale days',async()=>{
  const h=harness(), first=h.context.loadBodyRetention();
  assert.equal(h.calls[0].url,'/api/body-retention-preview');
  assert.equal(h.calls[0].body.days,180);
  vm.runInContext('bodyRetentionDays.value="30";bodyRetentionDays.handlers.input()',h.context);
  h.respond(h.calls[0],{items:[{event_pk:'stale'}],next_cursor:'old'});await first;
  assert.equal(vm.runInContext('bodyRetentionList.children.length',h.context),0);
  assert.equal(vm.runInContext('bodyRetentionNext.disabled',h.context),true);
  const fresh=h.context.loadBodyRetention();
  h.respond(h.calls[1],{items:[{event_pk:'event',source:'mail',received_at:'then',body_bytes:2,
    body_state:'cleared_by_retention',clear_receipt:{cleared_at:'now',actor:'owner'},clear_blockers:['already_cleared']}],next_cursor:null,json_scan_issues:[]});
  await fresh;
  assert.equal(vm.runInContext('bodyRetentionList.children.length',h.context),1);
  assert.equal(h.calls.every(call=>call.url==='/api/body-retention-preview'),true);
  vm.runInContext('bodyRetentionDays.value="0"',h.context);await h.context.loadBodyRetention();
  assert.equal(h.calls.length,2);
});

test('body clear requires a click and confirmation and uses the displayed digest',async()=>{
  const h=harness(), loading=h.context.loadBodyRetention();
  h.respond(h.calls[0],{items:[{event_pk:'event',source:'mail',received_at:'then',body_bytes:20,
    body_state:'stored',snapshot_digest:'a'.repeat(64),clear_blockers:[]}],next_cursor:null,json_scan_issues:[]});
  await loading;
  const button=vm.runInContext('bodyRetentionList.children[0].children.find(x=>x.textContent==="清理此条正文…")',h.context);
  assert.equal(h.calls.length,1);
  h.context.confirm=()=>false;await button.handlers.click();assert.equal(h.calls.length,1);
  h.context.confirm=text=>{assert.match(text,/消息正文和错误详情/);return true;};const clearing=button.handlers.click();
  assert.equal(h.calls[1].url,'/api/body-retention-clear');
  assert.equal(h.calls[1].body.expected.event,'a'.repeat(64));
  assert.equal(h.calls[1].body.confirm_error_details,true);
  h.respond(h.calls[1],{error:'uncertain'},false);await clearing;
  assert.equal(button.disabled,true);await button.handlers.click();assert.equal(h.calls.length,2);
});

test('reviewed feedback remains visible and links current knowledge without editing',async()=>{
  const h=harness();
  const loading=h.context.loadFeedbackQueue('','needs_revision');
  assert.equal(h.calls[0].body.state,'needs_revision');
  h.respond(h.calls[0],{items:[{case_id:'K3-review',use_id:'use',knowledge_id:'knw',
    entry_fingerprint:'a'.repeat(64),created_at:'then',decision:'needs_revision',
    reason:'missing detail',reviewed_at:'now'}],next_cursor:'next'});
  await loading;
  const rows=vm.runInContext('feedbackQueue.children',h.context);
  assert.equal(rows[0].children[1].textContent,'待修订（尚未修复）');
  rows[0].children.find(el=>el.textContent==='查看当前知识').handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/knowledge-detail');
  assert.equal(h.calls[1].body.knowledge_id,'knw');
  assert.equal(h.calls.filter(call=>call.url==='/api/sent-knowledge-review').length,0);
  rows[1].handlers.click();await settle();
  assert.equal(h.calls[2].body.state,'needs_revision');
  assert.equal(h.calls[2].body.after_id,'next');
});

for (const mode of ['success','denied','absent','missing']) test(`revision candidate copy is explicit and bounded: ${mode}`, async()=>{
  const h=harness(), copied=[];
  if(mode!=='absent')h.context.navigator={clipboard:{writeText:async text=>{
    if(mode==='denied')throw new Error('denied');copied.push(text);
  }}};
  const loading=h.context.loadFeedbackQueue('','needs_revision');
  h.respond(h.calls[0],{items:[{case_id:'K3',use_id:'use',knowledge_id:'known',request_id:'feedback',
    decision:'needs_revision'}]});await loading;
  const rows=vm.runInContext('feedbackQueue.children',h.context);
  rows[0].children.find(el=>el.textContent==='查看修订材料').handlers.click();await settle();
  const candidate={status:'candidate',query:'<script>private</script>'};
  h.respond(h.calls[1],{regression_candidate:mode==='missing'?null:candidate});await settle();
  assert.equal(copied.length,0);
  const children=h.get('case-actions').children;
  const copy=children.find(el=>el.textContent==='复制回归候选');
  if(mode==='missing'){assert.equal(copy,undefined);return;}
  const field=children.find(el=>el.readOnly);
  assert.equal(field.value,JSON.stringify(candidate)+'\n');
  await copy.handlers.click();
  assert.equal(h.calls.length,2);
  if(mode==='success'){
    assert.deepEqual(copied,[field.value]);assert.match(h.get('case-status').textContent,/已复制/);
  }else assert.match(h.get('case-status').textContent,/无法复制/);
  h.get('case-dialog').close();await copy.handlers.click();
  assert.equal(copied.length,mode==='success'?1:0);
});

for(const bound of [true,false])test(`sent reply feedback requires reviewed bound reply=${bound}`,async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const loading=h.context.loadDetail('K3-feedback');
  h.respond(h.calls[0],{...snapshot('case'),sent_knowledge:[{use_id:'use-exact',created_at:'today'}]});
  await loading;
  h.get('case-actions').children.find(button=>button.textContent.startsWith('评价已发送回复')).handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/sent-knowledge-preview');
  assert.equal(h.calls[1].body.use_id,'use-exact');
  h.respond(h.calls[1],{sent_text:'<script>untrusted</script>',note:'historical reply',
    version_bound:bound,content_digest:'a'.repeat(64)});await settle();
  assert.equal(h.get('case-content').textContent,'<script>untrusted</script>');
  assert.equal(h.calls.length,2);
  const buttons=h.get('case-actions').children;
  assert.equal(buttons.length,bound?3:0);
  if(!bound)return;
  const pending=buttons[1].handlers.click();
  assert.equal(h.calls[2].url,'/api/sent-knowledge-feedback');
  assert.equal(h.calls[2].body.verdict,'incorrect');
  assert.equal(h.calls[2].body.content_digest,'a'.repeat(64));
  assert.equal(h.calls[2].body.actor_id,undefined);
  await buttons[2].handlers.click();assert.equal(h.calls.length,3);
  h.respond(h.calls[2],{error:'unknown'},false);await pending;
  await buttons[1].handlers.click();assert.equal(h.calls.length,3);
  assert.match(h.get('case-status').textContent,/不自动重试/);
});

test('late sent feedback response cannot replace another Case',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const pending=h.context.loadSentKnowledge('K3-old','old-use');
  const current=h.context.loadDetail('K3-new');
  h.respond(h.calls[1],snapshot('new case'));await current;
  h.respond(h.calls[0],{sent_text:'old private reply',version_bound:true,note:'old'});await pending;
  assert.equal(h.get('case-content').textContent,'new case');
  assert.deepEqual(h.get('case-actions').children.map(button=>button.textContent),['创建编码任务','查看数据保留范围']);
});

test('content inventory is read only and stale responses cannot replace a Case',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const pending=h.context.loadContentInventory('K3-old');
  assert.equal(h.calls[0].url,'/api/case-content-inventory');
  const current=h.context.loadDetail('K3-new');
  h.respond(h.calls[1],snapshot('new case'));await current;
  h.respond(h.calls[0],{surfaces:[{table:'inbound_events',rows_counted:2,bytes_counted:10}]});await pending;
  assert.equal(h.get('case-content').textContent,'new case');
  assert.equal(h.calls.length,2);
});

test('content inventory shows per-member holds without granting clearance or rendering markup',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const pending=h.context.loadContentInventory('root');
  h.respond(h.calls[0],{surfaces:[],canonical_group_holds:{resolved_group_complete:false,members:[
    {case_id:'root',case_state:'closed',observed_holds:[]},
    {case_id:'<img src=x>',case_state:'active',observed_holds:[{reason:'delivery_unsettled',count:2}]}
  ]}});await pending;
  const text=h.get('case-content').textContent;
  assert.match(text,/root · closed/);
  assert.match(text,/<img src=x> · active：发送结果尚未确认 2 项/);
  assert.match(text,/关联组未完整解析/);
  assert.match(text,/不能相加/);
  assert.match(text,/不代表可清理/);
  assert.deepEqual(h.get('case-actions').children.map(button=>button.textContent),['返回任务详情']);
  assert.equal(h.calls.length,1);
});

test('retention preview explains execution holds and partial transform counts without apply controls',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const pending=h.context.loadContentInventory('case-fixture');
  h.respond(h.calls[0],{surfaces:[],observed_holds:[
    {reason:'worker_exit_unverified',count:1},
    {reason:'remote_execution_unsettled',count:2},
    {reason:'board_cleanup_unsettled',count:1},
    {reason:'resource_lock_present',count:1}
  ],outbox_json_fields:{candidate_transform_rows:3,candidate_removed_bytes:42,unclassified_rows:2,truncated:true}});
  await pending;
  const text=h.get('case-content').textContent;
  for(const label of ['编码执行退出尚未确认','远端执行收尾尚未确认','板卡收尾尚未确认','过期不代表已收尾',
    '3 条已识别','42 字节','2 条格式尚未覆盖','统计达到上限','不代表允许清理']) assert.ok(text.includes(label),label);
  assert.deepEqual(h.get('case-actions').children.map(button=>button.textContent),['返回任务详情']);
  assert.equal(h.calls.length,1);
});

test('canonical member dependency holds render alongside operational holds',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const pending=h.context.loadContentInventory('root');
  h.respond(h.calls[0],{surfaces:[],canonical_group_holds:{resolved_group_complete:true,members:[
    {case_id:'child-case',case_state:'resolved',observed_holds:[],dependency_holds:[
      {reason:'knowledge_source_reference',count:1},
      {reason:'shared_source_messages',count:2},
      {reason:'mail_summary_reference',count:3}
    ]}
  ]}});await pending;
  const text=h.get('case-content').textContent;
  assert.match(text,/child-case · resolved/);
  assert.match(text,/知识条目仍引用本任务或资料来源 1 项/);
  assert.match(text,/原始消息仍被其他任务或发送记录引用 2 项/);
  assert.match(text,/邮件摘要仍引用关联邮件，需单独处理保留期限 3 项/);
  assert.doesNotMatch(text,/child-case · resolved：未发现/);
  assert.deepEqual(h.get('case-actions').children.map(button=>button.textContent),['返回任务详情']);
});

test('archive replay requires a digest and confirmation and suppresses duplicate clicks',async()=>{
  const h=harness();
  const area=vm.runInContext('archiveRunControls("archive-one",replayArchiveEpoch)',h.context);
  const input=area.children[0].children[0],button=area.children[1],info=area.children[2],output=area.children[3];
  input.value='bad';await button.handlers.click();assert.equal(h.calls.length,0);
  input.value='a'.repeat(64);h.context.confirm=()=>false;await button.handlers.click();assert.equal(h.calls.length,0);
  h.context.confirm=()=>true;
  const pending=button.handlers.click();await settle();
  assert.ok(button.disabled);await button.handlers.click();assert.equal(h.calls.length,1);
  assert.deepEqual(h.calls[0].body,{name:'archive-one',manifest_digest:'a'.repeat(64),confirmed:true});
  h.respond(h.calls[0],{result:'<img src=x>',release_authorized:false});await pending;
  assert.match(output.textContent,/<img src=x>/);assert.match(info.textContent,/不代表/);
  assert.equal(button.disabled,false);
});

test('archive replay ignores stale results and does not retry failures automatically',async()=>{
  const h=harness();h.context.confirm=()=>true;
  const area=vm.runInContext('archiveRunControls("one",replayArchiveEpoch)',h.context);
  area.children[0].children[0].value='a'.repeat(64);
  const pending=area.children[1].handlers.click();await settle();
  vm.runInContext('++replayArchiveEpoch',h.context);
  h.respond(h.calls[0],{private:'old result'});await pending;
  assert.equal(area.children[3].textContent,'');
  const fresh=vm.runInContext('archiveRunControls("two",replayArchiveEpoch)',h.context);
  fresh.children[0].children[0].value='b'.repeat(64);
  const failed=fresh.children[1].handlers.click();await settle();
  h.respond(h.calls[1],{error:'PRIVATE ERROR'},false);await failed;
  assert.equal(h.calls.length,2);
  assert.match(fresh.children[2].textContent,/不要自动重试/);
  assert.doesNotMatch(fresh.children[2].textContent,/PRIVATE ERROR/);
  assert.equal(fresh.children[3].textContent,'');
});

test('remote cleanup observes first and confirms exact preview before applying',async()=>{
  const h=harness();
  const button=vm.runInContext('remoteCleanupButton("remote-1")',h.context);
  button.handlers.click();await settle();
  const observed=h.calls[0];assert.equal(observed.url,'/api/remote-observe');
  h.respond(observed,{state:'observed'});await settle();
  const preview=h.calls[1];assert.equal(preview.url,'/api/remote-cleanup-preview');
  assert.equal(preview.body.observation_id,observed.body.observation_id);
  h.respond(preview,{summary:'Only release occupancy',preview_digest:'bound'});await settle();
  const applied=h.calls[2];assert.equal(applied.url,'/api/remote-cleanup-apply');
  assert.equal(applied.body.preview_digest,'bound');
  assert.equal(applied.body.observation_id,observed.body.observation_id);
  h.respond(applied,{error:'connection lost'},false);await settle();
  button.handlers.click();await settle();
  assert.equal(h.calls.length,3);assert.match(vm.runInContext('executionInfo.textContent',h.context),/不自动重试/);
});

for(const outcome of ['unknown','navigation','cancel'])test(`remote cleanup never applies after ${outcome}`,async()=>{
  const h=harness();const button=vm.runInContext('remoteCleanupButton("remote-1")',h.context);
  button.handlers.click();await settle();
  if(outcome==='navigation')vm.runInContext('executionEpoch++',h.context);
  h.respond(h.calls[0],{state:outcome==='unknown'?'unknown':'observed'});await settle();
  if(outcome==='cancel'){
    h.context.confirm=()=>false;
    h.respond(h.calls[1],{summary:'Bound preview',preview_digest:'bound'});await settle();
  }
  assert.equal(h.calls.filter(call=>call.url==='/api/remote-cleanup-apply').length,0);
});

test('appearance switches locally without workflow requests or requiring storage',()=>{
  const h=harness();
  assert.equal(h.get('theme-toggle').textContent,'夜间');
  h.get('theme-toggle').handlers.click();
  assert.equal(h.get('theme-toggle').textContent,'日间');
  h.get('theme-toggle').handlers.click();
  assert.equal(h.get('theme-toggle').textContent,'夜间');
  assert.equal(h.calls.length,0);
});

test('appearance preference is bounded and persists only the selected theme',()=>{
  const h=harness(),writes=[];
  h.context.localStorage={setItem:(key,value)=>writes.push([key,value])};
  h.context.setAppearance('dark',true);
  h.context.setAppearance('untrusted-value',true);
  assert.deepEqual(writes,[['feishu-console-theme','dark'],['feishu-console-theme','light']]);
  assert.equal(h.calls.length,0);
});

for(const action of ['A','S'])test(`confirmation ${action} expands the full permission warning`,()=>{
  const h=harness(),details={open:false};
  h.get('mode-info').closest=()=>details;
  h.context.panel({text:'完整风险说明',buttons:[{text:'确认',callback_data:`fsc:${action}:panel`}]});
  assert.equal(details.open,true);
  assert.equal(h.get('mode-info').textContent,'完整风险说明');
  assert.equal(h.calls.length,0);
});

test('watch event names localize known Case events without rewriting release titles',()=>{
  const h=harness();
  assert.equal(h.context.watchEventTitle({source_kind:'case',title:'case_created'}),'事项已建立（case_created）');
  assert.equal(h.context.watchEventTitle({source_kind:'release',title:'case_created'}),'case_created');
  assert.equal(h.context.watchEventTitle({source_kind:'case',title:'future_event'}),'future_event');
  assert.equal(h.context.watchEventTitle({source_kind:'case',title:'constructor'}),'constructor');
});

for(const success of [true,false])test(`watch seen submits once and only refreshes on success=${success}`,async()=>{
  const h=harness(),loading=h.context.loadWatches();
  h.respond(h.calls[0],{note:'local',items:[{action_id:'exact-action',source_kind:'release',source_key:'uboot',title:'event',revision:'abc',occurred_at:'2026-09-08T00:00:00Z'}],next_after_id:null});await loading;
  const list=vm.runInContext('watchList',h.context),button=list.children[0].children.at(-1);
  const applying=button.handlers.click();await button.handlers.click();
  assert.equal(h.calls.length,2);assert.equal(h.calls[1].url,'/api/watch-seen');assert.equal(h.calls[1].body.action_id,'exact-action');
  h.respond(h.calls[1],success?{seen_at:'now'}:{error:'unknown'},success);await settle();
  if(success){assert.equal(h.calls[2].url,'/api/watch-list');h.respond(h.calls[2],{note:'local',items:[],next_after_id:null});}
  await applying;
  assert.equal(button.disabled,true);
  assert.equal(h.calls.filter(call=>call.url==='/api/watch-seen').length,1);
  if(!success){assert.equal(list.children[0].children.at(-1),button);assert.match(vm.runInContext('watchInfo',h.context).textContent,/未知/);}
});

test('watch settings require read preview apply and invalidate edited targets',async()=>{
  const h=harness(),get=name=>vm.runInContext(name,h.context);
  get('watchKey').value='uboot';const reading=get('watchSettingsRead').handlers.click();
  h.respond(h.calls[0],{source_kind:'release',source_key:'uboot',revision:0,enabled:0});await reading;
  get('watchPreview').handlers.click();assert.equal(h.calls.length,1);
  get('watchKey').handlers.input();assert.equal(get('watchApply').disabled,true);
  const reread=get('watchSettingsRead').handlers.click();
  h.respond(h.calls[1],{source_kind:'release',source_key:'uboot',revision:1,enabled:1});await reread;
  get('watchPreview').handlers.click();const applying=get('watchApply').handlers.click();
  assert.equal(h.calls[2].body.enabled,false);assert.equal(h.calls[2].body.expected_revision,1);
  h.respond(h.calls[2],{error:'unknown'},false);await applying;
  assert.equal(get('watchApply').disabled,true);assert.equal(h.calls.length,3);
});

test('watch inventory ignores late reads and opens exact Case without mutating',async()=>{
  const h=harness(),old=h.context.loadWatches(),latest=h.context.loadWatches();
  h.respond(h.calls[1],{note:'local',items:[{source_kind:'case',source_key:'CASE-7',title:'<b>event</b>',occurred_at:'2026-09-08T00:00:00Z'}],next_after_id:null});await latest;
  h.respond(h.calls[0],{note:'stale',items:[],next_after_id:'stale'});await old;
  const card=vm.runInContext('watchList',h.context).children[0];
  assert.equal(card.children[0].textContent,'<b>event</b>');
  assert.equal(vm.runInContext('watchMore',h.context).disabled,true);
  card.children[2].handlers.click();
  assert.equal(h.calls[2].body.case_id,'CASE-7');
  assert.equal(h.calls.length,3);
});

test('release inventory invalidates stale filters and renders assessment as text',async()=>{
  const h=harness(),repo=vm.runInContext('releaseRepo',h.context);
  repo.value='uboot';const old=h.context.loadReleases();
  repo.value='ec';repo.handlers.input();
  h.respond(h.calls[0],{items:[{subject:'stale'}],next_after_id:'old'});await old;
  assert.equal(vm.runInContext('releaseList',h.context).children.length,0);
  const current=h.context.loadReleases();
  assert.equal(h.calls[1].body.repository,'ec');
  h.respond(h.calls[1],{note:'local only',items:[{subject:'<script>test</script>',repository:'ec',change_id:'I1',revision:'abc',impact_level:'high',created_at:'2026-09-08T00:00:00Z',summary:'<b>untrusted</b>'}],next_after_id:null});await current;
  const card=vm.runInContext('releaseList',h.context).children[0];
  assert.equal(card.children[0].textContent,'<script>test</script>');
  assert.equal(card.children[3].textContent,'<b>untrusted</b>');
  assert.equal(h.calls.length,2);
});

test('mail timestamps show explicit viewing timezone and reject ambiguous input',()=>{
  const h=harness();
  const stamp='2026-09-07T16:26:27.748492+00:00';
  assert.match(h.context.displayInstant(stamp,'Asia/Shanghai'),/2026\/09\/08 00:26:27.*Asia\/Shanghai/);
  assert.match(h.context.displayInstant(stamp,'UTC'),/2026\/09\/07 16:26:27.*UTC/);
  assert.match(h.context.displayInstant('2026-09-07T16:26:27'),/未确认/);
  assert.match(h.context.displayInstant('brokenZ'),/未确认/);
  assert.match(h.context.displayInstant(stamp,'invalid-zone'),/未确认/);
  assert.equal(h.context.displayInstant(null),'无');
});

test('attention resume previews clearing only the subscription snooze',async()=>{
  const h=harness(),read=vm.runInContext('attentionRead',h.context),loading=read.handlers.click();
  h.respond(h.calls[0],{categories:['upstream'],items:[{category:'upstream',revision:2,enabled:1,snooze_until:'future'}]});await settle();h.respond(h.calls[1],{items:[],next_cursor:null});await loading;
  const card=vm.runInContext('attentionSettings',h.context).children[0],resume=card.children.at(-1);
  assert.equal(resume.disabled,false);resume.handlers.click();assert.equal(h.calls.length,2);assert.match(card.children[5].textContent,/不改变邮件自己的稍后/);
  const saving=card.children[4].handlers.click();assert.equal(h.calls[2].body.snooze_minutes,null);assert.equal(h.calls[2].body.expected_revision,2);
  h.respond(h.calls[2],{error:'unknown'},false);await saving;assert.equal(resume.disabled,true);
});

for(const successful of [true,false])test(`attention mail action rereads list without retry (success=${successful})`,async()=>{
  const h=harness(),opening=h.context.openAttention('att');
  h.respond(h.calls[0],{header:{subject:'Synthetic'},message_id:'m',revision:1,content_digest:'bound',effective_state:'todo'});await opening;
  const controls=h.get('mail-list').children[0].children.at(-1),acting=controls.children[1].handlers.click();
  assert.equal(h.calls[1].url,'/api/mail-action');h.respond(h.calls[1],successful?{}:{error:'unknown'},successful);await settle();
  assert.equal(h.calls[2].url,'/api/attention-list');h.respond(h.calls[2],{items:[],next_cursor:null});await acting;
  await controls.children[1].handlers.click();assert.equal(h.calls.length,3);
  assert.match(vm.runInContext('attentionList',h.context).children[0].textContent,/暂无匹配/);
  if(!successful)assert.match(h.get('mail-status').textContent,/不自动重试/);
});

test('attention detail opens exact mail and rejects a late response',async()=>{
  const h=harness(),pending=h.context.openAttention('att-one');
  assert.equal(h.calls[0].url,'/api/attention-detail');assert.equal(h.calls[0].body.action_id,'att-one');
  vm.runInContext('++mailEpoch',h.context);h.respond(h.calls[0],{});await pending;
  assert.notEqual(h.get('mail-status').textContent,'已打开精确关注邮件；下方操作沿用邮件待办规则。');
  const fresh=h.context.openAttention('att-two');h.respond(h.calls[1],{header:{subject:'Exact',category:'upstream'},message_id:'mail-two',effective_state:'todo',revision:0,content_digest:'bound'});await fresh;
  assert.match(h.get('mail-status').textContent,/精确关注邮件/);assert.equal(h.calls.length,2);
});

test('attention subscription previews before saving and does not retry uncertain results',async()=>{
  const h=harness(),read=vm.runInContext('attentionRead',h.context);
  const loading=read.handlers.click();h.respond(h.calls[0],{categories:['upstream'],items:[]});await settle();
  h.respond(h.calls[1],{items:[],next_cursor:null});await loading;
  const settings=vm.runInContext('attentionSettings',h.context),card=settings.children[0];
  const toggle=card.children[2],apply=card.children[4],info=card.children[5];
  assert.equal(apply.disabled,true);toggle.handlers.click();assert.equal(h.calls.length,2);
  const saving=apply.handlers.click();assert.equal(h.calls[2].body.category,'upstream');assert.equal(h.calls[2].body.enabled,true);assert.equal(h.calls[2].body.owner_id,undefined);
  h.respond(h.calls[2],{error:'unknown'},false);await saving;
  assert.match(info.textContent,/不自动重试/);assert.equal(apply.disabled,true);assert.equal(h.calls.length,3);
});

test('profile reset prepares unknown roles and requires separate confirmation',()=>{
  const h=harness(),box=h.context.profileCorrection({requester_id:'p',source:'operator',relationship:'peer',function_role:'qa',content_digest:'bound'});
  const reason=box.children[3],apply=box.children[5],reset=box.children.at(-1);
  reason.value='职位变化，等待刷新';reset.handlers.click();
  assert.equal(apply.disabled,false);assert.equal(h.calls.length,0);
  assert.match(box.children[6].textContent,/清空关系/);
  assert.equal(h.context.profileCorrection({source:'feishu_contact'}).children.at(-1).disabled,true);
});

test('profile correction requires evidence and explicit preview before posting',async()=>{
  const h=harness(),box=h.context.profileCorrection({requester_id:'person',relationship:'peer',function_role:'qa',content_digest:'bound'});
  const [_,relation,role,reason,preview,apply,info]=box.children;
  preview.handlers.click();assert.equal(apply.disabled,true);assert.equal(h.calls.length,0);
  reason.value='本人核实';relation.value='supervisor';preview.handlers.click();assert.match(info.textContent,/平级 → 直属上级/);assert.equal(h.calls.length,0);
  role.handlers.input();assert.equal(apply.disabled,true);
  preview.handlers.click();const pending=apply.handlers.click();assert.equal(h.calls[0].url,'/api/requester-profile-correct');assert.equal(h.calls[0].body.content_digest,'bound');assert.equal(h.calls[0].body.actor_id,undefined);
  h.respond(h.calls[0],{error:'unknown'},false);await pending;assert.match(info.textContent,/不自动重试/);assert.equal(apply.disabled,true);
});

test('cached profile filtering rejects stale results without directory refresh',async()=>{
  const h=harness(),query=vm.runInContext('profileQuery',h.context),list=vm.runInContext('profileList',h.context);
  const pending=h.context.loadProfiles();assert.equal(h.calls[0].url,'/api/requester-profiles');
  query.value='new';query.handlers.input();h.respond(h.calls[0],{items:[{display_name:'stale'}]});await pending;
  assert.equal(list.children.length,0);
  const fresh=h.context.loadProfiles();assert.equal(h.calls[1].body.query,'new');
  h.respond(h.calls[1],{items:[],next_after_id:null,scope:'cached only'});await fresh;
  assert.equal(h.calls.length,2);assert.equal(vm.runInContext('profileMore',h.context).disabled,true);
});

test('scenario form requires clarification and generates without dispatch',()=>{
  const h=harness(),fields=vm.runInContext('scenarioFields',h.context),build=vm.runInContext('scenarioBuild',h.context),input=vm.runInContext('simulationInput',h.context);
  fields.route.value='clarify';fields.question.value='';const before=input.value;build.handlers.click();assert.equal(input.value,before);
  fields.question.value='可以提供启动日志吗？';fields.relationship.value='supervisor';build.handlers.click();
  const generated=JSON.parse(input.value);assert.equal(generated.relationship,'supervisor');assert.equal(generated.proposal.fallback_route,'research');assert.equal(generated.proposal.clarification_question,fields.question.value);
  assert.equal(h.calls.length,0);
});

test('scenario form covers seven routes and preserves advanced JSON until explicit generation',()=>{
  const h=harness(),fields=vm.runInContext('scenarioFields',h.context),build=vm.runInContext('scenarioBuild',h.context),input=vm.runInContext('simulationInput',h.context);
  input.value='advanced edit';fields.query.handlers.input();assert.equal(input.value,'advanced edit');
  for(const route of ['ignore','direct_answer','clarify','research','codex_debug','owner_decision','urgent_notify']){
    fields.route.value=route;fields.question.value='版本是什么？';build.handlers.click();const generated=JSON.parse(input.value);
    assert.equal(generated.proposal.route,route);assert.equal(generated.proposal.reason_codes.length,1);
    assert.equal(generated.proposal.requires_owner_judgment,route==='owner_decision');
    assert.equal(generated.proposal.clarification_question,route==='clarify'?'版本是什么？':null);
  }
  assert.equal(h.calls.length,0);
});

test('policy comparison requires a threshold and ignores edited scenario responses',async()=>{
  const h=harness(),threshold=vm.runInContext('simulationThreshold',h.context),button=vm.runInContext('simulationCompare',h.context),info=vm.runInContext('simulationInfo',h.context);
  threshold.value='';await button.handlers.click();assert.equal(h.calls.length,0);
  threshold.value='0.99';const pending=button.handlers.click();
  assert.equal(h.calls[0].url,'/api/policy-comparison');assert.equal(h.calls[0].body.proposed_minimum_confidence,0.99);
  threshold.handlers.input();h.respond(h.calls[0],{candidate:'stale'});await pending;
  assert.match(info.textContent,/重新比较/);assert.equal(h.calls.length,1);
});

test('policy simulation drops stale replies and never issues actions',async()=>{
  const h=harness(),input=vm.runInContext('simulationInput',h.context),run=vm.runInContext('simulationRun',h.context),info=vm.runInContext('simulationInfo',h.context);
  const first=run.handlers.click();
  assert.equal(h.calls[0].url,'/api/policy-simulation');
  input.handlers.input();h.respond(h.calls[0],{result:{route:'stale'}});await first;
  assert.match(info.textContent,/重新试算/);
  const second=run.handlers.click();h.respond(h.calls[1],{result:{route:'owner_decision'},read_only:true});await second;
  assert.match(info.textContent,/owner_decision/);assert.equal(h.calls.length,2);
  input.value='invalid json';await run.handlers.click();
  assert.match(info.textContent,/试算失败/);assert.equal(h.calls.length,2);
});

test('knowledge import review displays literal old and new content',()=>{
  const h=harness();
  h.context.renderImportReview({changes:[{id:'article',action:'update',previous:{body_markdown:'<script>old</script>',entry_status:'retired'},proposed_revision:2}],entries:[{metadata:{id:'article'},body_markdown:'new'}]});
  const card=vm.runInContext('importReview',h.context).children[0];
  assert.match(card.children[0].textContent,/更新.*article/);
  assert.match(card.children[2].textContent,/<script>old<\/script>/);
  assert.match(card.children[2].textContent,/retired/);
  assert.match(card.children[4].textContent,/new/);
});

test('knowledge import preview is invalidated by edits and never auto-applies',async()=>{
  const h=harness();const input=vm.runInContext('importInput',h.context),preview=vm.runInContext('importPreview',h.context),apply=vm.runInContext('importApply',h.context);
  input.value='{"bundle_digest":"first"}';const pending=preview.handlers.click();
  assert.equal(h.calls[0].url,'/api/knowledge-import-preview');
  input.handlers.input();h.respond(h.calls[0],{draft_id:'stale'});await pending;
  assert.equal(apply.disabled,true);
  const fresh=preview.handlers.click();h.respond(h.calls[1],{draft_id:'fresh',entries:[]});await fresh;
  assert.equal(h.calls.length,2);assert.equal(apply.disabled,false);
  const applying=apply.handlers.click();assert.equal(h.calls[2].body.draft_id,'fresh');
  h.respond(h.calls[2],{error:'unknown'},false);await applying;
  assert.match(vm.runInContext('importInfo',h.context).textContent,/不自动重试/);
  assert.match(vm.runInContext('importInfo',h.context).textContent,/知识包导入.*fresh/);
  assert.equal(apply.disabled,true);
});

test('changed base shows migration warning and never applies on preview',async()=>{
  const h=harness();const reading=vm.runInContext('hoursRead',h.context).handlers.click();
  h.respond(h.calls[0],{revision:1,timezone:'UTC',needs_migration:true,values:{start:'10:00',end:'19:00'}});await reading;
  assert.match(vm.runInContext('hoursInfo',h.context).textContent,/旧覆盖暂停生效/);
  const preview=vm.runInContext('hoursPreview',h.context);
  assert.match(preview.textContent,/迁移/);
  const pending=preview.handlers.click();
  assert.equal(h.calls[1].body.migrate,true);
  h.respond(h.calls[1],{draft_id:'migration',migration:true});await pending;
  assert.equal(h.calls.length,2);
  assert.equal(vm.runInContext('hoursApply',h.context).disabled,false);
});

test('work-hour history rollback only prepares a draft',async()=>{
  const h=harness();const reading=vm.runInContext('hoursRead',h.context).handlers.click();
  h.respond(h.calls[0],{revision:1,timezone:'Asia/Shanghai',values:{start:'10:00',end:'19:00'},history:[{revision:1,previous:{start:'09:00',end:'18:00'},values:{start:'10:00',end:'19:00'},actor_id:'owner',updated_at:'now'}]});await reading;
  const button=vm.runInContext('hoursHistory',h.context).children[0].children.at(-1);
  const preparing=button.handlers.click();
  assert.equal(h.calls[1].body.rollback_revision,1);
  assert.equal(h.calls[1].body.expected_revision,1);
  assert.equal(h.calls[1].body.values,undefined);
  h.respond(h.calls[1],{draft_id:'rollback',proposed:{start:'09:00',end:'18:00'}});await preparing;
  assert.equal(h.calls.length,2);
  assert.equal(vm.runInContext('hoursApply',h.context).disabled,false);
});

test('work hours require preview and edits invalidate an outstanding draft',async()=>{
  const h=harness();const read=vm.runInContext('hoursRead',h.context);
  const loading=read.handlers.click();h.respond(h.calls[0],{revision:0,timezone:'Asia/Shanghai',values:{start:'09:00',end:'18:00'}});await loading;
  const start=vm.runInContext('hoursStart',h.context),preview=vm.runInContext('hoursPreview',h.context),apply=vm.runInContext('hoursApply',h.context);
  start.value='10:00';start.handlers.input();
  const pending=preview.handlers.click();
  assert.equal(h.calls[1].body.expected_revision,0);
  assert.equal(h.calls[1].body.values.start,'10:00');
  start.value='11:00';start.handlers.input();
  h.respond(h.calls[1],{draft_id:'stale'});await pending;
  assert.equal(apply.disabled,true);
  const fresh=preview.handlers.click();h.respond(h.calls[2],{draft_id:'fresh'});await fresh;
  const applying=apply.handlers.click();assert.equal(h.calls[3].body.draft_id,'fresh');
  h.respond(h.calls[3],{error:'unknown'},false);await applying;
  assert.match(vm.runInContext('hoursInfo',h.context).textContent,/不自动重试/);
  assert.equal(h.calls.length,4);
});

test('audit detail links open existing version-bound detail without approving',()=>{
  const h=harness();
  const card=h.context.auditItem({kind:'approval',target:'apr_a',summary:'snapshot'});
  card.children.at(-1).handlers.click();
  assert.equal(h.get('case-dialog').open,true);
  assert.equal(h.calls[0].url,'/api/approval-detail');
  assert.equal(h.calls[0].body.approval_id,'apr_a');
  assert.equal(h.calls.length,1);
});

test('audit filtering drops late responses and displays literal audit text',async()=>{
  const h=harness();const first=h.context.loadAudit();const second=h.context.loadAudit();
  h.respond(h.calls[1],{items:[{summary:'<script>literal</script>',actor_id:'owner',target:'job',kind:'execution',occurred_at:'now'}],total_matching:1,next_cursor:null});await second;
  const list=vm.runInContext('auditList',h.context);const selected=list.children[0];
  h.respond(h.calls[0],{items:[],total_matching:0,next_cursor:null});await first;
  assert.equal(list.children[0],selected);
  assert.equal(h.calls[0].url,'/api/audit');
});

test('execution stop stages distinguish main-process exit from board cleanup',()=>{
  const h=harness();
  const pending=h.context.executionStopProgress({process:'unverified',board_cleanup:'unverified'});
  assert.match(pending.textContent,/进程退出待核验/);
  const exited=h.context.executionStopProgress({process:'main_process_exited',board_cleanup:'unverified'});
  assert.match(exited.textContent,/主进程退出已有回执/);
  assert.match(exited.textContent,/BROM 收尾和租约释放待核验/);
  assert.match(exited.textContent,/不据此保证/);
  const service=h.context.executionStopProgress({process:'unverified',service_process:'service_main_exited',board_cleanup:'unverified'});
  assert.match(service.textContent,/独立服务主进程退出已有管理器证据/);
  assert.match(service.textContent,/进程退出待核验/);
  assert.match(service.textContent,/BROM 收尾和租约释放待核验/);
});

test('execution stop previews exact target and does not retry uncertain cancellation',async()=>{
  const h=harness();const button=h.context.executionStopButton('job_a');
  const action=button.handlers.click();
  assert.equal(h.calls[0].url,'/api/execution-stop-preview');
  h.respond(h.calls[0],{job_id:'job_a',case_id:'case_a',binding_digest:'exact',message:'stop only this execution'});
  await settle();
  assert.equal(h.calls[1].url,'/api/execution-stop');
  assert.equal(h.calls[1].body.binding_digest,'exact');
  h.respond(h.calls[1],{error:'uncertain'},false);await action;
  assert.equal(button.disabled,true);assert.equal(h.calls.length,2);
  assert.match(vm.runInContext('executionInfo',h.context).textContent,/不自动重试/);
});

test('execution recovery confirms exact preview and sends once on uncertain result',async()=>{
  const h=harness(),button=h.context.executionRecoveryButton('job_a');
  const action=button.handlers.click();await button.handlers.click();
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].url,'/api/execution-recovery-preview');
  h.respond(h.calls[0],{job_id:'job_a',case_id:'case_a',binding_digest:'exact',message:'queue only'});
  await settle();
  assert.equal(h.calls[1].url,'/api/execution-recovery');
  assert.equal(h.calls[1].body.binding_digest,'exact');
  h.respond(h.calls[1],{error:'uncertain'},false);await action;
  await button.handlers.click();
  assert.equal(h.calls.length,2);assert.equal(button.disabled,true);
  assert.match(vm.runInContext('executionInfo',h.context).textContent,/不自动重试/);
});

test('execution recovery discards stale preview without applying',async()=>{
  const h=harness(),button=h.context.executionRecoveryButton('old');
  const action=button.handlers.click();
  vm.runInContext('executionEpoch++',h.context);
  h.respond(h.calls[0],{job_id:'old',binding_digest:'stale'});await action;
  assert.equal(h.calls.length,1);
});

test('successful recovery rereads state instead of retaining the old action',async()=>{
  const h=harness(),button=h.context.executionRecoveryButton('job_a');
  const action=button.handlers.click();
  h.respond(h.calls[0],{job_id:'job_a',case_id:'case_a',binding_digest:'exact',message:'queue only'});await settle();
  h.respond(h.calls[1],{accepted:true});await settle();
  assert.equal(h.calls[2].url,'/api/executions');
  h.respond(h.calls[2],{items:[{job_id:'job_a',job_type:'codex',state:'queued',input_recovery_available:0}],
    total_matching:1,observed_at:'now',board:{name:'board1',lease:null}});await action;
  const card=vm.runInContext('executionList',h.context).children[0];
  assert.equal(card.children.at(-1).textContent,'停止本次编码任务');
  assert.ok(!card.children.some(child=>child.textContent==='校验并恢复排队'));
  assert.equal(h.calls.filter(call=>call.url==='/api/execution-recovery').length,1);
});

test('board cleanup status is rendered as text without recovery side effects',async()=>{
  const h=harness(),loading=h.context.loadExecutions();
  h.respond(h.calls[0],{items:[{job_id:'board-job',job_type:'codex',state:'waiting',board_cleanup:[{
    label:'收尾结果待核对',session_id:'<script>unsafe</script>',state:'unknown',lifecycle_round:2,attempt_no:1,updated_at:'now'
  }]}],total_matching:1,observed_at:'now',board:{name:'board1',lease:null}});
  await loading;
  const card=vm.runInContext('executionList',h.context).children[0];
  assert.ok(card.children.some(child=>child.textContent==='收尾结果待核对'));
  assert.ok(card.children.some(child=>child.textContent.includes('<script>unsafe</script>')));
  assert.ok(card.children.some(child=>child.textContent.includes('不代表板卡当前空闲')));
  assert.equal(h.calls.length,1);
});

test('unresolved cleanup remains visible when task filter returns no jobs',async()=>{
  const h=harness(),loading=h.context.loadExecutions();
  h.respond(h.calls[0],{items:[],total_matching:0,observed_at:'now',board:{name:'board1',lease:null,
    cleanup_pending:{total:1,items:[{label:'收尾结果待核对',session_id:'session-1',updated_at:'now',case_id:'case-1'}]}}});
  await loading;
  const board=vm.runInContext('executionBoard',h.context);
  assert.ok(board.children.some(child=>child.textContent.includes('不受任务筛选影响')));
  assert.ok(board.children.some(child=>child.textContent.includes('收尾结果待核对')));
  assert.equal(h.calls.length,1);
});

test('execution recovery is offered only for quarantined input',async()=>{
  const h=harness(),loading=h.context.loadExecutions();
  h.respond(h.calls[0],{items:[{job_id:'bad',job_type:'codex',state:'waiting',input_recovery_available:1},
    {job_id:'ordinary',job_type:'codex',state:'waiting',input_recovery_available:0}],total_matching:2,
    observed_at:'now',board:{name:'board1',lease:null}});await loading;
  const cards=vm.runInContext('executionList',h.context).children;
  assert.equal(cards[0].children.at(-1).textContent,'校验并恢复排队');
  assert.ok(!cards[1].children.some(child=>child.textContent==='校验并恢复排队'));
});

test('retention filter discards stale page and never performs recovery',async()=>{
  const h=harness(),first=h.context.loadRetention(),second=h.context.loadRetention();
  h.respond(h.calls[1],{items:[],total_matching:0,next_cursor:null,note:'read only'});await second;
  h.respond(h.calls[0],{items:[{label:'stale',attempt_id:'old',state:'failed',updated_at:'old'}],total_matching:1,next_cursor:'old',note:'old'});await first;
  assert.equal(vm.runInContext('retentionList',h.context).children[0].textContent,'没有匹配的保留记录');
  assert.equal(vm.runInContext('retentionNext.disabled',h.context),true);
  assert.ok(h.calls.every(call=>call.url==='/api/retention-inventory'));
});

test('purge UI binds preview and confirmation and never retries uncertain execution',async()=>{
  const h=harness(), loading=h.context.loadRetention();
  h.respond(h.calls[0],{items:[{label:'隔离',attempt_id:'attempt-1',state:'quarantined',updated_at:'now'}],total_matching:1});await loading;
  const card=vm.runInContext('retentionList',h.context).children[0];
  const button=card.children.find(child=>child.textContent==='预览永久清理…');
  button.handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/retention-purge-preview');
  h.respond(h.calls[1],{blockers:[],logical_bytes:10,binding_digest:'snapshot'});await settle();
  assert.equal(h.calls[2].url,'/api/retention-purge-prepare');
  assert.equal(h.calls[2].body.binding_digest,'snapshot');
  assert.equal(h.calls[2].body.confirm_permanent_delete,true);
  const request=h.calls[2].body.request_id;
  h.respond(h.calls[2],{request_id:request,state:'prepared'});await settle();
  assert.equal(h.calls[3].url,'/api/retention-purge-execute');
  h.respond(h.calls[3],{error:'unknown'},false);await settle();
  button.handlers.click();await settle();assert.equal(h.calls.length,4);
  assert.match(vm.runInContext('retentionInfo.textContent',h.context),/不自动重试/);
});

test('changing purge grace period discards late preview before confirmation',async()=>{
  const h=harness(), loading=h.context.loadRetention();
  h.respond(h.calls[0],{items:[{label:'隔离',attempt_id:'attempt-1',state:'quarantined',updated_at:'now'}],total_matching:1});await loading;
  const button=vm.runInContext('retentionList',h.context).children[0].children.find(child=>child.textContent==='预览永久清理…');
  button.handlers.click();await settle();
  vm.runInContext('retentionPurgeDays',h.context).handlers.input();
  h.respond(h.calls[1],{blockers:[],logical_bytes:10,binding_digest:'old'});await settle();
  assert.equal(h.calls.length,2);
});

test('purge reconciliation binds observed digest and never retries uncertain completion',async()=>{
  const h=harness(), loading=h.context.loadRetention();
  h.respond(h.calls[0],{items:[{label:'隔离',attempt_id:'a',state:'quarantined',purge_state:'unknown',purge_request_id:'r',updated_at:'now'}],total_matching:1});await loading;
  const card=vm.runInContext('retentionList',h.context).children[0];
  assert.ok(!card.children.some(child=>child.textContent==='预览恢复原件'));
  card.children.find(child=>child.textContent==='核对永久清理（只读）').handlers.click();await settle();
  h.respond(h.calls[1],{request_state:'unknown',source_binding:'quarantine',observation_digest:'observed',observations:{original_path:{state:'absent'},quarantine_path:{state:'absent'}}});await settle();
  const button=card.children.find(child=>child.textContent==='确认文件缺失，修正记录');
  assert.ok(button);assert.equal(h.calls.length,2);
  button.handlers.click();await settle();
  assert.equal(h.calls[2].url,'/api/retention-purge-reconcile');
  assert.deepEqual(h.calls[2].body,{request_id:'r',decision:'confirm_absence',observation_digest:'observed',confirm_observation:true});
  h.respond(h.calls[2],{error:'busy'},false);await settle();
  button.handlers.click();await settle();assert.equal(h.calls.length,3);
  assert.match(vm.runInContext('retentionInfo.textContent',h.context),/不会自动重试/);
});

test('retention restore confirms bound preview and never retries unknown apply',async()=>{
  const h=harness(),loading=h.context.loadRetention();
  h.respond(h.calls[0],{items:[{label:'已隔离',attempt_id:'attempt-1',state:'quarantined',updated_at:'now'}],total_matching:1,next_cursor:null,note:'read only'});await loading;
  const button=vm.runInContext('retentionList',h.context).children[0].children.at(-1);
  button.handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/retention-recovery-preview');
  h.respond(h.calls[1],{attempt_id:'attempt-1',event_pk:'event-1',binding_digest:'bound',summary:'restore'});await settle();
  assert.equal(h.calls[2].url,'/api/retention-recovery-apply');
  assert.equal(h.calls[2].body.binding_digest,'bound');
  h.respond(h.calls[2],{error:'unknown'},false);await settle();
  button.handlers.click();await settle();assert.equal(h.calls.length,3);
  assert.match(vm.runInContext('retentionInfo.textContent',h.context),/不自动重试/);
});

test('unknown retention request offers evidence check instead of another restore',async()=>{
  const h=harness(),loading=h.context.loadRetention();
  h.respond(h.calls[0],{items:[{label:'已恢复',attempt_id:'a',state:'restored',recovery_state:'unknown',recovery_request_id:'request-1',recovery_actor:'owner',updated_at:'now'}],total_matching:1,note:'read only'});await loading;
  const card=vm.runInContext('retentionList',h.context).children[0];
  assert.ok(!card.children.some(child=>child.textContent==='预览恢复原件'));
  card.children.at(-1).handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/retention-recovery-check');
  assert.equal(h.calls[1].body.request_id,'request-1');
  h.respond(h.calls[1],{state:'unknown'});await settle();
  assert.match(vm.runInContext('retentionInfo.textContent',h.context),/没有移动文件/);
  assert.equal(h.calls.length,2);
});

test('execution inventory discards stale pages and routes through existing Case detail',async()=>{
  const h=harness();
  const first=h.context.loadExecutions();
  const second=h.context.loadExecutions();
  const result={items:[{job_id:'job_b',case_id:'case_b',job_type:'codex',state:'running'}],total_matching:1,next_cursor:null,observed_at:'now',board:{name:'board1',lease:null}};
  h.respond(h.calls[1],result);await second;
  const list=vm.runInContext('executionList',h.context);
  const selected=list.children[0];
  h.respond(h.calls[0],{...result,items:[]});await first;
  assert.equal(list.children[0],selected);
  selected.children.at(-1).handlers.click();
  assert.equal(h.get('case-dialog').open,true);
  assert.equal(h.calls[2].url,'/api/case-detail');
  assert.equal(h.calls[2].body.case_id,'case_b');
  const board=vm.runInContext('executionBoard',h.context);
  assert.match(board.children[1].textContent,/实际占用未知/);
});

test('knowledge inventory binds filters and invalidates stale pages on input',async()=>{
  const h=harness();
  const [query,status,search,next,count]=h.get('knowledge-filters').children[0].children;
  query.value='风扇';status.value='candidate';search.handlers.click();
  assert.equal(h.calls[0].body.query,'风扇');
  h.respond(h.calls[0],{items:[{knowledge_id:'knw_a',title:'fan',status:'candidate'}],total_matching:31,next_cursor:'knw_a'});
  await settle();
  assert.equal(next.disabled,false);
  next.handlers.click();
  assert.equal(h.calls[1].body.after_id,'knw_a');
  assert.equal(h.calls[1].body.status,'candidate');
  query.value='EC';query.handlers.input();
  assert.equal(next.disabled,true);
  h.respond(h.calls[1],{items:[],total_matching:0,next_cursor:null});
  await settle();
  assert.match(count.textContent,/筛选已修改/);
  search.handlers.click();
  assert.equal(h.calls[2].body.after_id,'');
  assert.equal(h.calls[2].body.query,'EC');
  h.respond(h.calls[2],{items:[],total_matching:0,next_cursor:null});
  await settle();
  assert.match(count.textContent,/匹配 0 条/);
});

test('status refresh cannot replace active knowledge inventory and failed search clears results',async()=>{
  const h=harness();
  const searching=h.context.loadKnowledge();
  h.respond(h.calls[0],{items:[{knowledge_id:'knw_a',title:'selected',status:'candidate'}],total_matching:1,next_cursor:null});
  await searching;
  const selected=h.get('knowledge-list').children[0];
  h.context.list('knowledge-list',[],'background refresh');
  assert.equal(h.get('knowledge-list').children[0],selected);
  const second=h.context.loadKnowledge();
  h.respond(h.calls[1],{error:'failed'},false);
  await second;
  assert.notEqual(h.get('knowledge-list').children[0],selected);
  assert.match(h.get('knowledge-list').children[0].textContent,/读取失败/);
});

test('knowledge lifecycle binds displayed content and never retries uncertain writes',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const reading=h.context.loadKnowledgeDetail('knw_a');
  h.respond(h.calls[0],{plain_text:'entry',page:1,page_count:1,content_digest:'bound'});
  await reading;
  const button=h.get('case-actions').children[1];
  const action=button.handlers.click();
  assert.equal(h.calls[1].url,'/api/knowledge-lifecycle');
  assert.equal(h.calls[1].body.content_digest,'bound');
  assert.equal(h.calls[1].body.decision,'retired');
  h.respond(h.calls[1],{error:'unknown'},false);await action;
  assert.equal(h.calls.length,2);
  assert.equal(h.get('case-actions').children.length,0);
  assert.match(h.get('case-status').textContent,/不自动重试/);
});

test('knowledge details render plain text and version-bound pages',async()=>{
  const h=harness();
  const card=h.context.knowledgeItem({knowledge_id:'knw_x',title:'entry',status:'candidate'});
  card.children.at(-1).handlers.click();
  assert.equal(h.get('case-dialog').open,true);
  assert.equal(h.calls[0].url,'/api/knowledge-detail');
  h.respond(h.calls[0],{plain_text:'<script>literal</script>',page:1,page_count:2,content_digest:'bound'});
  await settle();
  assert.equal(h.get('case-content').textContent,'<script>literal</script>');
  h.get('case-next').handlers.click();
  assert.equal(h.calls[1].body.content_digest,'bound');
  assert.equal(h.calls[1].body.page,2);
  h.get('case-dialog').close();
  h.respond(h.calls[1],{plain_text:'late',page:2,page_count:2,content_digest:'bound'});
  await settle();
  assert.notEqual(h.get('case-content').textContent,'late');
});

test('knowledge source links are navigation only and titles remain literal',async()=>{
  const h=harness();h.get('case-dialog').showModal();
  const reading=h.context.loadKnowledgeDetail('knw_x');
  h.respond(h.calls[0],{plain_text:'guide',page:1,page_count:1,content_digest:'bound',
    source_links:[{title:'<script>literal</script>',url:'https://example.com/fan'},
                  {title:'unsafe',url:'javascript:alert(1)'}]});
  await reading;
  const sources=h.get('case-actions').children.at(-1);
  assert.equal(sources.children.length,2);
  const link=sources.children[1].children[0];
  assert.equal(link.textContent,'<script>literal</script>');
  assert.equal(link.href,'https://example.com/fan');
  assert.equal(link.rel,'noopener noreferrer');
  assert.equal(link.target,'_blank');
  assert.equal(h.calls.length,1);
});

test('mail meeting review opens the approval dialog before fetching details',async()=>{
  const h=harness();
  const reading=h.context.openMailMeeting('m1');
  h.respond(h.calls[0],{message_id:'m1',revision:1,draft:{summary:'x'},preparation:{approval_id:'apr_x',state:'prepared'}});
  await reading;
  const editor=h.get('mail').children.at(-1);
  editor.children.find(el=>el.textContent==='查看已有审批').handlers.click();
  assert.equal(h.get('case-dialog').open,true);
  assert.equal(h.calls[1].url,'/api/approval-detail');
  h.respond(h.calls[1],{case_id:'case',text:'complete preview',actions:[]});
  await settle();
  assert.match(h.get('case-content').textContent,/complete preview/);
});

test('budget editing previews exact decimals and does not retry uncertain apply',async()=>{
  const h=harness();
  const reading=h.context.loadBudget();
  h.respond(h.calls[0],{configured:false,policy:null});await reading;
  const area=h.get('system').children[0];
  area.children.find(el=>el.textContent==='编辑预算').handlers.click();
  const inputs=area.children.filter(el=>el.handlers.input);
  assert.equal(inputs.length,4);
  ['USD','10','2','1.000001'].forEach((value,index)=>inputs[index].value=value);
  const preview=area.children.find(el=>el.textContent==='预览预算差异');
  const apply=area.children.find(el=>el.textContent==='确认应用预算');
  const previewing=preview.handlers.click();
  assert.equal(h.calls[1].body.values.attempt_limit,'1.000001');
  assert.equal(apply.disabled,true);
  h.respond(h.calls[1],{draft_id:'draft',warning:'review',previous:null,proposed:{}});await previewing;
  const applying=apply.handlers.click();
  assert.equal(h.calls[2].url,'/api/budget-apply');
  assert.equal(h.calls[2].body.draft_id,'draft');
  h.respond(h.calls[2],{error:'unknown'},false);await applying;
  await apply.handlers.click();
  assert.equal(h.calls.length,3);
  assert.equal(apply.disabled,true);
});

test('mail actions bind observed version and do not retry unknown results',async()=>{
  const h=harness();
  const loading=h.context.loadMail();
  h.respond(h.calls[0],{items:[{header:{subject:'fan'},message_id:'m1',revision:2,content_digest:'hash',effective_state:'todo'}],next_cursor:null});
  await loading;
  const card=h.get('mail-list').children[0];
  const controls=card.children.at(-1);
  const action=controls.children[1].handlers.click();
  assert.equal(h.calls[1].url,'/api/mail-action');
  assert.equal(h.calls[1].body.expected_revision,2);
  assert.equal(h.calls[1].body.content_digest,'hash');
  h.respond(h.calls[1],{error:'unknown'},false);
  await action;
  await controls.children[1].handlers.click();
  assert.equal(h.calls.length,2);
  assert.match(h.get('mail-status').textContent,/不自动重试/);
  assert.equal(h.get('mail-reload').disabled,false);
});

test('mail filters reset pagination and discard late responses',async()=>{
  const h=harness();
  const old=h.context.loadMail('old-cursor');
  h.get('mail-category').value='upstream';
  h.get('mail-state').value='todo';
  const fresh=h.context.loadMail();
  assert.equal(h.calls[1].body.after_id,'');
  assert.equal(h.calls[1].body.category,'upstream');
  assert.equal(h.calls[1].body.state,'todo');
  h.respond(h.calls[1],{items:[],next_cursor:null});
  await fresh;
  h.respond(h.calls[0],{items:[],next_cursor:'obsolete'});
  await old;
  assert.equal(h.get('mail-next').disabled,true);
});

test('meeting draft save binds source and never creates a calendar event',async()=>{
  const h=harness();
  const draft={summary:'meeting',description:'',start:'',end:'',timezone:'Asia/Shanghai',attendee_ids:[]};
  const reading=h.context.openMailMeeting('m1');
  h.respond(h.calls[0],{message_id:'m1',draft,revision:0,source_digest:'source'});
  await reading;
  // Dynamically created editor nodes are held by the shipped controller.
  const editor=h.get('mail').children.at(-1);
  const save=editor.children.at(-2);
  const saving=save.handlers.click();
  assert.equal(h.calls[1].url,'/api/mail-meeting-draft-save');
  assert.equal(h.calls[1].body.source_digest,'source');
  assert.deepEqual(h.calls[1].body.draft.attendee_ids,[]);
  h.respond(h.calls[1],{error:'unknown'},false);
  await saving;
  await save.handlers.click();
  assert.equal(h.calls.length,2);
  assert.equal(save.disabled,true);
});

test('preparation cancellation binds exact request and never automatically repeats',async()=>{
  const h=harness();
  const reading=h.context.openMailMeeting('m1');
  h.respond(h.calls[0],{message_id:'m1',revision:1,draft:{summary:'x',description:'',start:'',end:'',timezone:'UTC',attendee_ids:[]},preparation:{request_id:'prepared',binding_digest:'hash',state:'dispatched'}});
  await reading;
  const editor=h.get('mail').children.at(-1);
  const cancel=editor.children.find(el=>el.textContent==='撤销预览准备');
  const cancelling=cancel.handlers.click();
  assert.equal(h.calls[1].url,'/api/mail-meeting-cancel');
  assert.equal(h.calls[1].body.prepare_request_id,'prepared');
  assert.equal(h.calls[1].body.binding_digest,'hash');
  h.respond(h.calls[1],{error:'unknown'},false);
  await cancelling;
  await cancel.handlers.click();
  assert.equal(h.calls.length,2);
  assert.equal(cancel.disabled,true);
});

test('night policy toggle does not clear manual snooze or masquerade as resume',async()=>{
  const h=harness();
  h.context.renderNotifications({revision:2,active:true,manual_active:false,night_enabled:true,until_at:'next morning'});
  assert.equal(h.get('notification-resume').disabled,true);
  assert.equal(h.get('notification-night').textContent,'关闭夜间汇总');
  const setting=h.context.setNotificationSnooze(null,false);
  assert.equal(h.calls[0].body.minutes,null);
  assert.equal(h.calls[0].body.night_enabled,false);
  assert.equal(h.calls[0].body.expected_revision,2);
  h.respond(h.calls[0],{error:'uncertain'},false);await setting;
  assert.equal(h.get('notification-night').disabled,true);
});

test('notification snooze is revision bound and unknown result is not retried',async()=>{
  const h=harness();
  h.context.renderNotifications({revision:3,active:false,until_at:null});
  const action=h.context.setNotificationSnooze(60);
  assert.equal(h.calls[0].url,'/api/notification-snooze');
  assert.equal(h.calls[0].body.expected_revision,3);
  assert.equal(h.calls[0].body.minutes,60);
  assert.match(h.calls[0].body.request_id,/^[a-f0-9-]{36}$/);
  assert.equal(h.get('notification-quiet').disabled,true);
  await h.context.setNotificationSnooze(60);
  assert.equal(h.calls.length,1);
  h.respond(h.calls[0],{error:'unknown'},false);await action;
  assert.equal(h.get('notification-quiet').disabled,true);
  assert.equal(h.get('notification-resume').disabled,true);
  assert.match(h.get('notice').textContent,/刷新核对/);
});

test('base migration shows old values as reference without inheriting enabled switches',async()=>{
  const h=harness();
  const edit=h.context.editFeatures();
  h.respond(h.calls[0],{revision:1,requires_rebase:true,values:{codex:false},historical_values:{codex:true},history:[]});await edit;
  assert.equal(h.get('features-editor').children[0].children[0].checked,false);
  assert.match(h.get('features-status').textContent,/默认不继承/);
  const preview=h.context.previewFeatures();
  assert.deepEqual(h.calls[1].body,{expected_revision:1,rebase:true,values:{codex:false}});
  h.respond(h.calls[1],{draft_id:'migration',rebase:true,base_change:{from:'old-base',to:'new-base'},changes:[],warning:'迁移不替代审批',expires_at:'later'});await preview;
  assert.match(h.get('features-diff').textContent,/old-base → new-base/);
  assert.equal(h.calls.length,2);
  assert.equal(h.get('features-apply').disabled,false);
});

test('feature edits need preview and explicit apply, cancelled late preview stays inert',async()=>{
  const h=harness();
  const edit=h.context.editFeatures();
  h.respond(h.calls[0],{revision:0,values:{codex:false},history:[]});await edit;
  const input=h.get('features-editor').children[0].children[0];
  input.checked=true;input.handlers.change();
  assert.equal(h.calls.length,1);
  const preview=h.context.previewFeatures();
  assert.deepEqual(h.calls[1].body,{expected_revision:0,values:{codex:true}});
  h.respond(h.calls[1],{draft_id:'bound-draft',changes:[{feature:'codex',before:false,after:true}],warning:'审批仍有效',expires_at:'later'});
  await preview;
  assert.equal(h.calls.length,2);
  assert.equal(h.get('features-apply').disabled,false);
  assert.match(h.get('features-diff').textContent,/关闭 → 开启/);
  const applying=h.context.applyFeatures();
  assert.deepEqual(h.calls[2].body,{draft_id:'bound-draft'});
  h.respond(h.calls[2],{error:'结果未知'},false);await applying;
  assert.equal(h.get('features-apply').disabled,true);
  assert.equal(h.calls.length,3);
  const again=h.context.editFeatures();
  h.respond(h.calls[3],{revision:1,values:{codex:true},history:[]});await again;
  const late=h.context.previewFeatures();
  h.context.cancelFeatures();
  h.respond(h.calls[4],{draft_id:'late',changes:[],warning:'late',expires_at:'later'});await late;
  assert.equal(h.get('features-apply').disabled,true);
  assert.equal(h.get('features-diff').textContent,'');
});

test('queue opens detail, displays source as text, and binds next page to snapshot',async()=>{
  const h=harness();
  const item=h.context.queueItem({case_id:'K3-123',title:'启动失败'});
  item.children.at(-1).handlers.click();
  assert.equal(h.get('case-dialog').open,true);
  assert.equal(h.calls[0].url,'/api/case-detail');
  h.respond(h.calls[0],snapshot('<script>untrusted source</script>'));
  await settle();
  assert.equal(h.get('case-content').textContent,'<script>untrusted source</script>');
  assert.equal(h.get('case-prev').disabled,true);
  h.get('case-next').handlers.click();
  assert.equal(h.calls[1].body.page,2);
  assert.equal(h.calls[1].body.content_digest,'1234567890abcdef');
  assert.equal(h.calls[1].body.origin_cursor,'bound-origin');
  h.respond(h.calls[1],{error:'stale'},false);
  await settle();
  assert.equal(h.get('case-content').textContent,'');
  assert.equal(h.get('case-next').disabled,true);
  assert.match(h.get('case-status').textContent,/stale/);
  h.get('case-refresh').handlers.click();
  assert.equal(h.calls[2].body.content_digest,undefined);
  h.respond(h.calls[2],snapshot('fresh'));
  await settle();
  assert.equal(h.get('case-content').textContent,'fresh');
});

test('late response cannot overwrite a newly opened Case or closed dialog',async()=>{
  const h=harness();
  h.get('case-dialog').showModal();
  const old=h.context.loadDetail('K3-old');
  const fresh=h.context.loadDetail('K3-new');
  h.respond(h.calls[1],snapshot('new result'));
  await fresh;
  h.respond(h.calls[0],snapshot('old result'));
  await old;
  assert.equal(h.get('case-content').textContent,'new result');
  const pending=h.context.loadDetail('K3-last');
  h.get('case-dialog').close();
  h.respond(h.calls[2],snapshot('must not appear'));
  await pending;
  assert.equal(h.get('case-content').textContent,'');
  assert.equal(h.get('case-dialog').open,false);
});

test('communication action submits only issued token and fails closed on uncertain response',async()=>{
  const h=harness();
  h.get('case-dialog').showModal();
  const loading=h.context.loadDetail('K3-action');
  h.respond(h.calls[0],{...snapshot('details'),actions:[{label:'我来回复',token:'issued-token'}]});
  await loading;
  const action=h.get('case-actions').children[0].handlers.click();
  assert.equal(h.calls[1].url,'/api/case-action');
  assert.equal(h.calls[1].body.token,'issued-token');
  assert.match(h.calls[1].body.request_id,/^[a-f0-9-]{36}$/);
  assert.equal(h.calls[1].body.user_id,undefined);
  h.respond(h.calls[1],{error:'操作结果未确认'},false);
  await action;
  assert.equal(h.get('case-actions').children.length,0);
  assert.equal(h.get('case-refresh').disabled,false);
  assert.match(h.get('case-status').textContent,/操作结果未确认/);
});

test('lifecycle confirmation displays a new button without automatically executing it',async()=>{
  const h=harness();
  h.get('case-dialog').showModal();
  const loading=h.context.loadDetail('K3-close');
  h.respond(h.calls[0],{...snapshot('details'),actions:[{label:'标记解决',token:'request-close'}]});
  await loading;
  const request=h.get('case-actions').children[0].handlers.click();
  h.respond(h.calls[1],{requires_confirmation:true,message:'确认收尾影响',confirmation:{label:'确认标记解决',token:'confirm-close'}});
  await request;
  assert.equal(h.calls.length,2);
  assert.equal(h.get('case-actions').children[0].textContent,'确认标记解决');
  assert.match(h.get('case-status').textContent,/确认收尾影响/);
  const confirmation=h.get('case-actions').children[0].handlers.click();
  assert.equal(h.calls[2].body.token,'confirm-close');
  assert.notEqual(h.calls[2].body.request_id,h.calls[1].body.request_id);
  h.respond(h.calls[2],{error:'stale'},false);
  await confirmation;
});

test('approval queue opens exact application and uses approval endpoint',async()=>{
  const h=harness();
  const item=h.context.queueItem({case_id:'K3-approval',target_id:'apr_exact',kind:'approval',title:'board1'});
  item.children.at(-1).handlers.click();
  assert.equal(h.calls[0].url,'/api/approval-detail');
  assert.equal(h.calls[0].body.approval_id,'apr_exact');
  h.respond(h.calls[0],{case_id:'K3-approval',text:'完整提交和命令',note:'范围限定',actions:[{token:'approve-exact',label:'同意'}]});
  await settle();
  assert.match(h.get('case-content').textContent,/完整提交和命令/);
  const approval=h.get('case-actions').children[0].handlers.click();
  assert.equal(h.calls[1].url,'/api/approval-action');
  assert.equal(h.calls[1].body.token,'approve-exact');
  h.respond(h.calls[1],{error:'申请已改变'},false);
  await approval;
  assert.equal(h.get('case-actions').children.length,0);
  assert.match(h.get('case-status').textContent,/申请已改变/);
});

test('execution cards show bound coding tool without claiming it started',async()=>{
  const h=harness(),loading=h.context.loadExecutions();
  h.respond(h.calls[0],{items:[{job_id:'job_dsh',job_type:'codex',state:'queued',coding_identity:{binding:'deployment_contract',label:'DeepSeek Harness',model:'bound-model',reasoning:'high'}},{job_id:'job_unknown',job_type:'codex',state:'waiting'}],total_matching:2,observed_at:'now',board:{name:'board1',lease:null}});
  await loading;
  const cards=vm.runInContext('executionList',h.context).children;
  const text=element=>[element.textContent,...element.children.map(text)].join(' ');
  assert.match(text(cards[0]),/DeepSeek Harness/);
  assert.match(text(cards[0]),/bound-model/);
  assert.match(text(cards[0]),/不代表工具已启动/);
  assert.match(text(cards[1]),/执行器身份未确认/);
});


test('coding form retries the same immutable request after uncertain response',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const loading=h.context.openCodingTask('case-1');
  h.respond(h.calls[0],{case_version:7,repositories:['repo'],items:[{id:'tool',status:'configured',label:'Tool',agent:'hermes',model:'model',reasoning:'high',contract_fingerprint:'abc'}]});
  await loading;
  const form=h.get('case-actions').children[0], fields=form.children.slice(0,4).map(label=>label.children[0]);
  fields.forEach((field,index)=>field.value=['repo','tool','Fix bug','Run tests'][index]);
  const submit=form.children.at(-2), send=()=>form.handlers.submit({preventDefault(){}});
  let pending=send();await settle();
  assert.equal(h.calls[1].url,'/api/coding-task');
  assert.equal(h.calls[1].body.case_version,7);
  assert.equal(submit.disabled,true);
  await send();assert.equal(h.calls.length,2);
  h.respond(h.calls[1],{error:'Connection lost'},false);await pending;
  assert.equal(submit.disabled,false);
  assert.ok(fields.every(field=>field.disabled));
  pending=send();await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{job_id:'job-1',state:'queued',created:false});await pending;
  assert.equal(submit.disabled,true);
  assert.match(form.children.at(-3).textContent,/已找到原任务.*job-1/);
});

test('coding choices never overwrite a later detail and empty catalog cannot submit',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const first=h.context.openCodingTask('case-1');
  const second=h.context.openCodingTask('case-2');
  h.respond(h.calls[1],{case_version:2,repositories:[],items:[]});await second;
  const form=h.get('case-actions').children[0];
  assert.equal(form.children.at(-2).disabled,true);
  h.respond(h.calls[0],{case_version:1,repositories:['old'],items:[]});await first;
  assert.equal(h.get('case-actions').children[0],form);
});

test('Bug detail keeps execution, repair and verification separate and renders text',async()=>{
  const h=harness();
  const pending=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{item_id:'123',case_id:'case-1',snapshot:{status_id:'open',observed_at:'now'},rounds:[{number:1,reason:'<script>bad()</script>',execution_state:'succeeded',repair_state:'ready',verification_state:'not_run'}],operations:[],remote_dispatch_available:false});
  await pending;
  const contents=h.get('bug-detail').children.map(c=>c.textContent).join('\n');
  assert.match(contents,/本轮执行成功/);
  assert.match(contents,/未验证/);
  assert.match(contents,/<script>bad\(\)<\/script>/);
  assert.doesNotMatch(contents,/验证通过/);
  assert.equal(h.calls.length,1);
});

test('Bug read coverage distinguishes absent fields and never certifies closure',async()=>{
  const h=harness(),pending=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{item_id:'123',case_id:'case-1',snapshot:{status_id:'CLOSED',observed_at:'now',read_evidence:{status_name:'VERIFIED',unobserved_field_keys:['hidden','empty']}},rounds:[],operations:[]});
  await pending;
  const text=h.get('bug-detail').children.map(c=>c.textContent).join('\n');
  assert.match(text,/2 个定义字段未返回值，不视为空值/);
  assert.match(text,/不代表原子快照/);
  assert.doesNotMatch(text,/验证通过|缺陷已关闭/);
});

test('late Bug response cannot replace another selected Bug',async()=>{
  const h=harness();
  const first=h.context.loadBugDetail('old');
  const second=h.context.loadBugDetail('new');
  const data={case_id:'case',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false};
  h.respond(h.calls[1],{...data,item_id:'new'});await second;
  h.respond(h.calls[0],{...data,item_id:'old'});await first;
  assert.equal(h.get('bug-detail').children[0].textContent,'Bug new');
});

test('Bug list failure clears stale results and disables pagination',async()=>{
  const h=harness();
  const success=h.context.loadBugs();
  h.respond(h.calls[0],{items:[{bug_id:'one',title:'One',project_key:'space',item_id:'1'}],next_cursor:'next'});await success;
  assert.equal(h.get('bugs-next').disabled,false);
  const failure=h.context.loadBugs('next');
  h.respond(h.calls[1],{error:'unavailable'},false);await failure;
  assert.equal(h.get('bugs-next').disabled,true);
  assert.equal(h.get('bugs-list').children.length,0);
});

function bugGrantNodes(element){return [element,...element.children.flatMap(bugGrantNodes)];}
const grantBug={bug_id:'bug-1',item_id:'123',host:'project.feishu.cn',project_key:'space',type_key:'issue',snapshot:{fields:{progress:'old'}}};

function fieldEditorFixture(){return {bug_id:'bug-1',item_id:'123',host:'project.feishu.cn',project_key:'space',type_key:'issue',revision:12,
  snapshot:{snapshot_id:'snapshot-12',fields:{description:'old body',priority:'P2',score:7,attachment:'file-ref',hidden:null},
    read_evidence:{field_names:{description:'Description',priority:'Priority',score:'Score',attachment:'Attachment',hidden:'Hidden'},
      field_types:{description:'text',priority:'text',score:'number',attachment:'file',hidden:'text'},
      unobserved_field_keys:['hidden'],attachment_fields:{attachment:{name:'Attachment',type:'file'}}}}};}

test('Bug field values show official labels without confusing missing and empty',()=>{
  const h=harness(),snapshot={read_evidence:{field_types:{priority:'select'},field_options:{priority:[{value:'1',label:'P1'}]}}};
  assert.equal(h.context.bugFieldValue('priority',{label:'P2',value:'2'},true,snapshot),'P2');
  assert.equal(h.context.bugFieldValue('priority','1',true,snapshot),'P1');
  assert.equal(h.context.bugFieldValue('priority','removed',true,snapshot),'"removed"');
  assert.equal(h.context.bugFieldValue('text',null,true,snapshot),'空值');
  assert.match(h.context.bugFieldValue('text',null,false,snapshot),/不能当作空值/);
});

test('Bug roles render observed people and distinguish unknown from an empty list',()=>{
  const h=harness(),area=h.get('role-members');
  h.context.renderBugRoles({read_evidence:{role_membership:{complete_roster:false,roles:[
    {key:'operator',name:'经办人',members_observed:true,members:[{key:'p1',name:'Test Operator'}]},
    {key:'reviewer',name:'审核人',members_observed:false,members:[]},
    {key:'tester',name:'软件测试',members_observed:true,members:[]}
  ]}}},area);
  const text=descendants(area).map(n=>n.textContent).join('\n');
  assert.match(text,/经办人：Test Operator/);assert.match(text,/审核人：本次未取得完整人员信息/);
  assert.match(text,/软件测试：本次返回人员列表为空/);assert.match(text,/未返回的角色不能推断/);
  assert.equal(h.calls.length,0);
});
function fieldGrant({id='grant-good',host='project.feishu.cn',project='space',type='issue',bugs=['bug-1'],status='active',expires=new Date(Date.now()+3600000).toISOString(),actions=['bug.read','bug.fields'],fields=['description','priority']}={}) {
  return {grant_id:id,status,expires_at:expires,scope:{host,project_key:project,type_key:type,bug_ids:bugs,actions,fields}};
}

test('Bug field editor filters exact active grant scope and only exposes observed text',async()=>{
  const h=harness(),area=h.get('field-editor'),value=fieldEditorFixture();h.context.renderBugFieldEditor(value,0,area);
  const grants=[fieldGrant({id:'wrong-host',host:'elsewhere'}),fieldGrant({id:'wrong-project',project:'other'}),fieldGrant({id:'wrong-type',type:'story'}),
    fieldGrant({id:'wrong-bug',bugs:['other']}),fieldGrant({id:'expired',expires:new Date(Date.now()-1000).toISOString()}),
    fieldGrant({id:'no-action',actions:['bug.read']}),fieldGrant({id:'first-page'})];
  h.respond(h.calls[0],{items:grants,next_cursor:'older-page'});await settle();
  const select=editorField(area,'要修改的字段'),grantSelect=editorField(area,'选择字段授权');
  assert.deepEqual(grantSelect.children.map(option=>option.value),['first-page']);
  assert.deepEqual(select.children.map(option=>option.value),['description','priority']);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/Description · description/);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/Score · score（类型不支持/);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/仍有更早授权/);
  const next=editorButton(area,'读取下一页授权'),page=next.handlers.click();
  assert.deepEqual(h.calls[1].body,{bug_id:'bug-1',after_id:'older-page'});
  h.respond(h.calls[1],{items:[fieldGrant({id:'older-grant',fields:['priority']})],next_cursor:null});await page;
  assert.deepEqual(grantSelect.children.map(option=>option.value),['first-page','older-grant']);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/已读完/);
});

test('Bug field editor searches related items and submits numeric multi IDs with display labels only in the preview',async()=>{
  const h=harness(),area=h.get('related-multi-editor'),value=fieldEditorFixture();
  value.snapshot.fields.related=[{id:7071729349,name:'bianbu-v4.0.6'}];
  value.snapshot.fields.unobservedRelated=null;value.snapshot.fields.omittedRelated=null;
  value.snapshot.read_evidence.field_names.related='Related releases';
  value.snapshot.read_evidence.field_types.related='workitem_related_multi_select';
  value.snapshot.read_evidence.field_types.unobservedRelated='work_item_related_multi_select';
  value.snapshot.read_evidence.field_types.omittedRelated='workitem_related_multi_select';
  value.snapshot.read_evidence.unobserved_field_keys.push('unobservedRelated');
  value.snapshot.read_evidence.omitted_value_field_keys=['omittedRelated'];
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['related','unobservedRelated','omittedRelated']})],next_cursor:null});await settle();
  const fields=editorField(area,'要修改的字段');
  assert.deepEqual(fields.children.map(option=>option.value),['related']);
  editorField(area,'搜索关联项').value='v4.0.7';
  const searching=editorButton(area,'搜索关联项').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/search-field-related');
  assert.deepEqual(h.calls[1].body,{bug_id:'bug-1',grant_id:'grant-good',field_key:'related',query:'v4.0.7'});
  h.respond(h.calls[1],{options:[{value:'8080',label:'bianbu-v4.0.7'}],narrow_query:false});await searching;
  await editorButton(area,'添加 bianbu-v4.0.7 · 8080').handlers.click();
  assert.equal(editorButton(area,'准备此字段写回').disabled,true);
  assert.equal(descendants(area).some(n=>n.textContent.includes('本地字段差异')),false);
  await editorButton(area,'查看字段差异').handlers.click();
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/原值：bianbu-v4.0.6 · 7071729349/);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/新值：bianbu-v4.0.6 · 7071729349、bianbu-v4.0.7 · 8080/);
  const submit=editorButton(area,'准备此字段写回'),request=submit.handlers.click();
  assert.deepEqual(h.calls[2].body.change,{fields:{related:[7071729349,8080]}});
  h.respond(h.calls[2],{operation_id:'related-op'});await settle();
  h.respond(h.calls[3],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await request;
});

test('Bug field editor supports single aliases and explicit null, freezes retries, and enforces the 20-item maximum',async()=>{
  const h=harness(),area=h.get('related-single-editor'),value=fieldEditorFixture();
  value.snapshot.fields.single=null;value.snapshot.fields.multi=Array.from({length:20},(_,i)=>({id:1000+i,name:`release-${i}`}));
  value.snapshot.fields.invalid={id:'not-numeric',name:'opaque'};
  value.snapshot.fields.stringId={id:'7071729349',name:'string IDs are not native observations'};
  value.snapshot.fields.zeroId={id:0,name:'zero is invalid'};
  value.snapshot.fields.missingName={id:42};
  value.snapshot.fields.extraShape={id:43,name:'extra native data',url:'/not accepted'};
  value.snapshot.fields.unsafeId={id:2**53,name:'outside safe integer range'};
  value.snapshot.fields.missingType={id:100,name:'not editable'};
  Object.assign(value.snapshot.read_evidence.field_types,{single:'work_item_related_select',multi:'work_item_related_multi_select',invalid:'workitem_related_select',stringId:'workitem_related_select',zeroId:'workitem_related_select',missingName:'workitem_related_select',extraShape:'workitem_related_select',unsafeId:'workitem_related_select'});
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['single','multi','invalid','stringId','zeroId','missingName','extraShape','unsafeId','missingType']})],next_cursor:null});await settle();
  const fields=editorField(area,'要修改的字段');
  assert.deepEqual(fields.children.map(option=>option.value),['multi','single']);
  fields.value='single';await fields.handlers.change();
  editorField(area,'搜索关联项').value='candidate';
  const searching=editorButton(area,'搜索关联项').handlers.click();
  h.respond(h.calls[1],{options:[{value:'7071729349',label:'bianbu-v4.0.6'}],narrow_query:true});await searching;
  await editorButton(area,'选择 bianbu-v4.0.6 · 7071729349').handlers.click();
  await editorButton(area,'查看字段差异').handlers.click();
  const prepare=editorButton(area,'准备此字段写回'),request=prepare.handlers.click(),frozen=structuredClone(h.calls[2].body);
  assert.deepEqual(frozen.change,{fields:{single:'7071729349'}});
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/新值：bianbu-v4.0.6 · 7071729349/);
  h.respond(h.calls[2],{error:'reply lost'},false);await request;
  assert.equal(editorField(area,'搜索关联项').disabled,true);
  const retry=prepare.handlers.click();assert.deepEqual(h.calls[3].body,frozen);
  h.respond(h.calls[3],{operation_id:'single-op'});await settle();
  h.respond(h.calls[4],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await retry;

  const h2=harness(),area2=h2.get('related-limit-editor'),value2=fieldEditorFixture();
  value2.snapshot.fields.multi=Array.from({length:20},(_,i)=>({id:2000+i,name:`item-${i}`}));
  value2.snapshot.read_evidence.field_types.multi='workitem_related_multi_select';
  h2.context.renderBugFieldEditor(value2,0,area2);
  h2.respond(h2.calls[0],{items:[fieldGrant({fields:['multi']})],next_cursor:null});await settle();
  const lookup=editorButton(area2,'搜索关联项').handlers.click();
  assert.equal(h2.calls.length,2,JSON.stringify({field:editorField(area2,'要修改的字段')?.value,grant:editorField(area2,'选择字段授权')?.value,text:descendants(area2).map(n=>n.textContent).join('\n')}));
  h2.respond(h2.calls[1],{options:[{value:'9999',label:'item-extra'}],narrow_query:false});await lookup;
  await editorButton(area2,'添加 item-extra · 9999').handlers.click();
  assert.match(descendants(area2).map(n=>n.textContent).join('\n'),/最多只能选择 20 个关联项/);
  assert.equal(descendants(area2).filter(n=>n.textContent==='item-extra · 9999').length,0);
});

test('Bug related field search rejects noncanonical IDs and ignores results after field or grant switches',async()=>{
  const h=harness(),area=h.get('related-search-race'),value=fieldEditorFixture();
  value.snapshot.fields.related=[{id:7,name:'observed'}];
  value.snapshot.read_evidence.field_types.related='workitem_related_multi_select';
  value.snapshot.read_evidence.field_types.priority='text';
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({id:'grant-good',fields:['related','priority']}),fieldGrant({id:'grant-second',fields:['related','priority']})],next_cursor:null});await settle();
  const field=editorField(area,'要修改的字段'),grant=editorField(area,'选择字段授权');
  field.value='related';await field.handlers.change();
  const staleByField=editorButton(area,'搜索关联项').handlers.click();
  field.value='priority';await field.handlers.change();field.value='related';await field.handlers.change();
  h.respond(h.calls[1],{options:[{value:'81',label:'stale field result'}],narrow_query:false});await staleByField;
  assert.equal(editorButton(area,'添加 stale field result · 81'),undefined);

  const staleByGrant=editorButton(area,'搜索关联项').handlers.click();
  grant.value='grant-second';await grant.handlers.change();grant.value='grant-good';await grant.handlers.change();
  h.respond(h.calls[2],{options:[{value:'82',label:'stale grant result'}],narrow_query:false});await staleByGrant;
  assert.equal(editorButton(area,'添加 stale grant result · 82'),undefined);

  field.value='related';await field.handlers.change();
  const malformed=editorButton(area,'搜索关联项').handlers.click();
  h.respond(h.calls[3],{options:[{value:'01',label:'leading zero'}],narrow_query:false});await malformed;
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/关联项结果格式无效/);
  assert.equal(editorButton(area,'添加 leading zero · 01'),undefined);
});

test('Bug related multi no-op compares ID sets and treats null like an empty selection',async()=>{
  const h=harness(),area=h.get('related-set-editor'),value=fieldEditorFixture();
  value.snapshot.fields.related=[{id:17,name:'first'},{id:23,name:'second'}];
  value.snapshot.read_evidence.field_types.related='workitem_related_multi_select';
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['related']})],next_cursor:null});await settle();
  await descendants(area).find(e=>e.attributes['aria-label']==='移除 first').handlers.click();
  editorField(area,'搜索关联项').value='first';
  const search=editorButton(area,'搜索关联项').handlers.click();h.respond(h.calls[1],{options:[{value:'17',label:'first'}],narrow_query:false});await search;
  await editorButton(area,'添加 first · 17').handlers.click();
  await editorButton(area,'查看字段差异').handlers.click();
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/关联项未变化，无需写回/);
  assert.equal(editorButton(area,'准备此字段写回').disabled,true);

  const empty=harness(),emptyArea=empty.get('related-null-multi-editor'),emptyValue=fieldEditorFixture();
  emptyValue.snapshot.fields.related=null;emptyValue.snapshot.read_evidence.field_types.related='work_item_related_multi_select';
  empty.context.renderBugFieldEditor(emptyValue,0,emptyArea);
  empty.respond(empty.calls[0],{items:[fieldGrant({fields:['related']})],next_cursor:null});await settle();
  await editorButton(emptyArea,'查看字段差异').handlers.click();
  assert.match(descendants(emptyArea).map(n=>n.textContent).join('\n'),/关联项未变化，无需写回/);
  assert.equal(editorButton(emptyArea,'准备此字段写回').disabled,true);
});

test('Bug related picker disables real array-like HTMLCollection controls',async()=>{
  const h=harness(),area=h.get('related-htmlcollection-editor'),value=fieldEditorFixture();
  value.snapshot.fields.related=[{id:17,name:'first'}];
  value.snapshot.read_evidence.field_types.related='workitem_related_multi_select';
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['related']})],next_cursor:null});await settle();
  editorField(area,'搜索关联项').value='second';
  const lookup=editorButton(area,'搜索关联项').handlers.click();
  h.respond(h.calls[1],{options:[{value:'23',label:'second'}],narrow_query:false});await lookup;
  const chosen=editorField(area,'已选关联项');
  const preview=editorButton(area,'查看字段差异'),prepare=editorButton(area,'准备此字段写回');
  const arrayLike=items=>Object.assign({length:items.length},Object.fromEntries(items.map((item,index)=>[index,item])));
  const replace=chosen.replaceChildren.bind(chosen);
  chosen.replaceChildren=(...items)=>{
    replace(...items);
    const rows=Array.from(chosen.children);
    for(const row of rows)row.children=arrayLike(Array.from(row.children||[]));
    chosen.children=arrayLike(rows);
  };
  const add=editorButton(area,'添加 second · 23');
  await add.handlers.click();
  const remove=chosen.children[1].children[0];
  assert.equal(remove.disabled,false);
  await preview.handlers.click();
  const request=prepare.handlers.click();
  assert.equal(remove.disabled,true);
  h.respond(h.calls[2],{error:'reply lost'},false);await request;
  assert.equal(remove.disabled,true);
  const retry=prepare.handlers.click();
  assert.deepEqual(h.calls[3].body,h.calls[2].body);
  h.respond(h.calls[3],{operation_id:'related-htmlcollection-op'});await settle();
  h.respond(h.calls[4],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await retry;
});

test('Bug field editor backfills explicit null text but rejects enum strings and unknown or omitted values',async()=>{
  const h=harness(),area=h.get('typed-field-editor'),value=fieldEditorFixture();
  Object.assign(value.snapshot.fields,{empty:null,omitted:null,unknown:'opaque'});
  Object.assign(value.snapshot.read_evidence.field_types,{empty:'multi-text',omitted:'text',priority:'select'});
  value.snapshot.read_evidence.omitted_value_field_keys=['omitted'];
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['empty','omitted','unknown','priority','hidden']})],next_cursor:null});await settle();
  assert.deepEqual(editorField(area,'要修改的字段').children.map(option=>option.value),['empty']);
  assert.equal(editorField(area,'新字段值').value,'');
  editorField(area,'新字段值').value='Verified software repair evidence';
  await editorButton(area,'查看字段差异').handlers.click();
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/原值：空值/);
  const request=editorButton(area,'准备此字段写回').handlers.click();
  assert.deepEqual(h.calls[1].body.change,{fields:{empty:'Verified software repair evidence'}});
  h.respond(h.calls[1],{operation_id:'op-typed'});await settle();
  h.respond(h.calls[2],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await request;
});

test('Bug field editor requires a refreshed snapshot when legacy type evidence is missing',async()=>{
  const h=harness(),area=h.get('legacy-field-editor'),value=fieldEditorFixture();
  delete value.snapshot.read_evidence.field_types;
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant()],next_cursor:null});await settle();
  assert.equal(editorField(area,'要修改的字段').children.length,0);
  assert.equal(editorButton(area,'查看字段差异').disabled,true);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/先刷新 Bug/);
});

test('Bug field editor selects official priority IDs and freezes the exact unknown request',async()=>{
  const h=harness(),area=h.get('priority-editor'),value=fieldEditorFixture();
  value.snapshot.fields.priority={label:'P2',value:'2'};
  value.snapshot.read_evidence.field_types.priority='select';
  value.snapshot.read_evidence.field_options={priority:[{value:'2',label:'P2'},{value:'1',label:'P1'}]};
  h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant({fields:['priority']})],next_cursor:null});await settle();
  const input=editorField(area,'新字段值');
  assert.deepEqual(input.children.map(o=>o.value),['','2','1']);
  assert.equal(input.value,'2');
  await editorButton(area,'查看字段差异').handlers.click();
  assert.equal(editorButton(area,'准备此字段写回').disabled,true);
  input.value='1';await input.handlers.change();await editorButton(area,'查看字段差异').handlers.click();
  const submit=editorButton(area,'准备此字段写回'),attempt=submit.handlers.click();
  assert.deepEqual(h.calls[1].body.change,{fields:{priority:'1'}});
  h.respond(h.calls[1],{error:'unknown'},false);await attempt;
  assert.equal(input.disabled,true);
  const retry=submit.handlers.click();assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{operation_id:'priority-op'});await settle();
  h.respond(h.calls[3],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await retry;
});

test('Bug field editor previews and prepares only the selected field after a distinct submit',async()=>{
  const h=harness(),area=h.get('field-editor'),value=fieldEditorFixture();h.context.renderBugFieldEditor(value,0,area);
  h.respond(h.calls[0],{items:[fieldGrant()],next_cursor:null});await settle();
  editorField(area,'要修改的字段').value='priority';editorField(area,'新字段值').value='P1';
  const preview=editorButton(area,'查看字段差异');await preview.handlers.click();
  assert.equal(h.calls.length,1);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/原值："P2"/);
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/新值："P1"/);
  const prepare=editorButton(area,'准备此字段写回'),request=prepare.handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/prepare-write');
  assert.deepEqual(h.calls[1].body,{bug_id:'bug-1',snapshot_id:'snapshot-12',expected_revision:12,grant_id:'grant-good',action:'bug.fields',change:{fields:{priority:'P1'}},request_id:h.calls[1].body.request_id});
  h.respond(h.calls[1],{operation_id:'op-prepared'});await settle();
  assert.equal(h.calls[2].url,'/api/project-bugs/detail');
  h.respond(h.calls[2],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await request;
  assert.equal(h.calls.length,3);
});

test('Bug field preparation retries the exact frozen body after an unknown response',async()=>{
  const h=harness(),area=h.get('field-editor');h.context.renderBugFieldEditor(fieldEditorFixture(),0,area);
  h.respond(h.calls[0],{items:[fieldGrant()],next_cursor:null});await settle();
  editorField(area,'新字段值').value='new';await editorButton(area,'查看字段差异').handlers.click();
  const submit=editorButton(area,'准备此字段写回'),first=submit.handlers.click(),body=structuredClone(h.calls[1].body);
  assert.equal(submit.disabled,true);
  h.respond(h.calls[1],{error:'reply lost'},false);await first;
  assert.equal(submit.disabled,false);assert.ok(editorField(area,'新字段值').disabled);
  const retry=submit.handlers.click();assert.deepEqual(h.calls[2].body,body);
  h.respond(h.calls[2],{operation_id:'op-prepared'});await settle();
  h.respond(h.calls[3],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await retry;
});

test('Bug grant paging invalidates an old preview and blocks preparation while the page is loading',async()=>{
  const h=harness(),area=h.get('field-editor');h.context.renderBugFieldEditor(fieldEditorFixture(),0,area);
  h.respond(h.calls[0],{items:[fieldGrant()],next_cursor:'older'});await settle();
  editorField(area,'新字段值').value='draft';await editorButton(area,'查看字段差异').handlers.click();
  const prepare=editorButton(area,'准备此字段写回'),paging=editorButton(area,'读取下一页授权').handlers.click();
  assert.equal(prepare.disabled,true);
  await prepare.handlers.click();assert.equal(h.calls.length,2);
  h.respond(h.calls[1],{items:[fieldGrant({id:'older',fields:['priority']})],next_cursor:null});await paging;
  assert.equal(prepare.disabled,true);
  assert.equal(h.calls.length,2);
});

test('Bug field editor unlocks known rejection for a fresh preview and fences stale editors',async()=>{
  const h=harness(),area=h.get('field-editor');h.context.renderBugFieldEditor(fieldEditorFixture(),0,area);
  h.respond(h.calls[0],{items:[fieldGrant()],next_cursor:null});await settle();
  editorField(area,'新字段值').value='first';await editorButton(area,'查看字段差异').handlers.click();
  const submit=editorButton(area,'准备此字段写回'),first=submit.handlers.click();
  h.calls[1].resolve({ok:false,status:409,json:async()=>({error:'stale revision'})});await first;
  assert.equal(editorField(area,'新字段值').disabled,false);assert.equal(submit.disabled,true);
  editorField(area,'新字段值').value='corrected';await editorButton(area,'查看字段差异').handlers.click();
  const second=submit.handlers.click();assert.notEqual(h.calls[1].body.request_id,h.calls[2].body.request_id);
  h.respond(h.calls[2],{error:'reply lost'},false);await second;
  h.context.renderBugFieldEditor(fieldEditorFixture(),0,h.get('stale-field-editor'));
  const staleArea=h.get('stale-field-editor');h.respond(h.calls[3],{items:[fieldGrant()],next_cursor:null});await settle();
  const staleSubmit=editorButton(staleArea,'准备此字段写回');
  const load=h.context.loadBugDetail('other');h.respond(h.calls[4],{item_id:'other',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await load;
  await editorButton(staleArea,'查看字段差异').handlers.click();await staleSubmit.handlers.click();
  assert.equal(h.calls.length,5);
});

test('Bug grant creation freezes the exact scope and request ID after an uncertain reply',async()=>{
  const h=harness(),area=h.get('grant-area');
  h.context.renderBugGrants(grantBug,0,area);
  h.respond(h.calls[0],{items:[],next_cursor:null});await new Promise(setImmediate);
  const nodes=bugGrantNodes(area),checkboxes=nodes.filter(n=>n.type==='checkbox');
  assert.equal(checkboxes.length,2);
  checkboxes.forEach(n=>n.checked=true);
  const submit=nodes.find(n=>n.textContent==='创建持续授权');
  const pending=submit.handlers.click();
  const original=structuredClone(h.calls[1].body);
  assert.equal(h.calls[1].url,'/api/project-bugs/issue-grant');
  assert.deepEqual(original.scope.actions,['bug.read','bug.comment','bug.fields']);
  assert.deepEqual(original.scope.bug_ids,['bug-1']);
  assert.deepEqual(original.scope.fields,['progress']);
  assert.deepEqual(original.scope.devices,[]);
  assert.ok(checkboxes.every(n=>n.disabled));
  h.respond(h.calls[1],{error:'reply lost'},false);await pending;
  assert.equal(submit.textContent,'重试同一授权请求');
  checkboxes.forEach(n=>n.checked=false);
  const retry=submit.handlers.click();
  assert.deepEqual(h.calls[2].body,original);
  h.respond(h.calls[2],{grant_id:'grant-1',status:'revoked',expires_at:original.expires_at});await new Promise(setImmediate);
  assert.equal(h.calls[3].url,'/api/project-bugs/list-grants');
  h.respond(h.calls[3],{items:[],next_cursor:null});await retry;
  assert.equal(submit.disabled,true);
  assert.match(bugGrantNodes(area).map(n=>n.textContent).join('\n'),/未触发任何执行/);
  assert.match(bugGrantNodes(area).map(n=>n.textContent).join('\n'),/状态 已撤销/);
});

test('Bug grant revocation uses only its ID and shows unknown outcome without claiming success',async()=>{
  const h=harness(),area=h.get('grant-area');
  h.context.renderBugGrants(grantBug,0,area);
  h.respond(h.calls[0],{items:[{grant_id:'grant-id',status:'active',expires_at:'later',can_revoke:true,
    scope:{...grantBug,bug_ids:['bug-1'],actions:['bug.read'],fields:['<script>literal</script>'],transitions:[],repositories:[],devices:[]}}],next_cursor:null});
  await new Promise(setImmediate);
  const button=bugGrantNodes(area).find(n=>n.textContent==='撤销此授权');
  const pending=button.handlers.click();
  assert.deepEqual(h.calls[1].body,{grant_id:'grant-id'});
  assert.equal(h.calls[1].url,'/api/project-bugs/revoke-grant');
  h.respond(h.calls[1],{error:'timeout'},false);await pending;
  assert.equal(button.disabled,false);
  const text=bugGrantNodes(area).map(n=>n.textContent).join('\n');
  assert.match(text,/撤销结果待核对/);
  assert.match(text,/<script>literal<\/script>/);
});

test('old Bug grant controls cannot act after selecting a different Bug',async()=>{
  const h=harness(),area=h.get('grant-area');
  h.context.renderBugGrants(grantBug,0,area);
  const submit=bugGrantNodes(area).find(n=>n.textContent==='创建持续授权');
  const pending=h.context.loadBugDetail('new');
  h.respond(h.calls[1],{item_id:'new',case_id:'case',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await pending;
  h.respond(h.calls[0],{items:[],next_cursor:null});await new Promise(setImmediate);
  await submit.handlers.click();
  assert.equal(h.calls.length,2);
  assert.equal(h.get('bug-detail').children[0].textContent,'Bug new');
});

test('Bug verification plan shows intent and missing execution evidence',async()=>{
  const h=harness();
  const pending=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{item_id:'123',case_id:'case',snapshot:null,rounds:[],operations:[],verification_plans:[{
    version:2,definition:{title:'<script>literal</script>',repositories:[{repository:'firmware',branch:'test',node:'build',candidate_commit:'abc'}],steps:[{title:'Function',required:true,node:'board-node',environment:'fixture',procedure:'Exercise',oracle:'Signal present'}]}
  }]});
  await pending;
  function textOf(e){return [e.textContent,...e.children.map(textOf)].join('\n');}
  const text=textOf(h.get('bug-detail'));
  assert.match(text,/未验证/);
  assert.match(text,/尚无可信执行证据/);
  assert.match(text,/Signal present/);
  assert.match(text,/<script>literal<\/script>/);
  assert.equal(h.calls.length,1);
});

test('Bug source observations expose mismatch without claiming functional pass',async()=>{
  const h=harness();
  const pending=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{item_id:'123',case_id:'case',snapshot:null,rounds:[],operations:[],verification_plans:[{
    version:1,verification_state:'unknown',definition:{title:'Source check',repositories:[],steps:[]},
    runs:[{step_id:'build',execution_state:'succeeded',receipt:{exit_code:0,finished_at:'now'},source_observations:[{phase:'after',state:'mismatch',sources:[{repository:'firmware',head:'actual-sha',tracked_count:10}]}]}]
  }]});
  await pending;
  function textOf(e){return [e.textContent,...e.children.map(textOf)].join('\n');}
  const text=textOf(h.get('bug-detail'));
  assert.match(text,/执行后源码核对：不一致/);
  assert.match(text,/实际提交 actual-sha/);
  assert.match(text,/不代表功能通过/);
  assert.match(text,/未覆盖忽略文件/);
});

function planEditorFixture() {
  const definition={title:'Regression',repositories:[{id:'repo',repository:'firmware',node:'build',branch:'target',base_commit:'a'.repeat(40),candidate_commit:'b'.repeat(40)}],artifacts:[],devices:[],steps:[{id:'test',title:'Function',layer:'software_test',required:true,depends_on:[],repositories:['repo'],artifacts:[],devices:[],node:'build',environment:'Fixture environment',procedure:'Exercise fixture',oracle:'Expected signal',timeout_seconds:60}]};
  return {bug_id:'bug',item_id:'123',revision:7,rounds:[{round_id:'round',number:1,execution_state:'planned',archived_at:null}],verification_catalog:{repositories:['firmware'],node:'build'},verification_plans:[{round_id:'round',definition}]};
}
function descendants(element){return [element,...element.children.flatMap(descendants)];}
function editorField(area,label){return descendants(area).find(e=>e.attributes['aria-label']===label);}
function editorButton(area,label){return descendants(area).find(e=>e.textContent===label&&e.handlers.click);}

function workflowFixture(){return {...fieldEditorFixture(),snapshot:{...fieldEditorFixture().snapshot,snapshot_id:'workflow-snapshot'}};}
function workflowGrant({id,actions=['bug.read'],transitions=[],fields=[],status='active',expires=new Date(Date.now()+3600000).toISOString(),bugs=['bug-1'],host='project.feishu.cn'}={}){
  return {grant_id:id,status,expires_at:expires,scope:{host,project_key:'space',type_key:'issue',bug_ids:bugs,actions,transitions,fields,repositories:[],devices:[]}};
}
function workflowResponse(options=[{transition_id:'to-progress',target_status_id:'in-progress',target_status_label:'进行中',action:'bug.transition',required_complete:true},{transition_id:'to-close',target_status_id:'closed',target_status_label:'已关闭',action:'bug.close',required_complete:true}]){
  return {snapshot_id:'workflow-snapshot',revision:12,observed_at:'now',current_status:{id:'open',label:'开放'},options,source:'official_current_metadata'};
}

test('Bug workflow reads paginated exact read grants and displays only official options',async()=>{
  const h=harness(),area=h.get('workflow-area'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'wrong-host',host:'elsewhere'}),workflowGrant({id:'read-grant'})],next_cursor:'page-2'});await settle();
  const read=editorButton(area,'读取当前可用流转');assert.equal(read.disabled,true);
  const page=editorButton(area,'读取下一页授权').handlers.click();assert.deepEqual(h.calls[1].body,{bug_id:'bug-1',after_id:'page-2'});
  h.respond(h.calls[1],{items:[workflowGrant({id:'expired',expires:new Date(Date.now()-10000).toISOString()}),workflowGrant({id:'mutation',actions:['bug.read','bug.transition'],transitions:['to-progress']})],next_cursor:null});await page;
  const readRequest=read.handlers.click();assert.equal(h.calls[2].url,'/api/project-bugs/workflow-options');
  assert.deepEqual(h.calls[2].body,{bug_id:'bug-1',grant_id:'read-grant',snapshot_id:'workflow-snapshot',expected_revision:12});
  h.respond(h.calls[2],workflowResponse());await readRequest;
  assert.deepEqual(editorField(area,'选择官方流转').children.map(o=>o.value),['to-progress','to-close']);
  const text=descendants(area).map(n=>n.textContent).join('\n');assert.match(text,/进行中 · to-progress · bug.transition/);assert.match(text,/已关闭 · to-close · bug.close/);
});

test('Bug workflow prepares exact selected transition and freezes the same request after an unknown reply',async()=>{
  const h=harness(),area=h.get('bug-detail'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'write',actions:['bug.transition'],transitions:['to-progress']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse());await read;
  const select=editorField(area,'选择官方流转');select.value='to-progress';select.handlers.change();
  const prepare=editorButton(area,'准备此流转'),first=prepare.handlers.click(),body=structuredClone(h.calls[2].body);
  assert.equal(h.calls[2].url,'/api/project-bugs/prepare-write');
  assert.deepEqual(body,{bug_id:'bug-1',grant_id:'write',snapshot_id:'workflow-snapshot',expected_revision:12,action:'bug.transition',change:{transition_id:'to-progress',target_status_id:'in-progress'},request_id:body.request_id});
  h.respond(h.calls[2],{error:'reply lost'},false);await first;assert.equal(prepare.disabled,true);
  const retry=editorButton(area,'重试同一准备请求').handlers.click();assert.deepEqual(h.calls[3].body,body);
  h.respond(h.calls[3],{operation_id:'prepared'});await settle();
  assert.equal(h.calls[4].url,'/api/project-bugs/detail');
  h.respond(h.calls[4],{item_id:'123',snapshot:null,rounds:[],operations:[],close_approvals:[],verification_plans:[],remote_dispatch_available:false});await retry;
  assert.equal(h.calls.length,5);
});

test('Bug workflow requires explicit exact-scope grant and selected close approval',async()=>{
  const h=harness(),area=h.get('bug-detail'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse());await read;
  const select=editorField(area,'选择官方流转');select.value='to-close';select.handlers.change();
  const authorize=editorButton(area,'授权此流转'),issue=authorize.handlers.click();
  assert.equal(h.calls[2].url,'/api/project-bugs/issue-grant');
  assert.deepEqual(h.calls[2].body.scope,{host:'project.feishu.cn',project_key:'space',type_key:'issue',bug_ids:['bug-1'],actions:['bug.read','bug.close'],fields:[],transitions:['to-close'],repositories:[],devices:[]});
  h.respond(h.calls[2],{grant_id:'close-grant'});await settle();
  assert.equal(h.calls[3].url,'/api/project-bugs/list-grants');h.respond(h.calls[3],{items:[workflowGrant({id:'read'}),workflowGrant({id:'close-write',actions:['bug.read','bug.close'],transitions:['to-close']})],next_cursor:null});await settle();
  assert.equal(editorField(area,'选择官方流转').children.length,0);
  const reread=editorButton(area,'读取当前可用流转').handlers.click();assert.equal(h.calls[4].url,'/api/project-bugs/workflow-options');h.respond(h.calls[4],workflowResponse());await reread;
  editorField(area,'选择官方流转').value='to-close';editorField(area,'选择官方流转').handlers.change();
  const request=editorButton(area,'申请所选关闭审批').handlers.click();assert.equal(h.calls[5].url,'/api/project-bugs/request-close-approval');
  assert.deepEqual(h.calls[5].body.change,{transition_id:'to-close',target_status_id:'closed'});assert.equal(Date.parse(h.calls[5].body.expires_at)>Date.now(),true);
  h.respond(h.calls[5],{approval_id:'approval'});await settle();
  assert.equal(h.calls[6].url,'/api/project-bugs/detail');h.respond(h.calls[6],{item_id:'123',snapshot:null,rounds:[],operations:[],close_approvals:[{approval_id:'approval',status:'requested',can_approve:true,can_deny:true}],verification_plans:[],remote_dispatch_available:false});await request;
  assert.ok(editorButton(area,'批准关闭审批'),descendants(area).map(n=>n.textContent).join('\n'));assert.ok(editorButton(area,'拒绝关闭审批'));
  assert.equal(h.calls.some(call=>call.url==='/api/project-bugs/decide-close-approval'),false);
});

test('Bug workflow disables preparation when official option has missing required items',async()=>{
  const h=harness(),area=h.get('workflow-incomplete'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'write',actions:['bug.transition'],transitions:['to-progress']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse([{transition_id:'to-progress',target_status_id:'in-progress',target_status_label:'进行中',action:'bug.transition',required_complete:false,missing_required:[{key:'description',class:'field'},{key:'hidden',class:'field'}]}]));await read;
  assert.equal(editorButton(area,'准备此流转').disabled,true);
  const text=descendants(area).map(n=>n.textContent).join('\n');assert.match(text,/Description（description）（field）/);assert.match(text,/Hidden（hidden）（field）：本次未读取到值，需重新读取，不能当作空值/);
  await editorButton(area,'准备此流转').handlers.click();assert.equal(h.calls.length,2);
});

test('Bug workflow explains incomplete metadata when the service gives no required-field keys',async()=>{
  const h=harness(),area=h.get('workflow-incomplete-fallback'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse([{transition_id:'to-progress',target_status_id:'in-progress',target_status_label:'进行中',action:'bug.transition',required_complete:false,missing_required:[]}]));await read;
  const text=descendants(area).map(n=>n.textContent).join('\n');assert.match(text,/可能受流程角色或元数据读取范围限制/);assert.match(text,/重新读取详情并确认流程角色/);
});

test('Bug workflow prepares a text required-field intent without treating its old value as empty',async()=>{
  const h=harness(),area=h.get('workflow-required-text'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'field-write',actions:['bug.fields'],fields:['description']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],{...workflowResponse([{transition_id:'to-progress',target_status_id:'in-progress',target_status_label:'进行中',action:'bug.transition',required_complete:false,missing_required:[{key:'description',class:'field'}]}]),writer_user_key:'writer'});await read;
  const input=editorField(area,'补齐必填字段值');assert.ok(input,descendants(area).map(n=>n.textContent).join('\n'));input.value='补齐说明';input.handlers.input();editorButton(area,'预览补齐必填字段').handlers.click();
  assert.match(descendants(area).map(n=>n.textContent).join('\n'),/官方未完成必填，值未观测/);
  const request=editorButton(area,'准备补齐必填字段').handlers.click(),body=h.calls[2].body;
  assert.equal(h.calls[2].url,'/api/project-bugs/prepare-write');assert.deepEqual(body,{bug_id:'bug-1',grant_id:'field-write',snapshot_id:'workflow-snapshot',expected_revision:12,action:'bug.fields',change:{fields:{description:'补齐说明'},required_target_status_id:'in-progress',required_missing_fields:['description']},request_id:body.request_id});
  h.respond(h.calls[2],{operation_id:'prepared'});await settle();
});

test('Bug workflow prepares an official select required-field intent',async()=>{
  const h=harness(),area=h.get('workflow-required-select'),value=workflowFixture();value.snapshot.read_evidence.field_types.priority='select';value.snapshot.read_evidence.field_options={priority:[{value:'p1',label:'P1'}]};h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'field-write',actions:['bug.fields'],fields:['priority']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],{...workflowResponse([{transition_id:'to-progress',target_status_id:'in-progress',target_status_label:'进行中',action:'bug.transition',required_complete:false,missing_required:[{key:'priority',class:'field'}]}]),writer_user_key:'writer'});await read;
  const input=editorField(area,'补齐必填字段值');assert.ok(input,descendants(area).map(n=>n.textContent).join('\n'));input.value='p1';input.handlers.change();editorButton(area,'预览补齐必填字段').handlers.click();
  const request=editorButton(area,'准备补齐必填字段').handlers.click(),body=h.calls[2].body;
  assert.equal(h.calls[2].url,'/api/project-bugs/prepare-write');assert.deepEqual(body.change,{fields:{priority:'p1'},required_target_status_id:'in-progress',required_missing_fields:['priority']});
  h.respond(h.calls[2],{operation_id:'prepared'});await settle();
});

test('Bug workflow invalidates choices and blocks mutations during an authorization refresh',async()=>{
  const h=harness(),area=h.get('workflow-refresh'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'mutation-only',actions:['bug.transition'],transitions:['to-progress']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse());await read;
  const select=editorField(area,'选择官方流转');select.value='to-progress';select.handlers.change();
  const refresh=editorButton(area,'重新读取授权').handlers.click();
  assert.equal(h.calls[2].url,'/api/project-bugs/list-grants');assert.equal(editorField(area,'选择官方流转').children.length,0);
  assert.equal(editorButton(area,'准备此流转').disabled,true);await editorButton(area,'准备此流转').handlers.click();
  assert.equal(h.calls.length,3);
  h.respond(h.calls[2],{items:[workflowGrant({id:'read'}),workflowGrant({id:'mutation-only',actions:['bug.transition'],transitions:['to-progress']})],next_cursor:null});await refresh;
  assert.deepEqual(editorField(area,'选择流转读取授权').children.map(o=>o.value),['read']);
});

test('Bug workflow known write rejection clears choices and requires a fresh official read',async()=>{
  const h=harness(),area=h.get('workflow-reject'),value=workflowFixture();h.context.renderBugWorkflow(value,0,area);
  h.respond(h.calls[0],{items:[workflowGrant({id:'read'}),workflowGrant({id:'mutation-only',actions:['bug.transition'],transitions:['to-progress']})],next_cursor:null});await settle();
  const read=editorButton(area,'读取当前可用流转').handlers.click();h.respond(h.calls[1],workflowResponse());await read;
  editorField(area,'选择官方流转').value='to-progress';editorField(area,'选择官方流转').handlers.change();
  const prepare=editorButton(area,'准备此流转').handlers.click();
  h.calls[2].resolve({ok:false,status:409,json:async()=>({error:'stale'})});await prepare;
  assert.equal(editorField(area,'选择官方流转').children.length,0);assert.equal(editorButton(area,'准备此流转').disabled,true);
  const fresh=editorButton(area,'读取当前可用流转').handlers.click();assert.equal(h.calls[3].url,'/api/project-bugs/workflow-options');h.respond(h.calls[3],workflowResponse());await fresh;
  assert.equal(editorField(area,'选择官方流转').children.length,2);
});

test('verification editor preserves bindings and freezes unknown delivery for exact replay',async()=>{
  const h=harness(),area=h.get('plan-editor'),value=planEditorFixture();
  h.context.renderBugPlanEditor(value,0,area);
  const submit=editorButton(area,'保存验证计划');
  const first=submit.handlers.click();
  assert.equal(h.calls.length,1);
  assert.deepEqual(h.calls[0].body.plan,value.verification_plans[0].definition);
  const original=JSON.parse(JSON.stringify(h.calls[0].body));
  h.respond(h.calls[0],{error:'uncertain'},false);await first;
  assert.equal(editorField(area,'候选完整提交号').disabled,true);
  editorField(area,'候选完整提交号').value='c'.repeat(40);
  const retry=submit.handlers.click();
  assert.deepEqual(h.calls[1].body,original);
  h.respond(h.calls[1],{version:2});await retry;
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/未启动执行/);
  assert.equal(h.calls.length,2);
});

test('verification editor rejects dangling resources before any request',async()=>{
  const h=harness(),area=h.get('plan-editor');h.context.renderBugPlanEditor(planEditorFixture(),0,area);
  editorButton(area,'移除仓库与版本').handlers.click();
  await editorButton(area,'保存验证计划').handlers.click();
  assert.equal(h.calls.length,0);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/仍引用已移除/);
});

test('verification editor unlocks rejected draft but cannot act after navigation',async()=>{
  const h=harness(),area=h.get('plan-editor');h.context.renderBugPlanEditor(planEditorFixture(),0,area);
  const submit=editorButton(area,'保存验证计划'),first=submit.handlers.click();
  h.calls[0].resolve({ok:false,status:409,json:async()=>({error:'stale revision'})});await first;
  assert.equal(editorField(area,'计划名称').disabled,false);
  const pending=h.context.loadBugDetail('other');
  h.respond(h.calls[1],{item_id:'other',snapshot:null,rounds:[],operations:[]});await pending;
  await submit.handlers.click();
  assert.equal(h.calls.length,2);
});

test('verification editor opens next local round without remote reopen',async()=>{
  const h=harness(),area=h.get('plan-editor'),value=planEditorFixture();
  value.rounds[0].execution_state='failed';h.context.renderBugPlanEditor(value,0,area);
  editorField(area,'调查目的').value='Next experiment';
  const pending=editorButton(area,'建立下一轮调查').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/start-round');
  assert.equal(h.calls[0].body.reason,'Next experiment');
  h.respond(h.calls[0],{error:'uncertain'},false);await pending;
  assert.equal(h.calls.length,1);
});

test('verification round editor lets a known rejection be corrected',async()=>{
  const h=harness(),area=h.get('plan-editor'),value=planEditorFixture();
  value.rounds=[];h.context.renderBugPlanEditor(value,0,area);
  const reason=editorField(area,'调查目的');reason.value='First attempt';
  const first=editorButton(area,'建立调查轮次').handlers.click();
  h.calls[0].resolve({ok:false,status:409,json:async()=>({error:'unsettled operation'})});await first;
  assert.equal(reason.disabled,false);reason.value='Corrected attempt';
  const retry=editorButton(area,'建立调查轮次').handlers.click();
  assert.notEqual(h.calls[0].body.request_id,h.calls[1].body.request_id);
  assert.equal(h.calls[1].body.reason,'Corrected attempt');
  h.respond(h.calls[1],{error:'uncertain'},false);await retry;
});

function refreshFixture(){return {bug_id:'bug-1',refresh_control:{available:true,grants:[{grant_id:'read-grant',expires_at:'later'}],requests:[]}};}
test('Project refresh freezes unknown submission and polls only its recorded request',async()=>{
  const h=harness(),area=h.get('refresh-control');await settle();h.get('console').hidden=false;h.context.renderBugRefresh(refreshFixture(),0,area);
  const select=editorField(area,'刷新使用的持续授权'),submit=editorButton(area,'从飞书项目刷新');
  const first=submit.handlers.click();h.respond(h.calls[0],{error:'uncertain'},false);await first;
  select.value='different';const retry=submit.handlers.click();
  assert.deepEqual(h.calls[0].body,h.calls[1].body);
  h.respond(h.calls[1],{refresh_id:'refresh-1',state:'queued',attempt:0});await retry;
  const poll=h.context.pollBugRefresh();
  assert.equal(h.calls[2].url,'/api/project-bugs/refresh-status');
  assert.deepEqual(h.calls[2].body,{refresh_id:'refresh-1'});
  h.respond(h.calls[2],{state:'succeeded',attempt:1});await poll;
  await h.context.pollBugRefresh();assert.equal(h.calls.length,3);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/刷新完成，可重新读取详情查看/);
});

test('Project refresh cannot submit without a grant or overwrite a newly selected Bug',async()=>{
  const h=harness(),area=h.get('refresh-control'),value=refreshFixture();await settle();h.get('console').hidden=false;value.refresh_control.grants=[];
  h.context.renderBugRefresh(value,0,area);await editorButton(area,'从飞书项目刷新').handlers.click();assert.equal(h.calls.length,0);
  value.refresh_control.requests=[{refresh_id:'old',state:'running',attempt:1}];
  h.context.renderBugRefresh(value,0,area);const poll=h.context.pollBugRefresh();
  const next=h.context.loadBugDetail('other');h.respond(h.calls[1],{item_id:'other',snapshot:null,rounds:[],operations:[]});await next;
  const prior=descendants(area).map(x=>x.textContent).join('\n');
  h.respond(h.calls[0],{state:'succeeded',attempt:1});await poll;
  assert.equal(descendants(area).map(x=>x.textContent).join('\n'),prior);
});

test('Project refresh shows expired lease and stops polling behind login',async()=>{
  const h=harness(),area=h.get('refresh-control'),value=refreshFixture();
  value.refresh_control.requests=[{refresh_id:'read',state:'running',attempt:1,lease_expired:true}];
  h.context.renderBugRefresh(value,0,area);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/读取租约已过期，等待后台核对/);
  h.get('console').hidden=true;await h.context.pollBugRefresh();assert.equal(h.calls.length,0);
});

test('Project refresh shows the latest result and folds older attempts',()=>{
  const h=harness(),area=h.get('refresh-history'),value=refreshFixture();
  value.refresh_control.requests=[{refresh_id:'new',state:'succeeded',attempt:1},{refresh_id:'old',state:'failed',attempt:2,error_code:'read_timeout'}];
  h.context.renderBugRefresh(value,0,area);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/查看较早的刷新记录（1 条）/);
  assert.equal(area.children[0].children[2].children[0].textContent,'查看较早的刷新记录（1 条）');
});

test('Bug link intake freezes its exact read authorization across uncertain submission',async()=>{
  const h=harness(),area=h.get('bugs-intake');await settle();h.get('console').hidden=false;h.context.renderBugIntakeForm({available:true,items:[],next_cursor:null},0,area);
  const url=editorField(area,'Bug 详情链接'),duration=editorField(area,'只读授权有效期（从提交起）'),submit=editorButton(area,'只读导入并建立授权');
  url.value='https://project.feishu.cn/k3/issue/detail/123';
  const first=submit.handlers.click();h.respond(h.calls[0],{error:'uncertain'},false);await first;
  url.value='https://other.example/';duration.value='24';const retry=submit.handlers.click();
  assert.deepEqual(h.calls[0].body,h.calls[1].body);assert.equal(h.calls[1].body.read_hours,8);
  h.respond(h.calls[1],{intake_id:'intake-1',state:'queued'});await retry;
  const poll=h.context.pollBugIntake();assert.deepEqual(h.calls[2].body,{intake_id:'intake-1'});
  h.respond(h.calls[2],{intake_id:'intake-1',state:'succeeded',bug_id:'bug-1',reused:1});await poll;
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/复用已有 Bug 记录/);
  assert.ok(editorButton(area,'查看导入的 Bug'));
  await h.context.pollBugIntake();assert.equal(h.calls.length,3);
});

test('disabled or stale Bug intake forms cannot submit and late status stays isolated',async()=>{
  const h=harness(),area=h.get('bugs-intake');await settle();h.get('console').hidden=false;h.context.renderBugIntakeForm({available:false,items:[]},0,area);
  const blocked=editorButton(area,'只读导入并建立授权');await blocked.handlers.click();assert.equal(h.calls.length,0);
  h.context.renderBugIntakeForm({available:true,items:[]},0,area);
  editorField(area,'Bug 详情链接').value='https://project.feishu.cn/k3/issue/detail/123';const old=editorButton(area,'只读导入并建立授权');
  const send=old.handlers.click();h.respond(h.calls[0],{intake_id:'old',state:'running'});await send;
  const poll=h.context.pollBugIntake(),reload=h.context.loadBugIntakes();
  h.respond(h.calls[2],{available:true,items:[],next_cursor:null});await reload;
  const before=descendants(area).map(x=>x.textContent).join('\n');
  h.respond(h.calls[1],{intake_id:'old',state:'succeeded',bug_id:'wrong'});await poll;
  assert.equal(descendants(area).map(x=>x.textContent).join('\n'),before);
  await old.handlers.click();assert.equal(h.calls.length,3);
});

test('known Bug intake scope rejection permits correcting the link with a new request',async()=>{
  const h=harness(),area=h.get('bugs-intake');await settle();h.get('console').hidden=false;
  h.context.renderBugIntakeForm({available:true,items:[]},0,area);
  const url=editorField(area,'Bug 详情链接'),submit=editorButton(area,'只读导入并建立授权');url.value='https://project.feishu.cn/other/issue/detail/123';
  const first=submit.handlers.click();h.calls[0].resolve({ok:false,status:403,json:async()=>({error:'scope mismatch'})});await first;
  assert.equal(url.disabled,false);url.value='https://project.feishu.cn/k3/issue/detail/123';
  const second=submit.handlers.click();assert.notEqual(h.calls[1].body.request_id,h.calls[0].body.request_id);
  h.respond(h.calls[1],{intake_id:'corrected',state:'queued'});await second;
});

for (const predecessor of [null,'previous-job']) test(`Bug coding launch keeps exact investigation binding on uncertain retry ${predecessor||'initial'}`,async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const loading=h.context.openCodingTask('case-1',{bug_id:'bug-1',round_id:'round-1',expected_revision:8,...(predecessor?{predecessor_job_id:predecessor}:{})});
  h.respond(h.calls[0],{case_version:7,repositories:['repo'],source_choices:{repo:{node:'node',deployment_fingerprint:'f'.repeat(64)}},items:[{id:'tool',status:'configured',label:'Tool',agent:'hermes',model:'model',reasoning:'high',contract_fingerprint:'abc'}]});await loading;
  const form=h.get('case-actions').children[0],fields=form.children.slice(0,4).map(label=>label.children[0]);
  fields.forEach((field,index)=>field.value=['repo','tool','Investigate','Run regression'][index]);
  const all=bugGrantNodes(form);all.find(n=>n.attributes?.['aria-label']==='基线分支').value='main';all.find(n=>n.attributes?.['aria-label']==='基线完整提交').value='a'.repeat(40);
  let sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.equal(h.calls[1].url,predecessor?'/api/project-bugs/continue-investigation-job':'/api/project-bugs/create-investigation-job');
  assert.equal(h.calls[1].body.predecessor_job_id,predecessor||undefined);
  assert.equal(h.calls[1].body.round_id,'round-1');assert.equal(h.calls[1].body.expected_revision,8);
  assert.equal(h.calls[1].body.case_id,undefined);
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{created:false,job_id:'job-1',state:'running'});await sending;
});

test('Bug continuation shows only settled unplanned rounds and fences stale clicks',async()=>{
  const h=harness();
  const value={bug_id:'bug-1',revision:8,item_id:'123',case_id:'case-1',snapshot:null,operations:[],
    rounds:[{round_id:'round-1',number:1,execution_state:'succeeded',repair_state:'not_started',verification_state:'not_run',settlement:{ready:true}}],
    investigation_jobs:[{job_id:'parent',round_id:'round-1',agent:'codex',state:'succeeded'}]};
  let loading=h.context.loadBugDetail('bug-1');h.respond(h.calls[0],value);await loading;
  const button=editorButton(h.get('bug-detail'),'接续本轮调查');assert.ok(button);
  loading=h.context.loadBugDetail('bug-2');h.respond(h.calls[1],{bug_id:'bug-2',item_id:'2',case_id:'case-2',snapshot:null,rounds:[],operations:[]});await loading;
  button.handlers.click();assert.equal(h.calls.length,2);
  loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[2],{...value,verification_plans:[{plan_id:'plan',round_id:'round-1',definition:{title:'Existing',repositories:[],steps:[]},version:1}]});await loading;
  assert.equal(editorButton(h.get('bug-detail'),'接续本轮调查'),undefined);
  loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[3],{...value,rounds:[{...value.rounds[0],settlement:{ready:false,blocker_count:1,blockers:[{kind:'resources_unsettled',identity:'parent'}]}}]});await loading;
  assert.equal(editorButton(h.get('bug-detail'),'接续本轮调查'),undefined);
});

test('Bug detail presents investigation job evidence and guards stale launch',async()=>{
  const h=harness();let loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',revision:8,item_id:'123',case_id:'case-1',snapshot:null,
    rounds:[{round_id:'round-1',number:1,execution_state:'running',repair_state:'in_progress',verification_state:'not_run'}],
    operations:[],investigation_jobs:[{job_id:'job-1',agent:'codex',state:'succeeded',source_observations:[{state:'matched',recorded_at:'now'}],checkout_observations:[{source:{path:'/private/job/repository',head:'a'.repeat(40),tracked_content_matches:true}}],checkout_after_observations:[{state:'clean',source:{head:'b'.repeat(40)}},{state:'unavailable',source:null}]}]});await loading;
  const launch=bugGrantNodes(h.get('bug-detail')).find(n=>n.textContent==='发起本轮编码调查');
  assert.ok(launch);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/执行成功仍需核验/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/源仓库基线核验：匹配/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/不证明实际工作副本或修复结果/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/命令开始前工作副本/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/不是命令使用来源或修复通过证明/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/命令结束后工作副本：已跟踪代码与当前提交一致/);
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/命令结束后工作副本：无法完成核验/);
  loading=h.context.loadBugDetail('bug-2');
  h.respond(h.calls[1],{bug_id:'bug-2',item_id:'456',case_id:'case-2',snapshot:null,rounds:[],operations:[]});await loading;
  launch.handlers.click();assert.equal(h.calls.length,2);
});

test('Bug detail separates independent verification jobs from coding investigations',async()=>{
  const h=harness();const loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',item_id:'123',case_id:'case-1',snapshot:null,rounds:[],operations:[],
    investigation_jobs:[{job_id:'repair-job',agent:'opencode',state:'succeeded',purpose:'investigation'},
      {job_id:'test-job',agent:'codex',state:'succeeded',purpose:'verification'}]});await loading;
  const lines=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent);
  assert.ok(lines.some(line=>line.includes('编码调查 repair-job')));
  assert.ok(lines.some(line=>line.includes('独立验证任务 test-job')));
  assert.ok(lines.some(line=>line.includes('命令执行成功仍须复核本步骤证据')));
});

test('Bug baseline form rejects abbreviated commits without submitting',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const loading=h.context.openCodingTask('case-1',{bug_id:'bug',round_id:'round',expected_revision:2});
  h.respond(h.calls[0],{case_version:1,repositories:['repo'],source_choices:{repo:{node:'node',deployment_fingerprint:'f'.repeat(64)}},items:[{id:'tool',status:'configured',label:'Tool',agent:'codex',model:'model',reasoning:'high',contract_fingerprint:'abc'}]});await loading;
  const form=h.get('case-actions').children[0];
  form.children.slice(0,4).forEach((label,index)=>label.children[0].value=['repo','tool','Investigate','Check'][index]);
  editorField(form,'基线分支').value='main';editorField(form,'基线完整提交').value='abcdef0';
  await form.handlers.submit({preventDefault(){}});assert.equal(h.calls.length,1);
  assert.match(bugGrantNodes(form).map(n=>n.textContent).join(' '),/完整的小写提交/);
  editorField(form,'基线完整提交').value='a'.repeat(40);
  let sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[1].body.source,{branch:'main',base_commit:'a'.repeat(40),version:'',node:'node',deployment_fingerprint:'f'.repeat(64)});
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  editorField(form,'基线分支').value='changed';
  sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{created:false,job_id:'job',state:'queued'});await sending;
});

async function openInvestigationDraft(h) {
  const loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',item_id:'123',case_id:'case-1',snapshot:null,rounds:[],operations:[],investigation_jobs:[{job_id:'job-1',agent:'codex',state:'succeeded'}]});
  await loading;
  return bugGrantNodes(h.get('bug-detail')).find(n=>n.textContent==='查看结果草稿 · job-1');
}
test('Bug result draft separates hostile model prose from independent evidence',async()=>{
  const h=harness(),button=await openInvestigationDraft(h);
  const reading=button.handlers.click();await settle();
  assert.deepEqual(h.calls[1].body,{bug_id:'bug-1',job_id:'job-1'});
  assert.equal(h.calls[1].url,'/api/project-bugs/investigation-result');
  h.respond(h.calls[1],{job_id:'job-1',round_id:'round-1',archived:true,execution_state:'succeeded',repair_state:'not_started',verification_state:'not_run',report:{state:'available',artifact_format:'invalid',digest:'abc',sections:{status:'completed',root_cause:'<img src=x onerror=alert(1)>'},truncated_sections:['root_cause']},review:{review_id:'review',state:'stale'},commands:[{request_id:'request',state:'unknown',exit_code:null}],checkout_after:[],evidence_limit:5});await reading;
  const nodes=bugGrantNodes(h.get('bug-detail')),text=nodes.map(n=>n.textContent).join(' ');
  assert.ok(nodes.some(n=>n.textContent==='<img src=x onerror=alert(1)>'));
  assert.match(text,/模型报告是待核验/);assert.match(text,/已归档/);assert.match(text,/产物清单格式不合规/);assert.match(text,/不必重复修改代码/);
  assert.match(text,/前 4000 字符/);assert.match(text,/退出码 未知/);
  assert.match(text,/缺少命令结束后的独立代码采样/);
  assert.equal(button.disabled,false);
});
test('Bug result draft shows complete changeset without promoting a repair verdict',async()=>{
  const h=harness(),button=await openInvestigationDraft(h);
  const reading=button.handlers.click();await settle();
  h.respond(h.calls[1],{job_id:'job-1',round_id:'round-1',archived:true,repair_state:'not_started',report:{state:'unavailable'},commands:[],evidence_limit:5,checkout_after:[{request_id:'req',state:'dirty',source:{head:'actual-head'},changeset:{state:'observed',base_commit:'base',head_commit:'actual-head',commits:['intermediate','actual-head'],paths:['a.py'],patch_text:'<script>untrusted patch</script>',patch_sha256:'digest',tracked_content_matches:false,untracked_paths:['ignored-output']}},{state:'clean',source:{head:'legacy'}}]});await reading;
  const nodes=bugGrantNodes(h.get('bug-detail')),text=nodes.map(n=>n.textContent).join(' ');
  assert.ok(nodes.some(n=>n.textContent==='<script>untrusted patch</script>'));
  for(const pattern of [/intermediate/,/ignored-output/,/未提交/,/不代表修复完成/,/缺少完整变更集/])assert.match(text,pattern);
});
test('Bug result draft ignores responses and clicks after navigation',async()=>{
  const h=harness(),button=await openInvestigationDraft(h);
  const reading=button.handlers.click();await settle();
  await button.handlers.click();assert.equal(h.calls.length,2);
  const loading=h.context.loadBugDetail('bug-2');
  h.respond(h.calls[2],{bug_id:'bug-2',item_id:'456',case_id:'case-2',snapshot:null,rounds:[],operations:[]});await loading;
  h.respond(h.calls[1],{report:{state:'available',sections:{root_cause:'stale marker'}}});await reading;
  await button.handlers.click();assert.equal(h.calls.length,3);
  assert.doesNotMatch(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/stale marker/);
});
test('Bug result draft failure permits a fresh read without writing',async()=>{
  const h=harness(),button=await openInvestigationDraft(h);
  let reading=button.handlers.click();await settle();
  h.respond(h.calls[1],{error:'temporary failure'},false);await reading;
  assert.equal(button.disabled,false);
  reading=button.handlers.click();await settle();
  h.respond(h.calls[2],{job_id:'job-1',round_id:'round-1',report:{state:'unavailable'},commands:[],checkout_after:[],evidence_limit:5});await reading;
  assert.match(bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' '),/完整性核验失败/);
  assert.ok(h.calls.slice(1).every(c=>c.url==='/api/project-bugs/investigation-result'));
});

test('Bug result comment freezes reviewed text and authority across uncertain save',async()=>{
  const h=harness(),area=h.get('comment-test');
  h.context.resultCommentEditor({bug_id:'bug',job_id:'job',snapshot_id:'snapshot',bug_revision:9,report:{digest:'d'.repeat(64),sections:{root_cause:'untrusted <script>'},truncated_sections:['root_cause']}},0,area);
  const all=()=>bugGrantNodes(area),button=label=>all().find(n=>n.textContent===label);
  const text=all().find(n=>n.attributes['aria-label']==='拟写回评论全文');
  assert.match(text.value,/此节已截断/);
  const load=button('读取评论授权').handlers.click();await settle();
  h.respond(h.calls[0],{items:[{grant_id:'expired',status:'expired',scope:{actions:['bug.comment']}},{grant_id:'read-only',status:'active',scope:{actions:['bug.read']}},{grant_id:'grant',status:'active',expires_at:'later',scope:{actions:['bug.comment']}}],next_cursor:null});await load;
  text.value='Reviewed exact text';
  const save=button('保存待写回评论');let sending=save.handlers.click();await settle();
  assert.equal(h.calls[1].url,'/api/project-bugs/prepare-result-comment');
  assert.equal(h.calls[1].body.text,'Reviewed exact text');assert.equal(h.calls[1].body.grant_id,'grant');
  assert.equal(h.calls[1].body.expected_revision,9);assert.equal(h.calls[1].body.result_digest,'d'.repeat(64));
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  text.value='Later edit must not replace frozen request';
  sending=save.handlers.click();await settle();assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{operation_id:'operation',state:'prepared'});await sending;
  assert.match(all().map(n=>n.textContent).join(' '),/尚未发送/);
});
test('Bug result comment grant pagination and stale controls do not stage a write',async()=>{
  const h=harness(),area=h.get('comment-test');
  h.context.resultCommentEditor({bug_id:'bug',job_id:'job',snapshot_id:'snapshot',bug_revision:9,report:{digest:'d',sections:{},truncated_sections:[]}},0,area);
  const nodes=()=>bugGrantNodes(area),more=nodes().find(n=>n.textContent==='读取评论授权'),save=nodes().find(n=>n.textContent==='保存待写回评论');
  let reading=more.handlers.click();await settle();h.respond(h.calls[0],{items:[],next_cursor:'next'});await reading;
  assert.equal(save.disabled,true);reading=more.handlers.click();await settle();assert.equal(h.calls[1].body.after_id,'next');
  const navigation=h.context.loadBugDetail('other');h.respond(h.calls[2],{bug_id:'other',item_id:'other',snapshot:null,rounds:[],operations:[]});await navigation;
  h.respond(h.calls[1],{items:[{grant_id:'late',status:'active',scope:{actions:['bug.comment']}}],next_cursor:null});await reading;
  await save.handlers.click();assert.equal(h.calls.length,3);
});
test('Bug result comment late grant page cannot unlock a frozen save',async()=>{
  const h=harness(),area=h.get('comment-test');
  h.context.resultCommentEditor({bug_id:'bug',job_id:'job',snapshot_id:'snapshot',bug_revision:9,report:{digest:'d',sections:{},truncated_sections:[]}},0,area);
  const nodes=()=>bugGrantNodes(area),more=nodes().find(n=>n.textContent==='读取评论授权'),save=nodes().find(n=>n.textContent==='保存待写回评论');
  let loading=more.handlers.click();await settle();
  h.respond(h.calls[0],{items:[{grant_id:'grant',status:'active',scope:{actions:['bug.comment']}}],next_cursor:'next'});await loading;
  loading=more.handlers.click();await settle();const sending=save.handlers.click();await settle();
  h.respond(h.calls[1],{items:[],next_cursor:null});await loading;
  assert.equal(save.disabled,true);assert.equal(more.disabled,true);
  h.respond(h.calls[2],{operation_id:'operation',state:'prepared'});await sending;
});

const verificationReviewFixture=()=>({run_id:'run',step_id:'step',plan_id:'plan',state:'unknown',evidence_digest:'evidence',review:null,review_available:true,pass_blockers:[],step:{title:'Functional check',layer:'software_test',node:'node',environment:'fixture',procedure:'Exercise fixture',oracle:'Expected signal'},remote:{command:'fixture-test'},bindings:{repositories:[{repository:'repo',candidate_commit:'a'.repeat(40)}]},execution:{receipt:{exit_code:0}}});
async function openVerificationReview(h,value=verificationReviewFixture()) {
  const area=h.get('review-test');h.context.verificationReviewButton('run',0,area);
  const opening=bugGrantNodes(area).find(n=>n.textContent==='核验执行证据 · run').handlers.click();await settle();
  h.respond(h.calls[0],value);await opening;return area;
}
test('verification review pages evidence safely and freezes the operator decision',async()=>{
  const h=harness(),area=await openVerificationReview(h),nodes=()=>bugGrantNodes(area);
  const output=nodes().find(n=>n.textContent==='读取 stdout 证据');let reading=output.handlers.click();await settle();
  assert.deepEqual(h.calls[1].body,{run_id:'run',evidence_digest:'evidence',channel:'stdout',offset:0});
  h.respond(h.calls[1],{text:'<img src=x>',next_offset:11});await reading;
  reading=output.handlers.click();await settle();assert.equal(h.calls[2].body.offset,11);
  h.respond(h.calls[2],{text:'tail',next_offset:null});await reading;
  assert.ok(nodes().some(n=>n.textContent==='<img src=x>tail'));
  const rationale=nodes().find(n=>n.attributes['aria-label']==='核验依据与差异'),verdict=nodes().find(n=>n.attributes['aria-label']==='核验结论'),attest=nodes().find(n=>n.type==='checkbox'),save=nodes().find(n=>n.textContent==='记录人工核验');
  await save.handlers.click();assert.equal(h.calls.length,3);
  rationale.value='Checked actual behavior';verdict.value='passed';attest.checked=true;
  let saving=save.handlers.click();await settle();assert.equal(h.calls[3].url,'/api/project-bugs/record-verification-review');
  h.respond(h.calls[3],{error:'timeout'},false);await saving;
  rationale.value='Different';saving=save.handlers.click();await settle();assert.deepEqual(h.calls[4].body,h.calls[3].body);
  h.respond(h.calls[4],{review_id:'review'});await saving;
  assert.match(nodes().map(n=>n.textContent).join(' '),/不会自动关闭缺陷/);
});
test('verification review disables pass with incomplete source or artifact evidence',async()=>{
  const h=harness(),view=verificationReviewFixture();view.pass_blockers=['source_not_verified','artifact_evidence_unavailable'];
  const area=await openVerificationReview(h,view),nodes=bugGrantNodes(area);
  assert.equal(nodes.find(n=>n.value==='passed').disabled,true);
  assert.match(nodes.map(n=>n.textContent).join(' '),/缺少独立产物证据/);
});
test('verification review ignores stale controls and late log output after navigation',async()=>{
  const h=harness(),area=await openVerificationReview(h),nodes=()=>bugGrantNodes(area);
  const read=nodes().find(n=>n.textContent==='读取 stdout 证据'),save=nodes().find(n=>n.textContent==='记录人工核验');
  const reading=read.handlers.click();await settle();
  const navigation=h.context.loadBugDetail('other');h.respond(h.calls[2],{bug_id:'other',item_id:'other',snapshot:null,rounds:[],operations:[]});await navigation;
  h.respond(h.calls[1],{text:'late evidence',next_offset:null});await reading;
  await save.handlers.click();assert.equal(h.calls.length,3);
  assert.doesNotMatch(nodes().map(n=>n.textContent).join(' '),/late evidence/);
});
test('verification review marks stale evidence unavailable for new decisions',async()=>{
  const h=harness(),view=verificationReviewFixture();view.review_available=false;view.pass_blockers=['obsolete_binding'];
  const area=await openVerificationReview(h,view),nodes=bugGrantNodes(area);
  assert.ok(!nodes.some(n=>n.textContent==='记录人工核验'));
  assert.match(nodes.map(n=>n.textContent).join(' '),/不能记录新的核验结论/);
});

test('completed investigation can save its verification plan without creating a new round',async()=>{
  const h=harness(),area=h.get('plan-editor'),value=planEditorFixture();
  value.rounds[0].execution_state='succeeded';value.rounds[0].settlement={ready:true,blockers:[],blocker_count:0};
  h.context.renderBugPlanEditor(value,0,area);
  assert.ok(editorButton(area,'建立下一轮调查'));
  const saving=editorButton(area,'保存验证计划').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/publish-verification-plan');
  assert.equal(h.calls[0].body.round_id,'round');
  assert.deepEqual(h.calls[0].body.plan,value.verification_plans[0].definition);
  h.respond(h.calls[0],{version:2});await saving;
  assert.equal(h.calls.length,1);
});
test('completed investigation with unsettled resources cannot edit or advance from stale controls',async()=>{
  const h=harness(),area=h.get('plan-editor'),value=planEditorFixture();
  value.rounds[0].execution_state='cancelled';value.rounds[0].settlement={ready:false,blockers:[{kind:'resources_unsettled',identity:'grant'}],blocker_count:1};
  h.context.renderBugPlanEditor(value,0,area);
  const start=editorButton(area,'建立下一轮调查');assert.equal(start.disabled,true);
  editorField(area,'调查目的').value='Must wait';await start.handlers.click();
  assert.equal(h.calls.length,0);assert.equal(editorButton(area,'保存验证计划'),undefined);
  assert.match(descendants(area).map(n=>n.textContent).join(' '),/资源尚未核对完毕/);
});
test('Bug detail distinguishes terminal task status from outstanding execution resources',async()=>{
  const h=harness(),loading=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{bug_id:'bug',item_id:'123',snapshot:null,rounds:[{number:1,execution_state:'succeeded',settlement:{ready:false,blocker_count:1,blockers:[{kind:'launch_unsettled',identity:'launch'}]}}],operations:[]});await loading;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' ');
  assert.match(text,/任务状态本身不能证明资源已经释放/);
  assert.match(text,/执行进程启动或退出待核对 · launch/);
});

test('Bug candidate selection pins previous commit and freezes timeout retry',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const loading=h.context.openCodingTask('case-1',{bug_id:'bug',round_id:'round',expected_revision:2});
  const source={branch:'main',base_commit:'b'.repeat(40),version:'v1',node:'node',deployment_fingerprint:'f'.repeat(64),candidate_request_id:'receipt-1'};
  h.respond(h.calls[0],{case_version:1,repositories:['repo'],source_choices:{repo:{node:'node',deployment_fingerprint:'f'.repeat(64)}},candidate_choices:[{job_id:'previous',request_id:'receipt-1',repository:'repo',source}],items:[{id:'tool',status:'configured',label:'Tool',agent:'codex',model:'model',reasoning:'high',contract_fingerprint:'abc'}]});await loading;
  const form=h.get('case-actions').children[0];
  form.children.slice(0,4).forEach((label,index)=>label.children[0].value=['repo','tool','Continue','Check'][index]);
  const select=editorField(form,'补丁来源');select.value='receipt-1';select.handlers.change();
  assert.equal(editorField(form,'基线完整提交').value,source.base_commit);
  assert.equal(editorField(form,'基线分支').readOnly,true);
  let sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[1].body.source,source);
  assert.equal(select.disabled,true);
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  select.value='';sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);
  h.respond(h.calls[2],{created:false,job_id:'new',state:'queued'});await sending;
});

test('Bug candidate evidence identifies predecessor without promoting verification',async()=>{
  const h=harness();const loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',item_id:'123',case_id:'case-1',snapshot:null,rounds:[],operations:[],investigation_jobs:[{job_id:'job-1',agent:'codex',state:'succeeded',source:{candidate_request_id:'receipt',branch:'main',base_commit:'b'.repeat(40),node:'node'},source_observations:[{state:'matched',recorded_at:'now'}]}]});await loading;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' ');
  assert.match(text,/前序补丁基线核验：匹配/);assert.match(text,/不证明实际工作副本或修复结果/);
});

for(const [state,label] of [['queued','等待准备'],['succeeded','已准备'],['unknown','结果待核对']])test(`verification workspace ${state} is separate from test execution`,async()=>{
  const h=harness();const loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',item_id:'123',case_id:'case-1',snapshot:null,rounds:[],operations:[],verification_plans:[{plan_id:'plan',version:1,verification_state:'unknown',definition:{title:'Test',repositories:[],steps:[]},runs:[{run_id:'run',step_id:'test',execution_state:'queued',workspace_preparation:{state},source_observations:[]}]}]});await loading;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join(' ');
  assert.ok(text.includes('验证副本准备：'+label));assert.match(text,/副本准备完成不代表测试已执行或通过/);assert.match(text,/执行：已排队/);
});

test('independent verification form pins plan candidate and exact command across retry',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const verification={plan_id:'plan',step_id:'step',repository:'repo',branch:'main',candidate_commit:'b'.repeat(40),environment:'fixture',procedure:'test',oracle:'expected'};
  const loading=h.context.openCodingTask('case-1',{bug_id:'bug',round_id:'round',expected_revision:4},verification);
  h.respond(h.calls[0],{case_version:2,repositories:['repo','other'],source_choices:{repo:{node:'node',deployment_fingerprint:'f'.repeat(64)}},items:[{id:'tool',status:'configured',label:'Tool',agent:'codex',model:'model',reasoning:'high',contract_fingerprint:'abc'}]});await loading;
  const form=h.get('case-actions').children[0];
  assert.equal(form.children[0].children[0].children.length,1);
  form.children[0].children[0].value='repo';form.children[1].children[0].value='tool';form.children[2].children[0].value='printf check';
  assert.equal(editorField(form,'基线完整提交').value,'b'.repeat(40));assert.equal(editorField(form,'基线分支').readOnly,true);
  let sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.equal(h.calls[1].url,'/api/project-bugs/create-verification-job');
  assert.deepEqual(h.calls[1].body.verification,{plan_id:'plan',step_id:'step',command:'printf check'});
  assert.equal(h.calls[1].body.source.base_commit,'b'.repeat(40));
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  form.children[2].children[0].value='different';sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);h.respond(h.calls[2],{created:false,job_id:'verifier',state:'queued'});await sending;
});

for(const dependency of [false,true])test(`verification launch from settled finished round respects dependencies ${dependency}`,async()=>{
  const h=harness();let loading=h.context.loadBugDetail('bug-1');
  h.respond(h.calls[0],{bug_id:'bug-1',item_id:'123',case_id:'case-1',revision:4,snapshot:null,rounds:[{round_id:'round',execution_state:'succeeded',settlement:{ready:true}}],operations:[],verification_plans:[{plan_id:'plan',round_id:'round',version:1,verification_state:'not_run',definition:{title:'Test',repositories:[{id:'repo',repository:'firmware',branch:'main',candidate_commit:'b'.repeat(40)}],steps:[{id:'test',title:'Regression',layer:'software_test',repositories:['repo'],artifacts:[],devices:[],depends_on:dependency?['prior']:[],oracle:'expected',procedure:'exercise',environment:'fixture'}]}}]});await loading;
  const launch=bugGrantNodes(h.get('bug-detail')).find(n=>n.textContent==='独立验证：Regression');assert.ok(launch);assert.equal(launch.disabled,dependency);
  if(dependency){launch.handlers.click();assert.equal(h.calls.length,1);return;}
  loading=h.context.loadBugDetail('bug-2');h.respond(h.calls[1],{bug_id:'bug-2',item_id:'456',case_id:'case-2',snapshot:null,rounds:[],operations:[]});await loading;
  launch.handlers.click();assert.equal(h.calls.length,2);
});

const searchOptions=()=>({available:true,scopes:[{simple_name:'k3',type_key:'issue',item_limit:2}],history:[],history_limit:20});
const searchResult=()=>({search_id:'search-1',state:'succeeded',result:{items:[{item_id:'123',title:'<script>literal</script>',status:{key:'custom',label:'Custom'},url:'https://project.feishu.cn/k3/issue/detail/123'}],next_after_id:123}});

test('Bug search freezes intent across uncertain retry and polls only behind login',async()=>{
  const h=harness(),area=h.get('bugs-search');await settle();h.get('console').hidden=false;
  h.context.renderBugSearch(searchOptions(),0,area);
  const keyword=editorField(area,'标题包含（可留空）'),submit=editorButton(area,'只读查询');keyword.value='title';
  const first=submit.handlers.click();h.respond(h.calls[0],{error:'unknown'},false);await first;
  keyword.value='changed';const retry=submit.handlers.click();assert.deepEqual(h.calls[1].body,h.calls[0].body);
  h.respond(h.calls[1],{search_id:'search-1',state:'queued'});await retry;
  h.get('console').hidden=true;await h.context.pollBugSearch();assert.equal(h.calls.length,2);
  h.get('console').hidden=false;const poll=h.context.pollBugSearch();h.respond(h.calls[2],searchResult());await poll;
  assert.ok(descendants(area).some(x=>x.textContent==='<script>literal</script>'));
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/各页是独立读取/);
  await h.context.pollBugSearch();assert.equal(h.calls.length,3);
});

test('Bug search next page carries only server request identity and reuses uncertain intent',async()=>{
  const h=harness(),area=h.get('bugs-search');h.context.showBugSearch(searchResult(),0,area);
  const next=editorButton(area,'按相同条件查询下一页');const first=next.handlers.click();
  assert.deepEqual(Object.keys(h.calls[0].body).sort(),['request_id','search_id']);
  h.respond(h.calls[0],{error:'unknown'},false);await first;
  const retry=next.handlers.click();assert.deepEqual(h.calls[1].body,h.calls[0].body);
  h.respond(h.calls[1],{search_id:'search-2',state:'queued'});await retry;
});

test('Bug search stale polling and stale controls cannot replace a new query form',async()=>{
  const h=harness(),area=h.get('bugs-search');await settle();h.get('console').hidden=false;
  h.context.renderBugSearch(searchOptions(),0,area);const old=editorButton(area,'只读查询');
  const send=old.handlers.click();h.respond(h.calls[0],{search_id:'old',state:'queued'});await send;
  const poll=h.context.pollBugSearch(),reload=h.context.loadBugSearch();
  h.respond(h.calls[2],searchOptions());await reload;
  const before=descendants(area).map(x=>x.textContent).join('\n');h.respond(h.calls[1],searchResult());await poll;
  assert.equal(descendants(area).map(x=>x.textContent).join('\n'),before);
  await old.handlers.click();assert.equal(h.calls.length,3);
});

test('Bug search selection only prefills the separately authorized intake form',async()=>{
  const h=harness(),area=h.get('bugs-search');h.context.showBugSearch(searchResult(),0,area);
  editorButton(area,'填入导入表单').handlers.click();assert.equal(h.calls[0].url,'/api/project-bugs/intakes');
  h.respond(h.calls[0],{available:true,items:[],next_cursor:null});await settle();
  assert.equal(editorField(h.get('bugs-intake'),'Bug 详情链接').value,'https://project.feishu.cn/k3/issue/detail/123');
  assert.equal(h.calls.length,1);
});

test('Bug search disabled configuration and expired pagination do not submit',async()=>{
  const h=harness(),area=h.get('bugs-search');h.context.renderBugSearch({...searchOptions(),available:false},0,area);
  await editorButton(area,'只读查询').handlers.click();assert.equal(h.calls.length,0);
  h.context.showBugSearch({...searchResult(),authorization_expired:true},0,area);
  await editorButton(area,'按相同条件查询下一页').handlers.click();assert.equal(h.calls.length,0);
});

const activityFixture=()=>({bug_id:'bug-1',activity_control:{available:true,grants:[{grant_id:'read-grant',expires_at:'later'}],requests:[],activity_limit:20}});
const activityPage=(kind='comments',activity_id='activity-1')=>({activity_id,kind,offset:0,next_offset:null,observation:{observed_at:'now',end_time_ms:1700000000000,record_count:1},items:kind==='comments'?[{comment_id:'7000000000000000000',content:'<script>do not execute</script>',created_at_display:'2026-06-16 15:26:31',creator:'Creator',attachment_reference:'https://private.invalid/opaque'}]:[{operation_time_ms:1700000000000,action:'modify',operator_type:'user',operator_key:'opaque-user',contents:[{old:['before'],new:['<img src=x>']}]}]});

test('Bug activity submission freezes kind grant and request across uncertain retry',async()=>{
  const h=harness(),area=h.get('activity');h.context.renderBugActivity(activityFixture(),0,area,'comments');
  const submit=editorButton(area,'从飞书项目读取评论');const first=submit.handlers.click();h.respond(h.calls[0],{error:'unknown'},false);await first;
  editorField(area,'评论读取授权').value='other';const retry=submit.handlers.click();assert.deepEqual(h.calls[1].body,h.calls[0].body);
  assert.equal(h.calls[1].body.kind,'comments');assert.equal(h.calls[1].body.grant_id,'read-grant');
  h.respond(h.calls[1],{activity_id:'a',kind:'comments',state:'queued'});await retry;
});

test('Bug activity displays comments literally without fetching attachments or inferring replies',async()=>{
  const h=harness(),area=h.get('activity-page');const load=h.context.loadBugActivityPage('activity-1',0,area,'comments');
  h.respond(h.calls[0],activityPage());await load;
  const text=descendants(area).map(x=>x.textContent).join('\n');
  assert.match(text,/<script>do not execute<\/script>/);assert.match(text,/7000000000000000000/);
  assert.match(text,/未核验楼层回复关系/);assert.match(text,/尚未下载或核验附件/);
  assert.equal(h.calls.length,1);assert.ok(!descendants(area).some(x=>x.href));
});

test('Bug activity keeps history evidence separate from current Bug status',async()=>{
  const h=harness(),area=h.get('activity-page');const load=h.context.loadBugActivityPage('activity-1',0,area,'history');
  h.respond(h.calls[0],activityPage('history'));await load;
  const text=descendants(area).map(x=>x.textContent).join('\n');assert.match(text,/不能证明当前状态、修复完成或验证通过/);assert.match(text,/<img src=x>/);
  assert.equal(h.calls.length,1);
});

test('Bug activity polling supports both readers and remains behind login',async()=>{
  const h=harness(),area=h.get('activity');await settle();h.get('console').hidden=true;
  const value=activityFixture();value.activity_control.requests=[{activity_id:'c',kind:'comments',state:'queued'},{activity_id:'h',kind:'history',state:'queued'}];
  h.context.renderBugActivity(value,0,area,'comments');h.context.renderBugActivity(value,0,area,'history');
  await h.context.pollBugActivity();assert.equal(h.calls.length,0);
  h.get('console').hidden=false;const poll=h.context.pollBugActivity();assert.equal(h.calls.length,2);
  assert.deepEqual(h.calls.map(c=>c.body.activity_id).sort(),['c','h']);
  for(const call of h.calls)h.respond(call,{activity_id:call.body.activity_id,kind:call.body.activity_id==='c'?'comments':'history',state:'blocked',error_code:'authorization_or_reader_changed'});
  await poll;await h.context.pollBugActivity();assert.equal(h.calls.length,2);
});

test('Bug activity older local pages cannot replace a newer observation or another Bug',async()=>{
  const h=harness(),area=h.get('activity-page');
  const old=h.context.loadBugActivityPage('old',0,area,'comments'),fresh=h.context.loadBugActivityPage('fresh',0,area,'comments');
  h.respond(h.calls[1],activityPage('comments','fresh'));await fresh;
  const before=descendants(area).map(x=>x.textContent).join('\n');h.respond(h.calls[0],{...activityPage('comments','old'),items:[]});await old;
  assert.equal(descendants(area).map(x=>x.textContent).join('\n'),before);
  const pending=h.context.loadBugActivityPage('fresh',0,area,'comments');vm.runInContext('++bugDetailEpoch',h.context);
  h.respond(h.calls[2],activityPage('comments','fresh'));await pending;
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/正在读取留存/);
});

test('Bug activity disabled grants and stale forms cannot submit',async()=>{
  const h=harness(),area=h.get('activity');const value=activityFixture();value.activity_control.grants=[];
  h.context.renderBugActivity(value,0,area,'history');await editorButton(area,'从飞书项目读取操作记录').handlers.click();assert.equal(h.calls.length,0);
  h.context.renderBugActivity(activityFixture(),0,area,'comments');const old=editorButton(area,'从飞书项目读取评论');vm.runInContext('++bugDetailEpoch',h.context);await old.handlers.click();assert.equal(h.calls.length,0);
});

const relationActivityPage=()=>({activity_id:'relations-1',kind:'relations',offset:0,next_offset:null,observation:{observed_at:'now',end_time_ms:null,record_count:3,definition_count:3,target_count:1},items:[
  {relation_name:'发现版本',state:'linked',target:{name:'<script>release</script>',type_name:'软件版本',project_name:'K3',item_id:'9007199254740993'}},
  {relation_name:'解决版本',state:'empty',target:null},{relation_name:'旧关联',state:'disabled',target:null}]});

test('Bug relations show official roles and distinguish absent from disabled without graph expansion',async()=>{
  const h=harness(),area=h.get('relations');const load=h.context.loadBugActivityPage('relations-1',0,area,'relations');
  h.respond(h.calls[0],relationActivityPage());await load;
  const text=descendants(area).map(x=>x.textContent).join('\n');
  assert.match(text,/发现版本/);assert.match(text,/解决版本/);assert.match(text,/关系已停用，未读取关联对象/);
  assert.match(text,/本次读取未发现该关系下的关联对象/);assert.match(text,/不证明实际测试版本或验证通过/);
  assert.match(text,/<script>release<\/script>/);assert.match(text,/9007199254740993/);assert.doesNotMatch(text,/查询截止/);
  assert.equal(h.calls.length,1);assert.ok(!descendants(area).some(x=>x.href));
});

test('Bug relation read uses the existing source Bug grant and exact activity kind',async()=>{
  const h=harness(),area=h.get('relations');h.context.renderBugActivity(activityFixture(),0,area,'relations');
  const sending=editorButton(area,'从飞书项目读取关系与版本').handlers.click();
  assert.equal(h.calls[0].body.kind,'relations');assert.equal(h.calls[0].body.bug_id,'bug-1');assert.equal(h.calls[0].body.grant_id,'read-grant');
  assert.equal('relation_id' in h.calls[0].body,false);
  h.respond(h.calls[0],{activity_id:'relations-1',kind:'relations',state:'queued'});await sending;
});

test('Bug relation local pagination retains observation identity and suppresses old page controls',async()=>{
  const h=harness(),area=h.get('relations');const first=h.context.loadBugActivityPage('relations-1',0,area,'relations');
  h.respond(h.calls[0],{...relationActivityPage(),next_offset:20});await first;
  const next=editorButton(area,'下一页关系与版本');next.handlers.click();assert.deepEqual(h.calls[1].body,{activity_id:'relations-1',offset:20});
  h.respond(h.calls[1],{...relationActivityPage(),offset:20,items:[]});await settle();
  await next.handlers.click();assert.equal(h.calls.length,2);
});


test('Bug attachments preserve display size and unknown fields without rendering active links',async()=>{
  const h=harness(),area=h.get('attachments');
  const pending=h.context.loadBugActivityPage('attachments-1',0,area,'attachments');
  h.respond(h.calls[0],{activity_id:'attachments-1',kind:'attachments',offset:0,next_offset:null,
    observation:{observed_at:'now',field_count:2,attachment_count:1},items:[
      {field_name:'附件',state:'listed',name:'<script>bad.zip</script>',size_display:'67.6MB',media_type_display:'application/zip'},
      {field_name:'patch文件',state:'unobserved'}]});await pending;
  const text=descendants(area).map(x=>x.textContent).join('\n');
  assert.match(text,/67.6MB/);assert.match(text,/<script>bad.zip<\/script>/);
  assert.match(text,/附件情况未知/);assert.match(text,/尚未下载/);
  assert.equal(descendants(area).filter(x=>x.tagName==='A').length,0);
});

test('Bug attachment inventory queues only the scoped source Bug intent',async()=>{
  const h=harness(),area=h.get('attachments');h.context.renderBugActivity(activityFixture(),0,area,'attachments');
  const sending=editorButton(area,'从飞书项目读取附件').handlers.click();
  assert.equal(h.calls[0].body.kind,'attachments');assert.equal(h.calls[0].body.grant_id,'read-grant');
  assert.equal('url' in h.calls[0].body,false);
  h.respond(h.calls[0],{activity_id:'attachments-1',kind:'attachments',state:'queued'});await sending;
});


test('Bug attachment download sends the frozen observed member and retries the same request',async()=>{
  const h=harness(),area=h.get('files');area.attachmentGrant={value:'read-grant'};area.attachmentAvailable=true;
  const load=h.context.loadBugActivityPage('inventory-1',0,area,'attachments');
  h.respond(h.calls[0],{activity_id:'inventory-1',kind:'attachments',offset:0,next_offset:null,
    observation:{observed_at:'now',field_count:1,attachment_count:1},items:[{state:'listed',field_name:'Files',field_key:'file-field',source_digest:'digest',name:'file.zip',size_display:'67.6MB',media_type_display:'zip',has_source_reference:true}]});await load;
  const button=editorButton(area,'获取并校验此附件');const first=button.handlers.click();
  const body=h.calls[1].body;assert.equal(body.activity_id,'inventory-1');assert.equal(body.source_digest,'digest');assert.equal(body.grant_id,'read-grant');assert.equal('url' in body,false);
  h.respond(h.calls[1],{error:'unknown outcome'},false);await first;
  const retry=button.handlers.click();assert.deepEqual(h.calls[2].body,body);
  h.respond(h.calls[2],{activity_id:'download-1',kind:'attachment_download',state:'queued'});await retry;
});

test('Bug attachment download receipt remains file evidence and restores pending progress',async()=>{
  const h=harness(),area=h.get('downloads');const value=activityFixture();
  value.activity_control.requests=[{activity_id:'download-1',kind:'attachment_download',state:'running'}];
  h.context.renderBugDownloads(value,0,area);const pending=h.context.pollBugActivity();
  assert.equal(h.calls[0].body.activity_id,'download-1');h.respond(h.calls[0],{activity_id:'download-1',kind:'attachment_download',state:'blocked'});await pending;
});

test('Bug attachment download receipt shows byte hash and does not imply repaired',async()=>{
  const h=harness(),area=h.get('receipt');const load=h.context.loadBugActivityPage('download-1',0,area,'attachment_download');
  h.respond(h.calls[0],{activity_id:'download-1',kind:'attachment_download',offset:0,next_offset:null,
    observation:{observed_at:'now'},items:[{name:'file.zip',size_bytes:5,parts:2,sha256:'abc'}]});await load;
  const text=descendants(area).map(x=>x.textContent).join('\n');assert.match(text,/SHA-256：abc/);assert.match(text,/未执行、解压或验证修复效果/);
  assert.ok(editorButton(area,'保存已校验文件'));
});

const reconcileOperation=()=>({operation_id:'operation-1',action:'bug.comment',write_digest:'frozen-write',state:'unknown',can_reconcile:true});
test('Bug comment reconciliation sends exact write identity and current read grant without a write action',async()=>{
  const h=harness(),card=h.get('reconcile');h.context.renderCommentReconcile(activityFixture(),reconcileOperation(),0,card);
  const button=editorButton(card,'只读核对这次评论写入'),pending=button.handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/comment-reconcile');const body=h.calls[0].body;
  assert.equal(body.operation_id,'operation-1');assert.equal(body.write_digest,'frozen-write');assert.equal(body.grant_id,'read-grant');assert.equal('text' in body,false);
  h.respond(h.calls[0],{error:'unknown submission'},false);await pending;
  const retry=button.handlers.click();assert.deepEqual(h.calls[1].body,body);
  h.respond(h.calls[1],{activity_id:'reconcile-1',kind:'comment_reconcile',state:'queued'});await retry;
});

test('Bug comment reconciliation completed query does not label an unknown write as confirmed',async()=>{
  const h=harness(),area=h.get('reconcile-result');const loading=h.context.loadBugActivityPage('reconcile-1',0,area,'comment_reconcile');
  h.respond(h.calls[0],{activity_id:'reconcile-1',kind:'comment_reconcile',offset:0,next_offset:null,observation:{observed_at:'now',write_performed:false},items:[{operation_id:'operation-1',state:'unknown',receipt_state:'missing',result:{outcome:'unknown'}}]});await loading;
  const text=descendants(area).map(x=>x.textContent).join('\n');assert.match(text,/评论写入仍未确认/);assert.match(text,/不会自动补发/);assert.doesNotMatch(text,/官方分页已读完/);
});

test('Bug comment reconciliation restores its pending read and stale forms cannot submit',async()=>{
  const h=harness(),card=h.get('reconcile');const value=activityFixture();value.activity_control.requests=[{activity_id:'reconcile-1',kind:'comment_reconcile',state:'running',source:{operation_id:'operation-1'}}];
  h.context.renderCommentReconcile(value,reconcileOperation(),0,card);assert.equal(editorButton(card,'只读核对这次评论写入').disabled,true);
  const poll=h.context.pollBugActivity();assert.equal(h.calls[0].body.activity_id,'reconcile-1');h.respond(h.calls[0],{activity_id:'reconcile-1',kind:'comment_reconcile',state:'blocked'});await poll;
  const fresh=h.get('fresh');h.context.renderCommentReconcile(activityFixture(),reconcileOperation(),0,fresh);vm.runInContext('++bugDetailEpoch',h.context);await editorButton(fresh,'只读核对这次评论写入').handlers.click();assert.equal(h.calls.length,1);
});

const commentSendOperation=()=>({operation_id:'op-send',action:'bug.comment',request_digest:'frozen-intent',state:'prepared',send_control:{available:true,reason:null,requests:[]}});
test('Bug comment sending never renders controls for fields or transitions',()=>{
  const h=harness();
  for(const action of ['bug.fields','bug.transition','bug.close']){
    const card=h.get(action);h.context.renderCommentSend({...commentSendOperation(),action},0,card);
    assert.equal(card.children.length,0);
  }
  assert.equal(h.calls.length,0);
});
test('Bug comment sending uses the existing frozen intent and retries only the same queued request',async()=>{
  const h=harness(),card=h.get('send');h.context.renderCommentSend(commentSendOperation(),0,card);
  const button=editorButton(card,'按现有授权发送评论'),first=button.handlers.click();const body=h.calls[0].body;
  assert.equal(h.calls[0].url,'/api/project-bugs/send-comment');assert.equal(body.expected_digest,'frozen-intent');assert.equal('text' in body,false);assert.equal('grant_id' in body,false);
  h.respond(h.calls[0],{error:'submission unknown'},false);await first;
  const retry=button.handlers.click();assert.deepEqual(h.calls[1].body,body);h.respond(h.calls[1],{dispatch_id:'send-1',state:'queued'});await retry;
});

test('Bug comment sending stays disabled for unaccepted native contract and stale views',async()=>{
  const h=harness(),card=h.get('send');const op=commentSendOperation();op.send_control.available=false;op.send_control.reason='native_contract_unverified';
  h.context.renderCommentSend(op,0,card);const button=editorButton(card,'按现有授权发送评论');assert.equal(button.disabled,true);await button.handlers.click();assert.equal(h.calls.length,0);
  const fresh=h.get('fresh-send');h.context.renderCommentSend(commentSendOperation(),0,fresh);vm.runInContext('++bugDetailEpoch',h.context);await editorButton(fresh,'按现有授权发送评论').handlers.click();assert.equal(h.calls.length,0);
});

test('Bug comment send progress restores pending work and separates unknown from confirmation',async()=>{
  const h=harness(),card=h.get('send');const op=commentSendOperation();op.send_control.requests=[{dispatch_id:'send-1',state:'running'}];
  h.context.renderCommentSend(op,0,card);assert.equal(editorButton(card,'按现有授权发送评论').disabled,true);
  const pending=h.context.pollCommentSend();h.respond(h.calls[0],{dispatch_id:'send-1',state:'succeeded',result:{operation_state:'unknown',reconcile_required:true}});await pending;
  const text=descendants(card).map(x=>x.textContent).join('\n');assert.match(text,/不会自动补发/);assert.doesNotMatch(text,/评论写入已确认/);
});

const writeSendOperation=(action='bug.fields')=>({operation_id:'op-write',request_digest:'frozen-write',action,state:'prepared',send_control:{available:true,reason:null,requests:[]}});
test('Bug write sending uses exact body and replays the frozen request ID until accepted',async()=>{
  const h=harness(),card=h.get('write-send');h.context.renderWriteSend(writeSendOperation(),0,card);
  const button=editorButton(card,'按现有授权写入字段'),first=button.handlers.click();const body=h.calls[0].body;
  assert.equal(h.calls[0].url,'/api/project-bugs/send-write');
  assert.deepEqual(Object.keys(body).sort(),['expected_digest','operation_id','request_id']);
  assert.equal(body.operation_id,'op-write');assert.equal(body.expected_digest,'frozen-write');
  h.respond(h.calls[0],{error:'submission unknown'},false);await first;
  const retry=button.handlers.click();assert.deepEqual(h.calls[1].body,body);
  h.respond(h.calls[1],{dispatch_id:'write-1',state:'queued'});await retry;
});

test('Bug write sending maps unavailable reason and unknown result without automatic resend',async()=>{
  const h=harness(),card=h.get('write-send');const op=writeSendOperation('bug.close');op.send_control.available=false;op.send_control.reason='risk_policy_forbids_dispatch';
  h.context.renderWriteSend(op,0,card);const button=editorButton(card,'按现有授权关闭缺陷');
  assert.equal(button.disabled,true);assert.match(descendants(card).map(x=>x.textContent).join('\n'),/当前风险策略禁止自动写入/);
  const active=writeSendOperation('bug.transition');active.send_control.requests=[{dispatch_id:'write-1',state:'running'}];
  const progress=h.get('write-progress');h.context.renderWriteSend(active,0,progress);assert.equal(editorButton(progress,'按现有授权执行流转').disabled,true);
  const pending=h.context.pollWriteSend();assert.deepEqual(h.calls[0].body,{dispatch_id:'write-1'});
  h.respond(h.calls[0],{dispatch_id:'write-1',state:'succeeded',result:{operation_state:'unknown',reconcile_required:true}});await pending;
  const text=descendants(progress).map(x=>x.textContent).join('\n');assert.match(text,/结果尚未确认，请使用只读核对或人工结算；不会自动重发/);assert.doesNotMatch(text,/已确认/);
});

test('human-settled write separates historical conflict, stored snapshot, and original unknown receipt',async()=>{
  const h=harness(),loading=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{bug_id:'bug',item_id:'7120451169',case_id:'case',snapshot:{fields:{priority:'P1'},read_evidence:{field_names:{priority:'Priority'},unobserved_field_keys:[]}},rounds:[],remote_dispatch_available:false,operations:[{
    operation_id:'op-settled',action:'bug.fields',state:'confirmed',result:{outcome:'applied',settled_by_human:true,settlement_id:'settlement-1'},
    preview:{differences:[{field:'priority',base:'P2',base_present:true,current:'P3',current_present:true,proposed:'P1',state:'conflict'}]},
    send_control:{requests:[{dispatch_id:'send-1',state:'succeeded',result:{operation_state:'unknown',reconcile_required:true}}]}
  }]});await loading;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join('\n');
  assert.match(text,/已确认字段：Priority/);assert.match(text,/查看历史差异与原始回执/);
  assert.match(text,/历史预览：当时读取值与原值不同/);assert.match(text,/最新留存快照已符合拟写值；操作已由人工结算确认。该快照不是新的远端读取。/);
  assert.match(text,/原始发送回执：结果尚未确认；该回执保持未知。操作已由人工结算确认生效，不代表新的远端读取。/);
  assert.match(text,/查看历史预览原始字段值/);assert.match(text,/查看最新留存快照字段值/);
});

test('human settlement retains a conflict when the latest stored snapshot differs',async()=>{
  const h=harness(),loading=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{bug_id:'bug',item_id:'7120451169',case_id:'case',snapshot:{fields:{priority:'P3'},read_evidence:{field_names:{priority:'Priority'},unobserved_field_keys:[]}},rounds:[],operations:[{
    operation_id:'op-settled',action:'bug.fields',state:'confirmed',result:{outcome:'applied',settled_by_human:true},
    preview:{differences:[{field:'priority',base:'P2',base_present:true,current:'P3',current_present:true,proposed:'P1',state:'conflict'}]}
  }]});await loading;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join('\n');
  assert.match(text,/历史预览：当时读取值与原值不同/);assert.match(text,/最新留存快照与历史拟写值不同；本次确认依据请查看原始回执和操作记录。/);
  assert.doesNotMatch(text,/最新留存快照已符合拟写值；操作已由人工结算确认/);
});

test('Bug unknown write settlement validates evidence and sends exact verdict body',async()=>{
  const h=harness(),card=h.get('settle');h.context.renderUnknownWriteSettlement({operation_id:'op-unknown',can_settle:true},0,card);
  const submit=editorButton(card,'提交人工结算');await submit.handlers.click();
  assert.equal(h.calls.length,0);assert.match(descendants(card).map(x=>x.textContent).join('\n'),/亲自核对远端结果/);
  editorField(card,'人工结算证据说明').value='checked remote item at 17:00';
  await submit.handlers.click();
  assert.equal(h.calls.length,0);assert.match(descendants(card).map(x=>x.textContent).join('\n'),/请明确选择/);
  editorField(card,'核实未生效').checked=true;editorField(card,'核实已生效').checked=false;editorField(card,'人工结算证据说明').value='checked remote item at 17:00';
  const pending=submit.handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/settle-unknown-write');
  assert.deepEqual(h.calls[0].body,{operation_id:'op-unknown',verdict:'confirmed_not_applied',evidence_text:'checked remote item at 17:00'});
  h.respond(h.calls[0],{ok:true});await pending;
});

test('Bug unknown settlement accepts native-style non-array child collections',async()=>{
  const h=harness(),card=h.get('settle-collection');
  h.context.renderUnknownWriteSettlement({operation_id:'op-unknown',can_settle:true},0,card);
  const submit=editorButton(card,'提交人工结算');
  editorField(card,'核实已生效').checked=true;
  editorField(card,'人工结算证据说明').value='independently read back exact changed field';
  for(const el of descendants(card))el.children=Object.assign({length:el.children.length},el.children);
  const pending=submit.handlers.click();
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].body.verdict,'confirmed_applied');
  h.respond(h.calls[0],{ok:true});await pending;
});

test('Bug close approvals render states and submit request plus approval decision bodies',async()=>{
  const h=harness(),area=h.get('close-approvals');
  h.context.renderCloseApprovals({bug_id:'bug-1',close_approvals:[{approval_id:'approval-1',bug_id:'bug-1',actor:'me',action_digest:'digest-action',verification_digest:'digest-verification',status:'requested',requested_at:'now',expires_at:'later',can_approve:true,can_deny:true,action:{host:'project.feishu.cn',project_key:'K3',type_key:'bug',item_id:'123',transition_id:'to-close',target_status_id:'closed'},current_verification:{verification_state:'passed'}}]},0,area,true);
  const text=descendants(area).map(x=>x.textContent).join('\n');assert.match(text,/等待决定/);assert.match(text,/关闭审批消费后单次生效/);
  editorField(area,'关闭流转 ID').value='transition-close';editorField(area,'目标状态 ID').value='closed';
  const request=editorButton(area,'发起关闭审批').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/request-close-approval');
  assert.deepEqual(Object.keys(h.calls[0].body).sort(),['bug_id','change','expires_at','request_id']);
  assert.deepEqual(h.calls[0].body.change,{transition_id:'transition-close',target_status_id:'closed'});
  h.respond(h.calls[0],{approval_id:'approval-new'});await request;
  const approve=editorButton(area,'批准关闭审批').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/decide-close-approval');
  assert.deepEqual(h.calls[1].body,{approval_id:'approval-1',request_id:h.calls[1].body.request_id,approve:true,expected_digest:'digest-action'});
  h.respond(h.calls[1],{ok:true});await approve;
});

test('Bug close approvals disable approval when evidence changes but permit denial',()=>{
  const h=harness(),area=h.get('close-approvals');
  h.context.renderCloseApprovals({bug_id:'bug-1',close_approvals:[{approval_id:'stale',status:'requested',can_approve:false,can_deny:true,approval_blockers:['verification_changed'],action:{host:'project.feishu.cn',project_key:'K3',type_key:'bug',item_id:'123',transition_id:'to-close',target_status_id:'closed'},current_verification:{verification_state:'failed'}}]},0,area,true);
  assert.equal(editorButton(area,'批准关闭审批').disabled,true);
  assert.equal(editorButton(area,'拒绝关闭审批').disabled,false);
  const text=descendants(area).map(x=>x.textContent).join(' ');
  assert.match(text,/to-close/);assert.match(text,/closed/);assert.match(text,/failed/);assert.match(text,/证据已变化/);
});

test('consumed Bug close approval shows historical receipt instead of current blockers',()=>{
  const h=harness(),area=h.get('close-approvals');
  h.context.renderCloseApprovals({bug_id:'bug-1',close_approvals:[{
    approval_id:'used',status:'consumed',actor:'me',expires_at:'past',verification_digest:'digest',
    consumed_at:'now',consumed_operation_id:'operation-1',
    current_verification:null,approval_blockers:['approval_expired','verification_unavailable']
  }]},0,area,false);
  const text=descendants(area).map(x=>x.textContent).join(' ');
  assert.match(text,/已消费/);assert.match(text,/operation-1/);assert.match(text,/关闭是否生效以远端状态/);
  assert.doesNotMatch(text,/当前验证：不可用|审批已过期|当前修复或验证证据不可用/);
  assert.equal(descendants(area).some(x=>x.tagName==='BUTTON'&&x.textContent==='发起关闭审批'),false);
  assert.match(text,/若缺陷重新打开，先读取远端状态并开始新轮次/);
});

test('Bug create draft console prepares drafts and keeps missing duplicate gates disabled',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='grant-1';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='K3';editorField(area,'草稿类型 Key').value='bug';
  editorField(area,'草稿字段 JSON').value=JSON.stringify({title:'Fan fails'});editorField(area,'必填字段，每行 field_key:label').value='title:标题';
  const prepare=editorButton(area,'准备创建草稿').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/prepare-create-draft');
  assert.deepEqual(h.calls[0].body.grant_id,'grant-1');assert.deepEqual(h.calls[0].body.field_values,{title:'Fan fails'});
  assert.deepEqual(h.calls[0].body.required_fields,[{field_key:'title',label:'标题'}]);
  h.respond(h.calls[0],{draft_id:'draft-1',request_digest:'draft-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Fan fails'},required_fields:[{field_key:'title',label:'标题'}],state:'draft',missing_required:['assignee'],duplicate_confirmed:false});await prepare;
  assert.equal(editorButton(area,'标记草稿就绪').disabled,true);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/需补齐必填.*完成查重确认/);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/草稿内容不可改/);
});

test('Bug create scope selector and active grants fill the draft scope without creating a Bug',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const load=editorButton(area,'读取可创建范围').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/create-scope-options');assert.deepEqual(h.calls[0].body,{});
  h.respond(h.calls[0],{available:true,reader_host:'project.feishu.cn',options:[{simple_name:'k3',project_key:'space',type_key:'issue'},{simple_name:'k3',project_key:'space',type_key:'bug'}]});await load;
  const scope=editorField(area,'创建空间和类型');assert.equal(scope.disabled,false);assert.equal(scope.children[0].textContent,'k3 · issue');
  assert.equal(editorField(area,'创建授权 Host').value,'project.feishu.cn');assert.equal(editorField(area,'创建授权项目 Key').value,'space');assert.equal(editorField(area,'创建授权类型 Key').value,'issue');
  assert.equal(editorField(area,'草稿 Host').value,'project.feishu.cn');assert.equal(editorField(area,'草稿项目 Key').value,'space');assert.equal(editorField(area,'草稿类型 Key').value,'issue');
  const issue=editorButton(area,'创建缺陷授权').handlers.click();assert.equal(h.calls[1].url,'/api/project-bugs/issue-create-grant');
  h.respond(h.calls[1],{grant_id:'new-grant',scope:{host:'project.feishu.cn',project_key:'space',type_key:'issue'}});await issue;
  assert.equal(editorField(area,'草稿授权 ID').value,'new-grant');
  const read=editorButton(area,'读取创建授权').handlers.click();assert.equal(h.calls[2].url,'/api/project-bugs/list-create-grants');
  h.respond(h.calls[2],{items:[{grant_id:'active-grant',status:'active',scope:{host:'project.feishu.cn',project_key:'other',type_key:'bug',max_creations:1},used:0,remaining:1,expires_at:'2099-01-01T00:00:00+00:00',can_revoke:true}],next_cursor:null});await read;
  assert.equal(editorField(area,'已有创建授权').disabled,false);assert.equal(editorField(area,'草稿授权 ID').value,'active-grant');
  assert.equal(editorField(area,'草稿项目 Key').value,'other');assert.equal(editorField(area,'草稿类型 Key').value,'bug');
  assert.equal(h.calls.length,3);
});

test('changing Bug creation scope clears an unrelated selected grant',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const load=editorButton(area,'读取可创建范围').handlers.click();
  h.respond(h.calls[0],{available:true,reader_host:'project.feishu.cn',options:[{simple_name:'k3',project_key:'space',type_key:'issue'},{simple_name:'k3',project_key:'space',type_key:'bug'}]});await load;
  const read=editorButton(area,'读取创建授权').handlers.click();
  h.respond(h.calls[1],{items:[{grant_id:'issue-grant',status:'active',scope:{host:'project.feishu.cn',project_key:'space',type_key:'issue',max_creations:1},used:0,remaining:1,expires_at:'2099-01-01T00:00:00+00:00',can_revoke:true}],next_cursor:null});await read;
  assert.equal(editorField(area,'草稿授权 ID').value,'issue-grant');
  const scope=editorField(area,'创建空间和类型');scope.value='1';scope.handlers.change();
  assert.equal(editorField(area,'草稿类型 Key').value,'bug');
  assert.equal(editorField(area,'草稿授权 ID').value,'');
});

test('ready draft creates then automatically queues its authorized read binding',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'draft-ready',request_digest:'ready-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Ready'},required_fields:[],state:'ready',missing_required:[],duplicate_confirmed:true},{draft_id:'draft-ok',request_digest:'ok-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Draft'},required_fields:[],state:'draft',missing_required:[],duplicate_confirmed:true}],next_cursor:null});await read;
  const rendered=descendants(area).map(x=>x.textContent).join('\n');
  assert.match(rendered,/原生创建通道已验收/);
  assert.match(rendered,/人工点击派发后创建一次/);
  assert.match(rendered,/创建并读取绑定/);
  assert.match(rendered,/8 小时只读授权读取和建立本地 P2 绑定/);
  const dispatchButtons=descendants(area).filter(x=>x.textContent==='创建并读取绑定'&&x.handlers&&x.handlers.click);
  assert.equal(dispatchButtons.length,2);
  assert.equal(dispatchButtons[0].disabled,false);
  assert.equal(dispatchButtons[1].disabled,true);
  editorField(area,'查重关键词').value='boot';
  const search=editorButton(area,'搜索可能重复的缺陷').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/search-create-duplicates');
  h.respond(h.calls[1],{search_id:'search-1',state:'queued'});await search;
  const result=editorButton(area,'读取查重结果').handlers.click();
  h.respond(h.calls[2],{state:'succeeded',result:{items:[{item_id:'123',title:'similar bug'}],next_after_id:null}});await result;
  assert.match(descendants(area).map(x=>x.textContent).join(' '),/similar bug/);
  const attach=editorButton(area,'采用以上查重结果').handlers.click();
  assert.equal(h.calls[3].url,'/api/project-bugs/attach-create-search');
  assert.deepEqual(h.calls[3].body,{draft_id:'draft-ready',expected_digest:'ready-digest',search_id:'search-1'});
  h.respond(h.calls[3],{ok:true});await attach;
  const confirm=editorButton(area,'确认不是重复缺陷').handlers.click();
  assert.equal(h.calls[4].url,'/api/project-bugs/confirm-create-not-duplicate');
  assert.deepEqual(h.calls[4].body,{draft_id:'draft-ready',expected_digest:'ready-digest'});
  h.respond(h.calls[4],{ok:true});await confirm;
  const send=dispatchButtons[0].handlers.click();
  assert.equal(h.calls[5].url,'/api/project-bugs/dispatch-create-draft');
  assert.deepEqual(h.calls[5].body,{draft_id:'draft-ready',expected_digest:'ready-digest'});
  h.respond(h.calls[5],{draft_id:'draft-ready',state:'created',created_item_id:'7123456789'});await settle();
  assert.equal(h.calls[6].url,'/api/project-bugs/bind-created-draft');
  assert.deepEqual(h.calls[6].body,{draft_id:'draft-ready',expected_digest:'ready-digest',read_hours:8,local_priority:'P2'});
  h.respond(h.calls[6],{intake_id:'created-read',state:'queued'});await send;
  h.get('console').hidden=false;
  const poll=h.context.pollBugIntake();assert.equal(h.calls[7].url,'/api/project-bugs/intake-status');
  h.respond(h.calls[7],{intake_id:'created-read',state:'succeeded',bug_id:'bound-bug'});await poll;
  assert.ok(editorButton(area,'查看导入的 Bug'));
  assert.equal(dispatchButtons[0].disabled,true);
});

test('unknown create dispatch result never starts a binding read',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'draft-ready',request_digest:'ready-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Ready'},required_fields:[],state:'ready',missing_required:[],duplicate_confirmed:true}],next_cursor:null});await read;
  const create=editorButton(area,'创建并读取绑定').handlers.click();
  h.respond(h.calls[1],{draft_id:'draft-ready',state:'unknown'});await create;
  assert.equal(h.calls.length,2);
  assert.doesNotMatch(descendants(area).map(x=>x.textContent).join('\n'),/读取并绑定新缺陷/);
});

test('created item remains recoverable when automatic binding cannot be queued',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'draft-ready',request_digest:'ready-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Ready'},required_fields:[],state:'ready',missing_required:[],duplicate_confirmed:true}],next_cursor:null});await read;
  const create=editorButton(area,'创建并读取绑定').handlers.click();
  h.respond(h.calls[1],{draft_id:'draft-ready',state:'created',created_item_id:'7123456789'});await settle();
  assert.equal(h.calls[2].url,'/api/project-bugs/bind-created-draft');
  h.respond(h.calls[2],{error:'read scope unavailable'},false);await create;
  assert.equal(h.calls.length,3);
  assert.equal(editorButton(area,'读取并绑定新缺陷').disabled,false);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/远端缺陷已创建，但绑定读取未确认/);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/已创建对象：7123456789；本地绑定仍需远端回读确认/);
  assert.equal(editorButton(area,'创建并读取绑定').disabled,true);
});

test('Bug create draft dispatch failure keeps the button consumed until the draft is re-read',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'draft-ready',request_digest:'ready-digest',scope:{host:'project.feishu.cn',project_key:'K3',type_key:'bug'},field_values:{title:'Ready'},required_fields:[],state:'ready',missing_required:[],duplicate_confirmed:true}],next_cursor:null});await read;
  const button=descendants(area).filter(x=>x.textContent==='创建并读取绑定'&&x.handlers&&x.handlers.click)[0];
  const send=button.handlers.click();
  h.respond(h.calls[1],{error:'native creation failed'},false);await send;
  assert.equal(button.disabled,true);
  assert.match(descendants(area).map(x=>x.textContent).join('\n'),/创建并读取绑定未确认/);
});

test('Created draft binds through read queue without another create and follows its result',async()=>{
  const h=harness(),area=h.get('create-console');h.get('console').hidden=false;h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'created',request_digest:'digest',scope:{host:'project.feishu.cn',project_key:'space',type_key:'issue'},state:'created',created_item_id:'123'}],next_cursor:null});await read;
  const button=editorButton(area,'读取并绑定新缺陷');
  const bind=button.handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/bind-created-draft');
  assert.deepEqual(h.calls[1].body,{draft_id:'created',expected_digest:'digest',read_hours:8,local_priority:'P2'});
  await button.handlers.click();assert.equal(h.calls.length,2);
  h.respond(h.calls[1],{intake_id:'read-created',state:'queued'});await bind;
  h.get("console").hidden=false;
  const poll=h.context.pollBugIntake();
  assert.equal(h.calls[2].url,'/api/project-bugs/intake-status');
  h.respond(h.calls[2],{intake_id:'read-created',state:'succeeded',bug_id:'bound-bug'});await poll;
  assert.ok(editorButton(area,'查看导入的 Bug'));
  assert.equal(h.calls.some(c=>c.url.includes('dispatch-create')),false);
});

test('Official creation form uses labelled fields and freezes scope with the draft',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='g';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='space';editorField(area,'草稿类型 Key').value='issue';
  const load=editorButton(area,'读取飞书创建字段').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/create-form');
  h.respond(h.calls[0],{fields:[{field_key:'name',label:'标题',editor:'text',required:true},{field_key:'owner',label:'经办人',editor:'json',required:false}]});await load;
  editorField(area,'标题').value='Boot hangs';editorField(area,'经办人').value='["member"]';
  const submit=editorButton(area,'准备创建草稿').handlers.click();
  assert.deepEqual(h.calls[1].body.field_values,{name:'Boot hangs',owner:['member']});
  assert.deepEqual(h.calls[1].body.required_fields,[{field_key:'name',label:'标题'}]);
  assert.equal(editorField(area,'标题').disabled,true);
  assert.equal(editorButton(area,'读取飞书创建字段').disabled,true);
  h.respond(h.calls[1],{draft_id:'d',state:'draft',request_digest:'digest',scope:{},field_values:{},missing_required:[],duplicate_confirmed:false});await submit;
});

test('Official flat select shows labels and submits the string option ID including zero',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='g';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='space';editorField(area,'草稿类型 Key').value='issue';
  const load=editorButton(area,'读取飞书创建字段').handlers.click();
  h.respond(h.calls[0],{fields:[{field_key:'priority',label:'优先级',editor:'select',required:true,options:[{value:'0',label:'P0'},{value:'99',label:'待定'}]}]});await load;
  const select=editorField(area,'优先级');assert.equal(select.children[1].textContent,'P0');select.value='0';
  const submit=editorButton(area,'准备创建草稿').handlers.click();
  assert.deepEqual(h.calls[1].body.field_values,{priority:'0'});
  h.respond(h.calls[1],{draft_id:'d',state:'draft',request_digest:'digest',scope:{},field_values:{},missing_required:[],duplicate_confirmed:false});await submit;
});

test('Creation member selector submits selected IDs and freezes with draft',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='g';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='space';editorField(area,'草稿类型 Key').value='issue';
  const load=editorButton(area,'读取飞书创建字段').handlers.click();
  h.respond(h.calls[0],{fields:[{field_key:'owner',label:'经办人',editor:'user',type:'multi_user',required:true}]});await load;
  editorField(area,'经办人搜索姓名或标识').value='Alice';
  const search=editorButton(area,'搜索人员').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/search-create-users');
  assert.equal(h.calls[1].body.field_key,'owner');
  h.respond(h.calls[1],{options:[{value:'u1',label:'Alice'}]});await search;
  editorButton(area,'Alice · u1').handlers.click();
  const submit=editorButton(area,'准备创建草稿').handlers.click();
  assert.deepEqual(h.calls[2].body.field_values,{owner:['u1']});
  assert.equal(editorButton(area,'搜索人员').disabled,true);
  assert.equal(editorButton(area,'移除 Alice').disabled,true);
  h.respond(h.calls[2],{draft_id:'d',state:'draft',request_digest:'digest',scope:{},field_values:{},missing_required:[],duplicate_confirmed:false});await submit;
});
test('Creation single member selector submits a scalar ID',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='g';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='space';editorField(area,'草稿类型 Key').value='issue';
  const load=editorButton(area,'读取飞书创建字段').handlers.click();
  h.respond(h.calls[0],{fields:[{field_key:'owner',label:'经办人',editor:'user',type:'user',required:true}]});await load;
  editorField(area,'经办人搜索姓名或标识').value='Alice';
  const search=editorButton(area,'搜索人员').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/search-create-users');
  assert.equal(h.calls[1].body.field_key,'owner');
  h.respond(h.calls[1],{options:[{value:'u1',label:'Alice'}]});await search;
  editorButton(area,'Alice · u1').handlers.click();
  const submit=editorButton(area,'准备创建草稿').handlers.click();
  assert.deepEqual(h.calls[2].body.field_values,{owner:'u1'});
  assert.equal(editorButton(area,'搜索人员').disabled,true);
  assert.equal(editorButton(area,'移除 Alice').disabled,true);
  h.respond(h.calls[2],{draft_id:'d',state:'draft',request_digest:'digest',scope:{},field_values:{},missing_required:[],duplicate_confirmed:false});await submit;
});

test('Creation relation selector submits numeric multi IDs and searches the field scope',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  editorField(area,'草稿授权 ID').value='g';editorField(area,'草稿 Host').value='project.feishu.cn';editorField(area,'草稿项目 Key').value='space';editorField(area,'草稿类型 Key').value='issue';
  const load=editorButton(area,'读取飞书创建字段').handlers.click();
  h.respond(h.calls[0],{fields:[{field_key:'version',label:'发现版本',editor:'related',type:'work_item_related_multi_select',required:true}]});await load;
  editorField(area,'发现版本搜索关联项名称').value='V1';
  const search=editorButton(area,'搜索关联项').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/search-create-related');
  assert.equal(h.calls[1].body.field_key,'version');
  assert.equal(h.calls[1].body.target_type,undefined);
  h.respond(h.calls[1],{options:[{value:'7111111111',label:'V1'}]});await search;
  editorButton(area,'V1 · 7111111111').handlers.click();
  const submit=editorButton(area,'准备创建草稿').handlers.click();
  assert.deepEqual(h.calls[2].body.field_values,{version:[7111111111]});
  h.respond(h.calls[2],{draft_id:'d',state:'draft',request_digest:'digest',scope:{},field_values:{},missing_required:[],duplicate_confirmed:false});await submit;
});


test('Bug repair review freezes uncertain decisions and displays repository evidence',async()=>{
  const h=harness(),area=h.get('repair-review');h.context.repairReviewButton('bug-1','round-1',0,area);
  const read=editorButton(area,'复核本轮修复范围').handlers.click();
  assert.equal(h.calls[0].url,'/api/project-bugs/repair-review-detail');
  h.respond(h.calls[0],{evidence_digest:'exact-evidence',review:null,review_available:true,can_mark_ready:true,ready_blockers:[],repositories:[{repository:'u-boot',base_commit:'base',head_commit:'head',commits:['head'],patch_sha256:'sha',patch_text:'<script>patch data</script>',review_base_commit:'original',segments:[{base_commit:'original',head_commit:'base',commits:['base'],patch_text:'earlier repair patch',patch_sha256:'first'},{base_commit:'base',head_commit:'head',commits:['head'],patch_text:'<script>patch data</script>',patch_sha256:'sha'}],blockers:[]}]});await read;
  assert.ok(descendants(area).some(n=>n.textContent==='<script>patch data</script>'));
  assert.ok(descendants(area).some(n=>n.textContent==='earlier repair patch'));
  editorField(area,'修复复核结论').value='ready';editorField(area,'修复范围与复核依据').value='Reviewed complete patch and all declared repositories.';
  descendants(area).find(n=>n.type==='checkbox').checked=true;
  const save=editorButton(area,'记录修复复核');let pending=save.handlers.click();
  const original=JSON.parse(JSON.stringify(h.calls[1].body));
  assert.equal(h.calls[1].url,'/api/project-bugs/record-repair-review');
  assert.deepEqual({...original,request_id:'id'},{bug_id:'bug-1',round_id:'round-1',request_id:'id',evidence_digest:'exact-evidence',expected_review_id:null,verdict:'ready',rationale:'Reviewed complete patch and all declared repositories.',attested:true});
  h.respond(h.calls[1],{error:'response lost'},false);await pending;
  editorField(area,'修复复核结论').value='not_applicable';pending=save.handlers.click();
  assert.deepEqual(h.calls[2].body,original);h.respond(h.calls[2],{review_id:'review-1'});await pending;
  assert.match(descendants(area).map(n=>n.textContent).join(' '),/验证和关闭结果保持独立/);
});

test('Bug repair review cannot override missing repository evidence or submit a stale form',async()=>{
  const h=harness(),area=h.get('repair-review');h.context.repairReviewButton('bug-1','round-1',0,area);
  const open=editorButton(area,'复核本轮修复范围');let read=open.handlers.click();
  const view={evidence_digest:'old',review:null,review_available:true,can_mark_ready:false,ready_blockers:['repository_evidence_incomplete'],repositories:[{repository:'ec',blockers:['repository_not_investigated']}]};
  h.respond(h.calls[0],view);await read;
  editorField(area,'修复复核结论').value='ready';editorField(area,'修复范围与复核依据').value='reason';descendants(area).find(n=>n.type==='checkbox').checked=true;
  const oldSave=editorButton(area,'记录修复复核');await oldSave.handlers.click();assert.equal(h.calls.length,1);
  read=open.handlers.click();h.respond(h.calls[1],{...view,evidence_digest:'new'});await read;
  await oldSave.handlers.click();assert.equal(h.calls.length,2);
  assert.match(descendants(area).map(n=>n.textContent).join(' '),/缺少该仓库的调查证据/);
});

test('Bug progress comment prepares without an investigation and filters exact grant scope',async()=>{
  const h=harness(),area=h.get('progress-comment');h.context.renderBugCommentComposer(fieldEditorFixture(),0,area);
  const good=fieldGrant({actions:['bug.read','bug.comment']});
  h.respond(h.calls[0],{items:[fieldGrant({actions:['bug.read']}),fieldGrant({id:'wrong',bugs:['another'],actions:['bug.comment']}),good],next_cursor:null});await settle();
  assert.deepEqual(editorField(area,'进度评论授权').children.map(x=>x.value),['grant-good']);
  editorField(area,'进度评论全文').value='Accepted; waiting for a reproducible log.';
  await editorButton(area,'预览进度评论').handlers.click();assert.equal(h.calls.length,1);
  const pending=editorButton(area,'准备进度评论').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/prepare-write');
  assert.deepEqual(h.calls[1].body.change,{text:'Accepted; waiting for a reproducible log.'});
  assert.equal(h.calls[1].body.action,'bug.comment');assert.equal(h.calls[1].body.snapshot_id,'snapshot-12');
  assert.equal('job_id' in h.calls[1].body,false);
  h.respond(h.calls[1],{operation_id:'prepared-comment'});await settle();
  assert.equal(h.calls[2].url,'/api/project-bugs/detail');
  h.respond(h.calls[2],{item_id:'123',snapshot:null,rounds:[],operations:[],remote_dispatch_available:false});await pending;
  assert.equal(h.calls.some(c=>c.url.includes('send-comment')),false);
});

test('Bug progress comment invalidates edited previews and freezes unknown preparation for exact retry',async()=>{
  const h=harness(),area=h.get('progress-comment');h.context.renderBugCommentComposer(fieldEditorFixture(),0,area);
  h.respond(h.calls[0],{items:[fieldGrant({actions:['bug.comment']})],next_cursor:null});await settle();
  const text=editorField(area,'进度评论全文'),preview=editorButton(area,'预览进度评论'),save=editorButton(area,'准备进度评论');
  text.value='first';await preview.handlers.click();text.value='updated';text.handlers.input();assert.equal(save.disabled,true);
  await save.handlers.click();assert.equal(h.calls.length,1);await preview.handlers.click();
  const first=save.handlers.click(),frozen=structuredClone(h.calls[1].body);h.respond(h.calls[1],{error:'response lost'},false);await first;
  assert.equal(text.disabled,true);assert.equal(save.disabled,false);
  text.value='programmatic change must not affect retry';const retry=save.handlers.click();
  assert.deepEqual(h.calls[2].body,frozen);h.respond(h.calls[2],{error:'still unknown'},false);await retry;
  assert.equal(h.calls.length,3);
});

test('Bug progress comment rejects expired grants and stale pages',async()=>{
  const h=harness(),area=h.get('progress-comment');h.context.renderBugCommentComposer(fieldEditorFixture(),0,area);
  h.respond(h.calls[0],{items:[fieldGrant({actions:['bug.comment'],expires:new Date(Date.now()-1000).toISOString()})],next_cursor:null});await settle();
  assert.equal(editorButton(area,'预览进度评论').disabled,true);
  vm.runInContext('bugDetailEpoch=1',h.context);await editorButton(area,'预览进度评论').handlers.click();await editorButton(area,'准备进度评论').handlers.click();assert.equal(h.calls.length,1);
});

test('coding form explains non-executable Case states and refuses programmatic submission',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const loading=h.context.openCodingTask('case-1');
  h.respond(h.calls[0],{case_version:1,submission_allowed:false,case_state:'intake',repositories:['repo'],items:[{id:'claude',status:'configured',label:'Claude',agent:'claude',model:'model',reasoning:'medium',contract_fingerprint:'abc'}]});await loading;
  const form=h.get('case-actions').children[0];assert.equal(form.children.at(-2).disabled,true);
  assert.match(form.children.at(-3).textContent,/先分流或恢复事项/);
  form.children.slice(0,4).map(label=>label.children[0]).forEach((field,i)=>field.value=['repo','claude','Fix','Test'][i]);
  await form.handlers.submit({preventDefault(){}});assert.equal(h.calls.length,1);
});

test('joint verification form freezes every candidate and companion source across retry',async()=>{
  const h=harness();h.get('case-dialog').open=true;
  const repos=[{id:'a',repository:'primary',node:'node',branch:'main',candidate_commit:'a'.repeat(40)},{id:'b',repository:'library',node:'node',branch:'stable',candidate_commit:'b'.repeat(40)}];
  const verification={plan_id:'plan',step_id:'joint',...repos[0],repositories:repos,environment:'fixture',procedure:'joint check',oracle:'compatible'};
  const loading=h.context.openCodingTask('case-1',{bug_id:'bug',round_id:'round',expected_revision:4},verification);
  h.respond(h.calls[0],{case_version:2,repositories:['primary','library'],source_choices:{primary:{node:'node',deployment_fingerprint:'f'.repeat(64)},library:{node:'node',deployment_fingerprint:'f'.repeat(64)}},items:[{id:'tool',status:'configured',label:'Tool',agent:'codex',model:'model',reasoning:'high',contract_fingerprint:'abc'}],candidate_choices:[{repository:'library',request_id:'seed-b',job_id:'old',source:{node:'node',branch:'stable',base_commit:'b'.repeat(40)}}]});await loading;
  const form=h.get('case-actions').children[0];form.children[0].children[0].value='primary';form.children[1].children[0].value='tool';form.children[2].children[0].value='joint-test';
  editorField(form,'联合仓库 library 的补丁来源').value='seed-b';
  let sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(Object.keys(h.calls[1].body.verification.sources).sort(),['library','primary']);
  assert.deepEqual(h.calls[1].body.verification.sources.primary,h.calls[1].body.source);
  assert.equal(h.calls[1].body.verification.sources.library.base_commit,'b'.repeat(40));
  assert.equal(h.calls[1].body.verification.sources.library.candidate_request_id,'seed-b');
  h.respond(h.calls[1],{error:'timeout'},false);await sending;
  editorField(form,'联合仓库 library 的补丁来源').value='';sending=form.handlers.submit({preventDefault(){}});await settle();
  assert.deepEqual(h.calls[2].body,h.calls[1].body);h.respond(h.calls[2],{created:false,job_id:'joint',state:'queued'});await sending;
});

test('Bug lifecycle detail labels reopened verification as history without erasing plan results',async()=>{
  const h=harness();
  const round={round_id:'round-old',number:1,archived_at:null,execution_state:'succeeded',repair_state:'ready',verification_state:'passed',lifecycle:{state:'reopened',requires_new_round:true}};
  const pending=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{bug_id:'bug',item_id:'123',case_id:'case',snapshot:null,rounds:[round],operations:[],verification_plans:[{
    round_id:round.round_id,version:1,verification_state:'passed',definition:{title:'Prior verification',repositories:[],steps:[]},runs:[]
  }]});
  await pending;
  const text=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent).join('\n');
  assert.match(text,/历史轮次记录/);
  assert.match(text,/重新打开/);
  assert.match(text,/须开始新轮次重新调查和验证/);
  assert.ok(text.split('重新打开').length>=3,'both summary and old plan explain recurrence');
  assert.equal(round.verification_state,'passed');
  assert.equal(h.calls.filter(call=>call.url.includes('prepare-write')||call.url.includes('start-round')).length,0);
});

test('Bug lifecycle outcome selects active round and marks uncertain or archived results',()=>{
  const h=harness(),area=h.get('outcome');
  const old={round_id:'old',archived_at:'past',execution_state:'succeeded',repair_state:'ready',verification_state:'passed'};
  const fresh={round_id:'new',archived_at:null,execution_state:'planned',repair_state:'unknown',verification_state:'not_run',lifecycle:{state:'current',requires_new_round:false}};
  h.context.renderBugOutcome({rounds:[fresh,old]},area);
  let text=bugGrantNodes(area).map(n=>n.textContent).join('\n');
  assert.doesNotMatch(text,/历史轮次记录|验证通过/);
  area.replaceChildren();
  h.context.renderBugOutcome({rounds:[old]},area);
  assert.match(bugGrantNodes(area).map(n=>n.textContent).join('\n'),/已归档/);
  area.replaceChildren();
  h.context.renderBugOutcome({rounds:[{...fresh,lifecycle:{state:'unavailable'}}]},area);
  text=bugGrantNodes(area).map(n=>n.textContent).join('\n');
  assert.match(text,/适用性待核对/);
  assert.doesNotMatch(text,/须开始新轮次/);
});

test('chat-created incomplete draft can be loaded and saved as a new immutable draft',async()=>{
  const h=harness(),area=h.get('create-console');h.context.renderCreateDraftConsole(area);
  const original={draft_id:'chat-draft',grant_id:'grant-chat',request_digest:'original-digest',scope:{host:'project.feishu.cn',project_key:'fixture-space',type_key:'fixture-type'},field_values:{title:'来自聊天的描述'},required_fields:[{field_key:'title',label:'标题'},{field_key:'severity',label:'严重程度'}],state:'draft',missing_required:['severity'],duplicate_confirmed:false};
  const read=editorButton(area,'读取创建草稿').handlers.click();h.respond(h.calls[0],{items:[original],next_cursor:null});await read;
  editorButton(area,'载入并补充草稿').handlers.click();
  assert.equal(h.calls.length,1);
  assert.equal(editorField(area,'草稿授权 ID').value,'grant-chat');
  assert.deepEqual(JSON.parse(editorField(area,'草稿字段 JSON').value),{title:'来自聊天的描述'});
  editorField(area,'草稿字段 JSON').value=JSON.stringify({title:'来自聊天的描述',severity:'official-high'});
  const save=editorButton(area,'准备创建草稿').handlers.click();
  assert.equal(h.calls[1].url,'/api/project-bugs/prepare-create-draft');
  assert.deepEqual(h.calls[1].body.field_values,{title:'来自聊天的描述',severity:'official-high'});
  assert.equal(h.calls[1].body.grant_id,'grant-chat');
  assert.equal(h.calls[1].body.draft_id,undefined);
  h.respond(h.calls[1],{...original,draft_id:'new-draft',request_digest:'new-digest',field_values:h.calls[1].body.field_values,missing_required:[]});await save;
  assert.deepEqual(original.field_values,{title:'来自聊天的描述'});
  assert.equal(editorButton(area,'创建并读取绑定').disabled,true);
});

test('Created draft failed read retries only that read and preserves request after response loss',async()=>{
  const h=harness(),area=h.get('create-console');h.get('console').hidden=false;h.context.renderCreateDraftConsole(area);
  const read=editorButton(area,'读取创建草稿').handlers.click();
  h.respond(h.calls[0],{items:[{draft_id:'created',request_digest:'digest',scope:{host:'project.feishu.cn',project_key:'space',type_key:'issue'},state:'created',created_item_id:'123'}],next_cursor:null});await read;
  const button=editorButton(area,'读取并绑定新缺陷');
  const bind=button.handlers.click();h.respond(h.calls[1],{intake_id:'first',state:'queued'});await bind;
  h.get('console').hidden=false;const poll=h.context.pollBugIntake();h.respond(h.calls[2],{intake_id:'first',state:'failed',error_code:'login_required'});await poll;
  assert.equal(button.disabled,false);assert.equal(button.textContent,'重新读取已创建缺陷');
  const retry=button.handlers.click();assert.equal(h.calls[3].url,'/api/project-bugs/retry-created-draft-read');
  assert.equal(h.calls[3].body.retry_intake_id,'first');
  h.respond(h.calls[3],{error:'response lost'},false);await retry;
  const replay=button.handlers.click();assert.deepEqual(h.calls[4].body,h.calls[3].body);
  h.respond(h.calls[4],{intake_id:'second',state:'queued'});await replay;
  assert.equal(button.disabled,true);
  assert.equal(h.calls.some(c=>c.url.includes('dispatch-create')),false);
});

test('Reopened Bug offers a new round instead of launching or continuing the old round',async()=>{
  for(const state of ['planned','succeeded']){
    const h=harness(),pending=h.context.loadBugDetail('bug');
    h.respond(h.calls[0],{bug_id:'bug',item_id:'123',case_id:'case',revision:4,snapshot:null,plan_controls_available:true,
      rounds:[{round_id:'old',number:1,archived_at:null,execution_state:state,settlement:{ready:true,blockers:[]},lifecycle:{state:'reopened',requires_new_round:true}}],
      operations:[],verification_plans:[],investigation_jobs:state==='planned'?[]:[{round_id:'old',job_id:'prior',state:'succeeded',repositories:['repo']}]});
    await pending;
    const labels=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent);
    assert.equal(labels.includes('发起本轮编码调查'),false);
    assert.equal(labels.includes('接续本轮调查'),false);
    assert.ok(labels.includes('建立下一轮调查'));
  }
});

test('Verification run wording uses only its exact current review and retains stale or pending distinctions',async()=>{
  for(const [state,reviewRun,expected] of [
    ['passed','run','独立核验通过（仅限本次运行及其证据）'],
    ['stale','run','旧核验结论已失效，须重新核对证据'],
    ['passed','another-run','版本、环境和通过判据尚待独立核验']]){
    const h=harness(),pending=h.context.loadBugDetail('bug');
    h.respond(h.calls[0],{bug_id:'bug',item_id:'123',case_id:'case',snapshot:null,
      rounds:[{round_id:'round',number:1,execution_state:'succeeded',archived_at:null,lifecycle:{state:'current'}}],operations:[],
      verification_plans:[{round_id:'round',version:1,verification_state:state,definition:{title:'Plan',repositories:[],steps:[]},
        operator_verification:{functional_step_required:true,steps:[{step_id:'step',run_id:reviewRun,state,review:{review_id:'review'}}]},
        runs:[{run_id:'run',step_id:'step',execution_state:'succeeded'}]}]});await pending;
    const labels=bugGrantNodes(h.get('bug-detail')).map(n=>n.textContent);
    assert.ok(labels.includes('步骤 step · 执行：命令执行成功 · '+expected));
  }
});

test('Reopened round does not offer another independent verifier against old evidence',async()=>{
  const h=harness(),pending=h.context.loadBugDetail('bug');
  h.respond(h.calls[0],{bug_id:'bug',item_id:'123',case_id:'case',snapshot:null,operations:[],
    rounds:[{round_id:'old',number:1,archived_at:null,execution_state:'succeeded',settlement:{ready:true,blockers:[]},lifecycle:{state:'reopened'}}],
    verification_plans:[{round_id:'old',version:1,verification_state:'passed',runs:[],definition:{title:'Old plan',
      repositories:[{id:'repo',repository:'repo',candidate_commit:'a'.repeat(40)}],
      steps:[{id:'check',title:'Old check',required:true,layer:'software_test',repositories:['repo'],artifacts:[],devices:[]}]}}]});
  await pending;
  assert.equal(bugGrantNodes(h.get('bug-detail')).some(n=>n.textContent==='独立验证：Old check'),false);
});
