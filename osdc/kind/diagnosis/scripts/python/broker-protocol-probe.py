"""Owned, isolated reproduction of scaleset v0.3.0 message polling protocol."""
import argparse, json, logging, os, time
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4
import requests
import drain_test_kind as m
from drain_test_run import run_controller, wait_for

OUT=m.ROOT/'diagnosis'
PATH='_apis/runtime/runnerscalesets'
class BrokerProbe(m.KindDrainTest):
    def __init__(self,args,key,initial):
        super().__init__(args,key)
        self.initial=initial;self.name='osdc-protocol-'+key
        self.branch='codex/osdc-broker-protocol-'+key
        self.workflow_name='OSDC Broker Protocol '+key
        self.connection=None;self.scale_id=None;self.created_intent=False
        self.session_data=None;self.last_message=0;self.observed=set();self.http=requests.Session()
        self.http.headers['User-Agent']=json.dumps({'system':'osdc-broker-protocol-diagnostic','version':'0.14.1-jeanschmidt.20','commit_sha':'','scale_set_id':0,'subsystem':'listener','build_version':'v0.3.0','build_commit_sha':'','kind':'scaleset'},separators=(',',':'))
        self.events=OUT/(key+'-broker-events.jsonl')
        fd=os.open(self.events,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        self.result={};self.fresh_fixture=None
    def trace(self,event,**data):
        row={'at':datetime.now(timezone.utc).isoformat(),'case_initial_capacity':self.initial,'scale_set_name':self.name,'event':event,**data}
        logging.info('Protocol: %s',json.dumps(row))
        try:
            with self.events.open('a') as file:file.write(json.dumps(row)+'\n')
        except FileNotFoundError:
            logging.warning('Trace directory moved; preserving event in process output')
    def official(self,url):
        p=urlparse(url)
        assert p.scheme=='https' and p.hostname and p.hostname.endswith('.actions.githubusercontent.com') and p.username is None, 'Unexpected Actions service URL'
        return url
    def admin(self,method='GET',suffix='',body=None,expected=200,query=None):
        url=self.official(self.connection['url'].rstrip('/')+'/'+PATH+suffix)
        response=self.http.request(method,url,headers={'Authorization':'Bearer '+self.connection['token'],'Content-Type':'application/json'},params={'api-version':'6.0-preview',**(query or {})},json=body,timeout=25,allow_redirects=False)
        allowed=(expected,) if isinstance(expected,int) else expected
        if response.status_code not in allowed:raise RuntimeError(f'Admin operation {method} {suffix}: HTTP {response.status_code}')
        if response.status_code==404:return {}
        return response.json() if response.content else {}
    def connect(self):
        token=self.api('actions/runners/registration-token',{},'POST')['token']
        response=self.http.post('https://api.github.com/actions/runner-registration',headers={'Authorization':'RemoteAuth '+token},json={'url':'https://github.com/'+m.REPOSITORY,'runner_event':'register'},timeout=25,allow_redirects=False)
        if response.status_code!=200:raise RuntimeError('Runner admin authorization HTTP '+str(response.status_code))
        self.connection=response.json();self.official(self.connection['url'])
        metadata=json.loads((OUT/'broker-primary-metadata.json').read_text())
        self.primary_id=metadata['id'];self.group_id=metadata['runnerGroupId']
        assert not self.admin(query={'runnerGroupId':self.group_id,'name':self.name}).get('value',[])
        self.created_intent=True
        obj=self.admin('POST',body={'name':self.name,'runnerGroupId':self.group_id,'labels':[{'name':self.name,'type':'System'}],'RunnerSetting':{'disableUpdate':True}})
        self.scale_id=obj['id'];assert self.scale_id!=self.primary_id and obj['name']==self.name
        self.trace('owned_scale_set_created',scale_set_id=self.scale_id,runner_group_id=self.group_id)
        self.session_data=self.admin('POST',f'/{self.scale_id}/sessions',{'ownerName':'osdc-protocol-'+self.run_key})
        self.official(self.session_data['messageQueueUrl'])
        self.trace('session_created',statistics=self.session_data.get('statistics'))
    def workflow(self):
        return f'''name: {self.workflow_name}
on:
  pull_request:
    types: [opened]
permissions:
  contents: read
jobs:
  drain-held:
    if: ${{{{ github.event.pull_request.head.repo.full_name == github.repository }}}}
    runs-on: {self.name}
    timeout-minutes: 5
    strategy:
      fail-fast: false
      matrix:
        probe: [1, 2, 3]
    steps:
      - name: Diagnostic task
        run: echo "Protocol diagnostic task"
'''
    def poll(self,capacity,stage):
        url=self.official(self.session_data['messageQueueUrl'])
        params={'lastMessageId':self.last_message} if self.last_message else None
        headers={'Authorization':'Bearer '+self.session_data['messageQueueAccessToken'],'Accept':'application/json; api-version=6.0-preview','X-ScaleSetMaxCapacity':str(capacity)}
        request=requests.Request('GET',url,headers=headers,params=params)
        prepared=self.http.prepare_request(request)
        assert prepared.headers['X-ScaleSetMaxCapacity']==str(capacity)
        self.trace('poll_sent',stage=stage,capacity_header=prepared.headers['X-ScaleSetMaxCapacity'],last_message_id=self.last_message)
        started=time.monotonic()
        settings=self.http.merge_environment_settings(prepared.url,{},None,None,None)
        response=self.http.send(prepared,timeout=75,allow_redirects=False,**settings)
        assert response.request.headers['X-ScaleSetMaxCapacity']==str(capacity)
        if response.status_code not in (200,202):raise RuntimeError('Message poll HTTP '+str(response.status_code))
        if response.status_code==202:
            self.trace('poll_response',stage=stage,capacity_header=str(capacity),status=202,seconds=round(time.monotonic()-started,3))
            return False
        payload=json.loads(response.content.decode('utf-8-sig'))
        assert payload['messageType']=='RunnerScaleSetJobMessages'
        messages=json.loads(payload.get('body') or '[]')
        selected=[]
        for item in messages:
            selected.append({k:item.get(k) for k in ('messageType','runnerRequestId','workflowRunId','jobId','jobDisplayName','requestLabels')})
            if item.get('messageType') in ('JobAvailable','JobAssigned'):
                allowed={self.run_id}
                if self.fresh_fixture and self.fresh_fixture.run_id:allowed.add(self.fresh_fixture.run_id)
                assert int(item['workflowRunId']) in allowed, 'Unexpected workflow on owned scale set'
                identity=item.get('jobId') or item.get('jobDisplayName')
                assert identity, 'Job message has no stable identity'
                self.observed.add((str(item['workflowRunId']),str(identity)))
        self.last_message=payload['messageId']
        self.trace('poll_response',stage=stage,capacity_header=str(capacity),status=200,seconds=round(time.monotonic()-started,3),message_id=self.last_message,statistics=payload.get('statistics'),messages=selected,distinct_requests=len(self.observed))
        # SDK acknowledges messages before any acquisition. We deliberately do
        # not acquire jobs/create runners: only queue routing is under test.
        ack=self.http.delete(url.rstrip('/')+'/'+str(self.last_message),headers={'Authorization':'Bearer '+self.session_data['messageQueueAccessToken'],'Content-Type':'application/json'},timeout=25,allow_redirects=False)
        if ack.status_code!=204:raise RuntimeError('Message acknowledgement HTTP '+str(ack.status_code))
        return len(self.observed)==3
    def run(self):
        self.connect()
        self.poll(self.initial,'before-jobs')
        self.create_pr();wait_for('protocol workflow created',self.find_run,120)
        wait_for('three protocol jobs queued without a runner',self.held_ready,120)
        self.trace('jobs_queued',pr=self.pr,workflow_run_id=self.run_id,job_ids=[j['id'] for j in self.jobs()])
        if self.initial==0:
            self.poll(0,'jobs-queued-at-zero')
            assert not self.observed, 'Unexpected delivery while zero capacity was advertised'
        deadline=time.monotonic()+getattr(self.args,'poll_seconds',180)
        delivered=False
        while time.monotonic()<deadline:
            if self.poll(3,'positive-after-queue'):
                delivered=True;break
        if not delivered:
            self.held_ready()
            self.trace('original_jobs_still_queued',job_ids=[j['id'] for j in self.jobs()])
        stats=self.admin(suffix='/'+str(self.scale_id)).get('statistics')
        self.result={'initial_capacity':self.initial,'delivered_three_jobs':delivered,'distinct_requests':len(self.observed),'scale_set_id':self.scale_id,'pr':self.pr,'run_id':self.run_id,'statistics':stats}
        self.trace('case_result',**self.result)
        if self.initial==3 and not delivered:raise RuntimeError('Positive-capacity protocol baseline failed')
        if not delivered and getattr(self.args,'fresh_job',False):
            original_ids={j['id'] for j in self.jobs()}
            fresh=m.KindDrainTest(self.args,uuid4().hex)
            self.fresh_fixture=fresh
            fresh.branch='codex/osdc-broker-fresh-'+fresh.run_key
            fresh.workflow_name='OSDC Broker Fresh '+fresh.run_key
            fresh.workflow=lambda:f'''name: {fresh.workflow_name}
on:
  pull_request:
    types: [opened]
permissions:
  contents: read
jobs:
  drain-fresh:
    if: ${{{{ github.event.pull_request.head.repo.full_name == github.repository }}}}
    runs-on: {self.name}
    timeout-minutes: 5
    steps:
      - name: Fresh routing diagnostic
        run: echo "Fresh routing diagnostic"
'''
            fresh.create_pr()
            wait_for('fresh protocol workflow created',fresh.find_run,120)
            wait_for('fresh protocol job visible',lambda:len(fresh.jobs())==1,120)
            self.trace('fresh_job_created',fresh_pr=fresh.pr,fresh_run_id=fresh.run_id,fresh_job_ids=[j['id'] for j in fresh.jobs()])
            for attempt in range(3):
                self.poll(3,'old-and-fresh-jobs-coexist')
                old_count=sum(key[0]==str(self.run_id) for key in self.observed)
                fresh_count=sum(key[0]==str(fresh.run_id) for key in self.observed)
                if fresh_count and old_count==0:break
                if old_count==3:break
            assert original_ids=={j['id'] for j in self.jobs()}
            self.trace('old_vs_fresh_result',old_messages=old_count,fresh_messages=fresh_count,old_jobs=[{k:j.get(k) for k in ('id','name','status','runner_id')} for j in self.jobs()],fresh_jobs=[{k:j.get(k) for k in ('id','name','status','runner_id')} for j in fresh.jobs()],statistics=self.admin(suffix='/'+str(self.scale_id)).get('statistics'))
        if not delivered and getattr(self.args,'refresh_metadata',False):
            original_ids={j['id'] for j in self.jobs()}
            payload={'id':self.scale_id,'name':self.name,'runnerGroupId':self.group_id,'labels':[{'name':self.name,'type':'System'}],'RunnerSetting':{'disableUpdate':True}}
            self.trace('refresh_metadata_same_parameters')
            obj=self.admin('PATCH','/'+str(self.scale_id),payload)
            assert obj['id']==self.scale_id and obj['name']==self.name
            matched=self.poll(3,'after-metadata-refresh')
            assert original_ids=={j['id'] for j in self.jobs()}
            self.trace('metadata_refresh_result',delivered_three_jobs=matched,original_job_ids=sorted(original_ids),statistics=self.admin(suffix='/'+str(self.scale_id)).get('statistics'))
            if not matched:
                self.trace('refresh_metadata_with_unused_alias')
                payload['labels'].append({'name':self.name+'-unused','type':'System'})
                obj=self.admin('PATCH','/'+str(self.scale_id),payload)
                assert obj['id']==self.scale_id and obj['name']==self.name
                matched=self.poll(3,'after-unused-alias-added')
                assert original_ids=={j['id'] for j in self.jobs()}
                self.trace('unused_alias_result',delivered_three_jobs=matched,original_job_ids=sorted(original_ids),statistics=self.admin(suffix='/'+str(self.scale_id)).get('statistics'))
        if not delivered and getattr(self.args,'recover_session',False):
            before_ids={j['id'] for j in self.jobs()}
            self.trace('reset_owned_session_for_positive_first_poll')
            self.admin('DELETE',f"/{self.scale_id}/sessions/{self.session_data['sessionId']}",expected=(204,404))
            self.session_data=None;self.last_message=0
            self.session_data=self.admin('POST',f'/{self.scale_id}/sessions',{'ownerName':'osdc-protocol-positive-'+self.run_key})
            self.official(self.session_data['messageQueueUrl'])
            self.trace('positive_first_session_created',statistics=self.session_data.get('statistics'))
            recovered=False
            for attempt in range(2):
                if self.poll(3,'new-session-first-positive'):
                    recovered=True;break
            assert before_ids=={j['id'] for j in self.jobs()}, 'Original tasks changed'
            self.trace('session_recovery_result',delivered_three_jobs=recovered,original_job_ids=sorted(before_ids),statistics=self.admin(suffix='/'+str(self.scale_id)).get('statistics'))

    def cleanup(self):
        fresh_ok=self.fresh_fixture.cleanup() if self.fresh_fixture else True
        ok=super().cleanup() and fresh_ok
        if self.created_intent and self.connection:
            try:
                if self.run_id:wait_for('owned protocol workflow completed before scale-set deletion',lambda:all(r['status']=='completed' for r in self.runs()),90,poll=5)
                if not self.scale_id:
                    rows=self.admin(query={'runnerGroupId':self.group_id,'name':self.name}).get('value',[])
                    assert len(rows)<=1
                    if rows:self.scale_id=rows[0]['id']
                if self.scale_id:
                    obj=self.admin(suffix='/'+str(self.scale_id),expected=(200,404))
                    assert not obj or (obj['name']==self.name and self.scale_id!=self.primary_id)
                    if obj and self.session_data:
                        self.admin('DELETE',f"/{self.scale_id}/sessions/{self.session_data['sessionId']}",expected=(204,404))
                        self.session_data=None
                    if obj:self.admin('DELETE','/'+str(self.scale_id),expected=(204,404))
                    self.trace('owned_scale_set_deleted',scale_set_id=self.scale_id)
                    self.scale_id=None
                self.created_intent=False
            except Exception:
                logging.exception('Owned protocol cleanup requires retry');return False
        return ok

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    parser=argparse.ArgumentParser();parser.add_argument('--initial-capacity',type=int,choices=[0,3],required=True);parser.add_argument('--recover-session',action='store_true');parser.add_argument('--refresh-metadata',action='store_true');parser.add_argument('--fresh-job',action='store_true');parser.add_argument('--poll-seconds',type=int,default=180);args=parser.parse_args()
    assert args.poll_seconds>0
    os.environ['GH_TOKEN']=m.read_token(m.ROOT)
    config=argparse.Namespace(root_dir=m.ROOT,upstream_dir=m.UPSTREAM,clusters_yaml=m.ROOT/'clusters.yaml',recover_session=args.recover_session,poll_seconds=args.poll_seconds,refresh_metadata=args.refresh_metadata,fresh_job=args.fresh_job)
    test=BrokerProbe(config,uuid4().hex,args.initial_capacity)
    run_controller(test)
