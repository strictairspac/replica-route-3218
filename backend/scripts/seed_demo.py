from sqlalchemy import select,func
from src.config.database import Session,Base,engine
from src.models.entities import User,Node,RequestSet,Trial,CrashScript,CrashDrill
from src.utils.security import password_hash

def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email=role+'@qiyuan.test'
            if not db.scalar(select(User).where(User.email==email)):db.add(User(email=email,password=password_hash('Demo2026!'),role=role))
        for name,role,pos,delay,note in [('harbor-primary','primary',105,0,'栖原主站 · 写入航道'),('north-replica','replica',104,150,'北站副本 · 延迟偏高'),('south-replica','replica',105,20,'南站副本 · 常用读航道')]:
            if not db.scalar(select(Node).where(Node.name==name)):db.add(Node(name=name,role=role,status='healthy',lineage='qiyuan-log-01',position=pos,delay_ms=delay,note=note))
        mixed=[{'at_ms':0,'kind':'write','session':'lab-A'},{'at_ms':100,'kind':'read','session':'lab-A'},{'at_ms':600,'kind':'replicate','node':'south-replica','position':106,'delay_ms':20},{'at_ms':700,'kind':'read','session':'lab-A'},{'at_ms':800,'kind':'node_down','node':'south-replica'},{'at_ms':900,'kind':'read','session':'lab-A'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-A'}]
        cases=[('QY-1117-A','交接班写后读与双节点故障',mixed),('QY-1117-B','延迟超标回退主库',[{'at_ms':0,'kind':'node_down','node':'south-replica'},{'at_ms':10,'kind':'read','session':'visitor'}]),('QY-1117-C','双副本不可读时拒绝',[{'at_ms':0,'kind':'write','session':'lab-B'},{'at_ms':1000,'kind':'node_down','node':'harbor-primary'},{'at_ms':1100,'kind':'read','session':'lab-B'}])]
        for code,title,events in cases:
            if not db.scalar(select(RequestSet).where(RequestSet.code==code)):db.add(RequestSet(code=code,title=title,events=events,note='栖原站点值班交接 · 09:15 请求轨迹'))
        crash_cases=[('QY-CRASH-A','缓存满排队、断电与恢复中断续跑',2,[
            {'at_ms':0,'kind':'write','session':'A','key':'acct','value':100},
            {'at_ms':10,'kind':'commit','session':'A'},
            {'at_ms':20,'kind':'write','session':'A','key':'acct','value':60},
            {'at_ms':30,'kind':'commit','session':'A'},
            {'at_ms':40,'kind':'write','session':'B','key':'acct','value':999},
            {'at_ms':50,'kind':'write','session':'C','key':'ship','value':'c1'},
            {'at_ms':60,'kind':'write','session':'D','key':'hold','value':'d1'},
            {'at_ms':70,'kind':'read','session':'A','key':'acct'},
            {'at_ms':80,'kind':'read','session':'B','key':'acct'},
            {'at_ms':150,'kind':'commit','session':'C'},
            {'at_ms':160,'kind':'commit','session':'B'},
            {'at_ms':170,'kind':'crash'},
            {'at_ms':180,'kind':'read','session':'A','key':'acct'},
            {'at_ms':200,'kind':'restart','apply_limit':1},
            {'at_ms':210,'kind':'read','session':'A','key':'acct'},
            {'at_ms':230,'kind':'restart'},
            {'at_ms':240,'kind':'read','session':'A','key':'acct'},
        ],'缓存2页：acct 与 ship 占满且未提交，hold 等待事务结束；随后断电、恢复中断再续跑'),
        ('QY-CRASH-B','已提交未刷盘页的断电恢复',4,[
            {'at_ms':0,'kind':'write','session':'A','key':'acct','value':100},
            {'at_ms':10,'kind':'commit','session':'A'},
            {'at_ms':20,'kind':'write','session':'A','key':'acct','value':60},
            {'at_ms':30,'kind':'commit','session':'A'},
            {'at_ms':40,'kind':'write','session':'B','key':'acct','value':999},
            {'at_ms':50,'kind':'crash'},
            {'at_ms':60,'kind':'restart'},
            {'at_ms':70,'kind':'read','session':'A','key':'acct'},
        ],'两个已提交事务留在缓存未刷盘，B 的未提交值既不得落盘也不得被读'),
        ('QY-CRASH-C','回滚释放槽位后继续写入',2,[
            {'at_ms':0,'kind':'write','session':'A','key':'acct','value':100},
            {'at_ms':10,'kind':'write','session':'B','key':'ship','value':'b1'},
            {'at_ms':20,'kind':'write','session':'C','key':'hold','value':'c1'},
            {'at_ms':100,'kind':'abort','session':'A'},
            {'at_ms':110,'kind':'read','session':'D','key':'acct'},
            {'at_ms':120,'kind':'commit','session':'B'},
            {'at_ms':130,'kind':'crash'},
            {'at_ms':140,'kind':'restart'},
            {'at_ms':150,'kind':'read','session':'D','key':'ship'},
        ],'未提交事务回滚直接释放槽位，已提交事务断电后由 WAL 重放补回')]
        for code,title,capacity,events,note in crash_cases:
            if not db.scalar(select(CrashScript).where(CrashScript.code==code)):
                db.add(CrashScript(code=code,title=title,capacity=capacity,events=events,note=note))
        db.commit();print({cls.__tablename__:db.scalar(select(func.count()).select_from(cls)) for cls in (User,Node,RequestSet,Trial,CrashScript,CrashDrill)})
if __name__=='__main__':seed()
