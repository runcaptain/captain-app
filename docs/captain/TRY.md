# Try Captain on your Mac

Captain is two repos. This one (`runcaptain/captain-app`, branch `captain`) is the desktop app. The second,
`runcaptain/captain-ide` (private, ask Edgar for access), is the Captain Search backend that ships inside the app.
There's no download yet. You build it yourself. It's an Apple Silicon build, ad-hoc signed, so it runs only on the Mac
that built it.

## What you need

- Xcode command line tools: `xcode-select --install`
- Rust 1.95.0 (`rustup`), Zig 0.16.0, `just`, and `uv`: `brew install rustup zig just uv`, then
  `rustup toolchain install 1.95.0`
- herdr-core (the terminal engine Captain bundles): `curl -fsSL https://releases.herdr.dev/app/install.sh | sh`. That
  also installs Herdr itself, which you can delete. Captain only needs `~/.local/bin/herdr-core`.
- GitHub access to `runcaptain/captain-ide`

## Build and open

```sh
mkdir -p ~/Code/workbench && cd ~/Code/workbench
git clone https://github.com/runcaptain/captain-ide.git CaptainSearchTools
git clone --recurse-submodules -b captain https://github.com/runcaptain/captain-app.git
cd captain-app
scripts/package-captain-macos          # about 10 minutes the first time; builds dist/Captain.app (~530 MB)
cp -R dist/Captain.app ~/Applications/ && open ~/Applications/Captain.app
```

If you cloned captain-ide somewhere else, set `CAPTAIN_IDE=<that folder>` before the build.

## First run

- Captain starts its backend by itself. Its data lives in `~/.config/captain-search`.
- **New, then Try the demo store** loads sample stores, so every screen has something in it.
- The right side is a real terminal. Claude Code or Codex there can drive everything with the `cst` command.

## Known problems right now (2026-11-06)

- Comparisons may say "Couldn't load backtests." A fix is in progress. The other screens work.
- Database drivers for Snowflake, Databricks and the rest aren't bundled yet. You can save a connection but can't query
  it from the app yet.
- The native notebook isn't in this build. It's on the `feat/native-runs-kit` branch.

What's planned: `runcaptain/captain-ide` `docs/product/plans/`.
