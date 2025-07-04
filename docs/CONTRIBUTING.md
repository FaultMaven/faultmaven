# Contributing to FaultMaven

First off, thank you for considering contributing to FaultMaven! We welcome any and all contributions, from bug reports and feature requests to code and documentation improvements. Every contribution helps make FaultMaven a better tool for the entire SRE and DevOps community.

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
    * It is highly recommended to use a Python virtual environment.
        ```bash
        python -m venv venv
        source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
        ```

3.  **Install Dependencies**
    * Install all required packages using the `requirements.txt` file.
        ```bash
        pip install -r requirements.txt
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
    * **Coding Style:** Please follow the **Black** code style. We use `flake8` for linting.
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