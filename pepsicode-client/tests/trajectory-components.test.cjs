const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const ts = require("typescript");
const React = require("react");
const { create, act } = require("react-test-renderer");
function load(file) {
  const filename = fs.existsSync(file + ".tsx") ? file + ".tsx" : file + ".ts";
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
  const result = {}, external = createRequire(filename);
  new Function("require","exports",code)(id => id.endsWith(".css") ? {default:{}} : id.startsWith(".") ? load(path.resolve(path.dirname(filename),id)) : external(id),result);
  return result;
}
const ThinkingGroup=load(path.resolve(__dirname,"../src/conversation/ThinkingGroup")).default;
const ExecutionTrace=load(path.resolve(__dirname,"../src/conversation/ExecutionTrace")).default;
const messages=[{id:"call",role:"tool_call",content:"",callId:"failed",toolName:"read_file",toolInput:{path:"missing"},timestamp:0},{id:"result",role:"tool_result",content:"File missing",callId:"failed",toolName:"read_file",isError:true,durationMs:2,timestamp:0}];

test("provider thinking is labeled and can be expanded independently from tool activity", () => {
  let renderer;
  act(()=>{renderer=create(React.createElement(ThinkingGroup,{messages:[{id:"thought",role:"thinking",content:"先检查目录，再读取说明。",timestamp:0}],autoExpanded:false}));});
  act(()=>renderer.root.findAllByType("button")[0].props.onClick());
  assert.ok(JSON.stringify(renderer.toJSON()).includes("模型思考"));
  assert.ok(JSON.stringify(renderer.toJSON()).includes("先检查目录，再读取说明。"));
  assert.equal(renderer.root.findAllByType("details").length,1);
  act(()=>renderer.unmount());
});

test("completed chat keeps an error summary and trace entry without expanding all error output", () => {
  let renderer, opened=0;
  act(()=>{renderer=create(React.createElement(ThinkingGroup,{messages,autoExpanded:false,onViewTrace:()=>opened++}));});
  assert.equal(renderer.root.findAllByType("details").length,0);
  assert.ok(JSON.stringify(renderer.toJSON()).includes("1 个失败"));
  act(()=>renderer.root.findAllByType("button").find(button=>button.children.includes("查看轨迹")).props.onClick());
  assert.equal(opened,1);
  act(()=>renderer.root.findAllByType("button")[0].props.onClick());
  assert.equal(renderer.root.findAllByType("details").length,1);
  act(()=>renderer.unmount());
});

test("trajectory inspector continues from the selected error and disables continuation while running", () => {
  let renderer, continued;
  const props={messages,running:false,verification:null,canContinue:true,onContinue:row=>{continued=row;}};
  act(()=>{renderer=create(React.createElement(ExecutionTrace,props));});
  const button=()=>renderer.root.findAllByType("button").find(item=>item.children.includes("继续处理"));
  act(()=>button().props.onClick());
  assert.equal(continued.callId,"failed");
  assert.equal(continued.output,"File missing");
  act(()=>renderer.update(React.createElement(ExecutionTrace,{...props,running:true,canContinue:false})));
  assert.equal(button().props.disabled,true);
  act(()=>renderer.unmount());
});
