# Findings and follow-up v1

The single current contract uses artifact schema 1, engine 1.5.0, finding checks 1.1.0 and renderer 1.7.0. Required fields include findings/actions (possibly empty), findingRules, findingVersion, source origins and presentationPolicy=explicit. Other versions or missing fields are rejected on reads and writes; there is no legacy action or finding translation. Drafts built with a different calculation registry must be rebuilt with a new requestKey before publication.

## Capabilities and explicit plans

`AnalysisInspect(section="protocol")` advertises `planPolicy=EXPLICIT_ONLY`, `findingChecks` and `actionChecks`. Prompts select scope, checks, action wording, evidence needs, priority and presentation. The host supplies deterministic operations, provenance validation, capability pairing, ownership checks and transactional state. No organization, metric name or value sign selects a workflow. The budget v2 adapter and readonly contract remain source integration boundaries.

After Build, inspect facts/derived and submit `AnalysisInspect(section="findings", analysisRef, findingPlan, thresholds?)` for a read-only preview. Publish uses the same findingPlan/thresholds and optionally hypotheses/actionPlan. Omitted plans produce no automatic findings or actions. A configured threshold must be explicitly selected. Plans participate in request-key idempotency and are frozen in the artifact.

Checks: SIGN(factRefs, sign=positive/negative/zero), UNIT_GAP(factRefs), MISSING_VALUE(factRefs), COVERAGE_GAP(), CALCULATION(derivedRefs), THRESHOLD(ruleRef), RECHECK(previousFindingRef, one current factRef). RECHECK evaluates a stored numeric predicate with matching provenance, signature, unit and precision. Matching a sign never adds business evidence requirements.

Each actionPlan entry supplies findingRefs, check, goal, requiredEvidence, expectedDecision and priorityBasis. Checks: VERIFY_VALUE, CONFIRM_UNIT, COMPLETE_COVERAGE, VERIFY_COMPOSITION (confirmed LINEAR formulas or captured FORMULA_CHECK targets), REQUEST_EVIDENCE. Scope, capabilityRefs, availability and completion are host-derived. REQUEST_EVIDENCE records missing information and remains NEEDS_INPUT; arbitrary prose goals cannot be mechanically completed. New bindings or changed scope require a new related analysis. There is no automatic finding-to-action mapping or fixed priority order.

## Host capability policy

AgentScope provides the tools through host `capabilities.analysis` and `capabilities.reports` (both default true); Skill names do not enable or disable them. Selected Skills guide the workflow and remain subject to ownership and package integrity checks. Existing owned MCP bindings determine accessible sources, and registered adapters determine which outputs can be analyzed. The global `activation: disabled` switch disables runtime assembly and analysis HTTP routes. Configuration and package integrity are checked for each new runtime and HTTP request; an in-flight reply retains its runtime settings.

`findingRules` optionally contains trusted threshold rules: source, version, sourceType (`policy` or `user_assumption`), nonempty appliesTo, operator (`lt` or `gt`), exact decimal threshold, explicit unit, scale (`1`). Publication inputs cannot register policy rules. User thresholds in AnalysisPublish always become user assumptions. No built-in financial risk thresholds or organization-name classifications exist.

Business readonly capability discovery checks actual leased tools on currently selected resource bindings. Ordinary MCP readOnlyHint is accepted. The budget v2 contract is also recognized by its exact version, contract resource, L1 risk, pinned-source boundary and evidence policy on the actual execute_semantic_plan/validate_evidence tools. That service writes query/evidence state, so its generic MCP readOnlyHint is deliberately false. This recognition does not change tool permissions, and does not prove that arbitrary detail sources exist. Both the execution and evidence tools must remain available. A parsed source formula can be rechecked with missing dependencies: the host freezes the target, origins and all already-known dependency formulas, and permits only additional facts actually reachable through that formula. Unsupported formulas and unknown detail access require input rather than an executable button.

Execution and verification must form a pair on each selected fact's resource, server and connection fingerprint. A verifier on another resource cannot prove matching execution/evidence IDs. Private sourceOrigins hashes preserve this provenance; public projections omit them. A resource being reconfigured mid-run requires a new plan.

## Immutable records

Finding contains findingId, analysisRef, scopeId, objectRefs, metricRefs, factRefs, derivedRefs, ruleRef, source/version/applicability, statement and explanations. Host text reports only explicitly selected checks and records numeric predicates where applicable. Each explanation distinguishes observation, arithmetic_composition and business_cause_hypothesis. Only the latter accepts model prose, always NEEDS_EVIDENCE with nonempty missingEvidence. SUPPORTED applies to the claim type only. An explicitly requested, source-matched numeric condition recheck can record CONTRADICTED with a previousFindingRef; neither earlier fact nor text changes.

ActionProposal freezes actionRef (`analysisRef#action_<24 lowercase hex>`), findingRefs, goal, requiredEvidence, expectedDecision, capabilityRefs, scope, priorityBasis and AVAILABLE/NEEDS_INPUT/UNAVAILABLE. Availability is not execution. Public projections omit source identity policies, raw receipts and capability internals.

Limits: at most 300 findings/actions per publication, 20 explicit user thresholds, 30 hypotheses, and 700 KB of public insight content. Over-limit results are refused, never silently truncated into a supposedly complete finding list. Public presentation retains the published insight scope even when the table is filtered.

Public insight capacity (including the frontend's 16,000 UTF-16-code-unit field bound) is checked before committing publication or action completion. Whitespace-only explanations and missing-evidence entries are refused. A rounded zero, changed unit or changed source cannot automatically contradict an earlier exact negative observation.

## Follow-up lifecycle

`POST /workspace/analyses/resolve-action?agent_id=...&session_id=...` accepts only actionRef, rechecks owner and immutable registration, returns text and actionRef, and performs no source read or run creation. The current chat input owns insertion. Existing draft content is retained until the user explicitly appends. In-memory drafts are isolated by server, user, agent and session; switching sessions cancels pending resolve requests. Sending remains the ordinary chat send path.

`AnalysisFollowUp(actionRef, requestKey)` binds to a real current user message in on_reply. Version 1 conservatively accepts the exact generated text only. Edited/appended text or attachments require a fresh analysis plan using baseAnalysisRef, without old actionRunRef. This avoids trying to prove natural-language goal equivalence. Same-key same-action requests restore the run; conflicting actions fail. Multiple keys for the same action in one user turn are rejected. Terminal runs are not silently reopened; a genuinely new user turn can start a new request.

Runs and events live in SQLite action_runs/action_events, independently of artifact blobs. States: PROPOSED, RUNNING, AWAITING_INPUT, PARTIAL, COMPLETED, FAILED, CANCELLED. Model tools expose no completed parameter. Publication and resultAnalysisRef/state commit in one transaction. Completion requires new, verified, complete evidence matching registered source, exact objects, period, measure and dimensions; unit/coverage/formula goals add their own conditions. A completed numeric or formula check is not a business-cause finding.

AnalysisBuild adds optional actionRunRef and baseAnalysisRef. Every selected fact must come from captures recorded by the host for that run. Old inputs cannot stand in for new evidence. A new snapshot is independently verified and cannot be mixed with old captures. If opaque binding identities change with a new snapshot, v1 refuses automatic old-action completion: rediscover and create a related fresh plan instead. History integrity and source requery availability are separate; FollowUp reports current requery as NOT_CHECKED until tools actually run.

Only matching verification captures recorded in the current run contribute to that run's verification status. An unrelated fresh verification does not refresh old evidence. LINEAR composition completion requires the same calculation-registry digest as the origin; a changed registry leaves the new result PARTIAL/FORMULA_RULE_CHANGED even if the rule reference string is unchanged. Source-formula completion instead requires an unchanged captured expression for each frozen formula node, matching origins, and a new FORMULA_CHECK of the frozen target with no unresolved dependencies. A changed source formula is rejected as ACTION_FORMULA_CHANGED; new values without the target check remain PARTIAL/FORMULA_EVIDENCE_REQUIRED. Both MATCH and MISMATCH can complete the act of checking. Public actionRuns.resultNote explicitly distinguishes agreement from a checked difference; COMPLETED does not assert agreement.

Cancellation, permission denial, uncompleted reply ends and recovered orphan RUNNING states never imply completion. Late background captures are retained against their originating run without reopening a cancelled run. Original source request/resume semantics remain in the budget Skill. No cross-service exactly-once guarantee is made. Runs allow 1,000 captures and 1,000 runs per owner; pinned evidence survives ordinary retention cleanup. Events contain status, time and diagnostic codes, not raw receipts or credentials.

The first run read after a host process restart marks an orphan RUNNING record PARTIAL with HOST_RESTART_WITHOUT_RESULT. This single-process recovery marker is not a distributed worker lease. Draft text is held in memory per chat scope; it is not persisted across browser restarts.

## Recovery and local validation

Use new analyses for acceptance. Current-protocol revisions retain their immutable evidence and transactional run state; this is part of multi-turn follow-up, not support for older storage or renderer formats. Deploy with active replies stopped. No migration or legacy reader is provided.

Python regression: `python -m unittest discover -s tests -p 'analysis_*_test.py' -v`. Frontend: `npm run test:analysis`, `npm run test:reports`, `npm run test:actions`, `npm run build`. Tests use synthetic fixtures, HTTPX and actual SDK MCP adapter metadata. Real model choice, actual business interpretation, source availability and interactive acceptance remain separate user acceptance tests.

Default business rendering shows model-authored claims and action goal/expectedDecision. Findings, requiredEvidence, priorityBasis and support/availability diagnostics remain internally recorded; technicalDetails=true explicitly enables their technical presentation. Presentation suppression never changes action eligibility, ownership or completion checks.
