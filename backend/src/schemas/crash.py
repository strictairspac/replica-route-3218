from typing import Literal
from pydantic import BaseModel,Field,model_validator

class CrashEvent(BaseModel):
    at_ms:int=Field(ge=0,le=86400000)
    kind:Literal['write','commit','abort','flush','read','crash','restart']
    session:str|None=Field(default=None,min_length=1,max_length=60)
    key:str|None=Field(default=None,min_length=1,max_length=60)
    value:str|int|float|bool|None=None
    apply_limit:int|None=Field(default=None,ge=1,le=1000)
    @model_validator(mode='after')
    def valid_event(self):
        if self.kind in ('write','commit','abort') and not self.session:raise ValueError('写入、提交与回滚事件需要session')
        if self.kind in ('write','read') and not self.key:raise ValueError('写入与读取事件需要key')
        if self.kind=='read' and not self.session:raise ValueError('读取事件需要session')
        if self.kind=='restart' and self.apply_limit is None:return self
        if self.kind!='restart' and self.apply_limit is not None:raise ValueError('apply_limit仅用于restart事件')
        return self

class CrashScriptInput(BaseModel):
    code:str=Field(min_length=2,max_length=80)
    title:str=Field(min_length=2,max_length=160)
    capacity:int=Field(ge=1,le=32)
    events:list[CrashEvent]=Field(min_length=1,max_length=200)
    note:str=Field(default='',max_length=300)
    @model_validator(mode='after')
    def chronological(self):
        if any(a.at_ms>b.at_ms for a,b in zip(self.events,self.events[1:])):raise ValueError('事件必须按at_ms非递减排列')
        return self

class CrashDrillInput(BaseModel):
    name:str=Field(min_length=2,max_length=120)
    script_id:int=Field(gt=0)
    capacity:int|None=Field(default=None,ge=1,le=32)
