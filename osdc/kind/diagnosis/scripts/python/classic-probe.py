import argparse, base64, contextlib, json, logging, os, re, time
from uuid import uuid4
import yaml
import drain_test_kind as m
from drain_test_run import command, run_controller, wait_for

NAME = 'osdc-classic-diagnostic'
OUT = m.ROOT / 'diagnosis'
OPS = m.LOCAL / 'diagnosis/recipes/classic-operations.just'
class ClassicProbe(m.KindDrainTest):
    classic_created = False
    def named_runners(self):
        return [r for r in self.api('actions/runners?per_page=100')['runners'] if r['name'] == NAME]
    def held(self):
        return [j for j in self.jobs() if 'held' in j['name']]
    def record(self, stage):
        data = {'stage':stage,'run_id':self.run_id,'pr':self.pr,'jobs':[{k:j.get(k) for k in ('id','name','status','conclusion','runner_id','runner_name','runner_group_name')} for j in self.jobs()]}
        path=OUT/f'{self.run_key}-{stage}.json'; path.write_text(json.dumps(data,indent=2)); path.chmod(0o600)
        logging.info('Evidence: %s',json.dumps(data))
    def run(self):
        try:
            super().run()
        except TimeoutError as exc:
            if not str(exc).startswith('held jobs succeed after undrain:'): raise
            logging.info('Original E2E remains failed; starting approved classic routing probe')
        else:
            raise RuntimeError('Original tasks unexpectedly succeeded; classic probe unnecessary')
        held = self.held()
        assert len(held)==3 and all(j['status']=='queued' and not j.get('runner_id') for j in held)
        assert self.capacities()[m.LABEL]==3
        pods=json.loads(self.kube('get','pods','-n','arc-runners','-o','json'))['items']
        assert all(p['metadata']['name'].startswith('ph-') for p in pods), 'Unexpected real runner/job pod'
        assert not self.named_runners(), 'Existing classic runner name is not owned'
        for resource in ('pod/'+NAME,'secret/'+NAME+'-registration'):
            assert not self.kube('get',resource,'-n','arc-runners','--ignore-not-found','-o','name').strip()
        self.original_ids={j['id'] for j in held}; self.record('before-classic')
        registration=self.api('actions/runners/registration-token',{},'POST')['token']
        pod=yaml.safe_load((OUT/'classic-runner-review.yaml').read_text())
        secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':NAME+'-registration','namespace':'arc-runners','labels':{'osdc.io/drain-kind-diagnostic':'classic-routing'}},'data':{'token':base64.b64encode(registration.encode()).decode()}}
        active=OUT/'classic-active.json'
        fd=os.open(active,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f: json.dump({'apiVersion':'v1','kind':'List','items':[secret,pod]},f)
        self.classic_created=True
        try:
            result=command(['just','--justfile',str(OPS),'apply'],cwd=m.UPSTREAM,timeout=90)
            logging.info('Classic resources: %s',result.stdout.strip())
            def picked():
                jobs=self.held()
                selected=[j for j in jobs if j['id'] in self.original_ids and j.get('runner_name')==NAME]
                if selected:
                    logging.info('Original queued task picked by classic runner: %s',json.dumps([{k:j.get(k) for k in ('id','name','status','conclusion','runner_id','runner_name')} for j in selected]))
                return any(j.get('conclusion')=='success' for j in selected)
            try:
                wait_for('original queued job succeeds on temporary classic runner',picked,180,poll=10)
                self.record('classic-success')
            except TimeoutError:
                self.record('classic-timeout')
                raise
        finally:
            with contextlib.suppress(Exception):
                raw=self.kube('logs',NAME,'-n','arc-runners','--tail=300')
                raw=raw.replace(registration,'[REDACTED]')
                raw=re.sub(r'(?:github_pat_|ghs_|ghp_)[A-Za-z0-9_]+','[REDACTED]',raw)
                raw=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','[REDACTED]',raw)
                path=OUT/f'{self.run_key}-classic-runner.log';path.write_text(raw);path.chmod(0o600)
                for line in raw.splitlines():
                    if any(s in line for s in ('Connected to','registered','Listening','Running job','completed','error','Error','version')):logging.info('Classic: %s',line)
    def cleanup(self):
        if self.classic_created:
            try:
                for run in self.runs():
                    if run['status']!='completed':self.api(f"actions/runs/{run['id']}/cancel",method='POST')
                wait_for('owned workflow completed before classic cleanup',lambda:all(r['status']=='completed' for r in self.runs()),90,poll=5)
                result=command(['just','--justfile',str(OPS),'cleanup'],cwd=m.UPSTREAM,timeout=210)
                logging.info('Classic cleanup: %s',result.stdout.strip())
                for runner in self.named_runners():
                    self.api(f"actions/runners/{runner['id']}",method='DELETE')
                assert not self.named_runners()
                (OUT/'classic-active.json').unlink(missing_ok=True)
                self.classic_created=False
            except Exception:
                logging.exception('Classic cleanup needs retry')
                super().cleanup()
                return False
        return super().cleanup()

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
os.environ['GH_TOKEN']=m.read_token(m.ROOT)
a=argparse.Namespace(action='test',root_dir=m.ROOT,upstream_dir=m.UPSTREAM,clusters_yaml=m.ROOT/'clusters.yaml',start_timeout=180,finish_timeout=60,drain_timeout=180,observe_seconds=60,simulate_drain_noop=False)
run_controller(ClassicProbe(a,uuid4().hex))
