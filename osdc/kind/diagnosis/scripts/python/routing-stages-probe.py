import argparse, base64, contextlib, json, logging, os, re, time
from uuid import uuid4
import yaml
import drain_test_kind as m
from drain_test_run import command, run_controller, wait_for

NAME='osdc-classic-diagnostic'
OUT=m.ROOT/'diagnosis'
OPS=m.LOCAL/'diagnosis/recipes/classic-operations.just'
class RoutingProbe(m.KindDrainTest):
    classic_created=False
    def named_runners(self):
        return [r for r in self.api('actions/runners?per_page=100')['runners'] if r['name']==NAME]
    def held(self):
        return [j for j in self.jobs() if 'held' in j['name']]
    def record(self,stage):
        data={'stage':stage,'run_id':self.run_id,'pr':self.pr,'jobs':[{k:j.get(k) for k in ('id','name','status','conclusion','runner_id','runner_name','started_at','completed_at')} for j in self.jobs()], 'classic':[{'id':r['id'],'status':r['status'],'busy':r['busy'],'labels':[x['name'] for x in r['labels']]} for r in self.named_runners()]}
        path=OUT/f'{self.run_key}-{stage}.json';path.write_text(json.dumps(data,indent=2));path.chmod(0o600)
        logging.info('Evidence: %s',json.dumps(data))
    def observe(self,stage):
        self.record(stage+'-start')
        deadline=time.monotonic()+60
        while True:
            jobs=self.held()
            assert len(jobs)==3 and {j['id'] for j in jobs}==self.original_ids
            if any(j.get('runner_id') or j['status'] not in ('queued','waiting') for j in jobs):
                self.record(stage+'-dispatch')
                logging.info('FIRST DISPATCH STAGE: %s',stage)
                try:wait_for('all original held jobs complete after '+stage,lambda:self.completed('held',3),120,poll=5)
                finally:self.record(stage+'-completion')
                return True
            remaining=deadline-time.monotonic()
            if remaining<=0:
                self.record(stage+'-no-dispatch')
                logging.info('NO DISPATCH within 60 seconds at stage %s',stage)
                return False
            time.sleep(min(5,remaining))
    def run(self):
        try:super().run()
        except TimeoutError as exc:
            if not str(exc).startswith('held jobs succeed after undrain:'):raise
            logging.info('Original E2E remains failed; approved routing stage experiment begins')
        else:raise RuntimeError('Original tasks already succeeded; routing intervention is unnecessary')
        held=self.held()
        assert len(held)==3 and all(j['status']=='queued' and not j.get('runner_id') for j in held)
        assert self.capacities()[m.LABEL]==3
        pods=json.loads(self.kube('get','pods','-n','arc-runners','-o','json'))['items']
        assert all(p['metadata']['name'].startswith('ph-') for p in pods)
        assert not self.named_runners()
        for resource in ('pod/'+NAME,'secret/'+NAME+'-registration'):
            assert not self.kube('get',resource,'-n','arc-runners','--ignore-not-found','-o','name').strip()
        self.original_ids={j['id'] for j in held};self.record('before-intervention')
        registration=self.api('actions/runners/registration-token',{},'POST')['token']
        pod=yaml.safe_load((OUT/'classic-runner-review.yaml').read_text())
        container=pod['spec']['containers'][0]
        container['env'].append({'name':'DIAG_UNMATCHED_LABEL','value':'osdc-unmatched-'+self.run_key})
        container['args']=['''set -euo pipefail
cd /home/runner
./config.sh --unattended --url https://github.com/kuring/ci-infra --token "$DIAG_REGISTRATION_TOKEN" --name "$DIAG_RUNNER_NAME" --no-default-labels --labels "$DIAG_UNMATCHED_LABEL" --ephemeral --disableupdate
echo DIAG_REGISTERED_OFFLINE
touch /tmp/diag-configured
while [[ ! -f /tmp/diag-start ]]; do sleep 1; done
echo DIAG_STARTING
exec ./run.sh
''']
        secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':NAME+'-registration','namespace':'arc-runners','labels':{'osdc.io/drain-kind-diagnostic':'classic-routing'}},'data':{'token':base64.b64encode(registration.encode()).decode()}}
        active=OUT/'classic-active.json';fd=os.open(active,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'apiVersion':'v1','kind':'List','items':[secret,pod]},f)
        self.classic_created=True
        try:
            logging.info('Classic resources: %s',command(['just','--justfile',str(OPS),'apply'],cwd=m.UPSTREAM,timeout=90).stdout.strip())
            def offline():
                runners=self.named_runners()
                if len(runners)!=1:return False
                r=runners[0]
                assert r['status']=='offline' and m.LABEL not in [x['name'] for x in r['labels']]
                self.classic_id=r['id'];return True
            wait_for('diagnostic runner registered offline with unmatched label',offline,90,poll=3)
            if self.observe('registered-offline-unmatched'):return
            command(['just','--justfile',str(OPS),'start'],cwd=m.UPSTREAM,timeout=60)
            wait_for('diagnostic runner online with unmatched label',lambda:any(r['id']==self.classic_id and r['status']=='online' for r in self.named_runners()),90,poll=3)
            if self.observe('online-unmatched'):return
            self.api(f'actions/runners/{self.classic_id}/labels',{'labels':[m.LABEL]},'POST')
            logging.info('Added matching label %s to owned runner id=%s',m.LABEL,self.classic_id)
            if not self.observe('online-matching-label-added'):
                raise RuntimeError('No task dispatch after all three intervention stages')
        finally:
            with contextlib.suppress(Exception):
                raw=self.kube('logs',NAME,'-n','arc-runners','--tail=400').replace(registration,'[REDACTED]')
                raw=re.sub(r'(?:github_pat_|ghs_|ghp_)[A-Za-z0-9_]+','[REDACTED]',raw)
                raw=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[REDACTED]',raw)
                path=OUT/f'{self.run_key}-stages-classic.log';path.write_text(raw);path.chmod(0o600)
                for line in raw.splitlines():
                    if any(s in line for s in ('DIAG_','Connected to','Listening for Jobs','Running job','completed with','Error')):logging.info('Classic: %s',line)
            with contextlib.suppress(Exception):
                pods=json.loads(self.kube('get','pods','-n','arc-systems','-l','app.kubernetes.io/component=runner-scale-set-listener','-o','json'))['items']
                for pod in pods:
                    raw=self.kube('logs',pod['metadata']['name'],'-n','arc-systems','--since=20m')
                    raw=re.sub(r'(?:github_pat_|ghs_|ghp_)[A-Za-z0-9_]+','[REDACTED]',raw)
                    raw=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[REDACTED]',raw)
                    path=OUT/f'{self.run_key}-stages-listener.log';path.write_text(raw);path.chmod(0o600)
    def cleanup(self):
        if self.classic_created:
            try:
                for run in self.runs():
                    if run['status']!='completed':self.api(f"actions/runs/{run['id']}/cancel",method='POST')
                wait_for('owned workflow completed before diagnostic cleanup',lambda:all(r['status']=='completed' for r in self.runs()),90,poll=5)
                logging.info('Classic cleanup: %s',command(['just','--justfile',str(OPS),'cleanup'],cwd=m.UPSTREAM,timeout=210).stdout.strip())
                for r in self.named_runners():self.api(f"actions/runners/{r['id']}",method='DELETE')
                assert not self.named_runners()
                (OUT/'classic-active.json').unlink(missing_ok=True);self.classic_created=False
            except Exception:
                logging.exception('Diagnostic cleanup requires retry');super().cleanup();return False
        return super().cleanup()

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
os.environ['GH_TOKEN']=m.read_token(m.ROOT)
a=argparse.Namespace(action='test',root_dir=m.ROOT,upstream_dir=m.UPSTREAM,clusters_yaml=m.ROOT/'clusters.yaml',start_timeout=180,finish_timeout=60,drain_timeout=180,observe_seconds=60,simulate_drain_noop=False)
run_controller(RoutingProbe(a,uuid4().hex))
