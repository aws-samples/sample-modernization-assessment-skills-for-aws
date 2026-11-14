# Contributing Guidelines

Thank you for your interest in contributing to our project. Whether it's a bug report, new feature,
correction, or additional documentation, we greatly value feedback and contributions from our community.

Please read through this document before submitting any issues or pull requests to ensure we have all
the necessary information to effectively respond to your bug report or contribution.

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else
hasn't already reported the issue. Please try to include as much information as you can. Details like
these are incredibly useful:

* A reproducible test case or series of steps
* The version of our code being used (commit hash, or the `stamp` in `~/.kiro/skills/*/.moda-install.json`)
* The agent client and model (Kiro CLI / Claude Code / Codex)
* Any modifications you've made relevant to the bug
* Anything unusual about your environment or deployment

Never include credential values, account IDs or customer data in an issue. Redact them before pasting
logs or artifacts.

## Contributing via Pull Requests

Contributions via pull requests are much appreciated. Before sending us a pull request, please ensure that:

1. You are working against the latest source on the *main* branch.
2. You check existing open, and recently merged, pull requests to make sure someone else hasn't
   addressed the problem already.
3. You open an issue to discuss any significant work - we would hate for your time to be wasted.

To send us a pull request, please:

1. Fork the repository.
2. Modify the source; please focus on the specific change you are contributing.
3. After editing anything under `skills/`, rebuild the install manifest:
   `python3 skills/aws-skills-install/scripts/build_manifest.py`
4. Run the checks and make sure they pass:
   ```bash
   python3 -m unittest discover -s tests
   python3 scripts/validate_bundle.py
   python3 skills/aws-skills-install/scripts/build_manifest.py --check
   ```
5. Commit to your fork using clear commit messages.
6. Send us a pull request, answering any default questions in the pull request interface.
7. Pay attention to any automated CI failures reported in the pull request, and stay involved in
   the conversation.

Project conventions worth knowing:

* Skill definitions (`SKILL.md`), references and canonical report drafts are written in English.
  Localized output is produced by `localize_artifacts.py` with the glossary; do not translate the
  skill text itself.
* `report_kit.py` exists in two skills and must stay byte-identical; a test enforces it.
* Helpers never estimate values. If a number is not read from AWS or supplied by the user, it is
  reported as unknown.
* All AWS calls made by helpers are read-only. A change that adds a mutating call needs an explicit
  user opt-in flag and a note in the skill's SKILL.md.

GitHub provides additional documentation on [forking a repository](https://help.github.com/articles/fork-a-repo/)
and [creating a pull request](https://help.github.com/articles/creating-a-pull-request/).

## Finding contributions to work on

Looking at the existing issues is a great way to find something to contribute on. Looking at any
'help wanted' issues is a great place to start.

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
opensource-codeofconduct@amazon.com with any additional questions or comments.

## Security issue notifications

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security
via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please
do **not** create a public GitHub issue.

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing
of your contribution.
