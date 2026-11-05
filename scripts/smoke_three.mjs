import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { agentBrowserCommand } from './agent-browser.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const output = resolve(root, '.work');
await mkdir(output, { recursive: true });
const project = await mkdtemp(resolve(output, 'three project '));
const session = `editonny-three-${process.pid}`;
const exec = promisify(execFile);
const [agentBrowser, ...agentBrowserPrefix] = agentBrowserCommand();
const browser = async (...args) => {
  const { stdout } = await exec(agentBrowser, [...agentBrowserPrefix, '--session', session, '--json', ...args], { timeout: 30000 });
  const response = JSON.parse(stdout);
  assert.equal(response.success, true, JSON.stringify(response.error));
  return response.data;
};
const missing = [];
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.ttf': 'font/ttf' };
const server = createServer(async (req, res) => {
  try {
    const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    if (pathname === '/favicon.ico') { res.writeHead(204).end(); return; }
    const path = resolve(project, `.${pathname === '/' ? '/index.html' : pathname}`);
    if (!path.startsWith(project + sep)) { res.writeHead(403).end(); return; }
    const data = await readFile(path);
    res.writeHead(200, { 'Content-Type': mime[extname(path)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    missing.push(req.url);
    res.writeHead(404).end();
  }
});

try {
  const template = resolve(root, 'skills/editonny/engine/project-template');
  for (const part of ['assets/three', 'assets/fonts', 'compositions/scenes/3d']) {
    await cp(resolve(template, part), resolve(project, part), { recursive: true });
  }
  await writeFile(resolve(project, 'index.html'), `<!doctype html>
<meta charset="utf-8"><title>Smoke 3D editonny</title>
<style>
@font-face {font-family:Montserrat;src:url('/assets/fonts/Montserrat.ttf')}
body {margin:0;background:#e7e9df} canvas {width:640px;height:640px}
</style><canvas id="canvas"></canvas><div id="labels"></div>
<script>window.smokeErrors=[];addEventListener('error',e=>smokeErrors.push(e.message));
addEventListener('unhandledrejection',e=>smokeErrors.push(String(e.reason)));</script>
<script type="module">
import {initialize, THREE} from './compositions/scenes/3d/three-scene.js';
const C={width:640,height:640,duration:1,colors:{base:'#242b28',paper:'#e7e9df',ink:'#242b28',line:'#9aaa9f',accent:'#65886c'}};
window.draw=await initialize({id:'smoke',C,canvas:document.querySelector('canvas'),layer:document.querySelector('#labels'),background:'#e7e9df',build:async P=>{
  P.lights(); P.floor();
  const cube=P.solid(P.box({color:'#d6a359'}),'box'); cube.position.x=-1;P.scene.add(cube);
  const text=await P.volumeText('3D',{height:0.7,fontSizePx:100});text.position.set(0.3,0,0);P.scene.add(text);
  const image=new Image();image.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="#65886c"/></svg>');await image.decode();
  const media=P.mediaTexture(image,{width:32,height:32});
  P.scene.add(new THREE.Mesh(new THREE.PlaneGeometry(1,1),new THREE.MeshBasicMaterial({map:media.texture})));
  const labels=P.labels([{text:'Local test',side:'above',size:30}]);
  window.textureReady=media.update();
  return t=>{cube.rotation.y=t;P.orbit({target:[0,0.6,0],dist:9,azim:25,elev:15});labels.update([{point:[-1,1,0]}]);};
}});draw(0.5);
</script>`);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  await browser('open', `http://127.0.0.1:${server.address().port}`);
  await browser('wait', '--fn', 'window.__scene3d?.smoke?.ready || window.__scene3d?.smoke?.error || window.smokeErrors.length');
  const result = (await browser('eval', '({state:window.__scene3d?.smoke,textureReady:window.textureReady,errors:window.smokeErrors})')).result;
  assert.equal(result.state?.ready, true, JSON.stringify(result));
  assert.equal(result.textureReady, true);
  assert.deepEqual(result.state.warnings, []);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(missing, []);
  await browser('screenshot', resolve(output, 'three-smoke.png'));
  console.log(`3D passed: solid, text, texture, label; image at ${resolve(output, 'three-smoke.png')}`);
} finally {
  try {
    await browser('close');
  } finally {
    try { await new Promise(resolve => server.close(resolve)); }
    finally { await rm(project, { recursive: true, force: true }); }
  }
}
