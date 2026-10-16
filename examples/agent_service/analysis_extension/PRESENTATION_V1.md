# Registered presentation protocol v1

Current contract: storage schema 1, engine 1.5.0, public schema 1 and renderer 1.11.1. Other artifact schemas and computation engines are rejected. Renderer versions identify derived layouts, not the validity of published data. There are no legacy readers or conversions. Export layout is required and must be standard or table.

All artifacts require `presentationPolicy=explicit`. `viewPlan` selects METRIC fact/scalar-derived references, TABLE groups, BAR_VALUES source facts, BAR_RANK references to successful RANK operations, BRIDGE references to successful LINEAR operations, and ORDER for initial display order. Empty viewPlan produces no tables, metrics or charts; frozen source facts remain intact. Public `chartsRequested`, `findings`, `actions` and `findingsScopeLabel` are required. An empty viewPlan sets chartsRequested=false, so a deliberately absent chart is not presented as an error.

METRIC may provide a title and field. COUNT fields are value (the default observed numeric count), positive, negative, zero, observed and unavailable; other scalars accept value only. Each requested reference produces one card in plan/reference order. Several views may select different fields/titles from the same COUNT. Nothing automatically expands a COUNT into category cards. Filtered projections recompute only previously approved COUNT/SUM operations whose inputs are direct selected facts, retaining the explicit field and title; other metrics require their full original input range. At most 200 explicitly requested metric cards are allowed.

At most one ORDER selects either one passed SORT/RANK result or an explicit sequence of fact references. Remaining explicitly displayed facts retain source order after those references, and selected facts of the same member stay together. ORDER does not expand the displayed scope. Without ORDER, computing one or more SORT/RANK operations does not reorder the presentation. Each table or chart follows its explicit orderRef or valueRefs order; table-header sorting affects only that table until reset. Multiple comparison charts are supported; each retains independent metric, range, aliases and ordering.

## Link and trust boundary

Complete standalone Markdown links to `./analysis/ana_<32 lowercase hex>/revisions/<positive integer>/analysis.json` become cards; complete inline links open the registered view on demand. The revision has at most nine digits, with no leading zero. Case changes, percent encoding within the path, backslashes, dot segments, query strings, fragments, absolute paths, and external URLs are rejected. Ordinary URL transport encoding of the whole query parameter is decoded once by HTTP. Streamdown repairs to unfinished Markdown cannot promote a card; the original source must contain the complete reference. Code and images remain ordinary content. Duplicated references in one message produce one card.

The manifest is a public projection, never authority. It contains `schemaVersion`, `analysisRef`, `dataDigest`, `rendererVersion`, and `snapshotDigest`; the frozen default document and rule set live in the private owner registry. Every read compares the registered artifact and file bytes with stored digests. Workspace symlinks and Windows junctions are rejected. Explicit restore rewrites only a registered manifest. Readability requires the current user, agent and session tuple to own the analysis and retain resource access. The existing service's X-User-ID identity boundary is inherited; this extension does not supply authentication or source business authorization.

## ViewSnapshot

All response fields below are projected by the host. The frontend validates the newly projected current fields and version; it never receives the frozen original layout as a current snapshot. `presentation-v1.example.json` is a synthetic executable sample.

| Field | Type and meaning |
|---|---|
| schemaVersion, rendererVersion, engineVersion | 1, supported layout version string, source computation version |
| analysisRef, dataDigest, viewId, scopeId | Immutable analysis identity, SHA-256, opaque display identity and computation scope |
| viewParameters | `{filter: all/positive/negative/zero/unavailable, sort: published/asc/desc, topN: null or integer 1..1000, columnKey: null or opaque column key}`; no supplied amounts |
| scopeLabel, isFiltered, total, originalTotal | Actual selected scope and fact counts; member counts and chart slice are labelled separately |
| publishedAt, verificationRecordedAt | Recorded timestamps or null; never a claim of current source validity |
| statuses | publicationState, receiptVerification, coverage, comparability, calculationValidation; retained separately |
| rows | Ordered `{rowKey,memberKey,valueRef,label,measure,exact,display,unit,state}`; exact is Decimal string or null, display is host-formatted text |
| metrics | `{valueRef,label,display,unit,scopeId}`; every scopeId matches this view |
| viewDerivedValues | `{valueRef,scopeId,operator,display,validation}` for bounded filter computations |
| scenes | Ordered BAR_VALUES, BAR_RANK, BRIDGE, FORMULA, HIERARCHY, WATERFALL or RECONCILIATION scenes, each with viewId |
| chartUnavailable | Human explanation or null; no missing value silently becomes zero |
| claims, claimsScopeLabel | Frozen original claims, explicitly labelled original scope after filtering |
| sources, limitations | Allow-listed business labels and restrictions; no receipts, credentials, evidence references or IRIs |
| page | `{offset,limit,total,nextOffset}` in member units; statistics cover the complete selected scope, not one page |
| capabilities, reports | Optional live UI permissions and at most 100 registered export metadata records; not part of the immutable snapshot |

Tables use source metric names as columns and member names as rows, with explicit TABLE groups and formula/scalar detail groups described below. Default table page and chart slice are 12 members; page size is at most 100. Initial charts describe their current-order first slice explicitly. Negative filters, ordering and Top N call the same Decimal engine over the frozen facts/rules. Source-value ordering is allowed under its original conditions; a BAR_RANK additionally requires successful business comparability. Filtered SUM runs only when the original whole-scope SUM was approved, using the same frozen rule. BRIDGE requires a passed LINEAR rule, at most 12 terms, and exact equality of composed total to the published value; it does not assert causation. Tables and charts share row identities; their declared ordering is independent. Zero and unavailable values remain distinct.

## ChartScene

`{type,width,height,zeroBaseline,description,rowKeys,primitives}`. Optional title and primary metadata identify the chart heading and initial disclosure. Type is BAR_VALUES, BAR_RANK, BRIDGE, FORMULA, HIERARCHY, WATERFALL or RECONCILIATION. Only `text(x,y,text,tone)`, `line(x1,y1,x2,y2,tone)` and `rect(x,y,width,height,tone,rowKey)` are supported; tones are positive, negative, axis, label, value, surface. Coordinates are finite and in 0..10000; the width is 600, the first chart slice has at most 12 rows, and primitives are bounded to 1000. Decimal ratios are converted to floats only for coordinates. Bars retain true proportion, including extremely small negatives; numeric labels remain visible. Full labels wrap in 23-character lines without assuming a details table exists; capacity is checked before rendering. Exact amount labels wrap in 18-character segments; geometry height follows the wrapped value. React and HTML render these same primitives as escaped SVG text; no source markup is executed. Narrow screens scroll inside graph/table regions and keep the page width bounded. Print styles retain table headers and scale graphs within a page.

## Routes and delivery

All paths use `/workspace/analyses` and agent_id/session_id query parameters. `GET /view` takes the canonical manifest path. `POST /project-view` takes analysisRef, viewParameters and optional offset/limit. `POST /restore` takes only the manifest path. `POST /render-report` takes analysisRef, requestKey, optional viewParameters and standard/table layout. `POST /report-token` mints a short-lived token bound to agent, session, analysis and export. `GET /report` accepts analysis_ref/export_ref and current user identity or that token; permission is checked again at download time. `download=true` returns a named HTML attachment. Preview uses the existing sanitized sandbox iframe without scripts or network resources.

Report metadata is `{analysisRef,exportRef,path,contentDigest,scopeLabel,isFiltered,viewParameters,rendererVersion,layout,sizeBytes,registered,markdown}`. `layout` is required. Export identity hashes the analysis, parameters, renderer and layout. A requestKey retry returns its registered current-format export; changing any request field, including rendererVersion, fails. Content is fully written atomically, registered, and read back with digest checks before success. An interrupted write never returns a usable registration. All HTTP report reads and downloads require current Skill activation and ownership checks.

The card report buttons choose the original published scope. Explicit API exports with viewParameters have distinct exportRef and scopeLabel. HTML contains the full selected table, not only the chat page. Card reads project the current artifact and its frozen rules through the current renderer. The registered manifest, frozen original snapshot and exported HTML remain unchanged. Self-contained HTML is read by registration and digest, independent of its original renderer version. No legacy snapshot parser or artifact migration is used.

## Bounds and failures

JSON request 64 KiB; manifest 64 KiB; one ViewSnapshot response 1 MiB; standalone HTML 10 MiB. Private artifacts retain Batch 1's 16 MiB/object and overall storage quota. Reads and writes enforce limits; they never truncate JSON or quietly reduce a complete report. Oversized reports fail with an explicit suggestion to choose a smaller named scope. Error responses carry a stable code while the card shows business language and a relevant retry/restore action. Session unmount cancels requests. Query keys include server, user, agent, session, path, analysisRef and view parameters. Independent workspace HTML file previews remain a separate current capability.

## Current grouped table contract (renderer 1.11.1)

TABLE plans now select independent groups with optional business titles. Source tables pivot members into rows and source metrics into columns. Formula tables display captured values, exact recalculation and differences; scalar calculation tables retain their original input scope. No acquired-status column is rendered. Approved report CSS and outer section layout are unchanged.

View parameters include nullable `columnKey` (opaque `column_` plus 24 hex digits). A mixed-metric filter/sort must identify an anchor column, or use the single metric selected by BAR_VALUES. Selection, Top N and pagination operate on members, retaining their explicitly displayed facts. `total`/`originalTotal` count facts; `objectCount`/`originalObjectCount` count members; `page.total` counts selected members. Required fields are `tables`, `columns`, `activeColumnKey`, `memberKeys`, and both member counts. Rows include `memberKey`. Tables contain tableKey/title/kind/columns/rows; each table row contains memberKey/label/cells and an optional note. Only values tables offer source-column filtering/sorting. A full report retains all published members; filtered reports do not present a cross-member derived result as a new subset total.

The current engine supports bounded FORMULA_CHECK and paginated formulas inspection. Only source-proven coordinates are used; missing dependencies, ambiguous formula metadata, external root formulas and unsupported operations remain explicit gaps. Nested cross-table inputs retain their captured in-table values and are marked as externalBoundaries, without claiming to have verified an external formula. PASS is arithmetic eligibility, while formulaTrace.status separately identifies MATCH/MISMATCH/BLOCKED. Fact/computed claims accept model-authored text interleaved with required valueRef parts, preserving literal part order. References bind only the selected scalar or source unit; the model supplies business names, unit placement and emphasis. Fact claims cannot cite derived values; computed claims must include a passed derived reference. Hypothesis claims require specific missingEvidence. REFERENCES_VALID certifies reference eligibility, not the semantics or causality of model-authored prose. Each rendered claim is checked against the public 16,000 UTF-16 code-unit bound. Old contracts are rejected; no migration or compatibility reader is provided. The approved HTML/CSS template is unchanged.


## Relationship charts and business display

FORMULA references FORMULA_CHECK derived IDs, including blocked traces with explicit missing inputs. It renders source metric labels, operations, dependency links, source values and exact recalculation differences. Captured Grid2 coordinates follow the native budget formula contract when display indexes are absent; explicit display indexes take precedence. Within-table tracing stops at captured external-input values; boundary metadata remains internal unless technicalDetails is requested. It does not interpret arithmetic agreement as a business cause.

HIERARCHY requires a frozen relationRef and fact references: parent first, then directly related children. Relations come from actual DIRECT, depth-1 traversal receipts with a resolved root identity. Origins, context, period, metric, unit and dimensions must agree. Partial coverage is labelled. Edges express membership. An explicit matching RECONCILE selected through comparisonRef adds the child sum and parent-minus-child difference; the service never silently assumes an aggregation expectation. Other business operators retain their normal rule/comparability requirements. An analysis may combine multiple independent comparison charts with formula, hierarchy and composition scenes. Within each selected trace, nodeRefs controls the display order in formula graphs, waterfalls and details; omitting it retains trace order. Arithmetic operand order never changes. Each fan-out group contains at most 12 inputs; continuations are labelled, with at most 100 relationship scenes.

Cards default to graph mode and honor model-selected groups and order. Explicit TABLE views appear alongside the related cards and charts; data-details mode hides charts while retaining tables. Numeric table headers cycle a local exact sort order, and each table paginates independently. Model-selected collapsed controls expansion; no fixed selection rule chooses a primary diagram. Source verification remains internal by default; specific missing inputs and mismatches remain visible. Build returns verificationTasks so the model can obtain actual verification receipts before rebuilding. This is actionable guidance, not an automatic verification claim or publication gate.

Relationship graphics fill the approved HTML template's existing chart region. The template CSS, colors, chapter order and outer markup remain unchanged.

## Model-authored presentation

Required artifact/snapshot boolean `technicalDetails` defaults to false at Build and is selected explicitly at Publish. It participates in publication idempotency and becomes immutable after publication. Internal finding support, evidence metadata, source lists and validation prose are rendered only for an explicitly requested technical report. Actual missing values and formula differences remain visible in the corresponding data/graphs. This is a presentation preference, not a validation bypass.

View options are `showUnit` (omitted=false), `description`, `collapsed` (omitted=false), `pageSize` (1..100, omitted=12), `includeDetails` for FORMULA/HIERARCHY only (omitted=false), and `nodeRefs` for FORMULA/WATERFALL (actual trace nodes[].factRef). Only TABLE or includeDetails=true creates tables. Units originate from frozen facts; the model chooses whether to attach them or place them in authored titles/claims. No empty unit placeholder or auto unit column is produced. Model titles and captions are preserved. Formula details use real business names by default, with raw expressions available only in technical mode.

Tables require collapsed/pageSize in snapshots. Card tables have pagination/disclosure; exported HTML preserves all selected rows within the approved detail component, with native disclosure for remaining rows. Claims render escaped model-authored **bold** text. Publish and Inspect(summary) return a structured summary, not a prewritten Markdown answer. Registered delivery.markdown remains unchanged.

The example service also always assembles NumericToolsMiddleware, independently of data-analysis activation. Calculate performs bounded exact rational arithmetic over decimal inputs; finite output is exact unless explicit decimalPlaces requests HALF_UP rounding. Nonterminating output requires explicit precision. SortRecords performs stable typed multi-key sorting over all supplied records before pagination, preserving records and distinguishing null from zero. Neither tool establishes business aggregation validity or creates an analysis artifact.

## Synthesis presentation controls

Claim parts accept format and scalar fields sourceValue/localValue/localDifference/difference for FORMULA_CHECK and sourceValue/childSum/difference for RECONCILE. These are exact bound values. View numberFormat accepts grouping and decimalPlaces (0..16); HALF_UP affects display only. No scale or unit conversion is implied. labels binds a business alias to an actual selected value or its source fact; inconsistent aliases for a shared table column are rejected.

Formula graphs show captured node values, local agreement and separately expanded results/differences. Difference-origin labels distinguish an actual local mismatch from propagation. Neutral explanatory notes do not receive error coloring merely because a cross-table boundary exists. Hierarchy comparisonRef must match the same relation and exact ordered valueRefs and have completed successfully.

These capabilities reuse the approved graph, table and report components. Diagram focus, wording, emphasis, units, disclosure and numeric formatting remain model choices. The HTML template structure, stylesheet and section order are unchanged.


## Publication repair and independent table groups (renderer 1.11.1)

AnalysisPublish validates independent claims/views before returning located issues. Errors identify JSON Pointer paths, claim/view IDs and applicable real references. Invalid optional fields are checked on a disposable diagnostic copy; accepted output is never silently coerced. A failed draft submission is retained privately by owner and analysis identity as repairRef. Retry with analysisRef, expectedBaseRevision, a new requestKey and repairRef/repairs. Ordered add/replace/remove operations can modify only publication content. All untouched charts, aliases, captions and claims remain intact. Facts, analysis identity and base revision cannot be patched. Published revisions remain immutable; exact successful retries retain idempotency.

Shared aliases are authored by the model. A measure alias is reused only for matching source/metric/context signatures, and object aliases only for the same source object. Explicit conflicting aliases are rejected with both paths; missing repeated declarations use the model's existing alias. The source labels are never edited. Local view aliases override shared names.

Each table now carries viewId. Every section block has metricIndexes, sceneIndexes and tableIndexes, each referring to exactly one corresponding view's payload. TABLE participates in model groups and plan order; it is not rendered again in an unrelated detail section. Existing report table and card components implement this grouping without changing the outer HTML template or CSS.

Explicit tables retain their complete selected row sets independently of the raw member page. The 1 MiB response limit is checked before publication: an oversized result is rejected, never silently truncated. Table controls sort exact decimal strings before local pagination and do not modify other tables, chart data, claims or the published report. Per-view orderRef sequences may differ across tables. API filters/Top N remain explicit operations on the selected whole scope; the first selected ordering supplies their default order, without imposing cross-table ordering constraints.

workRefs updates actual refs for Build's existing workPlan task IDs while preserving the original plan and computations. Completion is recomputed from real fact/derived/relation references. A declared pending/blocked task prevents a claim of complete publication; a partial result may be published with a model-authored partialReason explaining the remaining work. This text appears in the existing summary notice, while completion remains INCOMPLETE. READY proves usable execution references, not source agreement, business causation or full satisfaction of a user request.

## Composition

`presentation.reportTitle`, `summaryTitle` and `summaryPosition=start/end` organize the report. Groups choose `width=full/half`, `claimRefs` and `claimsPosition=before/after`; unassigned claims stay in the summary and every claim appears once. Sections address metrics/scenes/tables by indexes. These are the approved composition slots; existing HTML/CSS, colors and visual reference remain unchanged by this fix.



## Scope and read continuity

`scope.members` identifies the subject population; optional `referenceMembers` identifies disjoint comparison objects. Population proof still enumerates exactly the subject members. Default operation selection excludes reference members; explicit inputRefs or select.objectId can include them. wholePopulation must cover exactly the subject set.

An explicit baseAnalysisRef accepts a draft or published version from the same owner/session. Rebuilds retain declared workPlan tasks and same-subject population references; empty workPlan does not delete tasks. New computed references are revalidated and publication still checks the actual published base revision. Filling workRefs alone does not require a rebuild.

QueryResultRead may reduce an oversized array page while keeping each item intact. nextOffset refers to the consumed matching items, itemPointers refer to their original locations. A single oversized object returns bounded availableFields and readOptions with exact pointers; limit does not reduce its nested fields. Source receipts remain immutable.

Formula diagrams, detail rows and waterfalls share one label map: model aliases first, otherwise the original headers with common trace context omitted. No domain-specific shortening or arithmetic rewriting is performed.

## Renderer 1.11.1: preview and component choices

AnalysisPublish accepts previewOnly=true. The same schema/content/completion/projection checks run before saving a private publication proposal; no published revision, card or report is registered. repairRef+repairs=[] promotes the checked proposal; local repairs preserve all unaffected views and aliases. JSON-schema failures also return aggregated JSON Pointer issues when the immutable draft envelope is valid. Permission checks still precede execution.

previewPage points to the authenticated frontend preview page (analysis and report tabs); previewUrl is the header-authenticated API. Reads enforce current user/session/Skill authorization and current draft state. JSON returns a paged ViewSnapshot, HTML uses the approved renderer. diagnostics is advisory and explicitly reports visualInspectionPerformed=false; projection is not browser inspection.

FORMULA/HIERARCHY accept relationLayout (compact/flow) and emphasisRefs. FORMULA/HIERARCHY/WATERFALL accept showChecks. When omitted, a nonzero numeric difference or technicalDetails=true shows the comparison; explicit false remains authoritative and receives an advisory when it hides a formula difference. This compares source and calculated values, without enabling technical provenance or inventing a business component. BAR_VALUES/BAR_RANK/WATERFALL/RECONCILIATION accept orientation (horizontal/vertical). Linear formulas annotate exact operator/coefficient/contribution; only relative visual geometry is rounded. Nonlinear formulas do not invent additive contributions. Vertical waterfalls retain a linear zero-based scale and step connectors. Wide SVGs scroll without shrinking labels. React and report relationship components share these semantics; report.py and the approved reference HTML remain unchanged, with only scoped relationship CSS appended.

AnalysisInspect facts/inventory pages are bounded to 24,000 serialized characters, keeping complete items and authoritative nextOffset. Compact facts deduplicate contexts; factRef, fields and detail=full recover exact source fields. Oversized individual inventory items direct the caller to QueryResultRead rather than a nonexistent inventory fields option.
