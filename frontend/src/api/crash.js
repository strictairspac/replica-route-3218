import {api} from './client.js';
import {session} from '../hooks/session.js';
export const crashApi={
  listScripts:()=>api('/crash_scripts'),
  createScript:data=>api('/crash_scripts','POST',data),
  updateScript:(id,data)=>api('/crash_scripts/'+id,'PUT',data),
  listDrills:()=>api('/crash_drills'),
  createDrill:data=>api('/crash_drills','POST',data),
  getDrill:id=>api('/crash_drills/'+id),
};
export async function downloadDrill(id){
  const res=await fetch('/api/crash_drills/'+id+'/export',{headers:{Authorization:'Bearer '+session.token}});
  if(!res.ok)throw Error('导出失败，请重新登录后重试');
  const url=URL.createObjectURL(await res.blob());
  const a=document.createElement('a');a.href=url;a.download='crash-'+id+'.json';a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
