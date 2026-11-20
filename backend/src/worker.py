import traceback,json,os
from pathlib import Path
from src.config.database import Session
from src.models.entities import Trial,CrashDrill
from src.services.trials import queue
from src.services.crash import CRASH_QUEUE
from src.services.routing import simulate
from src.services.crash_recovery import simulate_crash

TRIAL_QUEUE='replicaroute:queue'
ARTIFACT_DIR=Path(os.getenv('ARTIFACT_DIR','/artifacts'))

def write_artifact(name,result):
    ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
    (ARTIFACT_DIR/name).write_text(json.dumps(result,ensure_ascii=False,indent=2))

def run_trial(ident):
    with Session() as db:
        obj=db.get(Trial,ident)
        if not obj:return
        obj.status='running';db.commit()
        snapshot=obj.input_snapshot;delay=obj.max_delay_ms;sticky=obj.sticky_ms
    result=simulate(snapshot['nodes'],snapshot['request_set']['events'],delay,sticky)
    with Session() as db:
        obj=db.get(Trial,ident)
        if not obj:return
        obj.result=result;obj.status='completed';db.commit()
    write_artifact('route-%d.json'%ident,result)

def run_crash_drill(ident):
    with Session() as db:
        obj=db.get(CrashDrill,ident)
        if not obj:return
        obj.status='running';db.commit()
        snapshot=obj.input_snapshot;capacity=obj.capacity
    result=simulate_crash(capacity,snapshot['script']['events'])
    with Session() as db:
        obj=db.get(CrashDrill,ident)
        if not obj:return
        obj.result=result;obj.status='completed';db.commit()
    write_artifact('crash-%d.json'%ident,result)

def mark_failed(model,ident,error):
    with Session() as db:
        obj=db.get(model,ident)
        if obj:obj.status='failed';obj.error=error;db.commit()

def main():
    while True:
        Path('/tmp/worker-heartbeat').touch()
        # Two independent queues; the first non-empty key wins (BPOP order by key name).
        item=queue.blpop([TRIAL_QUEUE,CRASH_QUEUE],timeout=2)
        if not item:continue
        queue_name,raw=item
        try:
            ident=int(raw)
            if queue_name==CRASH_QUEUE:run_crash_drill(ident)
            else:run_trial(ident)
        except Exception as e:
            traceback.print_exc()
            mark_failed(CrashDrill if queue_name==CRASH_QUEUE else Trial,ident,str(e))

if __name__=='__main__':main()
