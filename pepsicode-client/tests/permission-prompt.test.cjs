const {test}=require("node:test"),assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path");
const {createRequire}=require("node:module"),ts=require("typescript"),React=require("react"),{create,act}=require("react-test-renderer");
function load(file){
  const filename=file+(fs.existsSync(file+".tsx")?".tsx":".ts"),result={},external=createRequire(filename);
  const code=ts.transpileModule(fs.readFileSync(filename,"utf8"),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
  new Function("require","exports",code)(id=>id.endsWith(".css")?{default:{}}:id==="../stores/session"?{}:id.startsWith(".")?load(path.resolve(path.dirname(filename),id)):external(id),result);
  return result;
}
const {PermissionCard}=load(path.resolve(__dirname,"../src/conversation/PermissionPrompt"));
const {permissionPresentation,permissionChoice}=load(path.resolve(__dirname,"../src/conversation/permissionPresentation"));
const request={prompt_id:"command-1",kind:"command",summary:"pepsicode wants to run a dangerous command",scope:'python -c "print(1)"',details:['cwd: D:\\项目','command: python -c "print(1)"','reason: python can execute arbitrary local code'],choices:[
  {key:"y",label:"allow once",decision:"allow_once"},{key:"a",label:"always allow this command",decision:"allow_always"},{key:"n",label:"deny once",decision:"deny_once"},{key:"d",label:"always deny this command",decision:"deny_always"},
]};
const text=node=>typeof node==="string"?node:node.children.map(text).join("");
const button=(renderer,label)=>renderer.root.findAllByType("button").find(node=>text(node)===label);
async function render(props){global.document={activeElement:null,body:{}};let renderer;await act(async()=>{renderer=create(React.createElement(PermissionCard,{request,...props}));});return renderer;}

test("approval presents the exact command and accurate Chinese scopes",()=>{
  assert.equal(permissionPresentation(request).preview,'python -c "print(1)"');
  assert.equal(permissionPresentation(request).cwd,"D:\\项目");
  const persistent=permissionChoice(request,request.choices[1]);
  assert.equal(persistent.label,"始终允许此命令");
  assert.ok(!persistent.description.includes("本轮"));
  assert.equal(permissionChoice({...request,kind:"edit"},{decision:"allow_turn"}).label,"本轮允许此文件");
});

test("long command and scope are available without expanding details",async()=>{
  const command="git status "+"argument ".repeat(80)+"END";
  const renderer=await render({request:{...request,scope:command,details:["cwd: D:\\project",`command: ${command}`]},approve:async()=>{}});
  const previews=renderer.root.findAllByType("pre").filter(node=>text(node)===command);
  assert.equal(previews.length,1);
  assert.equal(previews[0].props.className,undefined);
  assert.equal(button(renderer,"展开完整内容"),undefined);
  assert.ok(text(renderer.root).includes(`授权范围: ${command}`));
  act(()=>renderer.unmount());
});

test("only a deliberate click approves; duplicate clicks are locked and failed submissions can retry",async()=>{
  let reject,calls=0;
  const renderer=await render({approve:()=>{calls++;return new Promise((resolve,no)=>{reject=no;});}});
  assert.equal(calls,0);
  assert.equal(renderer.root.findByType("section").props["aria-modal"],undefined);
  await act(async()=>{button(renderer,"仅允许这一次").props.onClick();button(renderer,"仅允许这一次").props.onClick();});
  assert.equal(calls,1);
  assert.equal(button(renderer,"拒绝").props.disabled,true);
  await act(async()=>{reject(new Error("Connection closed"));});
  assert.ok(text(renderer.root.findByProps({role:"alert"})).includes("Connection closed"));
  assert.equal(button(renderer,"仅允许这一次").props.disabled,false);
  await act(async()=>{button(renderer,"仅允许这一次").props.onClick();});
  assert.equal(calls,2);
  await act(async()=>{reject(new Error("offline"));});
  act(()=>renderer.unmount());
});

test("additional scopes retain their exact backend decisions",async()=>{
  const calls=[],renderer=await render({approve:async(...args)=>calls.push(args)});
  assert.equal(button(renderer,"始终允许此命令"),undefined);
  act(()=>button(renderer,"更多选项").props.onClick());
  await act(async()=>button(renderer,"始终允许此命令保存到允许列表，后续匹配的请求无需再次确认").props.onClick());
  assert.equal(calls[0][0],"allow_always");
  act(()=>renderer.unmount());
});

test("feedback denies only after nonempty guidance is submitted",async()=>{
  const calls=[],renderer=await render({request:{...request,kind:"edit",choices:[...request.choices,{key:"6",label:"feedback",decision:"deny_with_feedback"}]},approve:async(...args)=>calls.push(args)});
  act(()=>button(renderer,"更多选项").props.onClick());
  act(()=>button(renderer,"拒绝并提出修改意见将你的意见交给模型调整下一步操作").props.onClick());
  assert.equal(calls.length,0);
  assert.equal(button(renderer,"发送意见并拒绝").props.disabled,true);
  act(()=>renderer.root.findByType("textarea").props.onChange({target:{value:"先读取文件"}}));
  await act(async()=>button(renderer,"发送意见并拒绝").props.onClick());
  assert.deepEqual(calls,[["deny_with_feedback","先读取文件"]]);
  act(()=>renderer.unmount());
});
