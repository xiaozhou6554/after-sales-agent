// Compare application/test-harness observations with the frozen v2 expected contract.
// This evaluates supplied observations; it does not start the Agent or external systems.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { isDeepStrictEqual } = require('node:util');

const dataRoot = path.resolve(__dirname, '..');
const projectRoot = path.resolve(dataRoot, '..');
const resultPath = process.argv[2] ? path.resolve(process.cwd(), process.argv[2]) : null;
if (!resultPath || !fs.existsSync(resultPath)) {
  console.error('用法: node data/scripts/evaluate_scenario_run.cjs <实际执行结果.jsonl>');
  process.exit(2);
}
const scenarios = fs.readFileSync(path.join(dataRoot,'scenarios/v2/multi_system.jsonl'),'utf8').trim().split(/\r?\n/).map(JSON.parse);
const byId = new Map(scenarios.map((scenario)=>[scenario.case_id,scenario]));
const lines = fs.readFileSync(resultPath,'utf8').split(/\r?\n/).filter((line)=>line.trim());
const rows = lines.map((line,index)=>{ try { return JSON.parse(line); } catch { throw new Error(`第${index+1}行不是合法JSON`); } });
const seen = new Set();
const failures = [];
let executed = 0;
let notExecuted = 0;
const getPath = (object,dotted) => dotted.split('.').reduce((value,key)=>value == null ? undefined : value[key],object);
const satisfies = (actual,operator,expected) => {
  switch(operator) {
    case 'equals': return Object.is(actual,expected);
    case 'not_equals': return !Object.is(actual,expected);
    case 'in': return Array.isArray(expected)&&expected.includes(actual);
    case 'exists': return actual !== undefined && actual !== null;
    case 'not_exists': return actual === undefined || actual === null;
    case 'gte': return typeof actual==='number'&&actual>=expected;
    case 'lte': return typeof actual==='number'&&actual<=expected;
    default: return false;
  }
};
for (const row of rows) {
  const scenario = byId.get(row.case_id);
  if (!scenario) { failures.push({case_id:row.case_id,error:'unknown_case_id'}); continue; }
  if (seen.has(row.case_id)) { failures.push({case_id:row.case_id,error:'duplicate_result'}); continue; }
  seen.add(row.case_id);
  if (row.dataset_version !== scenario.dataset_version) { failures.push({case_id:row.case_id,error:'dataset_version_mismatch'}); continue; }
  if (!['executed','not_executed'].includes(row.status)) { failures.push({case_id:row.case_id,error:'invalid_execution_status'}); continue; }
  if (row.status === 'not_executed') { notExecuted += 1; continue; }
  executed += 1;
  if (row.expected_schema_version !== scenario.schema_version) failures.push({case_id:row.case_id,error:'expected_schema_version_mismatch'});
  const observed = row.observed || {};
  for (const fact of scenario.expected.facts) {
    const actual = getPath(observed.facts || {},fact.path);
    if (!satisfies(actual,fact.operator,fact.value)) failures.push({case_id:row.case_id,error:'fact_mismatch',path:fact.path,expected:{operator:fact.operator,value:fact.value},actual:actual===undefined?'__missing__':actual});
  }
  const calls = Array.isArray(observed.tool_calls)?observed.tool_calls:[];
  const names = calls.map((call)=>typeof call==='string'?call:call.tool_name);
  const matchesExpected = (actual,expected) => {
    if ((typeof actual==='string'?actual:actual?.tool_name)!==expected.tool_name) return false;
    if (typeof actual!=='string'&&expected.identity_ref&&actual?.identity_ref!==expected.identity_ref) return false;
    if (expected.arguments) {
      if (typeof actual==='string'||!actual.arguments) return false;
      return isDeepStrictEqual(actual.arguments,expected.arguments);
    }
    return true;
  };
  for (const expectedCall of scenario.expected.tool_calls.must) if (!calls.some((actual)=>matchesExpected(actual,expectedCall))) failures.push({case_id:row.case_id,error:'required_tool_or_arguments_missing',tool_name:expectedCall.tool_name});
  for (const forbiddenCall of scenario.expected.tool_calls.must_not) if (calls.some((actual)=>matchesExpected(actual,forbiddenCall))) failures.push({case_id:row.case_id,error:'forbidden_tool_called',tool_name:forbiddenCall.tool_name});
  const allowedCalls=[...scenario.expected.tool_calls.must,...scenario.expected.tool_calls.may];
  for (const actual of calls) if (!allowedCalls.some((expectedCall)=>matchesExpected(actual,expectedCall))) failures.push({case_id:row.case_id,error:'unexpected_tool_call',tool_name:typeof actual==='string'?actual:actual?.tool_name??'__missing__'});
  const workflowSteps = Array.isArray(observed.workflow_steps)?observed.workflow_steps:[];
  for (const expectedStep of scenario.expected.workflow_steps.must) {
    if (!workflowSteps.some((actual)=>actual.operation_id===expectedStep.operation_id&&actual.execution_layer===expectedStep.execution_layer&&actual.identity_ref===expectedStep.identity_ref)) failures.push({case_id:row.case_id,error:'required_workflow_step_or_identity_missing',operation_id:expectedStep.operation_id,identity_ref:expectedStep.identity_ref});
  }
  const allowedWorkflowSteps=[...scenario.expected.workflow_steps.must,...scenario.expected.workflow_steps.may];
  for (const actual of workflowSteps) if (!allowedWorkflowSteps.some((expectedStep)=>expectedStep.operation_id===actual.operation_id&&expectedStep.execution_layer===actual.execution_layer&&expectedStep.identity_ref===actual.identity_ref)) failures.push({case_id:row.case_id,error:'unexpected_workflow_step_or_identity',operation_id:actual.operation_id,identity_ref:actual.identity_ref});
  for (const expectedEffect of scenario.expected.effects) {
    const actualEffect = (observed.effects||[]).find((effect)=>effect.system===expectedEffect.system&&effect.action===expectedEffect.action);
    if (!actualEffect || actualEffect.result!==expectedEffect.expected) failures.push({case_id:row.case_id,error:'effect_mismatch',system:expectedEffect.system,action:expectedEffect.action,expected:expectedEffect.expected,actual:actualEffect?.result??'__missing__'});
  }
  const evidenceRefs = new Set(observed.evidence_refs||[]);
  for (const ref of scenario.expected.evidence_refs) if (!evidenceRefs.has(ref)) failures.push({case_id:row.case_id,error:'required_evidence_missing',evidence_ref:ref});
  for (const violation of observed.forbidden_violations||[]) failures.push({case_id:row.case_id,error:'forbidden_behavior',violation});
  for (const [key,value] of Object.entries(scenario.expected.state)) if (!Object.hasOwn(observed.state||{},key)||!Object.is(observed.state[key],value)) failures.push({case_id:row.case_id,error:'state_mismatch',path:key,expected:value,actual:Object.hasOwn(observed.state||{},key)?observed.state[key]:'__missing__'});
}
const missing = scenarios.filter((scenario)=>!seen.has(scenario.case_id)).map((scenario)=>scenario.case_id);
const result = failures.length ? 'failed' : (notExecuted||missing.length ? 'incomplete' : 'passed');
const report = { result, scope:'supplied_application_observations_only', dataset_version:scenarios[0]?.dataset_version, input_file:path.relative(projectRoot,resultPath).split(path.sep).join('/'), counts:{ total:scenarios.length, supplied:rows.length, executed, not_executed:notExecuted, missing:missing.length, failures:failures.length }, missing_case_ids:missing, failures };
const outputPath = path.join(dataRoot,'generated/reports/mock-v2-evaluation.json');
fs.mkdirSync(path.dirname(outputPath),{recursive:true});
fs.writeFileSync(outputPath,JSON.stringify(report,null,2)+'\n','utf8');
console.log(JSON.stringify(report));
if (result !== 'passed') process.exitCode=1;
