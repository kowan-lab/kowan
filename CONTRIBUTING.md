# Contributing to Kowan

Thanks for helping. Bug reports, ideas, docs fixes and code are all welcome.

## Before you start

- **Bugs and ideas:** open an issue first, so we can agree on the approach before you write code.
- **Questions:** use [Discussions](https://github.com/kowan-lab/kowan/discussions).
- **Security issues:** never open a public issue. Follow [SECURITY.md](SECURITY.md).

## Development setup

You need Python 3.10 or newer, Node.js 22 and a local Docker engine.

```bash
python3 -m venv .venv && . .venv/bin/activate
make deps        # Python dependencies
make frontend    # build the web UI into web/dist
make run         # server on http://localhost:5081
```

For UI work, run `make dev` in a second terminal: it serves the UI with hot reload on port 5173 and proxies API calls to the server.

The architecture is described in [docs/architecture.md](docs/architecture.md).

## Pull requests

- Keep each pull request focused on one change.
- Run `make test`. Add or update tests for behaviour you change.
- If you touch the UI, run `make frontend` and check the result in a browser.
- Pages import UI components only from `@/ui/kit`; follow [DESIGN.md](DESIGN.md) for visual rules.
- Kowan has not had a stable release yet, so there is no backward compatibility to keep: change schemas and APIs directly instead of adding migrations or aliases.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
