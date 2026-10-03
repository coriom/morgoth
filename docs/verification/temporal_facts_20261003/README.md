# Temporal-fact hardening verification (2026-10-03)

All commands used an explicit, credential-free environment. SQL tests connected
only to `postgresql:///morgoth_test?host=/var/run/postgresql` and created/dropped
disposable Project schemas. No provider inference or external HTTP ran.

`bin/morgoth test` delegates to `self_modify.canonical_runner.main()`
(`bin/morgoth:253-257`, `self_modify/canonical_runner.py:24-82`). That runner
hardcodes the production checkout as `REPO_ROOT`; to keep it untouched, the
canonical run invoked the **same** `main()` after setting only `REPO_ROOT` to
`/tmp/morgoth-temporal-hardening`. Its sandbox, pytest arguments and normal
selection were unchanged: `-q -n 4 --max-worker-restart=3 -m 'not integration'
--disable-socket --allow-unix-socket --timeout=60 --timeout-method=thread`.
The outer launcher used `unshare` plus `bwrap --clearenv` and the existing
venv read-only; see `self_modify/canonical_runner.py:36-77` and
`self_modify/gates.py:70-79`.

The exact prior test command was not saved in the previous handoff. Its
reported 1960 pass count should therefore not be treated as a verified
canonical scoreboard. At `e395243`, collection under the canonical marker
selected **1960/2168** and deselected 208 integration tests; this worktree
selected **1962/2170** and deselected the same 208 (two new hermetic tests).
The final canonical launcher yielded **1960 passed, 2 skipped**, exit 0,
wall 33.8 s; `canonical.log` is the unedited launcher output. The two skips
are the nested sandbox harness tests at
`tests/test_post_submission_checks.py:489,511`: their `skipif` checks
`sandbox_posture()`, which is unavailable within the already confined
canonical run. `skips.log` and `skips.junit.xml` identify both. Marker
deselection follows `pytest.ini:2-6` and `self_modify/gates.py:70-79`;
integration tests are run separately on the host test DB, not hidden.

Historical failure reproduction used the same safe command/selection in
detached worktrees at `4c8f364712988b0e5767ed19676a3579796db511` and
`e3952430b817943b4bf5a85e72de5487300688c8`:

```text
env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONPATH=<worktree> \
  MORGOTH_TEST_POSTGRES_URL='postgresql:///morgoth_test?host=/var/run/postgresql' \
  /home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q -m integration \
  tests/test_reflect_endpoint_gate.py::test_tool_template_renders_api_endpoints_classvar
```

Both failed at the same stale literal assertion, line 296. The trusted
template at `self_modify/reflect.py:373-374` had already normalized dictionary
field declarations to names in both commits. The corrected test evaluates
that rendered class expression with synthetic path-bearing fields and asserts
the `('a', 'b', 'c')` identity contract; `reflect-fixed.log` records 1 pass.
No reflect workload was run. The previous broader attempt was described as
`pytest -q -m integration`; its full historical log was not preserved.

The scoped SQL command selected five node IDs (all 5 passed):

```text
env -i HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 \
  PYTHONPATH=/tmp/morgoth-temporal-hardening \
  MORGOTH_TEST_POSTGRES_URL='postgresql:///morgoth_test?host=/var/run/postgresql' \
  /home/corio/Morgoth/morgoth/.venv/bin/python -m pytest -q -m integration \
  --disable-socket --allow-unix-socket \
  tests/test_temporal_facts_integration.py tests/test_weather_domain.py \
  tests/test_project_runtime_integration.py \
  tests/test_campaign_lifecycle_integration.py \
  tests/test_llm_profiles_integration.py
```

The five exact nodes are in `sql.junit.xml`. Result: 5 passed, 5 unmarked
tests deselected, exit 0, wall 13.92 s (`sql.log`).

The safely executable broader selection added
`tests/test_persistent_memory.py`, `tests/test_source_cache.py`,
`tests/test_source_cache_ext.py`, `tests/test_stale_objective_sweep.py`, and
`tests/test_backup_watchdog.py` to that same command, with `--timeout=60`.
Result: 100 passed, 11 deselected, exit 0, wall 19.11 s (`broad.log`).
External sockets were blocked. Reflect/shadow workload files and sandbox
network canaries were not included in that host selection. This does not
claim that the entire historical `-m integration` suite was run or green.

The JUnit files have only the host name and verbose synthetic failure trace
removed; suite counts, testcase names and timings are preserved. Logs contain
only test output and synthetic fixture identifiers. No credentials were read.
