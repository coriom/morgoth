# Browser decision-model qualification gate

V9.6's operator-reported inventory trace has five acknowledged actions, three
without observable UI change, and an independently detected loop. It is
evidence that the tested model's choices did not complete that task. It is not
evidence that the Bubblewrap controller failed, and it does not identify the
cause of an earlier cold-start timeout. V9.6.1's separate non-generative
reliability checks and bounded timing fields are carried into this branch.

The benchmark runs **one installed, operator-selected local model per
invocation**. It uses the existing Ollama action source, dynamic candidate
IDs, trusted task-bound input values, fresh-DOM checks, six-action ceiling,
loop suppression, SHA-pinned fixture trace and independent browser acceptance.
There is no model fallback or automatic action substitution. The model list is
read from the configured loopback `/api/tags` endpoint with a byte and time
limit; names appear only on the foreground operator terminal. The selected
model must be installed. An alternative that is not already resident must
have a reported model size that fits measured host and finite cgroup headroom
with a 1 GiB reserve. Unknown measurements or another loaded model block the
alternative. This guard is conservative; disk size is not a proof of runtime
memory demand, and CPU/GPU placement remains unknown. Neither selection nor
the benchmark changes the global model configuration or loads a model to warm
it before the trial. Each invocation requires its own `BROWSE ONCE` consent.

Tasks are fixed reviewed synthetic inputs, not service-specific controller
branches. `inventory` and `changed_layout` have the same objective, target and
independent result, with different rendered layouts. `catalog` and `support`
exercise other interfaces. `changed_layout` was developed in earlier browser
work; it is separated from this benchmark implementation but is **not** a
blind external benchmark. Model comparisons must use the *same* task, source
commit, action budget, and acceptance rule, and record whether each model was
cold or already loaded. Run models sequentially; never infer concurrently.
One run cannot establish statistical reliability or model superiority.

The offline lexical reference was run against actual Bubblewrap-confined
Chromium: inventory 4 actions, changed layout 6, catalog 4, support 4; all
four met independent acceptance with zero observed no-change actions. The
existing deferred-layout reference abstained after four actions and one
no-change observation. This
reference is intentionally simple and may exploit fixture wording. It is
never used as a fallback in a model trial. Scripted test responses prove
protocol and browser behavior, not semantic model quality.

The trial reports independent completion, safe abstention, unproductive
selection, loop termination, or technical failure separately from model-call,
action, no-change, timing, residency and bounded memory evidence. The public
report contains only fixed statuses, counts and reviewed synthetic trace IDs;
no raw prompt, model answer or page text is retained. A completed synthetic
task is not production qualification. If no alternative model is installed or
passes the resource preflight, comparative evidence remains insufficient.

Do not activate a hierarchical planner yet. A high-level subgoal proposal
could help the model distinguish navigation from data entry, but it would
consume additional local inference time and introduce another untrusted
output contract. A future experiment may issue one bounded subgoal from
current rendered evidence and verified history, then require the existing
low-level candidate chooser and independent acceptance checker. The subgoal
would never authorize an action. First compare a suitable installed decision
model under the frozen controller; if all available candidates remain
decision-limited, review a separately consented two-level trial with explicit
latency and failure budgets.

The browser remains synthetic-only. Chromium's internal sandbox is disabled
inside Bubblewrap, and there is no proven cgroup memory cap. Real websites,
accounts and production browser authority remain unsupported.
