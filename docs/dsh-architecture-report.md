# DSH Architecture & Stability Engineering — Technical Report

**Subject:** DeepSeek Harness desktop runtime, `app.asar` → `dsh/` (`@deepseek-ai/dsh-desktop-runtime` v0.2.0-rc.2)
**Method:** header-parse of the asar and read of individual files; all 2,917 `@deepseek-ai/**` source files extracted and read directly. Every claim below is backed by identifiers from real source.
**Caveat on the asar offset:** the extraction hint in the brief is off by 8 bytes. The correct data base is `8 + headerSize`, not `16 + headerSize`. Verified by probing `dsh/package.json` at each candidate delta. (The header is actually `u32 size` at 0, `u32 headerSize` at 4, `u32` at 8, `u32 jsonLen` at 12; the JSON string payload begins at 16 but data offsets are measured from `8 + headerSize`.)

---

## 1. Architecture summary

DSH is **not a monolith with plugins bolted on**. It is a Cordis DI container in which essentially the *entire product* — including the agent loop, tools, and the GUI — is plugin rows in a YAML patch document. `dsh/package.json` declares **287 dependencies, 279 of them `@deepseek-ai/dsh-*`**.

**Layers, bottom to top:**

| Layer | Packages | Role |
|---|---|---|
| Container | `cordis`, `cordis-plugin-loader`, `cordis-plugin-group`, `cordis-plugin-include` | Context/DI, `Fiber` lifecycle, service registry, config-driven plugin tree |
| Composition | `dsh-base` (a **static YAML patch**, `cordis.patch.yml`, 530 lines), `dsh-app-boot` | Profile/bundle layering, platform gating, load validation |
| Storage | `dsh-session`, `dsh-session-persistence-jsonl`, `dsh-atomic-write`, `dsh-storage-json`, `dsh-session-format*`, `dsh-session-query-sqlite` | Append-only event log, projections, migration, derived index |
| Capability | `dsh-llm`, `dsh-llm-retry`, `dsh-tools`, `dsh-tool-*`, `dsh-fs`, `dsh-shell` | Model + tools seams |
| Policy | `dsh-sandbox-policy`, `dsh-fs-sandbox`, `dsh-permission-presets`, `dsh-user-approval`, `dsh-authorization`, `dsh-checkpoint-policy` | Trust boundaries |
| Orchestration | `dsh-agent`, `dsh-agent-loop`, `dsh-subagent`, `dsh-goal`, `dsh-workflow`, `dsh-jobs` | Turn/step machine, delegation |
| Diagnostics | `dsh-invariants` + **38** `lib/invariant.js` companions | Runtime contract checks |
| Surfaces | `dsh-web-app`, `dsh-client-ui-*`, `dsh-acp`, `dsh-sdk*`, `dsh-desktop-host` | Electron/web/CLI/ACP front ends |

**Data flow for one model turn:**

```
user input → agent.inbox (durable agent/inbox/spliced) → AgentLoop.turn()
  → session.append("turn/start")                        [durable]
  → preStep: systemPrompt.assemble() → agent/pre-step waterfall
  → session.append("step/start")                        [durable]
  → session-checkpoint-policy: ctx.sessions.flush()     ← fsync barrier
  → ctx.llm.stream(request) → for await chunk → live.push(chunk)
  → session.append("assistant/message")                 [durable]
  → executeToolCalls → prepare/dispatch/finalize per call, results appended
  → session.append("turn/end")                          [durable]
```

The **session log is the spine**: `dsh-agent-loop` derives every request from it, `dsh-session-projection` folds every live value from it, `dsh-llm-retry` persists its retry counter into it, and `dsh-sandbox-policy` stores the per-session sandbox mode as one event in it.

---

## 2. Concrete stability mechanisms

### 1. Cordis fibers: per-plugin lifecycle with failure containment and reverse-order teardown

**What:** Every plugin runs in its own `Fiber` with an explicit state machine and an epoch-scoped effect scope. Disposal runs collected disposers in reverse order; one listener's throw can't break another's cleanup.

**Where:** `cordis/src/fiber.ts`

```ts
export const enum FiberState { PENDING, LOADING, ACTIVE, FAILED, DISPOSED, UNLOADING }
```
```ts
/** Notify plugin teardown without allowing one observer to break ownership cleanup. */
function emitPluginDisposed(context: Context, fiber: Fiber) {
  ...
  for (const callback of callbacks) {
    try { const returned = callback(...args); void Promise.resolve(returned).catch(e => context.logger.error(e)) }
    catch (error) { context.logger.error(error) }
  }
}
```
and the comment at `fiber.ts:287`:
> *"`this.inertia` itself should never reject — both `_reload` and `_unload` swallow their own work errors via `ctx.logger.error`. If it *does* reject, the only remaining cause is the logger itself failing... Let the rejection propagate; process-level crash is the honest outcome."*

**Real assessment:** this is genuine fault containment for **plugin teardown and event dispatch**, not a sandbox. `FAILED` is a *state*, not an isolate: a plugin whose `apply()` throws marks its fiber `FAILED` and its effects roll back, but it runs in the same process and event loop as everything else. There is no worker/process boundary per plugin.

---

### 2. Config-driven plugin tree with whole-row replacement and platform gating

**What:** The composition is a declarative patch list, not code. Each row is `{id, name, config, disabled, inject, group}`. Patches **replace** a row's whole config rather than merging.

**Where:** `cordis-plugin-loader/src/config/entry.ts`, `dsh-base/cordis.patch.yml`

```yaml
# The dsh-base bundle patch: the shared core of each base-backed profile, applied as
# ONE insert over the empty profile root. Later bundle patches and the user's
# profile cordis.patch.yml address these rows by id, with the last write
# winning per row.
#
# A patch replaces the targeted row's whole `config` rather than merging into
# it, so a row whose value differs by mode does NOT live here...
# Row order carries no load semantics (activation is service-availability
# driven); the grouping is for readers.
```
Platform gating is done on the rows themselves: `bash-sandbox` / `tool-bash` carry `disabled: !!js process.platform === 'win32'`, with inverted `pwsh-*` twins. The loader also implements `equalExceptVolatile` (`config/diff.ts`) so a volatile-only config change commits into running references instead of restarting the fiber.

**Real assessment:** strong *modularity* that buys real stability — composition errors are data errors, and `dsh-base`'s README documents composition rules that **fail the load** rather than silently misbehave (e.g. mounting `dsh-fs-local` alongside `dsh-fs-sandbox` double-registers `ctx.fs` and refuses to load). That's a fail-fast property, not a fault-tolerance one.

---

### 3. Append-only session event log as the single source of truth

**What:** Every fact is one immutable, monotonically-`seq`'d, `surfaceOp`-tagged event. Nothing is mutated in place; the "current state" is a fold.

**Where:** `dsh-session/lib/index.js`

```js
append(type, data, ...opts) {
  const surfaceOpts = opts[0];
  ... surfaceOpts?.sourceEventSeqs ...
  ... surfaceOpts?.surfaceOp ...
}
```
Surface operations are validated hard (`dsh-session/lib/index.js:295-328`):
```js
if (op === void 0) throw new Error(`session event "${event.type}" is surface-eligible and requires a surfaceOp marker`);
...
if (missing.length > 0) throw new Error(`surface replace: sourceEventSeqs must include every shadowed surface node; missing ${missing.join(", ")}`);
```
Read faces are pure derivations: `snapshotEvents()`, `deriveMessages()`, `requestHeader()`, `toolHistory()`.

---

### 4. Torn-tail detection and truncation repair on reload

**What:** On open, the log is scanned line by line; a trailing record without a newline is treated as a **torn tail** to be truncated, and a mid-file parse failure is held pending until the enclosing `turn/end` to decide whether it's recoverable.

**Where:** `dsh-session-persistence-jsonl/lib/index.js` (`SessionLogScanner`)

```js
/**
* Finish scanning, ignoring a final record without a newline as a torn tail.
* @returns the header, contiguous event prefix, and safe truncation offset.
*/
finish() { ... }
```
```js
consumeEventLine(line, endByte) {
  try { decoded = JSON.parse(line.toString("utf8")); }
  catch {
    const issue = new Error(`corrupt session log: unparsable committed event at line ${this.eventLine}`);
    if (this.recovery === "strict") throw issue;
    this.issue ??= issue;
    return;
  }
  ...
  if (this.issue !== void 0) {
    if (typeof decoded === "object" && decoded !== null && decoded.type === "turn/end") throw this.issue;
    return;
  }
```
The handle then repairs before its next append (`persistContiguous`):
```js
if (this.state.tornTruncateTo !== void 0) {
  await this.storage.truncateTornTail(this.header, this.state.tornTruncateTo);
  this.state.tornTruncateTo = void 0;
}
```
Corruption is reported as `corrupt session log: ...`, and the README/code notes it must read as *"upgrade the harness", never "corrupt session log"* for the format-unsupported case (line 961).

---

### 5. fsync'd appends with rollback of partial writes

**What:** Appends go through `open(path, "a")` → `writeFile` → `handle.sync()`. If the write or sync fails, the file is truncated **back** to its prior size, because the in-memory cursor didn't advance and the batch will be retried — leaving partial bytes would duplicate sequence numbers.

**Where:** `dsh-session-persistence-jsonl/lib/index.js:3210-3251`

```js
/**
* Append and fsync event lines. On a partial write or sync failure, restore the
* previous size before rethrowing because the unchanged cursor will retry the
* batch; leaving partial bytes would create duplicate sequence numbers.
*/
async appendLines(meta, events) {
  ...
  const { size: before } = await handle.stat();
  try { await handle.writeFile(content); await handle.sync(); }
  catch (error) {
    try { await closeAppendHandle(); await this.rollbackAppend(path, before); }
    catch (rollbackError) { throw new AggregateError([error, rollbackError], `failed to roll back append to "${path}"`); }
    throw error;
  }
}
```
New *files* are published atomically (temp + `wx` + `sync` + rename) with a POSIX parent-directory fsync:
```js
/** fsync a POSIX directory so a just-created/renamed entry is crash-durable. */
async syncDirPosix(dir) { const handle = await open(dir, "r"); try { await handle.sync(); } finally { await handle.close(); } }
```
On Windows, where Node exposes no parent-dir fsync, it drops to native `MoveFileExW(..., MOVEFILE_WRITE_THROUGH)` via Koffi (`lib/types/win32.js`), and the write lease becomes a named kernel semaphore derived from a SHA-256 of the case-folded path.

**Note the asymmetry with `dsh-atomic-write`**, which explicitly does **not** fsync ("Atomic, not durable — no `fsync` of the file or its directory"). The session log is the one place that pays for durability; the smaller config/credentials stores deliberately don't.

---

### 6. Semantic durability checkpoints before side effects (fail-closed)

**What:** Before dispatching a model request or a top-level tool body, the logged prefix is flushed to disk. If that flush fails, the side effect **does not happen**.

**Where:** `dsh-session-checkpoint-policy/lib/index.js` — this is the cleanest single file in the whole system:

```js
ctx.on("llm/stream", (options, next) => {
  if (options.sessionId === void 0) return next();
  const session = ctx.sessions.get(options.sessionId);
  return session === void 0 ? next() : afterCheckpoint(ctx, session, next);
});
ctx.on("tools/execute", async (exec, next) => {
  if (exec.agent === void 0 || exec.parent !== void 0) return next();
  await ctx.sessions.flush(exec.agent.session);
  if (exec.signal.aborted) return abortedBeforeDispatchResult();
  return next();
});
```
with the docstring:
> *"Checkpoint failures are fail-closed at the model and tool side-effect boundaries: the downstream adapter or tool body is not invoked."*

**Real assessment:** this is the strongest genuine reliability mechanism in the system. It is the difference between "we log what we did" and "we cannot do the thing unless the log already says we're about to."

---

### 7. Tool calls are scheduled in ordered barriers with a bounded rolling pool

**What:** Each assistant step's tool calls are grouped: parallel-safe calls run in a rolling pool capped by `maxParallelToolCalls` (default **10**); anything else is an exclusive ordering barrier. **Dispatch** overlaps, but **results and context commit strictly in model order**, so the durable log stays replayable.

**Where:** `dsh-agent-loop/lib/index.js` (`tool-calls.ts` region, lines 474-661)

```js
/**
* Schedules one assistant step's tool calls. Exclusive calls form barriers;
* parallel calls use a bounded rolling pool and are reclassified before start.
* Dispatch may overlap, while policy, results, and result context remain
* model-ordered. Abort or an internal scheduler failure stops replenishment
* and drains started calls.
*/
while (inFlight.size > 0) {
  const settledIndex = await Promise.race(inFlight.values());
  inFlight.delete(settledIndex);
  await commitReady();          // commits only the contiguous prefix
  if (signal.aborted) aborted = true;
  await fillPool();
}
```
`commitReady()` walks `committed` forward and stops at the first `undefined` slot. Classification is **fail-closed** — `dsh-tools/lib/index.js`:
```js
/**
* Classify a pending call through the caller's visible tool definition. Only
* an exact `true` is parallel; unknown, hidden, undeclared, invalid, or
* throwing classifiers are exclusive.
*/
executionMode(exec) {
  const tool = this.resolveExecution(exec.name, exec.agent, exec.parent !== void 0);
  if (!tool?.isConcurrencySafe) return { kind: "exclusive" };
  try { return tool.isConcurrencySafe(exec.arguments) === true ? { kind: "parallel" } : { kind: "exclusive" }; }
  catch { return { kind: "exclusive" }; }
}
```

---

### 8. Tool errors become `isError` envelopes, never thrown through the loop

**What:** A tool that throws produces a normal tool result with `isError: true` and `Error: <message>` text. The loop keeps running; the model sees the failure and can react. Only *scheduler* failures reject.

**Where:** `dsh-tools/lib/index.js:3616`

```js
function toolErrorResult(error) {
  const info = errorInfo(error);          // { name, code } only for HarnessError
  const message = errorMessage(error);    // Error.message, else .message, else String(), else "<unprintable thrown value>"
  return { content: [{ type: "text", text: `Error: ${message}` }], isError: true, error: { message, ...info ? { info } : {} } };
}
```
Used at every stage — `dispatchToolBody` (`catch (error) { return toolErrorResult(error); }`), `dispatchScheduledExecution`, `finalizeScheduledExecution`, and twice in `finishScheduledExecution` (materialization failure and final-content failure each degrade to an error result rather than propagating).

**Counterpoint — where the loop *does* throw, and it's deliberate:** `dsh-agent-loop/README.md`:
> *"Middleware, result-processing, tool, and other extension failures remain thrown and close the turn directly — **plugin failure ends the turn, not the loop**."*

So plugin bugs are not silently swallowed into model-visible error text; they terminate the turn and are recorded.

---

### 9. Crash-tail semantic repair: synthetic results for unanswered tool calls

**What:** If a step fails or is cancelled mid-flight, the loop records an error result for **every** tool call the model made, so later requests always see a balanced call/result history and are replayable.

**Where:** `dsh-agent-loop/lib/index.js`

```js
const toolRecovery = new ToolCallRecovery();
const stopRecovery = this.ctx.on("session/event", (session, event) => {
  if (session === this.session) toolRecovery.observe(event);
});
try { const stepEnd = await this.step(decision); ... }
catch (error) {
  try { for (const event of toolRecovery.results()) this.session.append("tool/result", event.data, {...}); }
  catch (recoveryError) { throw new AggregateError([error, recoveryError], "Step failed and its pending tool results could not be recorded", { cause: error }); }
  throw error;
} finally { stopRecovery(); this.session.append("step/end", { turn, step }); }
```
Two distinct codes, with deliberately different model guidance (`README.md`):
- A recorded call with no committed result → `TOOL_OUTCOME_UNKNOWN`, result says *"Its outcome is unknown."* and permits retry "only for read-only or idempotent operations; possible side effects require checking external state or asking the user first."
- A call with no start record → `TOOL_NOT_STARTED`, *"The tool call was interrupted before the Harness recorded it as started. Retry it if it is still needed."*

Cancelled-before-dispatch calls get `ABORTED_BEFORE_DISPATCH` + *"Error: tool call aborted before dispatch"* (`appendSkippedToolCall`).

**Real assessment:** this is the most sophisticated piece of genuine reliability engineering in the codebase. It distinguishes "we know it didn't happen" from "we don't know", and it encodes that distinction *into the model's context* in prose, rather than pretending the ambiguity away.

---

### 10. Cancellation preserves delivered text (`interrupted: true` anchors)

**What:** A cancelled stream doesn't discard what the user already saw. The loop appends the delivered prefix as an `assistant/message` with `interrupted: true`.

**Where:** `dsh-agent-loop/lib/index.js:1084-1100`

```js
if (signal.aborted) {
  const content = live.interruptedBlocks();
  if (content.length > 0) live.settle("assistant/message", () => this.session.append("assistant/message", {
    turn, step,
    message: createAssistantMessage({ content, source: {...} }),
    interrupted: true,
    ... live.usage === void 0 ? {} : { usage: live.usage },
    stream: live.stream
  }, { surfaceOp: "append" }).seq);
```
This keeps the durable log consistent with what the user actually saw — critical, because the next request derives from the log, and a model whose history omits text the user can see will contradict the user.

---

### 11. Approval fails closed on every path

**What:** Every way an approval can go wrong resolves to a non-grant. Only one of four outcomes is a grant.

**Where:** `dsh-user-approval/lib/index.js`

```js
const OUTCOMES = ["allowed-once", "rejected", "cancelled", "unavailable"];
const APPROVAL_POLICIES = ["ask", "never"];
```
```js
const answer = Promise.resolve()
  .then(() => this.ctx.waterfall(scopeTarget(req.agent, req.agent), "approval/request", req, () => Promise.resolve("unavailable")))
  .then((outcome) => OUTCOMES.includes(outcome) ? outcome : "unavailable", () => "unavailable");
```
- no answerer mounted → the waterfall default `"unavailable"` → no grant
- answerer throws → `.then(..., () => "unavailable")` → no grant
- answerer returns garbage → `OUTCOMES.includes(outcome) ? ... : "unavailable"` → no grant
- signal aborts → `"cancelled"` (raced via `onAbort`)
- policy `"never"` → `"rejected"` short-circuit before any answerer runs

And the audit pair is **turn-enclosed by precondition** — `request()` throws rather than appending outside a turn, with the reason spelled out:
> *"a bare event between turns is crash-tail garbage on reload"*

**Real assessment:** genuinely defensive. The docstring even pre-empts the exact concern: *"Missing answerers fail closed; grants apply only to the requested action."*

---

### 12. Path containment is realpath+inode based, not a string prefix

**What:** `workspace-write` containment re-canonicalizes the target **immediately before** delegating the mutation, then checks containment using filesystem identity (`dev`/`ino`) as a conservative fallback for aliases the lexical check can't see.

**Where:** `dsh-fs-sandbox/lib/index.js`

```js
async function isPathUnder(path, root, caseSensitive = process.platform !== "win32") {
  if (isLexicallyUnder(path, root, caseSensitive)) return true;
  const rootInfo = await statIfPresent(root);
  if (!rootInfo) return false;
  let ancestor = path;
  while (true) {
    const ancestorInfo = await statIfPresent(ancestor);
    if (ancestorInfo && sameIdentity(ancestorInfo, rootInfo)) return true;
    const parent = dirname(ancestor);
    if (parent === ancestor) return false;
    ancestor = parent;
  }
}
function sameIdentity(left, right) { return left.dev === right.dev && left.ino === right.ino; }
```
```js
async checkedTarget(target, sandboxPolicy) {
  const policy = sandboxPolicy ?? this.ctx.sandboxPolicy.resolve();
  const { mode } = policy;
  if (mode === "danger-full-access") return target;
  if (mode === "read-only") throw new FsError(`cannot write "${target.displayPath}": file access denied under read-only mode`, "FS_SANDBOX_DENIED");
  const fresh = await this.resolve(target.displayPath);
  let contained = false;
  for (const root of writableRoots(policy)) if (await isPathUnder(fresh.targetKey, root)) { contained = true; break; }
  if (!contained) throw new FsError(`cannot write "${target.displayPath}": file access denied under workspace-write mode`, "FS_SANDBOX_DENIED");
  return fresh;
}
```
**Crucially, it returns the fresh resolved target** so the checked identity is the mutated one — closing the obvious check-here/write-there TOCTOU. The module docstring is unusually honest about the residual:
> *"The fence is a policy check in TRUSTED code over a MODEL-CONTROLLED path, NOT a kernel boundary... The residual TOCTOU (an ancestor symlink swapped between the containment re-check and the syscall) is narrowed by re-canonicalizing immediately before delegating and **is accepted for this threat model**."*

The path-prefix helper is also correct — it appends `sep` before `startsWith(prefix)`, so `/work` does not contain `/workspace`.

---

### 13. Retry policy: bounded, jittered, backlog-durable, and cancel-aware

**What:** Retries run on the loop's `agent/request-error` extension point with exponential backoff + jitter, capped retry counts, and provider `Retry-After` respect.

**Where:** `dsh-llm-retry/lib/index.js` + `dsh-llm/lib/types/retry-policy.js`

```js
function localDelay(config, retry, random) {
  const exponent = Math.min(retry - 1, 1024);                    // overflow guard
  const exponential = Math.min(config.initialDelayMs * 2 ** exponent, config.maxDelayMs);
  const jitter = 1 - config.jitterRatio + 2 * config.jitterRatio * random();
  return Math.min(exponential * jitter, config.maxDelayMs);
}
```
Defaults: `DEFAULT_MAX_RETRIES = 5`, `DEFAULT_INITIAL_DELAY_MS = 500`, `DEFAULT_MAX_DELAY_MS = 10_000`, `DEFAULT_JITTER_RATIO = 0.1`, and
```js
const DEFAULT_RETRYABLE_CODES = Object.freeze([EMPTY_RESPONSE_CODE, 'RATE_LIMIT', 'SERVER', 'TIMEOUT', 'TRANSPORT']);
```
Note that `EMPTY_RESPONSE_CODE` is retryable — a known LLM failure mode. Retries are **durable before the wait**:
> *"Each scheduled retry is durable before its cancellable wait."* — `agent.session.append("llm/retry", eventData)` precedes `cancellableDelay(...)`, then `llm/retry-started`.
The retry counter is itself a **projection** (`key: "llmRetry"`), so it survives a crash and refolds from the log. Cancellation uses `AbortSignal.any([signal, lifetime.signal])` and a `cancellableDelay` that removes its listener, and plugin disposal calls `lifetime.abort(...)` then `Promise.allSettled([...active])` to drain.

**Honest caveat:** `retryPolicyKey`/`retryStateKey` are keyed by `JSON.stringify` of a sorted structure — sound but stringly-typed. No thrashing/retry-storm detection beyond `maxRetries`.

---

### 14. 38 packages ship runtime invariant companions

**What:** A registry (`ctx.invariants`) drives package-owned checks that assert *live* cross-cutting relationships, not types.

**Where:** `dsh-invariants/lib/index.js`

```js
var InvariantError = class extends Error {
  code = "INVARIANT";
  packageName;
  constructor(packageName, message) { super(`invariant violated by "${packageName}": ${message}`); ... }
};
```
```js
static Config = z.object({
  enabled: z.boolean().default(true),
  package_allowlist: z.array(z.string()).default([]),
  package_blocklist: z.array(z.string()).default([])
});
```
Registration reserves the name atomically, installs in a child fiber, and rolls back on failure:
```js
const child = ctx.plugin(installer.inject === void 0 ? installInvariant : Object.assign(installInvariant, { inject: installer.inject }));
try { await child; } catch (error) { await child.dispose(); throw error; }
```

**38 packages** ship `lib/invariant.js` (verified by path scan): `dsh-session`, `dsh-agent`, `dsh-agent-loop`, `dsh-tools`, `dsh-llm`, `dsh-llm-retry`, `dsh-compaction`, `dsh-sandbox-policy`, `dsh-user-approval`, `dsh-fs`, `dsh-scope`, `dsh-goal`, `dsh-jobs`, `dsh-subagent`, `dsh-workflow`, and more.

The `dsh-session` companion (`dsh-session/lib/invariant.js`) is the best example — a **full turn/step state machine validator** over the live event stream:
```js
case "turn/start":
  if (trace.openTurn !== null) fail(`turn/start ${event.data.turn} while turn ${trace.openTurn} is still open`);
  if (event.data.turn !== trace.nextTurn) fail(`turn/start expected turn ${trace.nextTurn}, got ${event.data.turn}`);
...
case "tool/result": {
  ...
  if (!trace.pendingCalls.has(callId) && !syntheticNotStarted) fail(`tool/result for ${callId} with no prior tool/call in this step`);
```
And `dsh-agent-loop/lib/invariant.js` asserts **log-reconstruction equality at dispatch time** — that the request actually sent equals the request the log derives:
```js
if (!Object.isFrozen(options)) fail("a loop-built request must be frozen");
const expected = session.deriveMessages();
if (JSON.stringify(options.messages) !== JSON.stringify(expected)) fail(`llm request for session "${String(session.id)}" diverges from the dispatch-time durable derivation (log-reconstruction desync)`);
```
**The design rule that makes this meaningful** (from `dsh-invariants/README.md`):
> *"Real relationships, not synthetic assertions. A companion checks an event-stream or mutable-data relation its package owns; confirming a method, plugin name, injection, or fixed pure result is a type, load, or unit-test concern, never a runtime invariant."*

**Real assessment:** this is genuine, and the constraint above is what separates it from decorative assertions. It's also **not free** — a `JSON.stringify` of the full message array per LLM request is a real cost, and `dsh-base` deliberately omits the registry (only `dsh-sdk-minimal` and the standard agent composition mount it).

---

### 15. Compaction cuts are computed by tool-pairing balance, and are transactional

**What:** Compaction must never split a tool-call from its result. Cut points are derived by folding `+N`/`-1` over surface events, with a cached fold invalidated by `replaceGeneration`.

**Where:** `dsh-compaction/lib/types/tool-pairing.js`

```js
function eventDelta(event) {
  switch (event.type) {
    case 'assistant/message': return event.data.message.content.filter(block => block.type === 'tool-call').length;
    case 'tool/result': return -1;
    default: return 0;
  }
}
...
if (inProgressToolCalls < 0) throw new Error(`tool-pairing balance: tool/result at surface seq ${seq} has no matching tool-call (corrupt surface)`);
```
The compaction itself is a durable transaction — `compaction/start` … `compaction/end` (`dsh-compaction-basic/lib/index.js:437`):
> *"`compaction/start` are synchronously adjacent, so the durable opening marker is [never orphaned]... exactly one `compaction/end` attempt; a failed close deliberately leaves the [open transaction visible]"*

Budgeting is derived from the real context window minus reserved output minus headroom, with loud configuration errors:
```js
const thresholdTokens = Math.floor(Math.min(contextWindow * policy.thresholdRatio, pressureBudgetTokens));
if (retainTokens >= thresholdTokens) throw new TargetPressureConfigError(targetKey, `BasicCompactionConfig: ${...} retainTokens (${retainTokens}) must be less than threshold tokens ${thresholdTokens}`);
```
and it bounds its own retries rather than looping forever:
```js
throw new Error(`compaction still above threshold after ${spec.compactionRetries + 1} compaction attempts (${measurement.totalTokens} estimated tokens >= threshold ${spec.thresholdTokens})`);
```
There are **two triggers**: `"pressure"` (step-boundary, staged) and `"context-overflow"` (provider-confirmed, bypasses threshold and retained-tail policy so it can force one useful reduction). Overflow first runs `toolResultPruner` before spending a summarization call — cheapest fix first.

---

### 16. Deterministic, replay-safe tool-result pruning

**What:** Oversized tool results are replaced in-surface with head+marker+tail, **without an LLM call**, and the replacement cites the shadowed node so replay can recover the input.

**Where:** `dsh-compaction-tool-result-pruner/lib/index.js`

```js
const PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n";
const DEFAULTS = deepFreeze({ thresholdChars: 8192, headChars: 4096, tailChars: 1024 });
```
```js
const emittedChars = resolved.headChars + codePointLength(PRUNE_MARKER) + resolved.tailChars;
if (emittedChars > resolved.thresholdChars) throw new Error(`ToolResultPruneConfig: headChars + marker + tailChars (${emittedChars}) must be at most thresholdChars (${resolved.thresholdChars})`);
```
Two invariants are asserted *at runtime* on the output:
```js
if (charsAfter > this.config.thresholdChars || charsAfter >= totalChars) throw new Error("tool-result prune: replacement must be smaller and within threshold");
```
It slices by **code point** (`Array.from(text)`), not UTF-16 unit, so a retained boundary can't split a surrogate pair — the docstring honestly notes grapheme clusters may still split. And it records the shadow price via the token meter:
```js
session.append("compaction/prune", { shadowedRange: {...}, shadowedSeqs: [seq], shadowedTokenCount: this.ctx.tokenMeter.estimateMessage(original) });
```

---

### 17. Oversized output handled by retention + spill-to-disk handles

**What:** `dsh-output-retention` bounds output mechanically and reports *exact* omission; `dsh-spill-policy` keeps full text retrievable via a locator.

**Where:** `dsh-output-retention/lib/index.js`, `dsh-spill-policy/lib/index.js`

The retention library's scope boundary is stated with unusual precision:
> *"The library owns ONLY the mechanical question 'what did we keep, what did we omit?'. Tool-specific code still owns business semantics... In particular `RetainedText.truncated`/`RetainedItems.truncated` means 'the retainer omitted otherwise-available content because of a budget' — NOT 'the upstream was incomplete'. Permission failures, skipped binaries, provider partial failures, and unreadable candidates stay in tool-domain fields, never folded into `truncated`."*

That distinction matters: it prevents a truncation flag from being misread as a failure signal. Spill uses a binary search under a monotonic token price to fit budgets (`fitText`), preserves surrogate pairs (`textSlice`), and never partially retains an image:
> *"Retain contiguous ends without moving or partially retaining an image. Each end receives half the content budget; unused space at an indivisible image stays unused."*

`dsh-spill-local` derives safe path segments with `encodeSegment` that is *"injective over ALL JS [strings]"* — neutralizing `../`, absolute paths, NUL, and separators *before* any filesystem call.

---

### 18. Session format versioning with a 4-link migration chain

**What:** The log carries a `version` in its header; missing migrations **refuse** rather than guess.

**Where:** `dsh-session-format-catalog/lib/types/generated.js`

```js
/** GENERATED by `scripts/gen-session-format-catalog.ts` — do not edit by hand.
 *  The direct imports make historical readability independent of mounted plugins. */
const sessionFormatCatalogOptions = {
  currentVersion: 4,
  codecs: [releasedV0SessionFormatCodec, releasedV1SessionFormatCodec, releasedV2SessionFormatCodec, releasedV3SessionFormatCodec, releasedV4SessionFormatCodec],
  currentEncoder: releasedV4SessionFormatCodec,
  migrations: [sessionFormatV0ToV1, sessionFormatV1ToV2, sessionFormatV2ToV3, sessionFormatV3ToV4],
  ...
};
```
`SESSION_FORMAT_VERSION = 4`. Each migration package ships its own **released codec** (`releasedV3SessionFormatCodec`) plus an `assertReleasedV*Header` / `restoreReleasedV*Artifact` pair, so a *historical* reader validates through the vocabulary of its own era, and a separate `validateInstalledCurrentSessionArtifact` validates the *current* one. Unsupported-migration is a distinct error type (`SessionFormatUnsupportedMigrationError`, "A readable artifact whose released source policy has no supported migration") — a readable log that the current binary cannot upgrade is reported as needing a newer harness, not as corruption.

---

### 19. Derived caches are explicitly non-authoritative

**What:** SQLite search and projection checkpoints are treated as disposable. The log is authoritative.

**Where:** `dsh-session-query-sqlite/README.md`, `dsh-session-projection-cache/README.md`

> *"**Derived index, never the source store.** The FTS rows live in a dedicated disposable database; the session-persistence database is never opened here."*
> *"The session log remains authoritative: a crash can leave a checkpoint stale, but never ahead of committed events, and incompatible records are ignored or backed up."*

The projection-cache config encodes this: `version: 7`, `invalidRecords: "backup-and-skip"`, and its own docstring — *"a stale or unreadable cache costs a longer tail [fold]"*. The projection registry validates every cached row against the live `stateVersion`:
```js
const need = row !== void 0 && row.ver === registration.def.stateVersion ? Math.max(row.seq + 1, 0) : 0;
```
and **refuses key collisions across versions**:
```js
if (existing.def.stateVersion !== erased.stateVersion) throw new Error(`session projection key ${JSON.stringify(key)} is already registered at stateVersion ${String(existing.def.stateVersion)}; refusing to share it with stateVersion ${String(erased.stateVersion)}`);
```

---

### 20. Read-before-edit and no-clobber guards at the provider

**What:** `edit` on an unread file is rejected; `write` to a file whose version changed since observation is rejected.

**Where:** `dsh-fs-observation-policy/lib/index.js`

```js
editIntent(target, actor) {
  const prior = ...;
  if (!owner || prior === void 0) throw new FsError(`edit requires reading "${target.displayPath}" first`, "FS_NOT_OBSERVED");
  if (prior.kind === "absent") throw new FsError(`cannot edit "${target.displayPath}": not found`, "FS_NOT_FOUND");
  return { version: prior.version };   // CAS basis
}
```
State is a `WeakMap` keyed by owner → target, so it is per-session and GC-friendly, and cleared on disposal for HMR safety: `ctx.effect(() => () => { gate.clear(); }, "fs-observation-policy observed-state teardown")`. The intent is *derived* from observation, not asserted by the tool: `replaceIfVersion` when present, `createIfAbsent` when unseen/absent.

---

### 21. Loop-escape and runaway guards

- **`dsh-repeat-tool-reminder`**: per-agent detector that fires at repeat counts **3, 5, 8** (base config) with escalating prose. Explicitly `Advisory, not veto`: *"The guard enriches post-execute decisions with model context; it never blocks or rewrites a call."* Thresholds are validated fail-loud (integers ≥ 2, no duplicates).
- **`maxParallelToolCalls`** bounds concurrency; **`maxActiveSubagents`** defaults to `8`; **`maxDepth`** defaults to `1`; **`maxParallelSubCalls`** defaults to `10`.
- **`dsh-tool-call-timeout-policy`**: cooperative deadlines via `dsh-timeout`'s `deadline()`, with a deliberate scoping trick — the timeout code is the plugin's own so a nested outer timer isn't misread:
  > *"Scoping `timeoutOf` to it keeps a nested outer deadline... from being misread as this plugin's own timeout — it reads as an ordinary upstream cancel."*
- **`clampTimeout`** enforces `min(requested ?? default, max)` and rejects zero as a disable sentinel.

**Notable gap, stated by the maintainers:** `dsh-agent-loop/README.md` → *"**No built-in turn budget** — tool calls or steering continue the current turn; a policy that bounds runaway turns must cancel from an existing lifecycle extension point such as `agent/turn-stopping`."*

---

## 3. Honest assessment: defensive engineering vs. good modularity

### Genuinely defensive engineering

These are mechanisms that exist specifically to make failures *survivable or visible*, and they show evidence of adversarial thinking:

1. **`dsh-session-checkpoint-policy`** — flush-before-side-effect with explicit fail-closed semantics. Smallest file, highest value.
2. **Torn-tail scan + truncation repair + rollback-on-partial-write** (`dsh-session-persistence-jsonl`). The `rollbackAppend` comment ("leaving partial bytes would create duplicate sequence numbers") shows the author reasoned about the *specific* corruption mode, not a generic "handle errors".
3. **Crash-tail semantic repair with two graded uncertainty codes** (`TOOL_OUTCOME_UNKNOWN` vs `TOOL_NOT_STARTED`) — and, notably, the *model-facing prose differs*, telling the model when retrying is safe.
4. **Containment by dev/ino with the fresh target returned** (`dsh-fs-sandbox`) — closes the check-here/write-there gap, and the module docstring names the residual TOCTOU it accepts rather than claiming it's closed.
5. **Approval fail-closed on all five paths**, with the turn-enclosure precondition justified by reference to crash-tail semantics.
6. **Tool-pairing balance for compaction cuts** — prevents a class of history corruption that would otherwise only show up as provider 400s.
7. **`dsh-agent-loop`'s reconstruction invariant** — asserting at dispatch time that the sent request equals the log-derived request is a genuinely strong end-to-end check.
8. **The whole-value event rule** (`dsh-session-projection`): *"a state-carrying log event MUST carry the complete post-change state, never a bare delta."* This makes every fold trivially cheap and every crash-truncated log safely replayable.
9. **Bounded, jittered, backlog-durable retry** with `Retry-After` respect and `EMPTY_RESPONSE` as a retryable code.
10. **Fail-closed tool concurrency classification** — unknown/throwing ⇒ exclusive.

### Good modularity (real value, but not fault tolerance)

- **Cordis itself.** Excellent DI/lifecycle engineering. `FiberState.FAILED` is a *bookkeeping* state, not an execution boundary. A plugin with a memory leak, an infinite loop, or a `process.exit` takes the whole host down. Isolation here means "clean teardown and scoped effects", not "blast radius containment".
- **The 38 invariant companions.** Real checks, but they are *diagnostics* — they throw `InvariantError` rather than preventing anything. Their value is turning a silent divergence into an attributable, package-named crash. **And availability of that value is uneven:** scanning every `cordis.patch.yml` in the bundle, `dsh-invariants` appears in exactly **one** composition — `dsh-sdk-minimal/cordis.patch.yml`. `dsh-base` (the shared core of the `web`, `headless`, `acp`, and `sdk` profiles) does **not** mount it, matching its own stated position that *"`dsh-base` deliberately omits runtime diagnostics."* So the shipped desktop/web product, which is what a user actually runs, executes with **no invariant checks active**, and all 38 companions are dead weight unless a custom bundle opts in.
- **`dsh-base` as a YAML patch.** Beautiful separation, and the Loader's `skippedBundles` mechanism (a bundle that fails to load is skipped and reported, rather than killing startup) is a real availability win. But note the honest caveat in its own README: *"Skipping a bundle does not guarantee that the remaining composition can provide the required services."*
- **The service-seam pattern** (definition package + implementation package + types package, e.g. `dsh-fs` / `dsh-fs-local` / `dsh-fs-sandbox`). This is what lets the sandbox swap be "load this instead of that" with the model-facing tools untouched. Excellent design; the reliability comes from the *sandbox*, not the seam.
- **`dsh-output-retention` as a library, not a service** — explicitly justified: *"deliberately a library, not a cordis service or plugin: it takes no `ctx`, registers nothing, and emits no events."* Good discipline; not a stability mechanism per se.

### Weaknesses and honest caveats

1. **Plugin failure is not contained at the process level.** No worker isolation per plugin. A `dsh-client-hmr` bug equals a broken GUI. The README's "a crashed or misbehaving child cannot corrupt the parent's session" applies to *subagent children*, whose session isolation is real — not to plugins.
2. **`dsh-atomic-write` is atomic but not durable** — no `fsync` at all, and the README states it plainly. It's used for config, credentials, and profile patches; those can be lost (not corrupted) on a hard crash. The session log is the only fsync'd store.
3. **The invariant registry's performance cost is unquantified, and it is off in the shipped product.** A full `JSON.stringify` comparison of the message array on every LLM request is non-trivial for long histories — and since `dsh-base` omits the registry, this cost/benefit tradeoff was resolved in favor of not paying it at all for the default surfaces.
4. **`maxParallelToolCalls` classification is unary** (stated as a known limitation): calls whose safety depends on comparing siblings or shared resources must be declared exclusive, which is a footgun for tool authors.
5. **No turn budget** — explicitly acknowledged. A model in a tool loop that keeps changing arguments (thus never tripping the repeat detector) can run the turn unbounded.
6. **PIDs are compared on the contender's host** in the writer lock — multi-host/network-FS sharing is documented as unsupported and could silently double-write.
7. **Retry math uses `Math.min(retry - 1, 1024)` as the exponent cap**, then `2 ** exponent` — with `initialDelayMs` up to `MAX_TIMER_DELAY_MS` this is finite, but the cap being on the *exponent* rather than the result is an unusual formulation (correct here only because `exponential` is then clamped by `maxDelayMs`).
8. **`dsh-session-query-sqlite` reconciliation is synchronous SQLite on the JS thread** — *"a statement already executing on the JavaScript thread cannot be interrupted."* Cancellation is only honored *between* calls.

### Bottom line

The reliability story is real but **concentrated, not pervasive**. Roughly three subsystems carry the weight:

- **`dsh-session` + `dsh-session-persistence-jsonl` + `dsh-session-checkpoint-policy`** — the append-only, fsync'd, torn-tail-repairable, checkpoint-before-side-effect log. This is the crash-safety core, and it is genuinely well built.
- **`dsh-agent-loop`** — tool-call scheduling with ordered commit, error envelopes, and crash-tail recovery results. This is the fault-handling core.
- **`dsh-fs-sandbox` + `dsh-sandbox-policy` + `dsh-user-approval`** — the fail-closed policy core, with a threat model stated honestly rather than overclaimed.

Everything else — the Cordis plugin tree, the 279-package decomposition, the generated migration catalog — is **excellent modularity and configurability** that *enables* those guarantees and keeps composition errors loud, but does not itself make the system more fault-tolerant. The distinguishing trait of this codebase is not that it has more mechanisms, but that the mechanisms it does have are **narrowly scoped and their limits are written down** — "this is containment, not a security boundary", "atomic, not durable", "we accept this residual TOCTOU", "no built-in turn budget". That kind of self-aware boundary documentation is rarer than the mechanisms themselves, and it is the strongest evidence of deliberate engineering rather than accumulated defensive habits.
