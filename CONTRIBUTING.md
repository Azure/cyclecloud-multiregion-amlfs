# Contributing to the Azure CycleCloud Multi-region plugin

This project welcomes contributions and suggestions.  Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

## Code of Conduct

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Issues and pull requests

- Search existing issues and pull requests before starting work. Follow
  [Support](SUPPORT.md) for bug reports and feature requests. Include the plugin
  version, reproduction steps, expected and actual behavior, and relevant logs
  with secrets removed.
- Report suspected vulnerabilities privately using [Security](SECURITY.md),
  not public issues or pull requests.
- Read the [Overview](docs/overview.md) and
  [Components and ownership](docs/components.md) before changing staging behavior.
- Work on a branch in your fork. Keep changes focused, update related documentation,
  and add focused tests for behavior changes. Explain the change and the validation
  performed in your pull request, including any checks you could not run.

## Testing

For runtime changes, follow the Python versions and Linux/Lua setup in the
[test workflow](.github/workflows/tests.yml). From the repository root, install the
[development dependencies](requirements-dev.txt) and run its test command:

```shell
pip install -r requirements-dev.txt
pytest -v --cov=amlfs_flow --cov-report=term-missing
```

For documentation-site changes, follow the Node.js setup and npm checks in the
same workflow. The [website package scripts](website/package.json) define the
site tests and build commands, which run from the `website` directory.
