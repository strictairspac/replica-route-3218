from sqlalchemy import select
from redis import Redis
from fastapi import HTTPException
from src.models.entities import CrashScript,CrashDrill
from src.config.settings import REDIS_URL
from src.utils.serialization import record
from .common import save
from .crash_recovery import simulate_crash

queue=Redis.from_url(REDIS_URL,decode_responses=True)
CRASH_QUEUE='replicaroute:crash-queue'

def _script_values(data):
    values=data.model_dump()
    values['events']=[e.model_dump() for e in data.events]
    return values

def listing_scripts(db):return [record(x) for x in db.scalars(select(CrashScript).order_by(CrashScript.id))]
def listing(db):return [record(x) for x in db.scalars(select(CrashDrill).order_by(CrashDrill.id.desc()))]

def detail(db,ident):
    obj=db.get(CrashDrill,ident)
    if not obj:raise HTTPException(404,'断电演练不存在')
    return record(obj)

def create_script(db,data,user):
    try:simulate_crash(data.capacity,[e.model_dump() for e in data.events])
    except ValueError as e:raise HTTPException(422,str(e))
    return save(db,CrashScript(**_script_values(data)),user,'创建断电时序 '+data.code)

def update_script(db,ident,data,user):
    obj=db.get(CrashScript,ident)
    if not obj:raise HTTPException(404,'时序脚本不存在')
    try:simulate_crash(data.capacity,[e.model_dump() for e in data.events])
    except ValueError as e:raise HTTPException(422,str(e))
    for k,v in _script_values(data).items():setattr(obj,k,v)
    return save(db,obj,user,'更新断电时序 '+data.code)

def create(db,data,user):
    script=db.get(CrashScript,data.script_id)
    if not script:raise HTTPException(404,'时序脚本不存在')
    capacity=data.capacity or script.capacity
    events=[dict(e) for e in script.events]
    try:simulate_crash(capacity,events)
    except ValueError as e:raise HTTPException(422,str(e))
    snapshot={'script':record(script),'capacity':capacity}
    obj=CrashDrill(name=data.name,script_id=script.id,capacity=capacity,input_snapshot=snapshot,status='queued')
    result=save(db,obj,user,'断电演练入队 '+data.name)
    try:queue.rpush(CRASH_QUEUE,str(obj.id))
    except Exception:
        obj.status='failed';obj.error='队列暂时不可用，请重新创建';db.commit()
        raise HTTPException(503,obj.error)
    return result
