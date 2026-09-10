import asyncio,copy,json,os,tempfile,unittest
from pathlib import Path
from film_engine import Engine,validate_plan,command,probe

def plan():
    return {'title':'Test','style':'cinematic','characters':[{'id':'c1','appearance':'Adult in gray robe','voice':'Calm voice'}],
        'locations':[{'id':'l1','appearance':'Stone room and bronze door'}],
        'shots':[{'location':'l1','cast':['c1'],'duration':3,'mode':'i2v','transition':'cut','start':'Adult lies on platform','end':'','action':'Blink and breathe gently','camera':'Fixed tripod','speaker':'','dialogue':''},
                 {'location':'l1','cast':['c1'],'duration':3,'mode':'flf','transition':'continue','start':'Same adult lies on platform','end':'Body is completely flat','action':'Gradually flatten body','camera':'Fixed tripod','speaker':'','dialogue':''}]}

def review(passed=True):
    return dict(identity_ok=True,costume_ok=True,action_ok=passed,background_ok=True,no_text=True,boundary_ok=True,issues=[] if passed else ['No flattening'],retry_instruction='Show actual body volume loss')

class Tests(unittest.IsolatedAsyncioTestCase):
    def test_no_openai_api(self):
        text=Path(__file__).with_name('film_engine.py').read_text()
        for forbidden in ['api.openai.com','OPENAI_API_KEY','async def openai_json','async def make_plan','async def image(']:self.assertNotIn(forbidden,text)
        validate_plan(plan(),6)

    def test_ui_api(self):
        root=Path(__file__).parent
        for mode in ['i2v','flf']:
            a=json.loads((root/f'film_{mode}_api.json').read_text());w=json.loads((root/f'LTX_film_{mode}_workflow.json').read_text())
            nodes={str(n['id']):n for n in w['nodes']}
            self.assertEqual(set(nodes),set(a))
            for lid,src,slot,dst,idx,typ in w['links']:
                port=nodes[str(dst)]['inputs'][idx]
                self.assertEqual(a[str(dst)]['inputs'][port['name']],[str(src),slot])
                self.assertEqual(port['link'],lid)
                self.assertIn(lid,nodes[str(src)]['outputs'][slot]['links'])

    async def test_chat_review_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            os.environ.update(FILM_DATA_DIR=temp,FILM_DOWNLOAD_SECRET='x'*40,FILM_PUBLIC_BASE_URL='https://example.test',FILM_I2V_WORKFLOW_ID='111',FILM_FLF_WORKFLOW_ID='222')
            calls=[]
            class Client:
                def __init__(self,s):pass
                async def run_workflow(self,**kw):calls.append(kw);return {'taskId':str(len(calls))}
            class Settings:
                @staticmethod
                def from_env():return None
            def overrides(graph,nodes):
                for n in nodes:assert n['fieldName'] in graph[n['nodeId']]['inputs']
                return graph
            class Fake(Engine):
                async def upload(self,path,kind):return 'openapi/'+path.name
                async def rh_state(self,task,kind):return 'SUCCESS',['https://provider.test/a'],''
                async def download(self,url,path,kind):
                    await command('ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=s=320x180:r=24','-t','3.05','-c:v','libx264',str(path))
            e=Fake({'Settings':Settings,'RunningHubClient':Client,'apply_workflow_overrides':overrides})
            j={'id':'a'*32,'status':'WAITING_ASSETS','stage':'SHOTS','plan':plan(),'shots':[{'stage':'START','attempt':0},{'stage':'START','attempt':0}],
               'shot_index':0,'target_seconds':6,'max_retries':1,'max_external_calls':10,'external_calls':0,'seed':123,'music_file':'','error':''}
            e.save(j);d=e.directory(j['id'])
            image=d/'fixture.png'
            await command('ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=blue:s=320x180','-frames:v','1',str(image))
            await e.store_frame(j,0,'start',image.read_bytes())
            async def advance():
                for _ in range(20):
                    await e.guarded_step(j['id']);state=e.load(j['id'])
                    if state['status']!='RUNNING':return state
                raise AssertionError('No review checkpoint')
            j=await advance();self.assertEqual(j['status'],'WAITING_REVIEW')
            self.assertIn('clip.mp4',e.summary(j)['review_assets'])
            with self.assertRaises(ValueError):e.accept_review(j,review(False),'advance')
            e.accept_review(j,review(False),'retry')
            j=await advance();self.assertEqual(j['status'],'WAITING_REVIEW')
            e.accept_review(j,review(),'advance')
            j=await advance();self.assertEqual(j['status'],'WAITING_ASSETS')
            self.assertEqual((d/'shot001/start.png').read_bytes(),(d/'shot000/last.png').read_bytes())
            await e.store_frame(j,1,'end',image.read_bytes())
            j=await advance();self.assertEqual(j['status'],'WAITING_REVIEW')
            e.accept_review(j,review(False),'draft')
            j=await advance();self.assertEqual(j['status'],'DRAFT_REVIEW_REQUIRED')
            self.assertEqual(len(calls),3)
            self.assertAlmostEqual(float((await probe(d/'final.mp4'))['format']['duration']),6,places=1)
            report=json.loads((d/'review_report.json').read_text())
            self.assertFalse(report['lip_sync_verified'])
            self.assertEqual(report['shots'][1]['review']['source'],'current_chat_review')

if __name__=='__main__':unittest.main()
