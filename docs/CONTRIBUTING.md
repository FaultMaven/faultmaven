# Contributing to FaultMaven

First off, thank you for considering contributing to FaultMaven! We welcome any and all contributions, from bug reports and feature requests to code and documentation improvements. Every contribution helps make FaultMaven a better troubleshooting tool for the community.

This document provides a set of guidelines to help you get started.

## Code of Conduct

This project and everyone participating in it is governed by the [FaultMaven Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior.

---

## How Can I Contribute?

There are many ways to contribute to the project.

* **Reporting Bugs:** If you find a bug, please open an issue and provide as much detail as possible.
* **Suggesting Enhancements:** If you have an idea for a new feature or an improvement to an existing one, open an issue to start a discussion.
* **Improving Documentation:** If you see an area where the documentation could be clearer or more complete, please feel free to submit a pull request.
* **Writing Code:** You can pick up an existing issue (especially those labeled `good first issue`) or contribute a new feature you've discussed with the maintainers.

---

## Your First Code Contribution

Unsure where to begin? A great way to start is by looking for issues tagged with `good first issue` or `help wanted` in the [Issues tab](https://github.com/your-org/faultmaven/issues). These are issues that have been identified as good entry points for new contributors.

### Development Setup

To get started with local development for the `faultmaven` monolith:

1.  **Fork & Clone the Repository**
    * Fork the `faultmaven` repository on GitHub.
    * Clone your fork locally:
        ```bash
        git clone [https://github.com/your-username/faultmaven.git](https://github.com/your-username/faultmaven.git)
        cd faultmaven
        ```

2.  **Set Up a Virtual Environment**
    * `./scripts/sync-venv.sh` builds one from a lockfile and records what it was built from:
        ```bash
        ./scripts/sync-venv.sh dev     # creates .venv-dev
        ```
    * There is no single environment that mirrors all of CI, because no lockfile is a superset — `mypy` and `import-linter` are in `dev` only, `boto3`/`opik`/`presidio` in `cloud` only. Build the one matching the job you care about:

        | lockfile | CI jobs that install it |
        |----------|-------------------------|
        | `requirements/dev.txt` | Architecture Boundary Check, API Contract Drift Check |
        | `requirements/test.txt` | Test Standalone, Code Quality Checks, Test Packaging Configuration, Environment Smoke Check |
        | `requirements/cloud.txt` | Test Cloud, Test PostgreSQL Integration |

        Security Scanning installs no lockfile — it audits the files themselves with `pip-audit --no-deps`.

        A lockfile is not the whole environment: **Test Cloud also runs a real Redis**, because `cloud.txt` pins `hiredis` and the cloud deployment runs a server. The two halves belong together — reproducing that job locally means starting one, or the app falls back to the in-process FakeRedis and you get failures no deployment can have (fm#1031):

        ```bash
        docker run --rm -d -p 6379:6379 redis:7.2-alpine
        REDIS_HOST=localhost python scripts/check_redis_pairing.py   # what CI asserts before the suite
        ```

3.  **Install Dependencies**
    * `sync-venv.sh` already did this — it runs `uv pip sync <lockfile>` then `uv pip install -e . --no-deps`. CI does the equivalent with `pip install -r <lockfile>` on an empty runner; `sync` is used locally because, unlike `pip install -r`, it also **removes** packages the lockfile omits, which is the half that matters on an environment you reuse.
    * The `--no-deps` matters: the version ranges in `pyproject.toml` are inputs to `./scripts/lock-deps.sh`, never resolved at install time. The lockfiles are the only source of versions, which is what makes an environment reproducible.
    * **A virtualenv goes stale on its own.** `requirements/*.txt` are exact pins under version control; a venv is persistent state. `git pull` updates the file and never the environment, so every lockfile bump silently widens the gap — and a drifted interpreter makes local results disagree with CI in ways that look like code problems. CI cannot drift, because it rebuilds from the lockfile on an empty runner every run.
    * A `post-merge` / `post-checkout` hook warns when it happens, comparing each stamped venv against the lockfile on the branch you are now on. Which command installs it depends on which hook path you use — the two are mutually exclusive, because `core.hooksPath` makes git ignore `.git/hooks/` entirely:
        ```bash
        # pre-commit framework (recommended — also runs secret/API-key scanning)
        pip install pre-commit && pre-commit install

        # or the repo's own hooks (black formatter + these warnings, no secret scanning)
        ./scripts/install-git-hooks.sh
        ```
        Both run the same check. `install-git-hooks.sh` refuses while the framework owns `.git/hooks`, rather than silently disabling its secret scanning; if you already use the framework, just re-run `pre-commit install` to pick up the new hook types.
    * The warning never modifies an environment — rewriting one underneath a test run in another terminal is worse than the drift. Re-sync when you see it, using the exact command the warning prints:
        ```bash
        ./scripts/sync-venv.sh dev
        ```
    * To check by hand at any time, from the repository root (the stamp records a repo-relative path):
        ```bash
        sha256sum -c --status .venv-dev/.locksum && echo in-sync   # shasum -a 256 -c on macOS
        ```

4.  **Run Local Services (Recommended)**
    * The `docker-compose.yml` file is provided to easily spin up any necessary backing services (like a local LLM or database for testing).
        ```bash
        docker-compose up --build
        ```

5.  **Run the Tests**
    * Before making any changes, ensure all tests are passing.
        ```bash
        pytest
        ```

---

## Pull Request Process

1.  **Create a Branch:** Create a new branch for your feature or bug fix from the `main` branch.
    ```bash
    git checkout -b feature/my-amazing-feature
    ```

2.  **Make Your Changes:** Write your code and any accompanying tests.
    * **Coding Style:** Please follow the **Black** code style (pinned `black==26.3.1`; CI runs `black --check`). To auto-format staged Python on commit, install the git hook once: `./scripts/install-git-hooks.sh` (or use the full pre-commit framework — see [faultmaven/CLAUDE.md](../CLAUDE.md) "Pre-commit Hooks").
    * **Error Logging:** All error logging MUST include `exc_info=True` or use `logger.exception()`. See [Logging Policy](operations/monitoring/logging-policy.md#error-logging-standards) for details.
    * **Commit Messages:** Please follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) specification. For example: `feat: Add new data classifier for TOML files`.

3.  **Run Tests:** Ensure all tests still pass after your changes.
    ```bash
    pytest
    ```

4.  **Update Documentation:** If your changes affect the documentation, please update it accordingly in the `/docs` folder.

5.  **Submit a Pull Request:** Push your branch to your fork and open a Pull Request against the `main` branch of the official `faultmaven` repository.
    * Provide a clear title and a detailed description of your changes in the PR. Link to any relevant issues.

6.  **Code Review:** One of the core contributors will review your PR. We may suggest some changes or improvements. Once the PR is approved, it will be merged.

Thank you again for your contribution!
