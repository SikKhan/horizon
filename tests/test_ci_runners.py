"""Which jobs may run on our own runners (docs/ci-runners.md).

- horizon-light (playhorizon's and other small runners, which refuse a fork's pull request): main's own scripts only.
- horizon-checks (isolated runners; an approved fork's pull request ending in 0 runs there too): no secret and
  no token that writes.
- Neither takes a job that holds a secret or whose token can push, publish or start workflows: playhorizon is a
  partner's server, and a checks runner keeps what a fork's job left behind.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
LIGHT = ("horizon-light", "playhorizon-light", "playhorizon-ci")
CHECKS = ("horizon-checks",)
FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"
VERIFY_PARTS = ("verify-tree", "verify-lint", "verify-server", "verify-launcher", "verify-test", "verify-mod-points")
# verify's parts: our checks runners only with SELF_HOSTED_CI=all, Blacksmith's otherwise.
ONLY_WITH_ALL = re.compile(r"^\$\{\{ vars\.SELF_HOSTED_CI == 'all' && fromJSON\('\[\"self-hosted\", \"linux\", "
                           r"\"horizon-checks\"\]'\) \|\| 'blacksmith-\d+vcpu-ubuntu-2404' \}\}$")
TRUSTED_WRITES = re.compile(r"^\s*(contents|actions|packages|id-token|deployments|attestations): write", re.M)
# actions: write can start workflows too; cancel-on-close needs it to cancel runs and runs no checkout at all.
CANCELS_RUNS = {"cancel-on-close.yml cancel"}


def jobs(text):
    """(name, block) for each job of a workflow, by indentation (two spaces under `jobs:`)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.M)
    return list(zip(parts[1::2], parts[2::2]))


def triggers(text):
    head = text.split("\njobs:\n", 1)[0]
    on = re.search(r"^on:\n((?:[ #].*\n|\n)*)", head, re.M)
    return set(re.findall(r"^  ([a-z_]+):", on.group(1), re.M)) if on else set()


def runs_on(block):
    """The job's runs-on value, a folded (`>-`) one included."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("runs-on:"):
            value = line.split("runs-on:", 1)[1].strip()
            if value in (">-", ">", "|"):
                indent = len(line) - len(line.lstrip()) + 2
                # The folded lines right under it.
                folded = []
                for l in lines[i + 1:]:
                    if not l.startswith(" " * indent):
                        break
                    folded.append(l.strip())
                value = " ".join(folded)
            return value
    return ""


def routed():
    """(where, triggers, workflow text, job block, runs-on, pools) for every job that can run on our runners."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        on = triggers(text)
        for name, block in jobs(text):
            value = runs_on(block)
            pools = {pool for pool, labels in (("light", LIGHT), ("checks", CHECKS))
                     if any(f'"{label}"' in value for label in labels)}
            if pools:
                yield f"{path.name} {name}", on, text, block, value, pools


class OwnRunnerTests(unittest.TestCase):
    def test_every_job_on_our_runners_can_go_back_to_github(self):
        count = 0
        for where, _, _, _, value, pools in routed():
            count += 1
            if ONLY_WITH_ALL.match(value):
                continue
            self.assertIn("vars.SELF_HOSTED_CI != 'off'", value, where)
            # Both labels select a GitHub-hosted or Blacksmith Ubuntu runner when our pools are disabled.
            self.assertRegex(value, r"\|\| '(?:ubuntu-(?:latest|24\.04)|blacksmith-\d+vcpu-ubuntu-2404)' \}\}$", where)
            if "checks" in pools:
                # SELF_HOSTED_CI=light: every checks job back to GitHub, the bots stay.
                self.assertIn("vars.SELF_HOSTED_CI != 'light'", value, where)
        self.assertGreaterEqual(count, 10)

    def test_light_runners_take_main_code_only(self):
        for where, on, _, block, _, pools in routed():
            if "light" not in pools:
                continue
            if "pull_request" in on:
                job_if = next((l for l in block.splitlines() if l.strip().startswith("if:")), "")
                self.assertIn(FORK_GUARD, job_if, f"{where}: a fork's pull request")
            if "pull_request_target" in on:
                # main's code only: every checkout is main's.
                self.assertEqual(block.count("uses: actions/checkout"), block.count("ref: main"), where)

    def test_no_secret_or_writing_token_on_our_runners(self):
        for where, _, text, block, _, pools in routed():
            self.assertNotIn("secrets.", block, where)
            self.assertNotIn("environment:", block, where)
            head = text.split("\njobs:\n", 1)[0]
            for part in (block, head):
                writes = {m.group(1) for m in TRUSTED_WRITES.finditer(part)}
                if where in CANCELS_RUNS:
                    writes -= {"actions"}
                    self.assertNotIn("actions/checkout", block, where)
                self.assertEqual(writes, set(), where)
            if "checks" in pools:
                # A checks runner keeps what a fork's job left: its jobs' tokens write nothing at all.
                self.assertNotRegex(block, r":\s*write", where)
                self.assertNotRegex(head, r":\s*write", where)

    def test_verify_parts_go_to_one_pool(self):
        ci = dict(jobs((WORKFLOWS / "ci.yml").read_text()))
        # Every part goes to our checks runners or every part to Blacksmith's (only the size differs); verify itself
        # only waits for them.
        for part in VERIFY_PARTS:
            with self.subTest(part=part):
                self.assertRegex(runs_on(ci[part]), ONLY_WITH_ALL)
        self.assertIn(f"needs: [{', '.join(VERIFY_PARTS)}]", ci["verify"])
        # Never skipped: GitHub counts a skipped required check as passed.
        self.assertIn("if: always()", ci["verify"])

    def test_forks_reach_only_the_checks_runners(self):
        # An approved fork's pull request ending in 0 runs on horizon-checks like this repository's own (GitHub's
        # 20-job pool is not the only place it may wait). Never the light runners: every light job that a pull request
        # can start carries the fork guard (test_light_runners_take_main_code_only), and ci.yml's two runs-on
        # expressions (the other workflows keep their own guards) no longer test the head repository.
        ci = dict(jobs((WORKFLOWS / "ci.yml").read_text()))
        for name in VERIFY_PARTS:
            self.assertNotIn("head.repo", runs_on(ci[name]), name)
        for where, on, _, block, _, pools in routed():
            if "light" in pools and "pull_request" in on:
                job_if = next((l for l in block.splitlines() if l.strip().startswith("if:")), "")
                self.assertIn(FORK_GUARD, job_if, where)

    def test_only_the_checks_runners_take_forks(self):
        script = (ROOT / "tools/ci/checks-runners.sh").read_text()
        self.assertIn('[ "${HORIZON_RUNNER_POOL:-}" != checks ]', script)
        self.assertIn('echo "HORIZON_RUNNER_POOL=checks"', script)
        # Only inside the branch of the runner that has the rust toolchain (the checks runner), never the light ones.
        env = script.split('echo "HORIZON_RUNNER_POOL=checks"')[0].rsplit("if [", 1)[1]
        self.assertTrue(env.startswith(' "$label" = playhorizon-ci'), env)

    def test_the_light_runners_refuse_forks_themselves(self):
        script = (ROOT / "tools/ci/checks-runners.sh").read_text()
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED=$shared/job-started.sh", script)
        self.assertIn('.pull_request.head.repo.full_name // "unknown"', script)
        self.assertIn('if [ "$head" != "${GITHUB_REPOSITORY:-}" ]', script)  # refuses unless HORIZON_RUNNER_POOL=checks


class HostedParityTests(unittest.TestCase):
    """parity-hosted.yml fetches the maintainers' encrypted vanilla data onto Blacksmith's runners. The secrets reach
    only the default branch's fetch and seal scripts, never beside the tested commit's code but on main. A fork's pull
    request can restore this repository's cache entries, so only main's runs write them, and nothing made from the
    data goes into one, an artifact or a log unsealed: the build folders (no game data) are the only clear entry."""

    SAVE_CACHES = ("  SAVE_CACHES: ${{ github.ref == 'refs/heads/main' && contains(fromJSON('[\"workflow_dispatch\", "
                   "\"push\", \"schedule\"]'), github.event_name) && 'true' || 'false' }}\n")
    SECRET_STEPS = ["Fetch the vanilla data", "Unseal the restored caches", "Seal for the caches"]

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(ROOT / "tools/ci/vendor/pyyaml-6.0.3.zip"))
        import yaml
        cls.text = (WORKFLOWS / "parity-hosted.yml").read_text()
        cls.workflow = yaml.safe_load(cls.text)
        cls.steps = cls.workflow["jobs"]["run"]["steps"]
        cls.names = [step.get("name") or step.get("uses") for step in cls.steps]

    def step(self, name):
        return self.steps[self.names.index(name)]

    def test_secret_values_reach_only_the_trusted_scripts(self):
        holders = []
        for job, body in self.workflow["jobs"].items():
            self.assertNotIn("secrets.", repr({k: v for k, v in body.items() if k != "steps"}), job)
            for step in body["steps"]:
                if "secrets." in repr(step):
                    holders.append((job, step.get("name"), step.get("env", {})))
        values = [(job, name) for job, name, env in holders if any(
            re.fullmatch(r"\$\{\{ secrets\.\w+ \}\}", str(value)) for value in env.values())]
        self.assertEqual(values, [("run", name) for name in self.SECRET_STEPS])
        # The plan learns only whether they are set.
        others = [(job, name) for job, name, _ in holders if (job, name) not in values]
        self.assertEqual(others, [("plan", "Access to the vanilla data")])
        # Each runs the default branch's copies only, never a script of the checkout.
        for name in self.SECRET_STEPS:
            run = self.step(name)["run"]
            self.assertNotIn("tools/", run, name)
            self.assertNotIn("python", run, name)
            self.assertTrue(re.findall(r"\bbash (\S+)", run), name)
            for script in re.findall(r"\bbash (\S+)", run):
                self.assertRegex(script, r'^"\$RUNNER_TEMP/trusted/[a-z-]+\.sh"$', name)

    def test_the_secrets_meet_no_tested_code_but_mains(self):
        # Before the checkout the workspace is empty: no code of the tested commit can run beside the secrets.
        checkout = next(index for index, name in enumerate(self.names) if name.startswith("actions/checkout@"))
        self.assertLess(self.names.index("Fetch the vanilla data"), checkout)
        self.assertLess(self.names.index("Unseal the restored caches"), checkout)
        # Sealing comes after the harnesses: on main only, whose code it is.
        self.assertGreater(self.names.index("Seal for the caches"), self.names.index("The shard's harnesses"))
        self.assertIn("env.SAVE_CACHES == 'true'", self.step("Seal for the caches")["if"])
        self.assertEqual(set(self.workflow[True]), {"workflow_dispatch"})
        # The default branch's copies of the scripts, or none: never the tested commit's.
        script = self.step("The fetch and seal scripts, from the default branch")["run"]
        self.assertEqual(re.findall(r"ref=\S+", script), ['ref=$TRUSTED_REF"'] * 2)
        self.assertEqual(self.step("The fetch and seal scripts, from the default branch")["env"]["TRUSTED_REF"],
                         "${{ github.event.repository.default_branch }}")
        # The oracle shim's runtime memo (machine-local stat data) is excluded wherever the oracle entry is sealed or
        # unsealed; tests/test_ci_sealed_cache.py checks the exclusion itself.
        self.assertIn('echo "RUNTIME_MEMO=$GITHUB_WORKSPACE/.local/parity-runner/runtime-memo"',
                      self.step("Folders for the data, outside the checkout")["run"])
        for name, mode in (("Unseal the restored caches", "unseal"), ("Seal for the caches", "seal")):
            calls = re.findall(rf'sealed-cache\.sh" {mode} \\\n\s+(.*)', self.step(name)["run"])
            oracle = [call for call in calls if "oracle" in call or "$label" in call]
            self.assertTrue(oracle, name)
            for call in oracle:
                self.assertIn('--exclude "$RUNTIME_MEMO"', call, name)
        # What the plan job (the tested tree) gives the keys is checked before it names anything.
        self.assertIn("the plan's keys are not what it computes", self.step("Cache keys")["run"])

    def test_only_mains_runs_save(self):
        self.assertIn(self.SAVE_CACHES, self.text)
        gates = {"owned": "Only Cargo wrote into the build folders", "seal": "Seal for the caches"}
        for name in gates.values():
            self.assertIn("env.SAVE_CACHES == 'true'", self.step(name)["if"], name)
        saves = [step for step in self.steps if step.get("uses", "").startswith("actions/cache/save@")]
        self.assertEqual(len(saves), 4)
        for step in saves:
            condition = step["if"]
            self.assertTrue("env.SAVE_CACHES == 'true'" in condition
                            or any(f"steps.{id}.outputs." in condition for id in gates), step["name"])
        # No action that restores and saves at once, no sticky disk, no compiler cache.
        for body in self.workflow["jobs"].values():
            for step in body["steps"]:
                uses = step.get("uses", "")
                self.assertFalse(re.search(r"^actions/cache@|sticky|useblacksmith/|rust-cache", uses), uses)
        self.assertEqual(self.workflow["jobs"]["run"]["env"]["HORIZON_NO_SCCACHE"], "1")

    def test_only_the_build_folders_are_cached_in_the_clear(self):
        clear = {".local/parity-target", "target"}
        for step in self.steps:
            if step.get("uses", "").startswith("actions/cache/"):
                paths = {line.strip() for line in step["with"]["path"].splitlines() if line.strip()}
                # The encrypted dataset's chunks as they are; everything else made from the data sealed.
                self.assertTrue(paths == clear or paths == {"${{ env.VANILLA_DATA_CACHE }}"}
                                or all(path.startswith("${{ env.SEALED }}/") for path in paths), step["name"])
        uploads = [step["with"]["path"] for step in self.steps if "upload-artifact" in step.get("uses", "")]
        self.assertEqual(uploads, ["${{ runner.temp }}/parity-hosted-logs/"])
        # Before main saves them in the clear, nothing but Cargo may have written into them.
        self.assertIn("tools/ci/cargo_owned.py check", self.step("Only Cargo wrote into the build folders")["run"])
        self.assertEqual(self.step("Save the build folders")["if"], "${{ !cancelled() && steps.owned.outputs.clear == 'true' }}")

    def test_no_required_check_waits_for_it(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name != "parity-hosted.yml":
                self.assertNotIn("parity-hosted", path.read_text(), path.name)
        verify = dict(jobs((WORKFLOWS / "ci.yml").read_text()))["verify"]
        self.assertNotIn("hosted", verify)


class TryRunTests(unittest.TestCase):
    """A maintainer's hand-started `checks` run of a branch (docs/ci-runners.md#try-runs) saves no sticky disk, no
    Rust cache, and holds no secret: only main's pushes and the merge queue write what pull requests start from."""

    def test_only_mains_pushes_and_the_merge_queue_save_a_build_disk(self):
        text = (WORKFLOWS / "ci.yml").read_text()
        # Every disk's `commit:` is this variable (tests/test_ci_source_mtimes.py).
        self.assertIn("  SAVE_BUILD_DISK: ${{ ((github.event_name == 'push' && github.ref == 'refs/heads/main') || "
                      "github.event_name == 'merge_group') && 'true' || 'false' }}\n", text)
        self.assertNotIn("secrets.", text)

    def test_the_windows_build_runs_by_hand_and_saves_no_cache(self):
        block = dict(jobs((WORKFLOWS / "ci.yml").read_text()))["windows-x64"]
        self.assertIn("    if: github.event_name == 'workflow_dispatch' && inputs.windows\n", block)
        # Nothing a branch built may reach a cache that main's builds restore.
        self.assertNotIn("rust-cache@", block)
        self.assertNotIn("actions/cache", block)
        self.assertIn("persist-credentials: false", block)
        self.assertIn("retention-days: 3", block)
        # Not the workflow's actions: read.
        self.assertIn("    permissions:\n      contents: read\n", block)


if __name__ == "__main__":
    unittest.main()
