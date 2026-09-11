export const prompt = `Implement deploy preflight for workspaces.

Add POST /api/workspaces/:id/deploy/preflight and check whether the workspace is ready for deployment: git state, scripts, secrets and required tooling.

Also improve deployment validation so that when the validation tool is missing we return validation_tool_missing instead of a generic deployment_failed error.

Add tests and make sure the worker test suite passes.`;
export const fingerprint = 'AssertionError | expected failed | received succeeded | line 589';
export type Step = {type:'agent'|'tools'|'failure'|'guard'|'root'|'success'|'comparison'; message:string; tools?:string[]; calls:number; tokens:number; progress:number; runs:number; failures:number; interventions:number};
const make = (type:Step['type'],message:string,calls:number,tokens:number,progress:number,runs:number,failures:number,tools?:string[],interventions=0):Step=>({type,message,calls,tokens,progress,runs,failures,tools,interventions});
const shared:Step[] = [
 make('agent',"I'll inspect the deployment runner and current validation flow.",0,400,8,0,0),
 make('tools','Inspecting & implementing',8,2600,32,0,0,['Reading workspace-deployment-runner.ts…','Reading deployment routes…','Searching for deployment_failed…','Editing validation logic…','Adding deploy/preflight endpoint…']),
 make('failure','FAIL #1',9,3400,36,1,1),
 make('agent',"The failure looks related to the validation result. I'll adjust the handling.",9,3800,36,1,1),
 make('tools','Revisiting validation',16,5600,36,1,1,['Reading validation code…','Editing workspace-deployment-runner.ts…','Running tests…']),
 make('failure','FAIL #2 · SAME FAILURE',17,6400,36,2,2),
];
export function scenario(guard:boolean):Step[]{
 if(guard) return [...shared,
 make('guard','Brak postępu',17,6400,36,2,2,undefined,1),
 make('agent','My previous patch did not change the observed behavior, so rerunning the same test again is unlikely to help.',17,7100,36,2,2,undefined,1),
 make('tools',`I'll verify where the "succeeded" status actually comes from and compare the source implementation with the compiled test artifact.`,24,9600,62,2,2,['Reading source implementation…','Reading .test-dist artifact…','Comparing runtime paths…'],1),
 make('root','The executed test path does not match the assumption made in the previous patch.',24,10300,78,2,2,undefined,1),
 make('tools','Applying a different strategy',28,12000,88,2,2,['Applying targeted fix…','Running tests…'],1),
 make('success','17/17 tests passed',29,12800,100,3,2,undefined,1),
 make('comparison','Run complete',29,12800,100,3,2,undefined,1)];
 return [...shared,
 make('tools','Let me inspect the test setup more closely.',24,8800,36,2,2,['Reading workspace-deployment-runner.test.js…','Searching validation mocks…','Editing test setup…','Running tests…']),
 make('failure','FAIL #3 · SAME FAILURE',25,9600,36,3,3),
 make('tools','There may be an issue with the generated test artifact.',32,12000,36,3,3,['Reading .test-dist/…','Comparing generated code…','Editing…','Running tests…']),
 make('failure','FAIL #4 · SAME FAILURE',33,12800,36,4,4),
 make('tools',"I'll try another adjustment to the validation handling.",39,14600,36,4,4,['Reading validation handler…','Adjusting status handling…','Running tests…']),
 make('failure','FAIL #5 · SAME FAILURE',40,15400,36,5,5),
 make('tools','I need to trace the actual status source and verify the executed code.',46,17600,82,5,5,['Comparing source with compiled artifact…','Tracing status source…','Applying fix…','Running tests…']),
 make('success','17/17 tests passed',47,18400,100,6,5),
 make('comparison','Run complete',47,18400,100,6,5)];
}
