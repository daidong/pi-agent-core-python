// Observe public contracts of the actual pinned upstream; no replacement loop.
import { Agent } from './pi-followup/packages/agent/src/agent.ts';
import { runAgentLoop, runAgentLoopContinue } from './pi-followup/packages/agent/src/agent-loop.ts';
import { AssistantMessageEventStream, EventStream } from './pi-followup/packages/ai/src/utils/event-stream.ts';

const usage = {input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
const model:any = {id:'mock',name:'mock',api:'mock',provider:'mock',baseUrl:'',reasoning:false,input:['text'],cost:usage.cost,contextWindow:10000,maxTokens:100};
const message = (stopReason='stop', timestamp=Date.now()):any => ({role:'assistant',content:[],api:'mock',provider:'mock',model:'mock',usage,stopReason,timestamp});
const streamFn:any = (_model:any, _context:any, options:any) => {
  const stream = new AssistantMessageEventStream();
  const m = message(options?.signal?.aborted ? 'aborted' : 'stop');
  if (m.stopReason === 'aborted') stream.push({type:'error',reason:'aborted',error:m});
  else stream.push({type:'done',reason:'stop',message:m});
  stream.end(m);
  return stream;
};
const observed:any = {};
for (const [key, reason, timestamp] of [['old_message','stop',1], ['new_deferred','deferred',undefined]] as const) {
  const stream = new AssistantMessageEventStream();
  const m = message(reason, timestamp);
  stream.push({type:'done',reason:reason as any,message:m});
  observed[key] = {timed:(await stream.result()).durationMs !== undefined};
}
const signal = AbortSignal.abort();
const events:any[] = [];
const config:any = {model, convertToLlm:(messages:any)=>messages};
const result = await runAgentLoop([{role:'user',content:'go',timestamp:Date.now()}], {messages:[],tools:[]}, config, e=>{events.push(e)}, signal, streamFn);
observed.pre_cancelled = {events:events.map(e=>e.type),roles:result.map(m=>m.role)};
try {
  await runAgentLoopContinue({messages:[{role:'user',content:'go',timestamp:0},message('error')],tools:[]}, config, ()=>{}, undefined, streamFn);
  observed.continue_failed_assistant = {accepted:true};
} catch { observed.continue_failed_assistant = {accepted:false}; }
const agent = new Agent({initialState:{model},streamFn,finishTurn:()=>({action:'end'})});
await agent.prompt('go');
observed.finish_object = {accepted:!agent.state.errorMessage};
const eventAgent = new Agent({initialState:{model},streamFn});
eventAgent.subscribe((e:any)=>{if(e.type==='agent_end') observed.agent_end={keys:Object.keys(e).sort()};});
await eventAgent.prompt('go');
const stream = new EventStream<number,number>(()=>false, x=>x);
for(let i=0;i<200;i++) stream.push(i);
stream.end(200);
const iterator=stream[Symbol.asyncIterator]();
await iterator.next();
await stream.result();
let remaining=0;
for(let next=await iterator.next();!next.done;next=await iterator.next()) remaining++;
observed.partial_stream_result = {completed:true,remaining_events:remaining};
console.log(JSON.stringify(observed));
