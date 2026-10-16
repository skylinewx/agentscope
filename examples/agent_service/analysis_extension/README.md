# Analysis extension v1

Single-host extension for the existing AgentScope example service. It captures registered source adapters' ToolResponse objects before outer offload/context handling, then exposes AnalysisBuild, AnalysisInspect and AnalysisPublish through one middleware. Budget MCP v2 is a first-class built-in adapter. Exact values are decimal strings. Scenario choices belong to prompts: findingPlan, actionPlan and viewPlan explicitly select checks, advice and presentation. The host does not infer a negative-value investigation or fixed business workflow. No model, browser, new server or budget write operation is introduced by the extension.

## Integration

`main.longterm_memory_factory` preserves AgenticMemoryMiddleware and appends `create_analysis_middlewares(...)`. AgentScope provides capabilities independently of Skill names: `capabilities.analysis` enables analysis follow-up, `capabilities.reports` enables AnalysisRenderReport, and either enables shared capture/read/build/publish tools and cards. Both default to true. `activation: disabled` stops all extension capabilities. Skills guide when and how to use tools; no selection, renaming, or removal of a Skill changes the capability policy. Ordinary queries still do not imply a request for analysis or reports.

The factory verifies selected Skill package hashes and records versions by resource ID. Existing ownership, session overrides and version pins remain enforced. MCP capture requires actual leased, enabled, user-owned tool bindings. Budget v2 recognition retains its schema and exact contract metadata checks; its request, pagination, continuation and evidence semantics live in the budget adapter. Other MCP tools continue to load and execute normally. To include their outputs in analysis, register a source adapter; this does not make arbitrary output automatically analyzable. Source-specific `numberFormats` and business rules remain explicit.

Supported config fields are `schemaVersion`, `activation`, `capabilities`, `sources`, `numberFormats`, `rules`, `findingRules` and `limits`. HTTP delivery and runtime tools share host policy and selected-package integrity checks. `sources` binds actual resource IDs and original tool names to adapters, rather than granting access to a resource. The adapter ID, version and options are required and frozen with each capture; changing current config never reinterprets a saved response. Inputs lacking this registration must be queried again; neither the response body nor the request contract is used to guess an adapter. Built-in budget recognition for live tools takes precedence over generic bindings.

For an existing JSON row tool, the built-in `json-records` adapter can be configured without changing that MCP:

```json
{"resourceId":"<actual resource ID>","toolName":"list_sales","adapter":"json-records","options":{"rowsPointer":"/records","idField":"id","labelField":"name","values":{"SALES":"amount"},"unitField":"unit","completePointer":"/complete","memberDimension":"recordId"}}
```

Place this binding in `sources`. `values` maps metric names to actual row fields; selectors always point to captured locations, never replacement numbers. Optional `periodField`, `dimensions` and `contextPointer` map actual metadata. Row IDs must be unique strings. Only a true `completePointer` value proves enumeration complete. Missing units stay empty and block calculations requiring units. No evidence verification or query continuation is invented for a generic JSON source. Implement a `SourceAdapter` for richer source-specific semantics and register it in `adapters.default_sources`.

Use `numberFormats: {"<registered resource id>": "grouped-dot"}` only for a source whose raw numeric strings were confirmed to use grouped thousands and a decimal point. It is a source decoding rule, not a money unit or scale assumption. Do not invent units or register conversion rules merely to make calculations pass. RECONCILE can compare identically labelled source values, including all-empty unit/scale labels, under an explicit caller-selected sum expectation; it does not invent units or register a source business rule.

Private files are under `examples/agent_service/analysis-data/`, outside model workspaces. Local config and data are gitignored. The SQLite registry commits only after an atomic, hashed content file write. Published versions cannot be overwritten. Revisions use requestKey and expectedBaseRevision. Each host operation rechecks agent/session access and owner identity. Existing budget permissionStatus is retained; this does not add source business authorization or an OS sandbox against arbitrary host code execution.

## Contract and limits

`artifact-v1.schema.json` freezes storage v1; `artifact-v1.example.json` is synthetic. Unknown noncritical fields may be added; unknown schema versions/operations never pass automatically. The current contract requires `findings`/`actions` (empty arrays when no plans select them), explicit presentation policy, finding version and source origins and independently stored action runs; see [ACTIONS_V1.md](ACTIONS_V1.md). Batch 2 adds registered chat cards and self-contained HTML through `AnalysisRenderReport`. See [PRESENTATION_V1.md](PRESENTATION_V1.md) for the frozen public projection and routing contract. The bounded renderer executes the explicit viewPlan; complete paginated details remain available without unrequested metrics or charts.

Defaults: 16 MiB per serialized capture/artifact, 512 MiB total content, seven-day retention for unpinned captures/orphans, 50,000 facts, 1,000 selections, 100 operators, 100 items per read page. Historical maximum tool output measured 747,790 UTF-8 bytes; a separate test exercises a response above 1 MiB. Limits apply jointly. Quota failures never save truncated data. Cleanup runs when a new enabled runtime is assembled; pinned analysis sources and versions are retained. Back up SQLite and blobs together with the service stopped or a consistent SQLite backup procedure.

Resume-only requests are linked to captured parent executions using original resource, connection, tool, context, query session, resumeRef and server continuationOf. Include the whole captured chain in AnalysisBuild. Missing roots, gaps, unknown totals or unproven population enumeration do not become COMPLETE. Within one source/query context, conflicting observations remain conflicts. Across queries, facts retain distinct sourceContextId values even if execution, observation or evidence IDs repeat; verification and formula dependencies cannot cross those boundaries.

The default `scope.contextMode=SINGLE_SNAPSHOT` accepts reopened budget contexts and multiple query sessions on the same real connection and data boundary (`sourceId`, `snapshotId`, `asOf`, `schemaHash` and every other context field except `contextId`). Query identities, continuation cursors, execution/observation IDs and evidence remain bound to their original full context and session. Explicit `COMPOSITE` allows report composition across contexts or adapters. Facts retain their own contexts. Table columns group the same business metric across members by data boundary, period, binding, non-member dimensions, source, unit and scale; different query identities alone do not create duplicate columns. Repeated observations of the same member and metric remain separate facts and cannot silently overwrite one table cell: choose the intended observation or present explicitly labelled separate views. Distinct data boundaries remain distinct columns. Compilation into one report does not assert comparability. Inventory/capture discovery with `memberDimension` exposes `memberRef` and the original `sourceObjectId`. Use member refs when IDs collide across sources. The host normalizes cross-source analysis `objectId` and scope members to these references for tables, counts, filters, missing values and relations; original source dimensions remain unchanged. Use returned analysis IDs for selectors, and original IDs for MCP requests. A population enumeration proves only its own source’s subject members, never the complete population of other sources. Select actual fact references or `select.sourceContextId`/`period` for computations, and use meaningful topic/column labels in reports.

Business comparisons default to one period/binding/data boundary/origin and matching non-member dimensions. A configuration-maintained rule with `source`, `version` and `operators` can define `comparability`: explicit `periods`, `metrics` (bindingRef/measureCode/cellKey), `varyingDimensions`, `contexts` and `sourceOrigins`. An operation must opt in via `comparabilityRef`; each supplied bound applies to all inputs. Cross-boundary exceptions require the corresponding lists, while unit/scale and completeness checks still apply. SUM/LINEAR/CONVERT additionally require their existing arithmetic/business rules. Rule provenance is recorded on derived values. Ranked charts and filtered aggregate metrics reuse the original operation’s comparability rule from the saved rule registry. ARITHMETIC remains bounded pure arithmetic and does not grant business comparability or causation.

## Validation and activation

From the AgentScope repository:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'analysis_*_test.py' -v
```

Select source, analysis and report Skills for the desired guidance on an agent with the intended MCP resources. The host config controls available tools independently. `AnalysisInspect(section="protocol")` exposes deliveryCapabilities, sourceAdapters, contextModes and registered rules. Config and bindings are checked for each new runtime and HTTP request; an in-flight reply retains its runtime settings. Web UI delivery and the current local filesystem backend retain their existing behavior.

The report Skill owns presentation guidance and the existing visual reference; the analysis Skill owns calculations, interpretation and analysis review. Both reuse the existing structured result protocol and renderer. Skill separation does not modify the approved report HTML, CSS, chart components or schema version, and report requests do not imply permission to start analysis.

Only the current analysis, finding and presentation formats are accepted. There is no migration, missing-field repair, renderer upgrade-on-read or historical read-only path. Start a new analysis for acceptance. Current-protocol linked revisions, idempotent requests, restart recovery and immutable source evidence remain supported. Private storage must remain outside the model workspace.

Frontend checks, from `examples/web_ui/frontend`: `npm run test:analysis`, `npm run test:reports`, `npm run build`; lint the changed TypeScript/TSX files. No Java or Docker change is required by Batch 2.

The October 10 acceptance uses real read-only budget MCP calls and the actual SDK acting/middleware chain, with a local model stub that cannot call a network API. The user explicitly excluded external model calls. Skill package mounting and protocol checks passed; model-driven skill selection/wording remains untested and must not be reported as passed.

## Synthesis capabilities (engine 1.5.0)

AnalysisInspect(inventory) discovers all current-owner/session captures without new queries. It returns source/context/task/period, exact values, actual formula metadata, names and observation identity, with exact filters and pagination. Names are joined only within the same source and context; conflicting observations remain separate. Enumeration is bounded and fails explicitly at the configured capture/cell limits.

Every derived result separates executionStatus from numeric agreement. Formula nodes expose localCheck over captured direct inputs, recursive recalculation, propagatedDifference and differenceOriginRefs. Captured cross-table input values are included while their external formulas remain unexpanded; this is not missing expense. Blocked dependencies remain blocked.

RECONCILE explicitly requires aggregation=SUM, a captured direct relation, parent-first references and all direct children of the same metric/context. It returns parent minus the exact child sum. Confirmed leaves return NOT_APPLICABLE with no child sum or difference. The Skill currently selects parent-equals-children as the user's budget default; no backend scenario default or invented registered rule is added.

The renderer consumes model-selected labels, numberFormat/format and hierarchy comparisonRef. Number formatting never changes stored values or calculations. report.py, report.css and the approved reference HTML remain unchanged. No ontology service changes or historical migration are needed.
