import { Agent } from './pi/packages/agent/src/agent.ts';
import { EventStream } from './pi/packages/ai/src/index.ts';
const durations:number[]=[];
const count=200;
for(let i=0;i<count+10;i++) {
 const start=performance.now();
 const agent=new Agent({streamFn:()=>{
  const stream=new EventStream((e:any)=>e.type==='done',(e:any)=>e.message);
  const message={role:'assistant',content:[{type:'text',text:'hello'}],api:'mock',provider:'mock',model:'mock',usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},stopReason:'stop',timestamp:0};
  queueMicrotask(()=>{stream.push({type:'done',reason:'stop',message});stream.end(message);});return stream;
 }});
 await agent.prompt('hello');
 if(i>=10)durations.push(performance.now()-start);
}
console.log(JSON.stringify({milliseconds:durations}));
