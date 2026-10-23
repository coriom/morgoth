# CMQ.3: repeated browser choices after a successful fill

This investigation starts from backend commit
`76595a20a8eb7c4c369d990137a803ca1174bf97`. It uses only reviewed
synthetic applications, scripted model responses and the existing confined
Chromium controller. No real Ollama request was made. The operator-reported
direct and hierarchical traces are historical evidence; their private model
requests and reasoning were not retained. The tests below reconstruct
requests from the current code and rendered fixtures, not the exact bytes
of those historical requests.

## Observed trials

| Trial | Model calls | Acknowledged actions | Ineffective actions | Independent result |
| --- | ---: | ---: | ---: | --- |
| Hierarchical Qwen3 4B | 6 | 4 | 1 repeated SKU fill | Budget exhausted after Search revealed C05; not accepted |
| Direct Qwen3 4B | 4 | 4 | 2: repeated SKU fill and Inventory click | Loop stopped; not accepted |

All reported model choices were schema-valid and authorized browser actions.
That proves transport and execution compatibility, not useful planning.

## Source and rendered-state findings

The direct action source sends the objective, trusted target, latest bounded
rendered observation, complete controller-issued action choices, current
input values, the authorized fill value, verified action/outcome history,
remaining model calls and time, and a false independent-completion status.
The native schema enumerates only the current revision and offered IDs.
The hierarchical low-level selector inherits this exact contract. Its
high-level advisory request contains bounded control names and verified
history, but not current input values; that omission occurs only on a
high-level request. It **cannot explain the repeated fill selected by the
low-level source before the no-change replan**. It may affect later advice,
but no historical subgoal text was retained and no causal claim is warranted.

The SHA-verified `heldout` fixture and a different `catalog` fixture were
rendered in Bubblewrap-confined Chromium using scripted choices. After
opening the relevant section and successfully filling the trusted target,
the next low-level request in both fixtures had:

- `TARGET_IN_VISIBLE_INPUT=true` and a fill candidate whose current value
  equaled its separately authorized value.
- A separate clickable search candidate (`C04` in both reviewed fixtures).
- A verified prior fill transition with `UI_CHANGE_OBSERVED`; independent
  acceptance still false.
- The objective and trusted target present, four remaining model calls, and
  a JSON Schema choice enum matching the offered catalog.

In `heldout`, the offered categories were reviewed C01 advice click, C02
Inventory click, C03 SKU fill (already equal), C04 Search stock click, and
Stop. The diagnostic exports only those fixture-scoped codes, action kinds,
bounded counters and equality/effect booleans. It does not export page text,
prompts, values, URLs, model output or hidden reasoning. The same generic
diagnostic confirmed the equivalent post-fill state in the catalog fixture.
The source digest check authenticates the fixture bytes, not an arbitrary
caller-provided observation; the rendered-browser tests supply the latter.

After a scripted second fill produced `NO_OBSERVABLE_CHANGE`, the next
request omitted the exact C03 fill binding in that unchanged visible state,
retained C04 search, recorded the no-change effect, and showed three
remaining calls. The controller therefore did not conceal search or fail to
suppress the proven ineffective fill. Before the no-op, it still offered the
same-value fill: some interfaces can react to a repeat fill even when the
displayed value is unchanged, so equality alone is not proof of a no-op.
The independent rendered-state comparison and loop policy decide that after
execution. Repeated Inventory remained technically permitted; on this
fixture it was unproductive, and the real direct run's loop detector stopped.

## Decision

No observation or action-binding defect was demonstrated that explains the
repeated selections. The current source provides the decisive low-level
facts; the model can still choose an ineffective permitted action. The two
real Qwen runs therefore support a **decision-quality limitation on this
task**, not a missing JSON field, browser-policy failure, or proof of a
universal model limitation. The exact historical prompts and latent model
reasoning remain unknown. We have left the controller, prompts, suppression,
loop limits, deadlines and six-call budget unchanged.

The next useful *single* experiment, if separately reviewed, is a
foreground-consented, one-request choice-only comparison on the same
SHA-verified post-fill observation with a **different already installed**
local action-planning model. It should use the same structured choice schema
and current candidates, no browser action, no model download or default-model
change, and a fresh memory preflight. Its question is narrow: can another
model choose the available search action rather than a known-equal fill
under identical evidence? Earlier 8B and 4B whole-task failures did not
control this decisive state. A single correct answer would not establish
multi-action reliability or production qualification; a later independently
accepted browser trial would still be necessary.

## Verification and safety

Focused hermetic privacy and contract tests plus existing confined-Chromium
regressions are recorded in the engineering handoff. The diagnostic has no
CLI, Ollama client, browser access or persistence. It refuses unknown
fixture labels and validates the reviewed fixture digest before emitting
only allowlisted synthetic control codes and fixed facts. No protected
governance, Project database, Desktop, model configuration, provider routing,
approval broker, or `SAFE_FOR_WORKLOADS=False` setting changed.
