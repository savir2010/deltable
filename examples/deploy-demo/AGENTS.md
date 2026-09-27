# deploy-demo: instructions for every agent

Facts about this repo live in Delta Memory, one live server that every agent workspace
shares. Use the `tools/dm` command (or the `delta-memory` MCP server if your harness
loads `.mcp.json`; they are the same server).

- Before using the deploy script path: `tools/dm read deploy_script path`. Use the `value`
  it returns and note its `version`. Do not trust what config.yaml said when you started.
- When you change the deploy script path, read first, then write with the version you read:
  `tools/dm write deploy_script path <new path> --value-type path --expected-version <version> --source tool_call:<your agent name>`
- If the write returns `"status": "conflict"`, another agent changed it first. Report
  that in your final answer, and do not retry blindly.
