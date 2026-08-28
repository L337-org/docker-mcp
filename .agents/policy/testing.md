# Testing

_when writing or running tests, or adding behaviour that needs them_

- **VE.11.1** You MUST build a mock from the primary source - the specification, the installed artefact's actual signature, or a recorded real response - and MUST NOT build it from what the behaviour probably is.
- **VE.11.2** You MUST confirm a mocked interface's signature, parameter names, return shape and error behaviour, exactly as you would before calling the real thing.
- **VE.11.3** You MUST pay particular attention to error behaviour, which is the half most often guessed because it is documented worst and exercised by hand least.
- **VE.11.4** You MUST NOT treat agreement between a mock and the code under test as evidence where the same assumption produced both; that confirms only that we are consistent with ourselves.
- **VE.11.5** You MUST record which primary source a mock's behaviour was derived from, so that it can be re-checked when the dependency moves rather than rediscovered from a production failure.
- **TE.1.1** You MUST NOT re-run a failing test to turn a build green; a test that fails intermittently is reporting something real, and re-running destroys the evidence.
- **TE.1.2** You MUST find the cause of an intermittent failure and name it as a product defect, a test defect, an infrastructure problem, or an observability gap.
- **TE.1.3** You MUST treat an infrastructure problem as a finding rather than an excuse: harden it where you can, and where it genuinely is transient and outside our control, say so explicitly and record why.
- **TE.1.4** You MUST raise and fix an observability gap as tracked work like the other three categories, because otherwise the next occurrence costs exactly what this one did.
- **TE.1.5** You MUST record intermittency on the issue rather than closing it as not-reproducible.
- **TE.2.1** Every source module MUST have a corresponding test module, named to match, so that a missing suite is visible at a glance.
- **TE.3.1** A test requiring a real external dependency MUST be marked as an integration test and excluded from the default run.
- **TE.3.2** Integration tests MUST be marked automatically by location, rather than by remembering to annotate each one.
- **TE.3.3** An integration test MUST skip cleanly when its dependency is absent, rather than failing.
- **TE.3.4** Both the default and the integration suite MUST be runnable with a single documented command.
- **TE.4.1** You MUST NOT treat a green fully-mocked suite as evidence that a feature works, because a mock can encode exactly the same misunderstanding as the code.
- **TE.4.2** You MUST exercise at least one path through the real thing - the real package installed, the real binary invoked, the real service connected to - before believing a feature works.
- **TE.4.3** Where a test substitutes a collaborator, you MUST NOT read the result as evidence about that collaborator's own behaviour: its overrides never execute, so the test asserts what the caller asked for and never what the callee did, and coverage counts the calling line either way.
- **TE.5.1** You MUST cover the failure paths of new behaviour as well as the happy path: malformed input, absent dependency, permission denied, timeout, partial result.
- **TE.5.2** You MUST assert what a failure says, not only that it happened, because an error path asserted only by exception type passes while its message is useless to whoever hits it.
- **TE.6.1** Where a convention or a piece of configuration can be checked mechanically, you SHOULD write a test that fails CI rather than prose someone has to remember.
- **TE.7.1** You MUST cover drift that no pull request could cause with a scheduled run, and its failure MUST reach a person, defaulting to the project's Slack channel or the organisation's where it has none.
- **TE.7.2** Where a pull request could also cause such a failure, that check SHOULD run on pull requests as well as on the schedule.
- **DS.9.1** A scheduled check MUST resolve and install dependencies across every platform the project claims to support, because an ecosystem can break a build with no change on our side.
- **DV.5.1** You MUST treat a clean install and an upgrade as different test cases, because the upgrade is the one that breaks.
- **DV.5.2** An upgrade test MUST start from the previous published release, install and configure it, then upgrade to the candidate, asserting that configuration survived unchanged, credentials still work, the service is still running, and the user was not prompted unnecessarily.
- **DV.5.3** Where several platforms or runtime versions are supported, the upgrade path MUST be tested on the oldest supported one, because that is where behaviour differences live.

