// Adapter around the real, pinned upstream Agent. Never an alternative loop model.
import { readFileSync } from 'node:fs';
import { Agent } from './pi/packages/agent/src/agent.ts';
import { EventStream } from './pi/packages/ai/src/index.ts';
const fixture = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const requests: any[] = [], effects: any[] = [], events: any[] = [], hooks: string[] = [];
const requestModels: string[] = [];
const usage = {input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
function message(m: any): any {
 if (m.role === 'assistant') return {...m, content:m.content.map((b:any)=>b.type==='tool_call'?{...b,type:'toolCall'}:b), api:'mock', provider:'mock',model:'mock',usage,stopReason:m.stop_reason==='tool_use'?'toolUse':m.stop_reason,...(m.error?{errorMessage:m.error}:{}),timestamp:0};
 if (m.role === 'system') return {role:'system',content:m.content??'',sections:m.sections,toolsAdded:m.tools_added?.map((t:any)=>({name:t.name,description:t.description,parameters:t.input_schema})),toolsRemoved:m.tools_removed?.map((name:string)=>({name})),timestamp:0};
 return {...m,timestamp:0};
}
function normalize(m:any):any {
 if(m.role==='system') return {role:'system',content:m.content,...(m.sections&&Object.keys(m.sections).length?{sections:m.sections}:{}),...(m.toolsAdded?.length?{tools_added:m.toolsAdded.map((t:any)=>({name:t.name,description:t.description,input_schema:t.parameters}))}:{}),...(m.toolsRemoved?.length?{tools_removed:m.toolsRemoved.map((t:any)=>t.name)}:{})};
 if(m.role==='assistant') return {role:'assistant',content:m.content.map((b:any)=>b.type==='toolCall'?{type:'tool_call',id:b.id,name:b.name,arguments:b.arguments}:{type:'text',text:b.text}),stop_reason:m.stopReason==='toolUse'?'tool_use':m.stopReason};
 if(m.role==='toolResult') return {role:'tool_result',call_id:m.toolCallId,name:m.toolName,content:m.content,is_error:m.isError};
 return {role:m.role,content:m.content};
}
let index=0,finishedTurns=0,preparedRequests=0;
const barriers = new Map<string,{promise:Promise<void>,resolve:()=>void}>();
function barrier(id:string){if(!barriers.has(id)){let resolve!:()=>void;const promise=new Promise<void>(r=>resolve=r);barriers.set(id,{promise,resolve});}return barriers.get(id)!;}
// A tool that honors the abort signal the way Pi's tools do: it rejects when aborted.
function untilAborted(signal:AbortSignal|undefined){return new Promise<never>((_,reject)=>{const stop=()=>reject(new Error('Operation aborted'));if(signal?.aborted)stop();else signal?.addEventListener('abort',stop,{once:true});});}
const tools=(fixture.tools??[]).map((t:any)=>({name:t.name,label:t.name,description:t.description??'',parameters:t.input_schema??{type:'object'},executionMode:t.execution_mode,
 prepareArguments:t.prepare_number?(args:any)=>({...args,[t.prepare_number]:Number(args[t.prepare_number])}):undefined,
 execute:async(id:string,args:any,signal?:AbortSignal)=>{effects.push({name:t.name,arguments:structuredClone(args)});if(t.wait_for)await barrier(t.wait_for).promise;if(t.wait_abort)await untilAborted(signal);if(t.error)throw new Error(t.error);return {content:[{type:'text',text:t.result??JSON.stringify(args)}],details:{},...(t.structured_content?{structuredContent:t.structured_content}:{}),...(t.terminate?{terminate:true}:{})};}}));
// Stream a response's text blocks, then fail with it or wait for abort, as pi-ai providers do.
function streamFailure(stream:any,m:any,signal:AbortSignal|undefined){
 const partial={...m,content:[] as any[],stopReason:'stop'};
 const snapshot=()=>({...partial,content:partial.content.map((b:any)=>({...b}))});
 stream.push({type:'start',partial:snapshot()});
 m.content.forEach((b:any,i:number)=>{partial.content.push({type:'text',text:''});stream.push({type:'text_start',contentIndex:i,partial:snapshot()});partial.content[i]={type:'text',text:b.text};stream.push({type:'text_delta',contentIndex:i,delta:b.text,partial:snapshot()});});
 const fail=()=>{const error={...snapshot(),stopReason:m.stopReason,errorMessage:m.errorMessage};stream.push({type:'error',reason:m.stopReason,error});stream.end(error);};
 if(m.stopReason!=='aborted'||signal?.aborted)fail();else signal?.addEventListener('abort',fail,{once:true});
}
const agent = new Agent({initialState:{systemPrompt:fixture.system_prompt??'',tools,model:{id:'mock',name:'mock',api:'mock',provider:'mock',baseUrl:'',reasoning:false,input:['text'],cost:{input:0,output:0,cacheRead:0,cacheWrite:0},contextWindow:0,maxTokens:0},messages:(fixture.history??[]).map(message)},
 toolExecution:fixture.execution_mode??'parallel',steeringMode:fixture.steering_mode??'one-at-a-time',followUpMode:fixture.follow_up_mode??'one-at-a-time',
 beforeToolCall:fixture.block_ids||fixture.before_error?async(ctx:any)=>{const error=fixture.before_error?.[ctx.toolCall.id];if(error)throw new Error(error);return fixture.block_ids?.includes(ctx.toolCall.id)?{block:true,reason:'blocked'}:undefined;}:undefined,
 afterToolCall:fixture.after_text?async()=>({content:[{type:'text',text:fixture.after_text}]}):undefined,
 finishTurn:fixture.finish?()=>{hooks.push('finish_turn');const action=fixture.finish[finishedTurns++];return action?{action}:undefined;}:undefined,
 transformContext:fixture.transform?async(ms:any[])=>{hooks.push('transform');return [...ms,message({role:'user',content:'transformed'})];}:undefined,
 convertToLlm:fixture.transform?async(ms:any[])=>{hooks.push('convert');return ms;}:undefined,
 prepareRequest:fixture.prepare_model?async()=>{hooks.push('prepare_request');return preparedRequests++===0?{model:{...agent.state.model,id:fixture.prepare_model}}:undefined;}:undefined,
 prepareNextTurn:fixture.next_message?async()=>{hooks.push('prepare_next_turn');return {messages:[message({role:'user',content:fixture.next_message})]};}:undefined,
 streamFn:(_model:any,context:any,options:any)=>{
 const stream = new EventStream((e:any)=>e.type==='done'||e.type==='error',(e:any)=>e.message??e.error);
 // A provider given an already aborted signal answers at once and sends no request.
 if(options?.signal?.aborted){const m=message({role:'assistant',content:[{type:'text',text:''}],stop_reason:'aborted',error:'Request aborted'});queueMicrotask(()=>{stream.push({type:'error',reason:'aborted',error:m});stream.end(m)});return stream;}
 requests.push(context.messages.map(normalize));requestModels.push(_model.id);
 if(!fixture.responses[index])throw new Error('Fixture responses exhausted');
 const spec=fixture.responses[index++];
 const m=message(spec);
 if(spec.stream)queueMicrotask(()=>streamFailure(stream,m,options?.signal));
 else queueMicrotask(()=>{stream.push({type:'done',reason:m.stopReason,message:m});stream.end(m)});
 return stream;
}});
agent.subscribe((event:any)=>{
 // Partial snapshots are not compared; committed messages are.
 events.push({type:event.type,...(event.toolCallId?{call_id:event.toolCallId}:{}),...(event.message&&event.type!=='message_update'?{message:normalize(event.message)}:{})});
 if(event.type==='tool_execution_end')barrier(event.toolCallId).resolve();
 for(const op of fixture.operations??[]){
  if(op.event!==event.type||op.done||(op.delta_type&&event.assistantMessageEvent?.type!==op.delta_type))continue;
  op.done=true;
  if(op.method==='abort')agent.abort();else agent[op.method==='follow_up'?'followUp':op.method](message(op.message));
 }
});
await agent.prompt(typeof fixture.prompt==='string'?fixture.prompt:message(fixture.prompt));
const last=agent.state.messages.at(-1);
const status=last?.role==='assistant'&&last.stopReason==='aborted'?'cancelled':agent.state.errorMessage?'failed':'completed';
console.log(JSON.stringify({requests,request_models:requestModels,effects,events,messages:agent.state.messages.map(normalize),hooks,status},null,2));
