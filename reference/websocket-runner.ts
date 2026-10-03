// Drive the pinned Codex adapter over a scripted in-process WebSocket; record sent frames.
import {readFileSync} from 'node:fs';
import {streamSimple, closeOpenAICodexWebSocketSessions} from './pi/packages/ai/src/api/openai-codex-responses.ts';
const fixture=JSON.parse(readFileSync(process.argv[2],'utf8'));
const sent:any[]=[];
let connections=0;
class ScriptedWebSocket {
 static OPEN=1;
 readyState=1;
 id=++connections;
 listeners=new Map<string,Set<(event:any)=>void>>();
 constructor(_url:string,_options?:unknown){queueMicrotask(()=>this.dispatch('open',{}));}
 addEventListener(type:string,listener:(event:any)=>void){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type)!.add(listener);}
 removeEventListener(type:string,listener:(event:any)=>void){this.listeners.get(type)?.delete(listener);}
 send(data:string){
  sent.push({connection:this.id,body:JSON.parse(data)});
  const events=fixture.responses[sent.length-1];
  queueMicrotask(()=>{for(const event of events)this.dispatch('message',{data:JSON.stringify(event)});});
 }
 close(){this.readyState=3;}
 dispatch(type:string,event:any){for(const listener of this.listeners.get(type)??[])listener(event);}
}
(globalThis as any).WebSocket=ScriptedWebSocket;
const model:any={...fixture.model,provider:'openai-codex',baseUrl:'https://fixture.test'};
let messages:any[]=[];
const stops:string[]=[];
for(const turn of fixture.turns){
 messages=[...messages,...turn];
 const result=await streamSimple(model,{messages} as any,{apiKey:fixture.api_key,transport:'websocket-cached',...fixture.options}).result();
 if(result.stopReason==='error')throw new Error(result.errorMessage);
 stops.push(result.stopReason==='toolUse'?'tool_use':result.stopReason);
 messages=[...messages,result];
}
console.log(JSON.stringify({sent,stops}));
// Cached sockets hold a five-minute idle timer; close them so the process can exit.
closeOpenAICodexWebSocketSessions();
process.exit(0);
