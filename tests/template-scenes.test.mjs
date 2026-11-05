import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';
import { pythonCommand } from '../scripts/python.mjs';

const [python, ...pythonPrefix] = pythonCommand();

// Reuse a prepared project runtime with EDITONNY_GSAP to run without network access.
test('2D scene templates mount, animate, and seek without missing targets or duplicate IDs', async () => {
  const root = fileURLToPath(new URL('../skills/editonny/engine/project-template/', import.meta.url));
  let gsap;
  if (process.env.EDITONNY_GSAP) gsap = await readFile(process.env.EDITONNY_GSAP);
  else {
    const build = await readFile(`${root}build.py`, 'utf8');
    const url = build.match(/^GSAP_URL = '([^']+)'/m)?.[1];
    assert.ok(url, 'The project must pin its GSAP runtime');
    const response = await fetch(url, { signal: AbortSignal.timeout(30000) });
    assert.ok(response.ok, `GSAP download failed: ${response.status}`);
    gsap = Buffer.from(await response.arrayBuffer());
  }
  const scenes = await Promise.all((await readdir(`${root}compositions/scenes/2d`)).filter(f => f.endsWith('.html')).map(async file => ({ id: file.slice(0, -5), html: await readFile(`${root}compositions/scenes/2d/${file}`, 'utf8') })));
  const propertyScenes = await Promise.all(['building-panel','floor-plan-panel','property-price-panel','location-map-panel'].map(async id => ({ id, html: await readFile(new URL(`../skills/editonny/visuals/scenes/${id}.html`, import.meta.url), 'utf8') })));
  const page = `<!doctype html><html lang="en"><head><script src="/gsap.js"></script></head><body><script>
  window.failures=[]; window.addEventListener('error',e=>failures.push(e.message));
  window.addEventListener('unhandledrejection',e=>failures.push(String(e.reason)));
  console.warn=(...args)=>{if(args.join(' ').includes('target')) failures.push(args.join(' '));};
  console.error=(...args)=>failures.push(args.join(' '));
  const createTimeline=gsap.timeline;
  gsap.timeline=function(options){
    const timeline=createTimeline(options);
    for(const [method,index] of [['fromTo',3],['to',2],['set',2]]){
      const original=timeline[method];
      timeline[method]=function(...args){
        if(typeof args[index]==='number' && !Number.isFinite(args[index])) failures.push('Nonfinite '+method+' position for '+args[0]);
        return original.apply(this,args);
      };
    }
    return timeline;
  };
  window.mount=async(source,context={})=>{
    gsap.globalTimeline.clear();document.querySelectorAll('body > :not(script)').forEach(e=>e.remove());
    window.__timelines={};window.__scenes={};window.failures=[];
    const doc=new DOMParser().parseFromString(source,'text/html');const fragment=doc.querySelector('template').content.cloneNode(true);
    const scripts=[...fragment.querySelectorAll('script')].map(s=>s.textContent);fragment.querySelectorAll('script').forEach(s=>s.remove());
    const sceneRoot=fragment.querySelector('[data-composition-id]');window.__scenes[sceneRoot.dataset.compositionId]=context;
    document.body.append(fragment);Object.assign(sceneRoot.style,{width:(context.width||1080)+'px',height:(context.height||1920)+'px'});
    for(const source of scripts){const s=document.createElement('script');s.textContent=source;document.body.append(s);}
    await document.fonts.ready;
  };
  </script></body></html>`;
  const server = createServer(async (req, res) => {
    try {
      if (req.url === '/') { res.setHeader('Content-Type', 'text/html');res.end(page); }
      else if (req.url === '/gsap.js') { res.setHeader('Content-Type', 'text/javascript');res.end(gsap); }
      else if (/^\/assets\/fonts\/[\w.-]+$/.test(req.url)) res.end(await readFile(root + req.url.slice(1)));
      else { res.statusCode=404;res.end(); }
    } catch { res.statusCode=500;res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const session = `editonny-template-scenes-${process.pid}`, exec = promisify(execFile);
  const browser = async (...args) => {
    const { stdout } = await exec('agent-browser', ['--session', session, '--json', ...args], { timeout:30000,maxBuffer:2**20 });
    const result = JSON.parse(stdout);assert.equal(result.success,true,JSON.stringify(result.error));return result.data;
  };
  const evaluate = async source => (await browser('eval',source)).result;
  const usedIds = new Set();
  try {
    await browser('open',`http://127.0.0.1:${server.address().port}`);
    for(const scene of scenes){
      await evaluate(`window.mount(${JSON.stringify(scene.html)})`);
      await browser('wait','--fn',`Boolean(window.__timelines[${JSON.stringify(scene.id)}])`);
      const result=await evaluate(`(()=>{
        const tl=window.__timelines[${JSON.stringify(scene.id)}],ids=[...document.querySelectorAll('[id]')].map(e=>e.id);
        const snapshots=[];let typingVisible=false,beforeVisible=false,afterVisible=false;
        for(let t=0;t<=tl.duration();t+=.05){tl.seek(t,false);typingVisible ||= [...document.querySelectorAll('.conversation-typing')].some(e=>Number(getComputedStyle(e).opacity)>.5);
          beforeVisible ||= Number(document.getElementById('file-badge-txt-before')?.style.opacity)>.9;
          afterVisible ||= Number(document.getElementById('file-badge-txt-after')?.style.opacity)>.9;}
        for(const t of [2,5,2]){tl.seek(t,false);snapshots.push([...document.querySelectorAll('[id]')].map(e=>e.getAttribute('style')).join('|'));}
        const diagramColors=[...document.querySelectorAll(".diagram-node")].map(e=>e.style.borderColor);
        return {diagramColors,ids,failures,typingVisible,beforeVisible,afterVisible,deterministic:snapshots[0]===snapshots[2],targets:tl.getChildren().flatMap(t=>t.targets?.()||[]).length};
      })()`);
      assert.deepEqual(result.failures,[],scene.id);assert.ok(result.targets>0,scene.id);assert.ok(result.deterministic,`${scene.id} backward seek`);
      assert.equal(new Set(result.ids).size,result.ids.length,`${scene.id} duplicate IDs`);
      for(const id of result.ids.filter(id=>id!=='root')){assert.ok(!usedIds.has(id),`shared scene ID: ${id}`);usedIds.add(id);}
      if(scene.id==='diagram')assert.deepEqual(result.diagramColors,['rgb(102, 80, 128)','rgb(102, 80, 128)','rgb(229, 72, 77)','rgb(46, 158, 91)'],'semantic roles select their colors');
      if(scene.id==='conversation')assert.ok(result.typingVisible,'typing indicator must become visible');
      if(scene.id==='file'){assert.ok(result.beforeVisible,'initial badge must appear');assert.ok(result.afterVisible,'updated badge must appear');}
      const landscape={width:1920,height:1080,safeArea:[140,100,1780,940]};
      const framed={frame:{closedAt:.2,freeArea:[140,230,940,1200]}};
      const variants=[{name:'landscape',context:landscape},{name:'framed',context:framed}];
      if(scene.id==='diagram')variants.push(
        ...['groups','cause'].flatMap(shape=>[{name:shape,params:{shape}},{name:shape+' landscape',params:{shape},context:landscape}]),
        {name:'compact grid',params:{area:[100,100,980,400],direction:'auto'}},
        {name:'no title or example',params:{title:null,example:null,numbers:false}});
      if(scene.id==='before-after')variants.push({name:'image comparison',params:{type:'image',arrangement:'side',difference:{at:2,box:[.1,.1,.5,.5]}}});
      if(scene.id==='file')variants.push({name:'no badge or clock',params:{badge:null,duration:null}});
      if(scene.id==='conversation')variants.push({name:'no initial messages',params:{messages:[]}});
      if(scene.id==='gauge')variants.push({name:'badge and initial reading',params:{reading:'HIGH',badge:{text:'DONE',at:4}}});
      if(scene.id==='notes')variants.push({name:'no title',params:{title:null}});
      if(scene.id==='search')variants.push({name:'no result or callout',params:{result:null,callout:null,submit:null}},{name:'plain callout',params:{callout:'Read more'}});
      if(scene.id==='terminal')variants.push({name:'no badges or submit',params:{badge:null,icon:null,topLabel:null,submit:null,end:3}});
      if(['gauge','notes','search','terminal'].includes(scene.id))variants.push({name:'above face',params:{area:'aboveFace'},context:{face:{top:600,x0:300,y0:650,x1:700,y1:1100}}});
      if(['search','terminal'].includes(scene.id)){
        const words=(scene.id==='search'?'how to organize tasks result':'edit this video for me ready').split(' ').map((text,i)=>({text,t:1+i*.4,end:1.3+i*.4}));
        variants.push({name:'spoken word typing',context:{words}});
      }
      for(const variant of variants){
        const source=scene.html.replace('const C = Object.assign',`Object.assign(PARAM,${JSON.stringify(variant.params||{})}); const C = Object.assign`);
        const label=`${scene.id}: ${variant.name}`;
        await evaluate(`window.mount(${JSON.stringify(source)},${JSON.stringify(variant.context||{})})`);
        assert.deepEqual(await evaluate('window.failures'),[],label);
        await browser('wait','--fn',`Boolean(window.__timelines[${JSON.stringify(scene.id)}])`);
        const checked=await evaluate(`(()=>{const tl=window.__timelines[${JSON.stringify(scene.id)}];for(const t of [0,tl.duration()/2,tl.duration(),1])tl.seek(t,false);return {failures,invalid:[...document.querySelectorAll('[style]')].some(e=>/NaN|Infinity/.test(e.getAttribute('style'))),nodes:document.querySelectorAll('.diagram-node').length};})()`);
        assert.deepEqual(checked.failures,[],label);assert.equal(checked.invalid,false,label);
        if(scene.id==='diagram')assert.equal(checked.nodes,4,`${label}: retain every node`);
      }
    }
    for(const scene of propertyScenes){
      await evaluate(`window.mount(${JSON.stringify(scene.html)})`);
      const checked=await evaluate(`(()=>{
        const timeline=window.__timelines[${JSON.stringify(scene.id)}];
        const snapshots=[];
        for(const time of [.3,2,5,2]){timeline.seek(time,false);snapshots.push([...document.querySelectorAll('[style]')].map(e=>e.getAttribute('style')).join('|'));}
        const invalid=timeline.getChildren().some(t=>!Number.isFinite(t.startTime())||!Number.isFinite(t.duration()));
        timeline.seek(.3,false);
        const background=document.querySelector('[id$="-background"]');
        return {failures,invalid,visible:Number(getComputedStyle(background).opacity)>.9,deterministic:snapshots[1]===snapshots[3]};
      })()`);
      assert.deepEqual(checked.failures,[],scene.id);
      assert.equal(checked.invalid,false,`${scene.id}: finite tween timing`);
      assert.equal(checked.visible,true,`${scene.id}: background visible after entrance`);
      assert.equal(checked.deterministic,true,`${scene.id}: backward seek`);
    }
    // Fixed layouts as scene --use writes them: renamed copies keep runtime-built selectors, and a final-frame hold skips the exit.
    const fixedCopies=JSON.parse((await exec(python,[...pythonPrefix,'-B','-c',`import json, apply
out = []
for c in apply.catalog().values():
    if c['layout'] in ('fullscreen', 'frame') and not c.get('adaptation'):
        new, d = c['id'] + '-01', float(c['duration']); copy = apply.replace_id(open('scenes/%s.html' % c['id'], encoding='utf-8').read(), c['id'], new)
        fit = lambda html: (apply.wait_for_frame if c['layout'] == 'frame' else apply.shorten)(apply.hold_exit(html, new, 20, d - .5), new, d, round(d * .9, 3))
        out.append({'id': new, 'copy': copy, 'hold': fit(copy)})
        if c['id'] == 'location-map-panel': out[-1]['late'] = fit(apply.set_parameter(copy, 'others', 'SCHOOL'))
print(json.dumps(out))`],{cwd:fileURLToPath(new URL('../skills/editonny/visuals/',import.meta.url)),maxBuffer:2**27})).stdout);
    assert.ok(fixedCopies.length>=38,'fixed-layout scenes');
    for(const scene of fixedCopies){
      await evaluate(`window.mount(${JSON.stringify(scene.copy)})`);
      await browser('wait','--fn',`Boolean(window.__timelines[${JSON.stringify(scene.id)}])`);
      assert.deepEqual(await evaluate(`(()=>{const tl=window.__timelines[${JSON.stringify(scene.id)}];for(let t=0;t<=tl.duration();t+=.25)tl.seek(t,false);return failures;})()`),[],`${scene.id}: copied selectors`);
      // The held copy was written for a cut ending at 20 s: ending there keeps it visible, moving it earlier restores the exit.
      const ending=async(source,context)=>{await evaluate(`window.mount(${JSON.stringify(source)},${JSON.stringify(context)})`);
        await browser('wait','--fn',`Boolean(window.__timelines[${JSON.stringify(scene.id)}])`);
        return evaluate(`(()=>{const tl=window.__timelines[${JSON.stringify(scene.id)}],opacity=()=>[...document.querySelectorAll('[id]')].map(e=>Number(getComputedStyle(e).opacity));
          tl.seek(tl.duration()-.6,false);const before=opacity();tl.seek(tl.duration(),false);const after=opacity();
          return {failures,root:Number(getComputedStyle(document.querySelector('[data-composition-id] > [id]')).opacity),faded:after.filter((o,i)=>before[i]>.5&&o<.1).length,
            late:[...document.querySelectorAll('[id]')].filter(e=>e.textContent==='SCHOOL').map(e=>Number(getComputedStyle(e).opacity))};})()`);};
      const held=await ending(scene.hold,{end:20});
      assert.deepEqual(held.failures,[],`${scene.id}: hold`);assert.ok(held.root>.99,`${scene.id}: visible on the final frame`);assert.equal(held.faded,0,`${scene.id}: no exit on the final frame`);
      assert.ok((await ending(scene.hold,{end:10})).root<.01,`${scene.id}: exit restored when the scene moves earlier`);
      if(scene.late){const late=await ending(scene.late,{end:20,words:[{text:'SCHOOL',t:4.9,end:5.1}]});assert.ok(late.late.length&&late.late.every(o=>o>.99),`${scene.id}: late word entrance on the final frame`);}
    }
    const recovery=await readFile(new URL('../skills/editonny/visuals/scenes/muscle-recovery.html',import.meta.url),'utf8');
    await evaluate(`window.mount(${JSON.stringify(recovery)})`);
    assert.deepEqual(await evaluate(`[...document.querySelectorAll('svg [fill]')].map(e=>e.getAttribute('fill')).filter(fill=>!CSS.supports('fill',fill))`),[],'recovery icons use valid configured colors');
    const attempts=await readFile(new URL('../skills/editonny/visuals/scenes/attempt-counter.html',import.meta.url),'utf8');
    for(const correct of [0,1,2,3]){
      await evaluate(`window.mount(${JSON.stringify(attempts)}.replace('correct: 3','correct: '+${correct}))`);
      const successful=await evaluate(`[...document.querySelectorAll('.attemptcounter-it')].flatMap((e,i)=>e.querySelectorAll('svg path').length===1?[i+1]:[])`);
      assert.deepEqual(successful,correct?[correct]:[],`attempt counter honors correct=${correct}`);
    }
    // Caption scenes show their demo phrase only standalone; an interval without speech gets no invented caption.
    const sceneDir=new URL('../skills/editonny/visuals/scenes/',import.meta.url);
    const textScenes=(await Promise.all((await readdir(sceneDir)).filter(f=>f.startsWith('text-')).map(async f=>readFile(new URL(f,sceneDir),'utf8')))).filter(html=>html.includes('C.words')&&html.split('screens(').length>2);   // Caption scenes call screens().
    assert.ok(textScenes.length>=9,'caption scenes with a standalone demo');
    for(const html of textScenes){
      const demo=async context=>{await evaluate(`window.mount(${JSON.stringify(html)},${JSON.stringify(context)})`);
        await browser('wait','--fn','Object.keys(window.__timelines).length>0');
        return evaluate(`(()=>{let demo=false;for(let t=0;t<=3.4;t+=.1)for(const tl of Object.values(window.__timelines)){tl.seek(t,false);demo||=/editor/i.test(document.querySelector('[data-composition-id]').textContent);}return {failures,demo};})()`);};
      const id=html.match(/data-composition-id="([^"]+)"/)[1];
      assert.deepEqual(await demo({}),{failures:[],demo:true},`${id}: standalone demo`);
      assert.deepEqual(await demo({words:[]}),{failures:[],demo:false},`${id}: silent interval`);
    }
  } finally {
    try { await browser('close'); }
    finally { await new Promise(resolve=>server.close(resolve)); }
  }
});
