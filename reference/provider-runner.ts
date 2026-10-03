// Execute the pinned upstream providers against synthetic transport fixtures.
import {zstdDecompressSync} from 'node:zlib';
import {readFileSync} from 'node:fs';
import {stream as anthropic, streamSimple as anthropicSimple} from './pi/packages/ai/src/api/anthropic-messages.ts';
import {stream as openai, streamSimple as openaiSimple} from './pi/packages/ai/src/api/openai-responses.ts';
import {stream as codex, streamSimple as codexSimple} from './pi/packages/ai/src/api/openai-codex-responses.ts';
import {stream as completions, streamSimple as completionsSimple} from './pi/packages/ai/src/api/openai-completions.ts';
import {streamProxy} from './pi/packages/agent/src/proxy.ts';
const fixture=JSON.parse(readFileSync(process.argv[2],'utf8'));
// Chat Completions fixtures: chunks are bare `data:` lines ending with [DONE], the model
// may name its own provider, and `base_url` may name the endpoint (fetch is still faked).
const chat=fixture.provider==='openai-completions';
const api=fixture.provider==='anthropic'?'anthropic-messages':fixture.provider==='openai-codex'?'openai-codex-responses':chat?'openai-completions':'openai-responses';
// A fixture may pin a catalog model descriptor; the transport endpoint is always local.
const model:any={id:'test-model',name:'Test',api,provider:fixture.provider==='proxy'?'openai':fixture.provider,reasoning:true,input:['text','image'],maxTokens:4096,contextWindow:100000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0},...(fixture.model??{}),baseUrl:chat&&fixture.base_url?fixture.base_url:'https://fixture.test'};
const data=chat?fixture.events.map((e:any)=>`data: ${JSON.stringify(e)}\n\n`).join('')+'data: [DONE]\n\n':fixture.events.map((e:any)=>`event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join('');
let request:any;
const headerNames=['authorization','x-api-key','anthropic-version','anthropic-beta','content-type','chatgpt-account-id','originator','openai-beta','session-id','x-client-request-id','x-app','anthropic-dangerous-direct-browser-access',...(chat?['session_id','x-session-affinity','x-session-id']:[])];
const fakeFetch:any=async(url:any, init:any)=>{
 const headers=new Headers(init?.headers ?? url.headers);
 let raw=init?.body ?? await url.text();
 if(headers.get("content-encoding")==="zstd") raw=zstdDecompressSync(raw).toString("utf8");
 request={url:String(url.url??url),body:JSON.parse(raw),headers:Object.fromEntries(headerNames.filter(k=>headers.has(k)).map(k=>[k,headers.get(k)]))};
 return new Response(data,{status:200,headers:{'content-type':'text/event-stream'}});
};
const context:any=fixture.context??{messages:[{role:'user',content:'go',timestamp:0}]};
const options:any={apiKey:fixture.api_key??(fixture.oauth?'sk-ant-oat-fixture':'fixture'),fetch:fakeFetch,maxRetries:0,transport:'sse',...fixture.options};
let stream:any;
if(fixture.provider==='proxy'){
 globalThis.fetch=fakeFetch;
 stream=streamProxy(model,context,{proxyUrl:'https://fixture.test',authToken:'fixture',...fixture.options});
}else{
 // "simple" is the entry pi-agent-core uses: a reasoning level instead of provider options.
 const simple=fixture.entry==='simple';
 stream=(fixture.provider==='anthropic'?(simple?anthropicSimple:anthropic):fixture.provider==='openai-codex'?(simple?codexSimple:codex):chat?(simple?completionsSimple:completions):(simple?openaiSimple:openai))(model,context,options);
}
function block(b:any):any {
 if(b.type==='text')return {type:'text',text:b.text,...(b.textSignature?{text_signature:b.textSignature}:{})};
 if(b.type==='thinking')return {type:'thinking',thinking:b.thinking,...(b.thinkingSignature?{thinking_signature:b.thinkingSignature}:{}),redacted:b.redacted??false};
 if(b.type==='toolCall')return {type:'tool_call',id:b.id,name:b.name,arguments:b.arguments,...(b.thoughtSignature?{thought_signature:b.thoughtSignature}:{}),...(b.namespace?{namespace:b.namespace}:{})};
 throw new Error(`Unexpected output ${b.type}`);
}
const events=[];
for await (const e of stream) {
 const row:any={type:e.type};
 if(e.contentIndex!==undefined)row.index=e.contentIndex;
 if(e.delta!==undefined)row.delta=e.delta;
 if(e.content!==undefined)row.content=e.content;
 if(e.toolCall)row.block=block(e.toolCall);
 if(e.reason)row.reason=e.reason==='toolUse'?'tool_use':e.reason;
 events.push(row);
}
const result=await stream.result();
if(result.stopReason==='error')throw new Error(result.errorMessage);
// Chat Completions fixtures also compare token usage and response metadata (not cost).
const u=result.usage;
const meta=chat?{meta:{usage:{input:u.input,output:u.output,cache_read:u.cacheRead,cache_write:u.cacheWrite,reasoning:u.reasoning??0,total_tokens:u.totalTokens},response_id:result.responseId??null,response_model:result.responseModel??null,raw_stop_reason:result.rawStopReason??null}}:{};
console.log(JSON.stringify({content:result.content.map(block),...(result.providerThinkingLevel?{provider_thinking_level:result.providerThinkingLevel}:{}),stop_reason:result.stopReason==='toolUse'?'tool_use':result.stopReason,events,...(fixture.provider==='proxy'?{}:{request}),...meta}));
