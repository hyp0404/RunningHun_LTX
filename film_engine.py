"""Text-to-film orchestration; register(mcp, legacy_server_globals).

Planning, images and reviews come from the current ChatGPT conversation. Only RunningHub TTS/video are called by this server.
No claim of deterministic character identity, action quality or lip sync.
One active step per job; files checkpointed on a persistent volume.
"""
from __future__ import annotations
import asyncio
import fcntl
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import time
import wave
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict
from starlette.responses import FileResponse, JSONResponse


class Character(BaseModel):
    id: str = Field(pattern=r'^c[0-9]+$')
    appearance: str = Field(min_length=5, max_length=1200)
    voice: str = Field(min_length=3, max_length=800)


class Location(BaseModel):
    id: str = Field(pattern=r'^l[0-9]+$')
    appearance: str = Field(min_length=5, max_length=1200)


class Shot(BaseModel):
    location: str
    cast: list[str] = Field(max_length=2)
    duration: int = Field(ge=1, le=5)
    mode: Literal['i2v', 'flf']
    transition: Literal['cut', 'continue']
    start: str = Field(min_length=5, max_length=1500)
    end: str = Field(default='', max_length=1500)
    action: str = Field(min_length=5, max_length=1500)
    camera: str = Field(min_length=3, max_length=600)
    speaker: str = ''
    dialogue: str = Field(default='', max_length=80)


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str
    style: str
    characters: list[Character] = Field(min_length=1, max_length=4)
    locations: list[Location] = Field(min_length=1, max_length=6)
    shots: list[Shot] = Field(min_length=1, max_length=24)


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid')
    identity_ok: bool
    costume_ok: bool
    action_ok: bool
    background_ok: bool
    no_text: bool
    boundary_ok: bool
    issues: list[str]
    retry_instruction: str


def validate_plan(data: dict, total: int) -> dict:
    p = Plan.model_validate(data)
    chars = {c.id for c in p.characters}
    locs = {l.id for l in p.locations}
    if len(chars) != len(p.characters) or len(locs) != len(p.locations):
        raise ValueError('Duplicate character or location IDs')
    if sum(s.duration for s in p.shots) != total:
        raise ValueError('Storyboard durations must sum exactly to requested duration')
    for i,s in enumerate(p.shots):
        if s.location not in locs or not set(s.cast) <= chars:
            raise ValueError('Undefined storyboard reference')
        if s.dialogue and (s.speaker not in s.cast or len(s.dialogue) > 3*s.duration):
            raise ValueError('Dialogue must name a visible speaker and fit the shot (<=3 characters/sec)')
        if s.mode == 'flf' and not s.end:
            raise ValueError('Transformation shots require an explicit end state')
        if s.transition == 'continue' and (i == 0 or s.location != p.shots[i-1].location or s.cast != p.shots[i-1].cast):
            raise ValueError('Continue requires the same location and cast; otherwise use cut')
    return p.model_dump()


def allocate_durations(total: int) -> list[int]:
    if not 5 <= total <= 120:
        raise ValueError('Duration must be 5–120 whole seconds')
    count = math.ceil(total/5)
    q,r = divmod(total,count)
    return [q+int(i<r) for i in range(count)]


async def command(*args: str, timeout: int = 240) -> bytes:
    proc = await asyncio.create_subprocess_exec(*map(str,args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout,stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        proc.kill(); await proc.wait(); raise
    if proc.returncode:
        # ffmpeg receives only locally constructed paths, never secrets.
        raise RuntimeError(stderr.decode(errors='replace')[-1000:])
    return stdout


async def probe(path: Path) -> dict:
    return json.loads(await command('ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)))


def silent_wav(seconds: float) -> bytes:
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as w:
        w.setparams((2,2,48000,0,'NONE','not compressed'))
        w.writeframes(b'\0'*(int((seconds+.1)*48000)*4))
    return buffer.getvalue()


class Engine:
    def __init__(self, legacy: dict):
        self.legacy=legacy
        self.tasks: dict[str,asyncio.Task] = {}
        self.root=Path(os.getenv('FILM_DATA_DIR','/data/films'))
        self.root.mkdir(parents=True,exist_ok=True)

    def directory(self,job_id: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{32}',job_id):
            raise ValueError('Invalid job ID')
        return self.root/job_id

    def save(self,j: dict):
        d=self.directory(j['id']);d.mkdir(exist_ok=True)
        j['updated_at']=time.time()
        temp=d/'state.tmp';temp.write_text(json.dumps(j,ensure_ascii=False,indent=2))
        os.replace(temp,d/'state.json')

    def load(self,job_id: str) -> dict:
        return json.loads((self.directory(job_id)/'state.json').read_text())

    def workflow(self,kind: str) -> tuple[str,dict]:
        wid=os.getenv('FILM_'+kind.upper()+'_WORKFLOW_ID','').strip()
        if not wid.isdigit():raise ValueError(f'Configure FILM_{kind.upper()}_WORKFLOW_ID')
        path=Path(__file__).with_name(f'film_{kind}_api.json')
        graph=json.loads(path.read_text())
        return wid,graph


    def link(self,j: dict,name: str) -> str:
        expires=int(time.time())+86400
        msg=f"{j['id']}/{name}:{expires}"
        sig=hmac.new(os.environ['FILM_DOWNLOAD_SECRET'].encode(),msg.encode(),hashlib.sha256).hexdigest()
        return f"{os.environ['FILM_PUBLIC_BASE_URL'].rstrip('/')}/film-files/{j['id']}/{name}?expires={expires}&sig={sig}"


    def charge(self,j:dict,kind:str):
        if j['external_calls'] >= j['max_external_calls']:raise ValueError('External-call budget reached')
        j['external_calls']+=1
        j['pending_external']=kind
        self.save(j)

    def settled(self,j:dict):
        j.pop('pending_external',None)
        self.save(j)



    async def upload(self,path:Path,kind:str) -> str:
        settings=self.legacy['Settings'].from_env()
        return await self.legacy['RunningHubClient'](settings).upload_bytes(path.read_bytes(),path.name,kind)

    async def download(self,url:str,path:Path,kind:str):
        settings=self.legacy['Settings'].from_env()
        data,_,_=await self.legacy['download_public_file'](settings,url,expected_kind=kind)
        path.write_bytes(data)

    async def rh_state(self,task:str,kind:str):
        settings=self.legacy['Settings'].from_env()
        return await self.legacy['runninghub_task_state'](self.legacy['RunningHubClient'](settings),task,kind)


    async def submit_tts(self,j:dict,s:dict,shot:dict):
        settings=self.legacy['Settings'].from_env();client=self.legacy['RunningHubClient'](settings)
        mapping,_=await self.legacy['resolve_app_nodes'](settings,client,settings.qwen,self.legacy['TTS_ROLE_ALIASES'])
        if 'script' not in mapping or 'voice_a' not in mapping:raise ValueError('Qwen script and voice_a mappings are required')
        voice=next(c['voice'] for c in j['plan']['characters'] if c['id']==shot['speaker'])
        values=self.legacy['film_tts_values'](shot['dialogue'],voice)
        nodes=self.legacy['make_node_info_list'](mapping,values,settings.qwen.extra_node_info)
        self.charge(j,'RunningHub TTS')
        task=await client.run_ai_app(settings.qwen,nodes)
        s['tts_task']=str(task['taskId']);s['stage']='TTS_WAIT';self.settled(j)

    async def recover_failed_tts(self,j:dict):
        """Recheck the provider before retiring a failed TTS task.

        Unknown states/errors retain the task; successful/running tasks are
        reused. Explicit recovery may resubmit a confirmed failure once.
        """
        i=j.get('shot_index',0)
        if i>=len(j['shots']):return
        s=j['shots'][i]
        if s.get('stage')!='TTS_WAIT':return
        task_id=s.get('tts_task')
        if not task_id:raise ValueError('Missing TTS task ID; inspect provider history before recovery.')
        state,urls,reason=await self.rh_state(task_id,'audio')
        if state=='FAILED':
            s.setdefault('tts_failure_history',[]).append({'task_id':task_id,'reason':reason})
            s.pop('tts_task',None)
            s.pop('audio_url',None)
            s['stage']='AUDIO'
        elif state=='SUCCESS':
            if not urls:raise ValueError('Successful TTS task returned no audio URL; refusing to resubmit.')
            s['audio_url']=urls[0];s['stage']='AUDIO_PREP'
        # Other states retain TTS_WAIT and the original task ID.

    async def submit_video(self,j:dict,s:dict,shot:dict,d:Path):
        wid,graph=self.workflow(shot['mode'])
        image=await self.upload(d/'start.png','image/png')
        audio=await self.upload(d/'audio.wav','audio/wav')
        descriptors=' '.join(c['id']+': '+c['appearance'] for c in j['plan']['characters'] if c['id'] in shot['cast'])
        speech=(f"Only {shot['speaker']} speaks, synchronize their lips to the supplied speech audio; other visible people keep mouths closed." if shot['dialogue'] else 'No speech, keep mouths closed.')
        prompt=' '.join([j['plan']['style'],descriptors,shot['action'],shot['camera'],speech,'Normal playback speed. No text, subtitles or watermarks.',s.get('retry_instruction','')])
        vals=[('61','image',image),('60','audio',audio),('320','text',prompt),('135','value',shot['duration']),('62','value',24),('77','noise_seed',j['seed']+j['shot_index']*100+s['attempt'])]
        if shot['mode']=='flf':
            vals += [('301','image',await self.upload(d/'end.png','image/png')),('310','frame_idx',0),('311','frame_idx',-1),('310','strength',1.0),('311','strength',1.0)]
        nodes=[{'nodeId':n,'fieldName':f,'fieldValue':v} for n,f,v in vals]
        self.legacy['apply_workflow_overrides'](graph,nodes)
        s['submitted_nodes']=nodes
        settings=self.legacy['Settings'].from_env();client=self.legacy['RunningHubClient'](settings)
        self.charge(j,'RunningHub LTX')
        task=await client.run_workflow(workflow_id=wid,node_info_list=nodes,access_password=os.getenv('FILM_'+shot['mode'].upper()+'_ACCESS_PASSWORD',''))
        s['video_task']=str(task['taskId']);s['stage']='VIDEO_WAIT';self.settled(j)

    async def normalize(self,d:Path,duration:int):
        info=await probe(d/'raw.mp4')
        video=next((s for s in info['streams'] if s['codec_type']=='video'),None)
        if not video or float(video.get('duration',info['format']['duration'])) < duration-.02:
            raise ValueError('Generated video is shorter than the shot; refusing to slow down or freeze-pad')
        await command('ffmpeg','-v','error','-y','-i',str(d/'raw.mp4'),'-i',str(d/'audio.wav'),
            '-map','0:v:0','-map','1:a:0','-t',str(duration),'-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24',
            '-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2','-movflags','+faststart',str(d/'clip.mp4'))
        for name,t in [('first',0),('quarter',duration*.25),('middle',duration*.5),('threequarter',duration*.75),('last',duration-1/24)]:
            await command('ffmpeg','-v','error','-y','-ss',str(t),'-i',str(d/'clip.mp4'),'-frames:v','1',str(d/(name+'.png')))


    async def merge(self,j:dict):
        d=self.directory(j['id'])
        # All clips have matching codec, dimensions, rate and duration. Hard cuts
        # preserve planned times; continuous shots reuse the actual preceding last frame.
        listing=d/'concat.txt'
        listing.write_text(''.join(f"file 'shot{i:03d}/clip.mp4'\n" for i in range(len(j['shots']))))
        await command('ffmpeg','-v','error','-y','-f','concat','-safe','1','-i',str(listing),'-c:v','libx264','-preset','fast','-crf','20','-c:a','aac','-t',str(j['target_seconds']),'-movflags','+faststart',str(d/'final.mp4'),timeout=600)
        if j.get('music_file'):
            await command('ffmpeg','-v','error','-y','-i',str(d/'final.mp4'),'-stream_loop','-1','-i',j['music_file'],
                '-filter_complex','[1:a]volume=0.10[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]',
                '-map','0:v','-map','[a]','-c:v','copy','-c:a','aac','-t',str(j['target_seconds']),'-movflags','+faststart',str(d/'mixed.mp4'))
            os.replace(d/'mixed.mp4',d/'final.mp4')
        info=await probe(d/'final.mp4')
        v=next(s for s in info['streams'] if s['codec_type']=='video')
        if abs(float(v.get('duration',info['format']['duration']))-j['target_seconds'])>.05:
            raise ValueError('Final duration verification failed')
        passed=all(s.get('review',{}).get('passed',False) for s in j['shots'])
        j['status']='COMPLETED_CHAT_REVIEW' if passed else 'DRAFT_REVIEW_REQUIRED'
        j['stage']='DONE'
        report={'status':j['status'],'human_review_required':True,'lip_sync_verified':False,
                'checks':'Review submitted by the current ChatGPT conversation; not automatic or exhaustive motion/lip-sync verification.',
                'target_seconds':j['target_seconds'],'actual_seconds':float(v.get('duration',info['format']['duration'])),
                'external_calls':j['external_calls'],'shots':j['shots']}
        (d/'review_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        self.save(j)


    async def guarded_step(self,job_id:str):
        # Advisory file lock also prevents concurrent steps across web workers.
        with (self.directory(job_id)/'step.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return
            j=self.load(job_id)
            if j['status']!='RUNNING':return
            if j.get('pending_external'):
                j['status']='NEEDS_ATTENTION';j['error']='An external request was interrupted before checkpointing. Inspect provider history before allowing a repeat.';self.save(j);return
            try:await self.step(j)
            except Exception as exc:
                j['status']='NEEDS_ATTENTION';j['error']=str(exc)[:1000];self.save(j)

    async def run_job(self,job_id:str):
        while self.load(job_id)['status']=='RUNNING':
            await self.guarded_step(job_id)
            j=self.load(job_id)
            if j['status']!='RUNNING':break
            i=j.get('shot_index',0)
            shots=j.get('shots',[])
            waiting=i<len(shots) and shots[i].get('stage') in {'TTS_WAIT','VIDEO_WAIT'}
            await asyncio.sleep(8 if waiting else .05)

    def inspect(self) -> dict:
        missing=[n for n in ['FILM_PUBLIC_BASE_URL','FILM_DOWNLOAD_SECRET','RUNNINGHUB_API_KEY'] if not os.getenv(n)]
        for binary in ['ffmpeg','ffprobe']:
            if not shutil.which(binary):missing.append(binary)
        if os.getenv('FILM_PUBLIC_BASE_URL') and not os.getenv('FILM_PUBLIC_BASE_URL','').startswith('https://'):
            missing.append('FILM_PUBLIC_BASE_URL must use HTTPS')
        for kind in ['i2v','flf']:
            try:self.workflow(kind)
            except (ValueError,OSError):missing.append(f'FILM_{kind.upper()}_WORKFLOW_ID / API file')
        if len(os.getenv('FILM_DOWNLOAD_SECRET',''))<32:missing.append('FILM_DOWNLOAD_SECRET >=32 characters')
        return {'ok':not missing,'missing':missing,'remote_compatibility_verified':False,
                'planning_images_review':'Provided in the current ChatGPT conversation; server makes no OpenAI API calls.',
                'costs':['RunningHub TTS/video','Hosting/storage as charged by your host'],
                'limits':'5–120 seconds, <=24 shots, one speaker per shot; no guarantee of action or lip-sync accuracy'}

    def summary(self,j:dict) -> dict:
        result={k:j.get(k) for k in ['id','status','stage','shot_index','error','external_calls','target_seconds','updated_at']}
        result['shot_count']=len(j['shots'])
        result['shots']=[{'index':i,'stage':s['stage'],'attempt':s.get('attempt',0),'review':s.get('review')} for i,s in enumerate(j['shots'])]
        d=self.directory(j['id'])
        for name in ['storyboard.json','review_report.json','final.mp4']:
            if (d/name).is_file():result[name]=self.link(j,name)
        i=j['shot_index']
        if i<len(j['shots']):
            prefix=f'shot{i:03d}/'
            result['review_assets']={name:self.link(j,prefix+name) for name in ['start.png','end.png','first.png','quarter.png','middle.png','threequarter.png','last.png','clip.mp4'] if (d/prefix/name).is_file()}
            result['current_shot']=j['plan']['shots'][i]
        result['next_action']={'WAITING_ASSETS':'Create the requested reference frame(s) in ChatGPT and call upload_film_frame.',
            'WAITING_REVIEW':'Download/view review_assets in ChatGPT, inspect the actual images/video, then call submit_film_review with concrete findings.',
            'RUNNING':'Call query_full_film to monitor.',
            'NEEDS_ATTENTION':'Inspect error; resume only after resolving it.',
            'BLOCKED_AUDIO':'Use revise_blocked_dialogue to shorten the current line.'}.get(j['status'],'Review final video and report.')
        result['human_review_required']=True
        return result

    async def step(self,j:dict):
        d=self.directory(j['id']);i=j['shot_index']
        if i>=len(j['shots']):return await self.merge(j)
        s=j['shots'][i];shot=j['plan']['shots'][i]
        sd=d/f'shot{i:03d}';sd.mkdir(exist_ok=True)
        if s['stage']=='START':
            if shot['transition']=='continue':
                shutil.copyfile(d/f'shot{i-1:03d}'/'last.png',sd/'start.png')
            elif not (sd/'start.png').is_file():
                j['status']='WAITING_ASSETS';self.save(j);return
            if shot['mode']=='flf' and not (sd/'end.png').is_file():
                j['status']='WAITING_ASSETS';self.save(j);return
            s['stage']='AUDIO';self.save(j)
        elif s['stage']=='AUDIO':
            if shot['dialogue']:await self.submit_tts(j,s,shot)
            else:
                (sd/'audio.wav').write_bytes(silent_wav(shot['duration']))
                s['stage']='VIDEO_SUBMIT';self.save(j)
        elif s['stage']=='TTS_WAIT':
            state,urls,reason=await self.rh_state(s['tts_task'],'audio')
            if state=='FAILED':raise ValueError('TTS failed: '+reason)
            if state=='SUCCESS':s['audio_url']=urls[0];s['stage']='AUDIO_PREP';self.save(j)
        elif s['stage']=='AUDIO_PREP':
            await self.download(s['audio_url'],sd/'voice_source','audio')
            length=float((await probe(sd/'voice_source'))['format']['duration'])
            if length>shot['duration']:
                j['status']='BLOCKED_AUDIO';j['error']=f'Shot {i}: speech {length:.2f}s exceeds {shot["duration"]}s. No speech was cut.';self.save(j);return
            await command('ffmpeg','-v','error','-y','-i',str(sd/'voice_source'),'-af','apad','-t',str(shot['duration']+.1),'-ar','48000','-ac','2',str(sd/'audio.wav'))
            s['stage']='VIDEO_SUBMIT';self.save(j)
        elif s['stage']=='VIDEO_SUBMIT':await self.submit_video(j,s,shot,sd)
        elif s['stage']=='VIDEO_WAIT':
            state,urls,reason=await self.rh_state(s['video_task'],'video')
            if state=='FAILED':
                if s['attempt']<j['max_retries']:
                    s['attempt']+=1;s['stage']='VIDEO_SUBMIT';s['last_generation_error']=reason;self.save(j)
                else:raise ValueError('Video failed after retry limit: '+reason)
            elif state=='SUCCESS':s['video_url']=urls[0];s['stage']='NORMALIZE';self.save(j)
        elif s['stage']=='NORMALIZE':
            await self.download(s['video_url'],sd/'raw.mp4','video')
            await self.normalize(sd,shot['duration'])
            s['stage']='REVIEW';j['status']='WAITING_REVIEW';self.save(j)

    def accept_review(self,j:dict,data:dict,action:str):
        if j['status']!='WAITING_REVIEW':raise ValueError('Job is not waiting for review')
        review=Review.model_validate(data).model_dump()
        review['passed']=all(review[k] for k in ['identity_ok','costume_ok','action_ok','background_ok','no_text','boundary_ok'])
        review['source']='current_chat_review'
        if action not in {'retry','advance','draft'}:raise ValueError('action must be retry, advance or draft')
        if action=='advance' and not review['passed']:raise ValueError('Failed review must retry or explicitly advance as draft')
        s=j['shots'][j['shot_index']]
        if action=='retry' and s['attempt']>=j['max_retries']:raise ValueError('Retry limit reached; revise inputs or explicitly keep a draft')
        s.setdefault('review_history',[]).append(review);s['review']=review
        if action=='retry':
            s['attempt']+=1;s['retry_instruction']=review['retry_instruction'];s['stage']='VIDEO_SUBMIT'
            d=self.directory(j['id'])/f"shot{j['shot_index']:03d}"
            shutil.copyfile(d/'clip.mp4',d/f"rejected_{s['attempt']}.mp4")
        else:
            if action=='draft':review['passed']=False
            s['stage']='DONE';j['shot_index']+=1
        j['status']='RUNNING';j['error']='';self.save(j)

    async def store_frame(self,j:dict,shot_index:int,role:str,data:bytes):
        if j['status'] not in {'WAITING_ASSETS','WAITING_REVIEW','BLOCKED_AUDIO','NEEDS_ATTENTION'}:
            raise ValueError('Wait until the active step stops before uploading images')
        if not j['shot_index']<=shot_index<len(j['shots']):raise ValueError('Cannot replace an already completed shot')
        shot=j['plan']['shots'][shot_index]
        if role not in {'start','end'} or (role=='end' and shot['mode']!='flf'):
            raise ValueError('End images apply only to flf shots')
        if role=='start' and shot['transition']=='continue':
            raise ValueError('Continue shots reuse the previous actual last frame automatically')
        s=j['shots'][shot_index]
        if s['stage'] not in {'START','REVIEW'}:raise ValueError('Image changes allowed only before generation or while reviewing')
        sd=self.directory(j['id'])/f'shot{shot_index:03d}';sd.mkdir(exist_ok=True)
        raw=sd/f'{role}.upload';raw.write_bytes(data)
        await command('ffmpeg','-v','error','-y','-i',str(raw),'-vf','scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2','-frames:v','1',str(sd/f'{role}.png'))
        # During review, retain WAITING_REVIEW so the chat explicitly requests retry.
        if j['status']=='WAITING_ASSETS':j['status']='RUNNING'
        self.save(j)


def register(mcp,legacy):
    globals()['OpenAIFile']=legacy['OpenAIFile']
    engine=Engine(legacy)

    def launch(job_id):
        task=engine.tasks.get(job_id)
        if task is None or task.done():engine.tasks[job_id]=asyncio.create_task(engine.run_job(job_id))

    @mcp.tool(annotations={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False})
    async def inspect_full_film() -> dict:
        """只读检查 RH 制片配置。不调用 OpenAI API。"""
        return engine.inspect()

    @mcp.tool(annotations={'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False})
    async def get_film_plan_schema() -> dict:
        """返回分镜和检查结果的 JSON 结构，由当前 ChatGPT 对话填充。"""
        return {'plan_schema':Plan.model_json_schema(),'review_schema':Review.model_json_schema(),
                'rules':'5–120s, <=24 shots, each 1–5s, <=4 main characters, <=6 locations, <=2 visible main characters/shot, <=1 speaker/shot. Dialogue <=3 chars/sec. First transition=cut. FLF requires an end description.'}

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':False})
    async def start_full_film(plan_json:str,duration_seconds:int=90,max_retries:int=1,max_external_calls:int=80,seed:int=12345,music_name:str='') -> dict:
        """接收当前 ChatGPT 编写的分镜 JSON，创建等待图片上传的任务。后续配音/视频只调用 RunningHub。"""
        config=engine.inspect()
        if not config['ok']:return config
        allocate_durations(duration_seconds)
        if not 0<=max_retries<=2 or not 1<=max_external_calls<=200 or not 0<=seed<2**63:raise ValueError('Invalid retry, budget or seed')
        if len(plan_json)>200000:raise ValueError('Storyboard too large')
        p=validate_plan(json.loads(plan_json),duration_seconds)
        music=''
        if music_name:
            if Path(music_name).name!=music_name:raise ValueError('music_name must be a filename')
            music=str(engine.root/'music'/music_name)
            if not Path(music).is_file():raise ValueError('Music file not found')
        j={'id':secrets.token_hex(16),'status':'WAITING_ASSETS','stage':'SHOTS','plan':p,
           'shots':[{'stage':'START','attempt':0} for _ in p['shots']], 'target_seconds':duration_seconds,
           'max_retries':max_retries,'max_external_calls':max_external_calls,'external_calls':0,'seed':seed,'shot_index':0,'music_file':music,'error':''}
        engine.save(j)
        (engine.directory(j['id'])/'storyboard.json').write_text(json.dumps(p,ensure_ascii=False,indent=2))
        return engine.summary(j)

    @mcp.tool(meta={'openai/fileParams':['frame_file']},annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':True})
    async def upload_film_frame(job_id:str,shot_index:int,role:Literal['start','end'],frame_file:OpenAIFile) -> dict:
        """上传当前对话生成或用户提供的图片。shot_index 从0开始。不会调用图片生成 API。"""
        j=engine.load(job_id)
        settings=legacy['Settings'].from_env()
        content,_,_=await legacy['download_public_file'](settings,frame_file.download_url,expected_kind='image')
        await engine.store_frame(j,shot_index,role,content)
        # Next query launches processing, allowing all frames to be uploaded first.
        if j['status']=='RUNNING':j['status']='WAITING_ASSETS';engine.save(j)
        return engine.summary(j)
    legacy['finalize_openai_file_param_schema'](upload_film_frame,'frame_file')

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':True})
    async def query_full_film(job_id:str,wait_seconds:int=1) -> dict:
        """查询/推进 RH 配音视频及剪辑。等待画面检查时只返回素材链接，不自动通过。"""
        if not 0<=wait_seconds<=20:raise ValueError('wait_seconds must be 0–20')
        j=engine.load(job_id)
        if j['status']=='WAITING_ASSETS':
            i=j['shot_index'];shot=j['plan']['shots'][i];sd=engine.directory(job_id)/f'shot{i:03d}'
            ready=((sd/'start.png').exists() or shot['transition']=='continue') and (shot['mode']=='i2v' or (sd/'end.png').exists())
            if ready:j['status']='RUNNING';engine.save(j)
        if j['status']=='RUNNING':launch(job_id)
        task=engine.tasks.get(job_id)
        if task and wait_seconds:
            try:await asyncio.wait_for(asyncio.shield(task),wait_seconds)
            except asyncio.TimeoutError:pass
        return engine.summary(engine.load(job_id))

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'idempotentHint':False,'openWorldHint':True})
    async def submit_film_review(job_id:str,review_json:str,action:Literal['advance','retry','draft']) -> dict:
        """当前 ChatGPT 必须先实际查看 review_assets，再提交检查。advance=通过继续，retry=重做可能扣RH，draft=明确保留未通过草稿。"""
        j=engine.load(job_id);engine.accept_review(j,json.loads(review_json),action)
        launch(job_id)
        return engine.summary(j)

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False})
    async def revise_blocked_dialogue(job_id:str,dialogue:str) -> dict:
        """对白超时时，由当前对话修改该句，服务器不调用语言模型。"""
        j=engine.load(job_id)
        if j['status']!='BLOCKED_AUDIO':raise ValueError('Job is not blocked on audio')
        shot=j['plan']['shots'][j['shot_index']]
        if not dialogue or len(dialogue)>3*shot['duration']:raise ValueError('Dialogue too long or empty')
        shot['dialogue']=dialogue;j['shots'][j['shot_index']]['stage']='AUDIO';j['status']='RUNNING';j['error']='';engine.save(j)
        (engine.directory(job_id)/'storyboard.json').write_text(json.dumps(j['plan'],ensure_ascii=False,indent=2))
        launch(job_id);return engine.summary(j)

    @mcp.tool(annotations={'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False})
    async def resume_full_film(job_id:str,allow_repeat_uncertain_request:bool=False) -> dict:
        """修复服务错误后恢复。结果不明的RH提交，先核对历史，避免重复扣费。"""
        j=engine.load(job_id)
        if j['status']!='NEEDS_ATTENTION':raise ValueError('Job is not paused')
        if j.get('pending_external') and not allow_repeat_uncertain_request:return {'ok':False,'error':'Check RunningHub history before repeating the uncertain request.'}
        await engine.recover_failed_tts(j)
        j.pop('pending_external',None);j['status']='RUNNING';j['error']='';engine.save(j);launch(job_id)
        return engine.summary(j)

    @mcp.custom_route('/film-files/{job_id}/{asset:path}',methods=['GET'])
    async def film_download(request):
        try:
            job_id=request.path_params['job_id'];name=request.path_params['asset']
            allowed=name in {'final.mp4','storyboard.json','review_report.json'} or re.fullmatch(r'shot[0-9]{3}/(start|end|first|quarter|middle|threequarter|last)\.png|shot[0-9]{3}/clip\.mp4',name)
            if not allowed:raise ValueError()
            expires=int(request.query_params['expires'])
            if expires<time.time():raise ValueError()
            expected=hmac.new(os.environ['FILM_DOWNLOAD_SECRET'].encode(),f'{job_id}/{name}:{expires}'.encode(),hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected,request.query_params.get('sig','')):raise ValueError()
            path=engine.directory(job_id)/name
            if not path.is_file():raise ValueError()
            return FileResponse(path,filename=path.name)
        except (KeyError,ValueError):return JSONResponse({'error':'Invalid or expired download link'},status_code=403)
    return engine
