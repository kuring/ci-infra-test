import argparse, json, logging, os, subprocess
from uuid import uuid4
import drain_test_kind as m
from drain_test_run import command, run_controller, wait_for
from drain_test_checks import require_limits, runner_limits

OUT=m.ROOT/'diagnosis'
OPS=m.LOCAL/'diagnosis/recipes/capacity-control.just'
class StaticControl(m.KindDrainTest):
    static_installed=False
    def static_limits(self,desired):
        require_limits(runner_limits(self.scale_sets()),desired)
        items=json.loads(self.kube('get','autoscalinglisteners','-n','arc-systems','-o','json'))['items']
        selected=[x for x in items if x['spec']['autoscalingRunnerSetName']==m.LABEL]
        assert len(selected)==1
        value=selected[0]['spec'].get('maxRunners',0)
        if value!=desired[m.LABEL]:raise ValueError('Waiting for static listener maxRunners')
        pods=json.loads(self.kube('get','pods','-n','arc-systems','-l','app.kubernetes.io/component=runner-scale-set-listener','-o','json'))['items']
        assert len(pods)==1
        env={x['name']:x.get('value') for x in pods[0]['spec']['containers'][0].get('env',[])}
        if env.get('CAPACITY_AWARE_ENABLED')!='false':raise ValueError('Capacity monitor not disabled')
        return True
    def record(self,stage):
        data={'experiment':'static-capacity-control','stage':stage,'pr':self.pr,'run_id':self.run_id,'jobs':[{k:j.get(k) for k in ('id','name','status','conclusion','runner_id','runner_name','started_at','completed_at')} for j in self.jobs()] if self.run_id else []}
        path=OUT/f'{self.run_key}-static-{stage}.json';path.write_text(json.dumps(data,indent=2));path.chmod(0o600)
        logging.info('Evidence: %s',json.dumps(data))
    def run(self):
        self.static_installed=True
        logging.info('Installing same ARC version with capacity-aware disabled')
        result=command(['just','--justfile',str(OPS),'install-static'],cwd=m.UPSTREAM,timeout=360)
        logging.info('Static install completed: %s',result.stdout.strip())
        self.prepare();self.check_probe(self.scale_sets())
        wait_for('static listener configured maxRunners=3 and capacity-aware disabled',lambda:self.static_limits(self.desired),180)
        self.record('configured');self.create_pr()
        wait_for('canary workflow created',self.find_run,120)
        wait_for('static control in-flight job executing',self.inflight_ready,self.args.start_timeout)
        self.draining=True
        self.drain=subprocess.Popen(['just','drain-runners',self.cluster],cwd=self.args.root_dir,env={**self.env,'OSDC_DRAIN_TIMEOUT_SECS':'300'},start_new_session=True)
        wait_for('static ARS and listener maxRunners=0',lambda:self.static_limits({m.LABEL:0}),180)
        self.api(f'pulls/{self.pr}',{'body':f'OSDC_DRAIN_RELEASE_{self.run_key}'},'PATCH')
        wait_for('static control in-flight job succeeds',lambda:self.completed('inflight',1),180)
        if self.drain.wait(timeout=180)!=0:raise RuntimeError('Static drain failed')
        wait_for('static control held jobs queued',self.held_ready,120)
        self.record('queued-while-drained')
        import time
        deadline=time.monotonic()+60
        while True:
            self.held_ready();self.static_limits({m.LABEL:0})
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            time.sleep(min(5,remaining))
        self.resume()
        wait_for('static listener configured maxRunners=3 after resume',lambda:self.static_limits(self.desired),180)
        self.record('after-resume')
        try:wait_for('static control held jobs succeed after undrain',lambda:self.completed('held',3),180)
        finally:self.record('end')
    def cleanup(self):
        ok=super().cleanup()
        if self.static_installed and not self.draining:
            try:
                result=command(['just','--justfile',str(OPS),'restore-capacity-aware'],cwd=m.UPSTREAM,timeout=360)
                logging.info('Restored original capacity-aware configuration: %s',result.stdout.strip())
                wait_for('original advertised capacity restored to 3',lambda:self.capacities().get(m.LABEL)==3,180)
                self.static_installed=False
            except Exception:logging.exception('Capacity-aware restoration needs retry');return False
        return ok

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
p=argparse.ArgumentParser();p.add_argument('action',choices=['install-static','restore','test']);a=p.parse_args()
if a.action=='install-static':
    original=m.runner_values
    def static_values():
        values=original()
        for entry in values['listenerTemplate']['spec']['containers'][0]['env']:
            if entry['name']=='CAPACITY_AWARE_ENABLED':entry['value']='false'
        return values
    m.runner_values=static_values
    m.install()
elif a.action=='restore':m.install()
else:
    os.environ['GH_TOKEN']=m.read_token(m.ROOT)
    args=argparse.Namespace(action='test',root_dir=m.ROOT,upstream_dir=m.UPSTREAM,clusters_yaml=m.ROOT/'clusters.yaml',start_timeout=180,finish_timeout=180,drain_timeout=180,observe_seconds=60,simulate_drain_noop=False)
    run_controller(StaticControl(args,uuid4().hex))
