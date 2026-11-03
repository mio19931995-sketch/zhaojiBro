// Windows Electron -> local API -> real FFmpeg -> native download, isolated data only.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {_electron as electron} from 'playwright';

const root=process.cwd();
await fs.mkdir(path.join(root,'.test-data'),{recursive:true});
const data=await fs.mkdtemp(path.join(root,'.test-data','video-tools-desktop-'));
const source=path.join(data,'原始视频.mp4');
const silent=path.join(data,'无音轨视频.mp4');
const logo=path.join(data,'品牌图片.png');
function ffmpeg(args){
  const result=spawnSync('ffmpeg',['-hide_banner','-loglevel','error','-y',...args],{encoding:'utf8',windowsHide:true,timeout:45000});
  assert.equal(result.status,0,result.stderr||result.error?.message);
}
ffmpeg(['-f','lavfi','-i','color=c=0x203040:s=320x240:r=30:d=3','-f','lavfi','-i','sine=frequency=440:duration=3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',source]);
ffmpeg(['-i',source,'-an','-c:v','copy',silent]);
ffmpeg(['-f','lavfi','-i','color=c=red:s=80x40','-frames:v','1','-threads','1',logo]);
const hash=async file=>crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex');
const originalHash=await hash(source);
const env={...process.env,LULU_DATA_DIR:data,LULU_USER_DATA:path.join(data,'profile')};
delete env.ELECTRON_RUN_AS_NODE;
let app,page,base;
const checks=[];
const errors=[];
async function waitFor(fn,label,timeout=45000){
  const deadline=Date.now()+timeout;
  while(Date.now()<deadline){if(await fn())return;await new Promise(r=>setTimeout(r,150));}
  throw Error('Timed out: '+label);
}
async function api(route){const response=await fetch(base+'/api'+route);assert(response.ok,route+': '+response.status);return response.json();}
async function importFile(file){
  const response=page.waitForResponse(r=>r.url().endsWith('/api/import')&&r.request().method()==='POST');
  await page.getByTestId('video-tools-import').setInputFiles(file);
  const r=await response;assert(r.ok(),await r.text());
  const [item]=await r.json();
  await waitFor(async()=>await page.getByTestId('video-tools-source').inputValue()===item.id&&await page.getByTestId('video-tools-drop').isEnabled(),'source ready');
  return item;
}
async function exportAndWait(){
  const response=page.waitForResponse(r=>r.url().endsWith('/api/video-tools/jobs')&&r.request().method()==='POST');
  await page.getByTestId('video-tools-export').click();
  const r=await response;assert(r.ok(),await r.text());const started=await r.json();
  let done;
  await waitFor(async()=>{done=(await api('/video-tools/jobs')).find(j=>j.id===started.id);return done&&!['queued','running'].includes(done.status);},'export completion',90000);
  assert.equal(done.status,'done',JSON.stringify(done));
  await page.locator(`[data-job-id="${done.id}"].done`).waitFor();
  return done;
}
async function download(job,name){
  const target=path.join(data,name);
  await app.evaluate((_electron,target)=>{global.__videoDownloadPath=target;},target);
  await page.locator(`[data-job-id="${job.id}"]`).getByRole('link',{name:'下载视频'}).click();
  await waitFor(async()=>await app.evaluate(()=>global.__videoDownloadDone)==='completed'&&await fs.stat(target).then(s=>s.size>0).catch(()=>false),'native video download');
  return target;
}
function probe(file){
  const r=spawnSync('ffprobe',['-v','error','-count_frames','-show_streams','-show_format','-of','json',file],{encoding:'utf8',windowsHide:true});
  assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);
}
function frame(file){
  const r=spawnSync('ffmpeg',['-hide_banner','-loglevel','error','-ss','1','-i',file,'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],{windowsHide:true,maxBuffer:1024*1024});
  assert.equal(r.status,0,r.stderr?.toString());assert.equal(r.stdout.length,320*240*3);return r.stdout;
}
try{
  app=await electron.launch({args:['electron/main.cjs'],cwd:root,env,timeout:45000});
  page=await app.firstWindow();page.on('pageerror',e=>errors.push(e.message));
  await page.getByLabel('作品链接').waitFor();
  ({url:base}=JSON.parse(await fs.readFile(path.join(data,'logs/runtime.json'),'utf8')));
  await app.evaluate(({BrowserWindow})=>{
    const win=BrowserWindow.getAllWindows()[0];win.setContentSize(1280,900);
    win.webContents.session.on('will-download',(_event,item)=>{
      global.__videoDownloadDone='pending';item.setSavePath(global.__videoDownloadPath);
      item.once('done',(_event,state)=>{global.__videoDownloadDone=state;});
    });
  });
  await page.locator('summary').filter({hasText:'视频与配音'}).click();
  await page.getByRole('button',{name:'视频处理',exact:true}).click();
  const imported=await importFile(source);
  assert.equal((await api('/items/'+imported.id)).status,'idle','Import must not run transcription');
  const storedSource=(await api('/items/'+imported.id)).media_path;
  await page.getByTestId('video-tools-drop').fill('30');
  assert(await page.getByTestId('video-tools-export').isDisabled());
  await page.getByTestId('video-tools-drop').fill('6');
  await page.getByTestId('video-tools-watermark-type').selectOption('text');
  await page.getByTestId('video-tools-watermark-text').fill("满满超人 100% : ' [ ]");
  const preview=page.waitForResponse(r=>r.url().endsWith('/api/video-tools/preview')&&r.request().method()==='POST');
  await page.getByTestId('video-tools-preview').click();
  const image=await preview;assert(image.ok(),await image.text());
  assert(image.headers()['content-type'].startsWith('image/png'));
  await fs.writeFile(path.join(data,'actual-watermark.png'),await image.body());
  await page.locator('.vt-effect-frame').waitFor();
  await page.screenshot({path:path.join(data,'desktop-preview.png'),fullPage:true});
  const job=await exportAndWait();assert.equal(job.options.drop_per_second,6);
  const target=await download(job,'文字水印结果.mp4');
  const metadata=probe(target);const picture=metadata.streams.find(s=>s.codec_type==='video');const audio=metadata.streams.find(s=>s.codec_type==='audio');
  assert.equal(picture.avg_frame_rate,'24/1');assert.equal(Number(picture.nb_read_frames),72);
  assert.equal(picture.width,320);assert.equal(picture.height,240);
  assert(Math.abs(Number(picture.duration)-3)<.01);assert(audio&&Math.abs(Number(audio.duration)-3)<.03);
  const rgb=frame(target);let bright=0;
  for(let i=0;i<rgb.length;i+=3)if(rgb[i]>130&&rgb[i+1]>130&&rgb[i+2]>130)bright++;
  assert(bright>30,'Text watermark must be burned into exported video');
  assert.equal(await hash(source),originalHash);assert.equal(await hash(storedSource),originalHash);
  checks.push('Native import without ASR, invalid count blocked, real PNG preview, Chinese watermark burned in, 30→24 fps / 72 frames / 3 seconds with audio, native download, originals unchanged');
  await page.locator(`[data-job-id="${job.id}"]`).getByRole('button',{name:'预览结果'}).click();
  await waitFor(()=>page.getByLabel('处理后的视频预览').evaluate(v=>v.readyState>=2&&v.videoWidth===320),'export video playback');
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].setContentSize(920,760));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Narrow window must not overflow horizontally');
  await page.screenshot({path:path.join(data,'desktop-narrow.png'),fullPage:true});
  checks.push('Exported MP4 decodes inside Electron; 920px layout fits');
  await importFile(silent);
  await page.getByTestId('video-tools-drop').fill('0');
  await page.getByTestId('video-tools-watermark-type').selectOption('image');
  const uploaded=page.waitForResponse(r=>r.url().endsWith('/api/video-tools/watermark')&&r.request().method()==='POST');
  await page.getByTestId('video-tools-watermark-file').setInputFiles(logo);
  assert((await uploaded).ok());
  const imageJob=await exportAndWait();const imageTarget=await download(imageJob,'图片水印结果.mp4');
  const imageMetadata=probe(imageTarget);
  assert.equal(imageMetadata.streams.find(s=>s.codec_type==='video').nb_read_frames,'90');
  assert(!imageMetadata.streams.some(s=>s.codec_type==='audio'));
  const pixels=frame(imageTarget);let red=0;
  for(let i=0;i<pixels.length;i+=3)if(pixels[i]>150&&pixels[i]>pixels[i+1]*2&&pixels[i]>pixels[i+2]*2)red++;
  assert(red>30,'Uploaded logo must appear in exported video');
  checks.push('Uploaded image watermark, zero frame removal, silent video and native download verified');
  await page.reload();await page.getByTestId('video-tools-source').waitFor();
  await page.locator(`[data-job-id="${imageJob.id}"].done`).waitFor();
  assert.equal(await page.getByTestId('video-tools-watermark-type').inputValue(),'image');
  checks.push('Page reload retains settings and completed jobs');
  assert.deepEqual(errors,[]);
  await fs.writeFile(path.join(data,'results.json'),JSON.stringify({ok:true,checks,data},null,2));
  console.log(JSON.stringify({ok:true,checks,data}));
}catch(error){
  if(page)await page.screenshot({path:path.join(data,'failure.png'),fullPage:true}).catch(()=>{});
  console.error('Artifacts:',data);throw error;
}finally{
  if(app)await app.close();
}
