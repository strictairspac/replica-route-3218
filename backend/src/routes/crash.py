from fastapi import APIRouter,Depends
from fastapi.responses import Response
import json
from src.config.database import db_session
from src.utils.security import current,writer
from src.schemas.crash import CrashScriptInput,CrashDrillInput
from src.services import crash as service
router=APIRouter()

@router.get('/crash_scripts')
def scripts(db=Depends(db_session),user=Depends(current)):return service.listing_scripts(db)

@router.post('/crash_scripts',status_code=201)
def create_script(data:CrashScriptInput,db=Depends(db_session),user=Depends(writer)):return service.create_script(db,data,user)

@router.put('/crash_scripts/{ident}')
def update_script(ident:int,data:CrashScriptInput,db=Depends(db_session),user=Depends(writer)):return service.update_script(db,ident,data,user)

@router.get('/crash_drills')
def listing(db=Depends(db_session),user=Depends(current)):return service.listing(db)

@router.post('/crash_drills',status_code=201)
def create(data:CrashDrillInput,db=Depends(db_session),user=Depends(writer)):return service.create(db,data,user)

@router.get('/crash_drills/{ident}')
def detail(ident:int,db=Depends(db_session),user=Depends(current)):return service.detail(db,ident)

@router.get('/crash_drills/{ident}/export')
def export(ident:int,db=Depends(db_session),user=Depends(current)):
    return Response(json.dumps(service.detail(db,ident),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="crash-{ident}.json"'})
